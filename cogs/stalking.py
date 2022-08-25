"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import re
import struct
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable, NamedTuple, TypeVar

import asyncpg
import discord
import lru
import tabulate
from discord.ext import commands, tasks

from utils.formats import clean_triple_backtick
from utils.time import human_timedelta


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import Context, GuildContext

T = TypeVar('T')


class FakeMember(discord.Object):
    guild: discord.Object


log = logging.getLogger(__name__)

REDIS_NICK_NONE = (
    'NoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNoneNone'
)

PG_ARG_MAX = 32767


def multi_insert_str(lst: list[Any]) -> str:
    count = len(lst)
    size = len(lst[0])
    elems = [f'${i + 1}' for i in range(count * size)]
    indiv = [f'({", ".join(elems[i:i + size])})' for i in range(0, count * size, size)]
    return ', '.join(indiv)


def grouper(it: Iterable[T], n: int) -> zip[T]:
    return zip(*([iter(it)] * n))


class LastSeenTuple(NamedTuple):
    last_seen: datetime = datetime.fromtimestamp(0, tz=timezone.utc)
    last_spoke: datetime = datetime.fromtimestamp(0, tz=timezone.utc)
    guild_last_spoke: datetime = datetime.fromtimestamp(0, tz=timezone.utc)


def name_key(member: discord.Member | discord.User) -> str:
    return f'ayaka:last_username:{member.id}'


def nick_key(member: discord.Member) -> str:
    return f'ayaka:last_nickname:{member.id}:{member.guild.id}'


def name_from_redis(name_or: str) -> str | None:
    if name_or == REDIS_NICK_NONE:
        return None
    return name_or


def name_to_redis(name_or: str | None) -> str:
    if name_or is None:
        return REDIS_NICK_NONE
    return name_or


# Entries with expiries
class SeenUpdate(NamedTuple):
    member_id: int
    date: datetime


