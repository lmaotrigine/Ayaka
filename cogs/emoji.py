"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

import asyncpg
import discord
import yarl
from discord.ext import commands, tasks

from utils import checks
from utils.paginator import RoboPages, TextPageSource


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import Context, GuildContext


log = logging.getLogger(__name__)

EMOJI_REGEX = re.compile(r'<a?:.+?:([0-9]{15,21})>')
EMOJI_NAME_REGEX = re.compile(r'[0-9a-zA-Z\_]{2,32}')


def partial_emoji(argument: str, *, regex: re.Pattern = EMOJI_REGEX) -> int:
    if argument.isdigit():
        # assume it's an emoji ID
        return int(argument)
    m = regex.match(argument)
    if m is None:
        raise commands.BadArgument("That's not a custom emoji...")
    return int(m.group(1))


def emoji_name(argument: str, *, regex: re.Pattern = EMOJI_NAME_REGEX) -> str:
    m = regex.match(argument)
    if m is None:
        raise commands.BadArgument('Invalid emoji name.')
    return argument


class EmojiURL:
    def __init__(self, *, animated: bool, url: str | yarl.URL) -> None:
        self.url = url
        self.animated = animated

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> EmojiURL:
        try:
            partial = await commands.PartialEmojiConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                url = yarl.URL(argument)
                if url.scheme not in ('http', 'https'):
                    raise RuntimeError
                path = url.path.lower()
                if not path.endswith(('.png', '.jpeg', '.jpg', '.gif')):
                    raise RuntimeError
                return cls(animated=url.path.endswith('.gif'), url=url)
            except Exception:
                raise commands.BadArgument('Not a valid or supported emoji URL.') from None
        else:
            return cls(animated=partial.animated, url=str(partial.url))


def usage_per_day(dt: datetime.datetime, usages: int) -> float:
    tracking_started = datetime.datetime(2022, 4, 1, tzinfo=datetime.timezone.utc)
    now = discord.utils.utcnow()
    if dt < tracking_started:
        base = tracking_started
    else:
        base = dt

    days = (now - base).total_seconds() / 86400
    if int(days) == 0:
        return usages
    return usages / days


