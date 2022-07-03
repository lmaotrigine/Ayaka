"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from io import BytesIO
from typing import TYPE_CHECKING, Optional

import asyncpg
import discord
from discord.ext import commands, tasks

from utils import cache, db


if TYPE_CHECKING:
    from bot import Ayaka


class Avatars(db.Table):
    id = db.PrimaryKeyColumn()
    user_id = db.Column(db.Integer(big=True), index=True)
    attachment = db.Column(db.String)
    avatar = db.Column(db.String, index=True)


class AvatarCache:
    __slots__ = ('user_id', 'urls', 'last_avatar')

    user_id: int
    urls: list[str]
    last_avatar: Optional[str]

    @classmethod
    def from_record(cls, records: list[asyncpg.Record]) -> AvatarCache:
        self = cls()
        self.user_id = records[0]['user_id']
        self.urls = [record['attachment'] for record in records]
        self.last_avatar = records[0]['avatar']
        return self


class Logging(commands.Cog):
    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        # member_id: list[tuple[attachment_url, last_avatar_url]]
        self._avy_cache = defaultdict(list)
        self._batch_lock = asyncio.Lock()
        self.batch_update.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_update.start()

    def cog_unload(self) -> None:
        self.batch_update.stop()

    @discord.utils.cached_property
    def webhook(self) -> discord.Webhook:
        hook = discord.Webhook.from_url(self.bot.config.avatar_webhook, session=self.bot.session)
        return hook

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if before.display_avatar == after.display_avatar:
            return
        await self.save_avatar(after)

    async def save_avatar(self, member: discord.User | discord.Member) -> None:
        fp = BytesIO()
        avy = member.display_avatar
        ext = 'gif' if avy.is_animated() else 'png'
        avy = avy.with_size(1024).with_format(ext)
        await avy.save(fp)
        msg = await self.webhook.send(file=discord.File(fp, f'{member.id}.{ext}'), wait=True)
        self._avy_cache[member.id].append((msg.attachments[0].url, avy.url))

    async def upsert(self, member: discord.User | discord.Member) -> None:
        avs = await self.get_user_avys(member.id)
        ext = 'gif' if member.display_avatar.is_animated() else 'png'
        current = member.display_avatar.with_format(ext).with_size(1024).key
        if current != avs.last_avatar:
            await self.save_avatar(member)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.upsert(member)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not guild.chunked:
            await guild.chunk()
        for members in discord.utils.as_chunks(guild.members, 10):
            files = []
            avys = {}
            for member in members:
                exist = await self.get_user_avys(member.id)
                avy = member.display_avatar.with_size(1024)
                ext = 'gif' if avy.is_animated() else 'png'
                avy = avy.with_format(ext)
                if avy.key == exist.last_avatar:
                    continue
                fp = BytesIO()
                avys[member.id] = avy.key
                await avy.save(fp)
                files.append(discord.File(fp, f'{member.id}.{ext}'))
            msg = await self.webhook.send(files=files, wait=True)
            for a in msg.attachments:
                m_id = int(a.filename.split('.')[0])
                self._avy_cache[m_id].append((a.url, avys[m_id]))

    async def _batch_update(self):
        query = """INSERT INTO avatars (user_id, attachment, avatar)
                   SELECT x.user_id, x.attachment, x.avatar
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(user_id BIGINT, attachment TEXT, avatar TEXT);
                """
        if not self._avy_cache:
            return

        final_data = []
        for user_id, data in self._avy_cache.items():
            for attachment, avatar in data:
                final_data.append({'user_id': user_id, 'attachment': attachment, 'avatar': avatar})
            self.get_user_avys.invalidate(self, user_id)

        await self.bot.pool.execute(query, final_data)
        self._avy_cache.clear()

    @tasks.loop(seconds=10)
    async def batch_update(self):
        async with self._batch_lock:
            await self._batch_update()

    @cache.cache()
    async def get_user_avys(self, user_id: int) -> AvatarCache:
        query = """SELECT * FROM avatars WHERE user_id = $1 ORDER BY id DESC;"""
        async with self.bot.pool.acquire(timeout=300) as con:
            records = await con.fetch(query, user_id)
            if not records:
                records = [{'user_id': user_id, 'attachment': None, 'avatar': None}]
            return AvatarCache.from_record(records)

    async def avatar_history(self, user_id: int) -> dict[str, str | list[str]]:
        user = self.bot.get_user(user_id)  # ignore if not in cache idc
        if user is None:
            return {'avatars': []}
        image = user.display_avatar.with_static_format('png').with_size(1024).url
        avatars = await self.get_user_avys(user_id)
        return {'user': str(user), 'image': image, 'avatars': avatars.urls}


async def setup(bot: Ayaka):
    await bot.add_cog(Logging(bot))