MAX_SEEN_INSERTS = (PG_ARG_MAX // 2) - 1


class SpokeUpdate(NamedTuple):
    member_id: int
    guild_id: int
    date: datetime


MAX_SPOKE_INSERTS = (PG_ARG_MAX // 3) - 1


def datetime_from_redis(s: str | None) -> datetime:
    if s is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(struct.unpack('q', s.encode('utf-8'))[0] / 1000, tz=timezone.utc)


class Stalking(commands.Cog):
    """Enhanced user information."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot

        self._recent_pins = lru.LRU(128)

        self.batch_last_spoke_updates = []
        self._batch_last_spoke_curr_updates = []
        self.batch_last_seen_updates = []
        self._batch_last_seen_curr_updates = []

        self.batch_name_updates = []
        self._batch_name_curr_updates = []
        self.batch_presence.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_name.add_exception_type(asyncpg.PostgresConnectionError)

        self._name_update_lock = asyncio.Lock()
        self._last_spoke_update_lock = asyncio.Lock()
        self._last_seen_update_lock = asyncio.Lock()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{EYES}')

    async def cog_load(self) -> None:
        self.batch_presence.start()
        self.batch_name.start()

    async def cog_unload(self) -> None:
        self.batch_presence.stop()
        self.batch_name.stop()

    @tasks.loop(seconds=1)
    async def batch_presence(self) -> None:
        self._batch_last_seen_curr_updates = self.batch_last_seen_updates
        self._batch_last_spoke_curr_updates = self.batch_last_spoke_updates
        self.batch_last_seen_updates = []
        self.batch_last_spoke_updates = []

        # split due to arg limit
        while self._batch_last_seen_curr_updates or self._batch_last_spoke_curr_updates:
            curr_last_seen = self._batch_last_seen_curr_updates[:MAX_SEEN_INSERTS]
            self._batch_last_seen_curr_updates = self._batch_last_seen_curr_updates[MAX_SEEN_INSERTS:]

            curr_spoke_updates = self._batch_last_spoke_curr_updates[:MAX_SPOKE_INSERTS]
            self._batch_last_spoke_curr_updates = self._batch_last_spoke_curr_updates[MAX_SPOKE_INSERTS:]

            def dedupe_seen(last_seens: list[SeenUpdate]) -> list[SeenUpdate]:
                seen = {}
                for ls in last_seens:
                    try:
                        seen[ls.member_id] = max(seen[ls.member_id], ls, key=lambda x: x.date)
                    except KeyError:
                        seen[ls.member_id] = ls
                return list(seen.values())

            def dedupe_spoke(last_spokes: list[SpokeUpdate]) -> list[SpokeUpdate]:
                seen = {}
                for ls in last_spokes:
                    try:
                        seen[ls.member_id, ls.guild_id] = max(seen[ls.member_id, ls.guild_id], ls, key=lambda x: x.date)
                    except KeyError:
                        seen[ls.member_id, ls.guild_id] = ls
                return list(seen.values())

            await self.batch_insert_presence_updates(dedupe_seen(curr_last_seen), dedupe_spoke(curr_spoke_updates))

    async def batch_insert_presence_updates(self, seen_updates: list[SeenUpdate], spoke_updates: list[SpokeUpdate]) -> None:
        assert len(seen_updates) < (PG_ARG_MAX // 2)
        assert len(spoke_updates) < (PG_ARG_MAX // 3)
        # do multi_insert_str since it's 2x faster than executemany
        async with self.bot.pool.acquire() as conn:
            async with conn.transaction():
                # avoid deadlocks
                await conn.execute('LOCK TABLE last_seen IN EXCLUSIVE MODE')
                await conn.execute('LOCK TABLE last_spoke IN EXCLUSIVE MODE')
                if seen_updates:
                    query = f"""INSERT INTO last_seen (id, date)
                                VALUES {multi_insert_str(seen_updates)}
                                ON CONFLICT (id) DO UPDATE
                                SET date = EXCLUDED.date WHERE EXCLUDED.date > last_seen.date;
                            """
                    await conn.execute(query, *itertools.chain(*seen_updates))
                if spoke_updates:
                    print(spoke_updates)
                    query = f"""INSERT INTO last_spoke (id, guild_id, date)
                                VALUES {multi_insert_str(spoke_updates)}
                                ON CONFLICT (id, guild_id) DO UPDATE
                                SET date = EXCLUDED.date WHERE EXCLUDED.date > last_spoke.date;
                            """
                    await conn.execute(query, *itertools.chain(*spoke_updates))

    @tasks.loop(seconds=1)
    async def batch_name(self) -> None:
        # qw process a maximum of 32767/5 elements at once to respect psql arg limit
        all_updates = self.batch_name_updates
        updates = all_updates[:6553]
        all_updates = all_updates[6553:]
        # TODO: make the redis lookup similar to the iteration update for calculating inserts
        pending_name_updates, pending_nick_updates = await self.batch_get_redis_mismatch(updates)

        current_names, current_nicks = await self.batch_get_current_names(pending_name_updates, pending_nick_updates)
        name_inserts, nick_inserts, current_names, current_nicks = await self.calculate_needed_inserts(
            pending_name_updates, pending_nick_updates, current_names, current_nicks
        )
        await self.batch_insert_name_updates(name_inserts, nick_inserts)
        await self.batch_set_redis_names(current_names, current_nicks)

    async def batch_get_redis_mismatch(
        self, updates: list[tuple[discord.Member, int]]
    ) -> tuple[list[tuple[discord.Member, int]], list[tuple[discord.Member, int]]]:
        assert len(updates) <= 50000  # limit mget to 100k keys
        count = len(updates)

        name_redis_keys = (name_key(m) for m, _ in updates)
        nick_redis_keys = (nick_key(m) for m, _ in updates)

        if not name_redis_keys and not nick_redis_keys:
            return [], []
        try:
            res = await self.bot.redis.mget(*name_redis_keys, *nick_redis_keys)
        except TypeError:
            return [], []

        names = res[:count]
        nicks = res[count:]

        pending_name_updates = []
        pending_nick_updates = []

        for idx in range(count):
            member, timestamp = updates[idx]
            name = names[idx]
            nick = nicks[idx]

            if not name or name_from_redis(name) != member.name:
                pending_name_updates.append((member, timestamp))
            if not nick or name_from_redis(nick) != member.nick:
                pending_nick_updates.append((member, timestamp))

        return pending_name_updates, pending_nick_updates

    async def batch_get_current_names(
        self, pending_name_updates: list[tuple[discord.Member, int]], pending_nick_updates: list[tuple[discord.Member, int]]
    ) -> tuple[dict[int, tuple[str, int]], dict[tuple[int, int], tuple[str | None, int]]]:
        async with self.bot.pool.acquire() as conn:
            query = """SELECT id, name, idx FROM namechanges
                       WHERE id = ANY($1::BIGINT[])
                       ORDER BY idx ASC;
                    """
            name_rows = await conn.fetch(query, [m.id for m, _ in pending_name_updates])

            query = """SELECT id, guild_id, name, idx FROM nickchanges
                       WHERE id = ANY($1::BIGINT[]) AND guild_id = ANY($2::BIGINT[])
                       ORDER BY idx ASC;
                    """
            nick_rows = await conn.fetch(
                query, [m.id for m, _ in pending_nick_updates], [m.guild.id for m, _ in pending_nick_updates]
            )

            current_names: dict[int, tuple[str, int]] = {m_id: (m_name, m_idx) for m_id, m_name, m_idx in name_rows}
            current_nicks: dict[tuple[int, int], tuple[str | None, int]] = {
                (m_id, m_guild): (m_name, m_idx) for m_id, m_guild, m_name, m_idx in nick_rows
            }
            return current_names, current_nicks

    async def calculate_needed_inserts(
        self,
        pending_name_updates: list[tuple[discord.Member, int]],
        pending_nick_updates: list[tuple[discord.Member, int]],
        current_names: dict[int, tuple[str, int]],
        current_nicks: dict[tuple[int, int], tuple[str | None, int]],
    ) -> tuple[
        list[tuple[int, str, int, int]],
        list[tuple[int, int, str | None, int, int]],
        dict[int, tuple[str, int]],
        dict[tuple[int, int], tuple[str | None, int]],
    ]:
        name_inserts: list[tuple[int, str, int, int]] = []
        nick_inserts: list[tuple[int, int, str | None, int, int]] = []

        for member, timestamp in pending_name_updates:
            curr_name, curr_idx = current_names.get(member.id) or (None, 0)

            if curr_name != member.name:
                curr_idx += 1
                name_inserts.append((member.id, member.name, curr_idx, timestamp))
            current_names[member.id] = (member.name, curr_idx)

        for member, timestamp in pending_nick_updates:
            curr_name, curr_idx = current_nicks.get((member.id, member.guild.id)) or (None, 0)

            if curr_name != member.nick:
                curr_idx += 1
                nick_inserts.append((member.id, member.guild.id, member.nick, curr_idx, timestamp))
            current_nicks[member.id, member.guild.id] = (member.nick, curr_idx)
        return name_inserts, nick_inserts, current_names, current_nicks

    async def batch_insert_name_updates(
        self, name_inserts: list[tuple[int, str, int, int]], nick_inserts: list[tuple[int, int, str | None, int, int]]
    ) -> None:
        assert len(name_inserts) < (PG_ARG_MAX // 4)
        assert len(nick_inserts) < (PG_ARG_MAX // 5)
        async with self.bot.pool.acquire() as conn:
            if name_inserts:
                query = f"""INSERT INTO namechanges (id, name, idx, date)
                            VALUES {multi_insert_str(name_inserts)}
                            ON CONFLICT (id, idx) DO NOTHING;
                        """
                await conn.execute(query, *itertools.chain(*name_inserts))
            if nick_inserts:
                query = f"""INSERT INTO nickchanges (id, guild_id, name, idx, date)
                            VALUES {multi_insert_str(nick_inserts)}
                            ON CONFLICT (id, guild_id, idx) DO NOTHING;
                        """
                await conn.execute(query, *itertools.chain(*nick_inserts))

    async def batch_set_redis_names(
        self, current_names: dict[int, tuple[str, int]], current_nicks: dict[tuple[int, int], tuple[str | None, int]]
    ) -> None:
        assert len(current_names) <= 50000
        assert len(current_nicks) <= 50000
        if not current_names and not current_nicks:
            return

        user_keys = {f'ayaka:last_username:{m_id}': name_to_redis(m_name) for m_id, (m_name, _) in current_names.items()}
        name_keys = {
            f'ayaka:last_nickname:{m_id}:{m_guild}': name_to_redis(m_name)
            for (m_id, m_guild), (m_name, _) in current_nicks.items()
        }
        final = {**user_keys, **name_keys}
        await self.bot.redis.mset(final)  # type: ignore

    async def _last_username(self, member: discord.Member | discord.User) -> str | None:
        last_name = await self.bot.redis.get(name_key(member))
        if last_name:
            return name_from_redis(last_name)

        async with self.bot.pool.acquire() as conn:
            row: str | None = await conn.fetchval(
                'SELECT name FROM namechanges WHERE id = $1 ORDER BY idx DESC LIMIT 1;', member.id
            )
            if row:
                last_name = row

        await self.bot.redis.set(name_key(member), name_to_redis(last_name))
        return last_name

    async def _last_nickname(self, member: discord.Member) -> str | None:
        last_name = await self.bot.redis.get(nick_key(member))
        if last_name:
            return name_from_redis(last_name)

        async with self.bot.pool.acquire() as conn:
            row: str | None = await conn.fetchval(
                'SELECT name FROM nickchanges WHERE id = $1 AND guild_id = $2 ORDER BY idx DESC LIMIT 1;',
                member.id,
                member.guild.id,
            )
            if row:
                last_name = row

        await self.bot.redis.set(nick_key(member), name_to_redis(last_name))
        return last_name

    async def names_for(self, member: discord.Member | discord.User, since: timedelta | None = None) -> list[str]:
        async with self.bot.pool.acquire() as conn:
            params = []
            query = 'SELECT name, idx FROM namechanges WHERE id = $1 '
            params.append(member.id)
            if since:
                query += 'AND date >= $2 '
                params.append(discord.utils.utcnow() - since)
            query += 'ORDER BY idx DESC '
            if since:
                query = f"""(SELECT name, idx FROM namechanges
                            WHERE id = $1 AND date < $2
                            ORDER BY idx DESC LIMIT 1)
                            UNION ({query})
                            ORDER BY idx DESC;
                        """
            rows = await conn.fetch(query, *params)

            if rows:
                return [row[0] for row in rows]
            last_name = await self._last_username(member)
            if last_name:
                return [last_name]
            return []

    async def nicks_for(self, member: discord.Member, since: timedelta | None = None) -> list[str]:
        async with self.bot.pool.acquire() as conn:
            params = []
            query = 'SELECT name, idx FROM nickchanges WHERE id = $1 AND guild_id = $2 '
            params.extend((member.id, member.guild.id))
            if since:
                query += 'AND date >= $3 '
                params.append(discord.utils.utcnow() - since)
            query += 'ORDER BY idx DESC '
            if since:
                query = f"""(SELECT name, idx FROM nickchanges
                            WHERE id = $1 AND guild_id = $2 AND date < $3
                            ORDER BY idx DESC LIMIT 1)
                            UNION ({query})
                            ORDER BY idx DESC;
                        """
            rows = await conn.fetch(query, *params)

            if rows:
                return [row[0] for row in rows if row[0] is not None]
            last_name = await self._last_nickname(member)
            if last_name:
                return [last_name]
            return []

    async def update_name_change(self, member: discord.Member) -> None:
        last_name = await self._last_username(member)
        if last_name == member.name:
            return

        async with self.bot.pool.acquire() as conn:
            query = """SELECT name, idx FROM namechanges
                       WHERE id = $1
                       ORDER BY idx DESC
                       LIMIT 1;
                    """
            name, idx = await conn.fetchrow(query, member.id) or (None, 0)
            if name != member.name:
                query = """INSERT INTO namechanges (id, name, idx)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (id, idx) DO NOTHING;
                        """
                await conn.execute(query, member.id, member.name, idx + 1)

        await self.bot.redis.set(name_key(member), name_to_redis(member.name))

    async def update_nick_change(self, member: discord.Member) -> None:
        last_nick = await self._last_nickname(member)
        if last_nick == member.nick:
            return

        async with self.bot.pool.acquire() as conn:
            query = """SELECT name, idx FROM nickchanges
                       WHERE id = $1 AND guild_id = $2
                       ORDER BY idx DESC
                       LIMIT 1;
                    """
            name, idx = await conn.fetchrow(query, member.id, member.guild.id) or (None, 0)
            if name != member.nick:
                query = """INSERT INTO nickchanges (id, guild_id, name, idx)
                           VALUES ($1, $2, $3, $4)
                           ON CONFLICT (id, guild_id, idx) DO NOTHING;
                        """
                await conn.execute(query, member.id, member.guild.id, member.nick, idx + 1)

        await self.bot.redis.set(nick_key(member), name_to_redis(member.nick))

    def queue_batch_names_update(self, member: discord.Member) -> None:
        self.batch_name_updates.append((member, discord.utils.utcnow()))

    async def last_seen(self, member: discord.User | discord.Member) -> LastSeenTuple:
        async with self.bot.pool.acquire() as conn:
            last_seen = await conn.fetchval('SELECT date FROM last_seen WHERE id = $1 LIMIT 1;', member.id)
            last_spoke = await conn.fetchval(
                'SELECT date FROM last_spoke WHERE id = $1 AND guild_id = 0 LIMIT 1;', member.id
            )
            if hasattr(member, 'guild'):
                guild_last_spoke = await conn.fetchval('SELECT date FROM last_spoke WHERE id = $1 AND guild_id = $2 LIMIT 1;', member.id, member.guild.id)  # type: ignore
            else:
                guild_last_spoke = datetime.fromtimestamp(0, tz=timezone.utc)

        return LastSeenTuple(
            last_seen=last_seen or datetime.fromtimestamp(0, tz=timezone.utc),
            last_spoke=last_spoke or datetime.fromtimestamp(0, tz=timezone.utc),
            guild_last_spoke=guild_last_spoke or datetime.fromtimestamp(0, tz=timezone.utc),
        )

    async def bulk_last_seen(self, members: list[discord.Member]) -> list[LastSeenTuple]:
        ids = [member.id for member in members]
        guild_id = members[0].guild.id

        async with self.bot.pool.acquire() as conn:
            last_seens = {
                member_id: date
                for member_id, date in await conn.fetch(
                    'SELECT id, date FROM last_seen WHERE id = ANY($1::BIGINT[]);',
                    ids,
                )
            }
            last_spokes = {
                member_id: date
                for member_id, date in await conn.fetch(
                    'SELECT id, date FROM last_spoke WHERE id = ANY($1::BIGINT[]) AND guild_id = 0;',
                    ids,
                )
            }
            guild_last_spokes = {
                member_id: date
                for member_id, date in await conn.fetch(
                    'SELECT id, date FROM last_spoke WHERE id = ANY($1::BIGINT[]) AND guild_id = $2;',
                    ids,
                    guild_id,
                )
            }

        return [
            LastSeenTuple(
                last_seen=last_seens.get(member.id, datetime.fromtimestamp(0, tz=timezone.utc)),
                last_spoke=last_spokes.get(member.id, datetime.fromtimestamp(0, tz=timezone.utc)),
                guild_last_spoke=guild_last_spokes.get(member.id, datetime.fromtimestamp(0, tz=timezone.utc)),
            )
            for member in members
        ]

    async def update_last_update(self, member: discord.Member | discord.User) -> None:
        async with self._last_seen_update_lock:
            self.queue_batch_last_update(member)

    async def update_last_message(self, member: discord.Member | discord.User) -> None:
        async with self._last_spoke_update_lock:
            self.queue_batch_last_spoke_update(member)
        async with self._last_seen_update_lock:
            self.queue_batch_last_update(member)

    def queue_batch_last_spoke_update(
        self, member: discord.Member | discord.User | FakeMember | discord.Object, at_time: datetime | None = None
    ) -> None:
        at_time = at_time or discord.utils.utcnow()
        self.batch_last_spoke_updates.append(SpokeUpdate(member.id, 0, at_time))
        if hasattr(member, 'guild'):
            self.batch_last_spoke_updates.append(SpokeUpdate(member.id, member.guild.id, at_time))  # type: ignore

    def queue_batch_last_update(self, member: discord.abc.Snowflake, at_time: datetime | None = None) -> None:
        at_time = at_time or discord.utils.utcnow()
        self.batch_last_seen_updates.append(SeenUpdate(member.id, at_time))

    async def queue_migrate_redis(self) -> None:
        cur = 0
        while cur:
            cur, keys = await self.bot.redis.scan(cur, match='ayaka:last_seen:*', count=5000)
            values = await self.bot.redis.mget(*keys)

            async with self._last_seen_update_lock:
                for key, value in zip(keys, values):
                    user_id = re.match(r'ayaka:last_seen:(\d+)', key).group(1)  # type: ignore # match is never None
                    self.queue_batch_last_update(discord.Object(int(user_id)), at_time=datetime_from_redis(value))

        cur = 0
        while cur:
            cur, keys = await self.bot.redis.scan(cur, match='ayaka:last_spoke:*', count=5000)
            values = await self.bot.redis.mget(*keys)

            async with self._last_spoke_update_lock:
                for key, value in zip(keys, values):
                    user_id, guild_id = re.match(r'ayaka:last_spoke:(\d+)(?::(\d+))?', key).groups()  # type: ignore # match is never None
                    if guild_id:
                        fake_user = FakeMember(int(user_id))
                        fake_user.guild = discord.Object(int(guild_id))
                        self.queue_batch_last_spoke_update(fake_user, at_time=datetime_from_redis(value))
                    else:
                        self.queue_batch_last_spoke_update(discord.Object(int(user_id)), at_time=datetime_from_redis(value))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self.on_guild_join(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        for member in copy.copy(list(guild.members)):
            self.queue_batch_names_update(member)
            if member.status is not discord.Status.offline:
                self.queue_batch_last_update(member)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.status != after.status:
            async with self._last_seen_update_lock:
                self.queue_batch_last_update(after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        async with self._name_update_lock:
            self.queue_batch_names_update(after)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await asyncio.gather(
            self.update_last_update(member),
            self.update_name_change(member),
            self.update_nick_change(member),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.update_last_message(message.author)

    @commands.Cog.listener()
    async def on_typing(self, channel: discord.abc.Messageable, user: discord.User | discord.Member, when: datetime) -> None:
        await self.update_last_update(user)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        if 'author' not in data or not data['author']:
            return  # link embed
        if data['author']['discriminator'] == '0000':
            return  # webhook
        if data['edited_timestamp'] is None:
            return  # pins etc. that aren't actually edits

        # If we had a recent pin in this channel, and the edited_timestamp is
        # not within 5 seconds of the pin, or newer than the pin, ignore as this
        # is a pin.

        edit_time = discord.utils.parse_time(data['edited_timestamp'])
        last_pin_time = self._recent_pins.get(data['channel_id'])

        # If a message is edited, then pinned within 5 seconds, we will end up
        # updating incorrectly, but oh well.
        if last_pin_time and edit_time < last_pin_time - timedelta(seconds=5):
            return  # If we get an edit in the past, ignore it, it's a pin.
        elif edit_time < discord.utils.utcnow() - timedelta(minutes=2):
            log.info('Got edit with old timestamp, missed pin? %s', data)
            return  # This *may* be a pin, since it's old, but we didn't get a pin event.

        if 'guild_id' in data and data['guild_id']:
            guild = self.bot.get_guild(int(data['guild_id']))
            if guild is None:
                return  # :yert:
            author = guild.get_member(int(data['author']['id']))
        else:
            author = self.bot.get_user(int(data['author']['id']))

        if not author:
            log.warning('Got raw_message_edit for nonexistent author %s', data)
            return
        await self.update_last_message(author)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(
        self, channel: discord.abc.GuildChannel | discord.Thread, last_pin: datetime | None
    ) -> None:
        self._recent_pins[str(channel.id)] = discord.utils.utcnow()

    @commands.Cog.listener()
    async def on_private_channel_pins_update(self, channel: discord.abc.PrivateChannel, last_pin: datetime | None) -> None:
        self._recent_pins[str(channel.id)] = discord.utils.utcnow()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return
            member = guild.get_member(payload.user_id)
        else:
            member = self.bot.get_user(payload.user_id)
        if member is None:
            return
        await self.update_last_update(member)

    @commands.group(invoke_without_command=True)
    async def names(self, ctx: Context, *, user: discord.Member | discord.User = commands.Author) -> None:
        """Shows a user's previous names within the last 90 days."""
        names = await self.names_for(user, since=timedelta(days=90))
        names = re.sub(r'([`*_])', r'\\\1', ', '.join(names))
        names = names.replace('@', '@\u200b')
        await ctx.send(f'Names for {user} in the last 90 days\n{names}')

    @names.command(name='all')
    async def names_all(self, ctx: Context, *, user: discord.Member | discord.User = commands.Author) -> None:
        """Shows all of a user's previous names."""
        names = await self.names_for(user)
        names = re.sub(r'([`*_])', r'\\\1', ', '.join(names))
        names = names.replace('@', '@\u200b')
        await ctx.send(f'All names for {user}\n{names}')

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def nicks(self, ctx: GuildContext, *, user: discord.Member = commands.Author) -> None:
        """Shows a member's previous nicks within the last 90 days."""
        names = await self.nicks_for(user, since=timedelta(days=90))
        names = re.sub(r'([`*_])', r'\\\1', ', '.join(names))
        names = names.replace('@', '@\u200b')
        await ctx.send(f'Nicks for {user} in the last 90 days\n{names}')

    @nicks.command(name='all')
    @commands.guild_only()
    async def nicks_all(self, ctx: GuildContext, *, user: discord.Member = commands.Author) -> None:
        """Shows all of a member's previous nicks."""
        names = await self.nicks_for(user)
        names = re.sub(r'([`*_])', r'\\\1', ', '.join(names))
        names = names.replace('@', '@\u200b')
        await ctx.send(f'All nicks for {user}\n{names}')

    @commands.command(name='stalk', aliases=['ui', 'ls'])
    async def _stalk(self, ctx: Context, *, user: discord.Member | discord.User = commands.Author) -> None:
        """Similar to `info` but gives you past names and last seen information."""
        names = ', '.join((await self.names_for(user))[:3])
        builder = [('User', str(user)), ('Names', names or user.name)]
        if isinstance(user, discord.Member):
            builder.append(('Nicks', ', '.join((await self.nicks_for(user))[:3]) or 'N/A'))
        builder.append(('Shared Guilds', sum(g.get_member(user.id) is not None for g in self.bot.guilds)))
        last_seen = await self.last_seen(user)

        def format_date(dt: datetime):
            if dt <= datetime.fromtimestamp(0, tz=timezone.utc):
                return 'N/A'
            return f'{dt:%Y-%m-%d %H:%M} ({human_timedelta(dt, accuracy=1)})'

        builder.append(('Created', format_date(user.created_at)))
        if isinstance(user, discord.Member):
            builder.append(('Joined', format_date(user.joined_at)))
        builder.append(('Last Seen', format_date(last_seen.last_seen)))
        builder.append(('Last Spoke', format_date(last_seen.last_spoke)))
        if isinstance(user, discord.Member):
            builder.append(('Spoke Here', format_date(last_seen.guild_last_spoke)))
        col_len = max(len(name) for name, _ in builder)

        def value_format(k, v):
            v = str(v).split('\n')
            return f'{k:>{col_len}}: {v[0]}' + ''.join('\n{" " * col_len}  {subv}' for subv in v[1:])

        fmt = '\n'.join(value_format(k, v) for k, v in builder)
        fmt = fmt.replace('@', '@\u200b')
        fmt = clean_triple_backtick(fmt)
        fmt = fmt.replace('discord.gg/', 'discord.gg/\u200b')
        await ctx.send(f'```prolog\n{fmt}\n```')

    @commands.command()
    @commands.is_owner()
    async def updateall(self, ctx: Context) -> None:
        for g in self.bot.guilds:
            await self.on_guild_join(g)
        await self.updatestatus(ctx)

    @commands.command()
    @commands.is_owner()
    async def updatestatus(self, ctx: Context) -> None:
        rows = (
            (
                len(self.batch_name_updates),
                len(self._batch_name_curr_updates),
                str(self.batch_name._task._state if self.batch_name._task else 'Not running'),
                len(self.batch_last_spoke_updates),
                len(self._batch_last_spoke_curr_updates),
                len(self.batch_last_seen_updates),
                len(self._batch_last_seen_curr_updates),
                str(self.batch_presence._task._state if self.batch_presence._task else 'Not running'),
            ),
        )
        lines = tabulate.tabulate(
            rows,
            headers=['BNU', 'CNU', 'BNS', 'BSpU', 'CSpU', 'BSeU', 'CSeU', 'BPS'],
            tablefmt='simple',
        )
        await ctx.send(f'```prolog\n{lines}\n```')

    @commands.command()
    @commands.is_owner()
    async def migrate_presence_db(self, ctx: Context) -> None:
        async with ctx.typing():
            await self.queue_migrate_redis()
            await self.updatestatus(ctx)


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(Stalking(bot))