class Emoji(commands.Cog):
    """Custom emoji tracking."""

    def __init__(self, bot: Ayaka):
        self.bot = bot
        self._batch_of_data = defaultdict(Counter)
        self._batch_lock = asyncio.Lock()
        self.bulk_insert.add_exception_type(asyncpg.PostgresConnectionError)
        self.bulk_insert.start()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{LOWER LEFT PAINTBRUSH}\ufe0f')

    def cog_unload(self):
        self.bulk_insert.stop()

    async def cog_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(f'{error}')

    @tasks.loop(seconds=60.0)
    async def bulk_insert(self) -> None:
        query = """INSERT INTO emoji_stats (guild_id, emoji_id, total)
                   SELECT x.guild, x.emoji, x.added
                   FROM jsonb_to_recordset($1::jsonb) AS x(guild BIGINT, emoji BIGINT, added INT)
                   ON CONFLICT (guild_id, emoji_id) DO UPDATE
                   SET total = emoji_stats.total + excluded.total;
                """

        async with self._batch_lock:
            transformed = [
                {'guild': guild_id, 'emoji': emoji_id, 'added': count}
                for guild_id, data in self._batch_of_data.items()
                for emoji_id, count in data.items()
            ]
            self._batch_of_data.clear()
            await self.bot.pool.execute(query, transformed)

    def find_all_emoji(self, message: discord.Message, *, regex: re.Pattern = EMOJI_REGEX) -> list[str]:
        return regex.findall(message.content)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        if message.author.bot:
            return

        matches = EMOJI_REGEX.findall(message.content)
        if not matches:
            return

        async with self._batch_lock:
            self._batch_of_data[message.guild.id].update(map(int, matches))

    def emoji_fmt(self, emoji_id: int, count: int, total: int) -> str:
        emoji = self.bot.get_emoji(emoji_id)
        if emoji is None:
            name = f'[\N{WHITE QUESTION MARK ORNAMENT}](https://cdn.discordapp.com/emojis/{emoji_id}.png)'
            emoji = discord.Object(id=emoji_id)
        else:
            name = str(emoji)

        per_day = usage_per_day(emoji.created_at, count)
        p = count / total
        return f'{name}: {count} uses ({p:.1%}), {per_day:.1f} uses/day.'

    async def get_guild_stats(self, ctx: GuildContext) -> None:

        e = discord.Embed(title='Emoji Leaderboard', colour=discord.Colour.og_blurple())

        query = """SELECT
                       COALESCE(SUM(total), 0) AS "Count",
                       COUNT(*) AS "Emoji"
                   FROM emoji_stats
                   WHERE guild_id = $1
                   GROUP BY guild_id;
                """
        record = await ctx.db.fetchrow(query, ctx.guild.id)
        if record is None:
            await ctx.send('This server has no emoji stats...')
            return

        total = record['Count']
        emoji_used = record['Emoji']
        per_day = usage_per_day(ctx.me.joined_at, total)  # type: ignore
        e.set_footer(text=f'{total} uses over {emoji_used} emoji for {per_day:.2f} uses per day.')

        query = """SELECT emoji_id, total
                   FROM emoji_stats
                   WHERE guild_id = $1
                   ORDER BY total DESC
                   LIMIT 10;
                """

        top = await ctx.db.fetch(query, ctx.guild.id)

        e.description = '\n'.join(f'{i}. {self.emoji_fmt(emoji, count, total)}' for i, (emoji, count) in enumerate(top, 1))
        await ctx.send(embed=e)

    async def get_emoji_stats(self, ctx: GuildContext, emoji_id: int) -> None:

        e = discord.Embed(title='Emoji Stats', colour=discord.Colour.og_blurple())
        cdn = f'https://cdn.discordapp.com/emojis/{emoji_id}.png'

        # first verify it's a real ID
        async with ctx.session.get(cdn) as resp:
            if resp.status == 404:
                e.description = "This isn't a valid emoji."
                e.set_thumbnail(url='https://vj.is-very.moe/09e106.jpg')
                await ctx.send(embed=e)
                return

        e.set_thumbnail(url=cdn)

        # valid emoji ID so let's use it
        query = """SELECT guild_id, SUM(total) AS "Count"
                   FROM emoji_stats
                   WHERE emoji_id = $1
                   GROUP BY guild_id;
                """

        records = await ctx.db.fetch(query, emoji_id)
        transformed = {k: v for k, v in records}
        total = sum(transformed.values())

        dt = discord.utils.snowflake_time(emoji_id)

        # get the stats for this guild in particular
        try:
            count = transformed[ctx.guild.id]
            per_day = usage_per_day(dt, count)
            value = f'{count} uses ({count / total:.2f} of global uses), {per_day:.2f} uses/day'
        except KeyError:
            value = 'Not used here.'

        e.add_field(name='Server Stats', value=value, inline=False)

        # global stats
        per_day = usage_per_day(dt, total)
        value = f'{total} uses, {per_day:.2f} uses/day'
        e.add_field(name='Global Stats', value=value, inline=False)
        e.set_footer(text='These statistics are for the servers I am in')
        await ctx.send(embed=e)

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def emojistats(self, ctx: GuildContext, *, emoji: partial_emoji = None) -> None:  # type: ignore
        """Shows you statistics about the emoji usage in this server.

        If no emoji is given, then it gives you the top 10 emoji used.
        """

        if emoji is None:
            await self.get_guild_stats(ctx)
        else:
            await self.get_emoji_stats(ctx, emoji)

    @emojistats.command(name='server', aliases=['guild'])
    @commands.guild_only()
    async def emojistats_guild(self, ctx: GuildContext) -> None:
        """Shows you statistics about the local server emojis in this server."""

        emoji_ids = [e.id for e in ctx.guild.emojis]

        if not emoji_ids:
            await ctx.send('This guild has no custom emoji.')

        query = """SELECT emoji_id, total
                   FROM emoji_stats
                   WHERE guild_id = $1 AND emoji_id = ANY($2::BIGINT[])
                   ORDER BY total DESC;
                """

        e = discord.Embed(title='Emoji Leaderboard', colour=discord.Colour.og_blurple())
        records = await ctx.db.fetch(query, ctx.guild.id, emoji_ids)

        total = sum(a for _, a in records)
        emoji_used = len(records)
        per_day = usage_per_day(ctx.me.joined_at, total)  # type: ignore
        e.set_footer(text=f'{total} uses over {emoji_used} emoji for {per_day:.2f} uses per day.')
        top = records[:10]
        value = '\n'.join(self.emoji_fmt(emoji, count, total) for emoji, count in top)
        e.add_field(name=f'Top {len(top)}', value=value or 'Nothing...')

        record_count = len(records)
        if record_count > 10:
            bottom = records[-10:] if record_count >= 20 else records[-record_count + 10 :]
            value = '\n'.join(self.emoji_fmt(emoji, count, total) for emoji, count in bottom)
            e.add_field(name=f'Bottom {len(bottom)}', value=value)

        await ctx.send(embed=e)

    @commands.group(name='emoji')
    @commands.guild_only()
    @checks.has_guild_permissions(manage_emojis=True)
    async def _emoji(self, ctx: Context) -> None:
        """Emoji management commands."""
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)

    @_emoji.command(name='list')
    @commands.guild_only()
    async def _emoji_list(self, ctx: GuildContext) -> None:
        """Fancy post server emojis."""
        emojis = sorted([e for e in ctx.guild.emojis if len(e.roles) == 0 and e.available], key=lambda e: e.name.lower())
        fmt = '\n'.join(f'{emoji} -- `{emoji}`' for emoji in emojis)
        source = TextPageSource(fmt, prefix='', suffix='')
        pages = RoboPages(source, ctx=ctx)
        await pages.start()

    @_emoji.command(name='create')
    async def _emoji_create(self, ctx: GuildContext, name: emoji_name, *, emoji: EmojiURL = None) -> None:  # type: ignore
        """Create an for the server under the given name.

        You must have Manage Emoji permission to use this.
        The bot must have this permission too.
        """

        if not ctx.guild.me.guild_permissions.manage_emojis:
            await ctx.send('I do not have permission to add emoji.')
            return

        reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        if emoji is None:
            try:
                url = ctx.message.attachments[0].url
            except IndexError:
                await ctx.send('You must provide either an image URL or an attachment.')
                return
            else:
                emoji = await EmojiURL.convert(ctx, url)
        emoji_count = sum(e.animated == emoji.animated for e in ctx.guild.emojis)
        if emoji_count >= ctx.guild.emoji_limit:
            await ctx.send('There are no more emoji slots in this server.')
            return

        async with self.bot.session.get(emoji.url) as resp:
            if resp.status >= 400:
                await ctx.send('Could not fetch the image.')
                return
            if int(resp.headers['Content-Length']) >= (256 * 1024):
                await ctx.send('Image is too big.')
                return
            data = await resp.read()
            coro = ctx.guild.create_custom_emoji(name=name, image=data, reason=reason)
            async with ctx.typing():
                try:
                    created = await asyncio.wait_for(coro, timeout=10.0)
                except asyncio.TimeoutError:
                    await ctx.send('Sorry, the bot is rate limited or it took too long.')
                    return
                except discord.HTTPException as e:
                    await ctx.send(f'Failed to create emoji somehow: {e}')
                else:
                    await ctx.send(f'Created {created}')


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(Emoji(bot))
