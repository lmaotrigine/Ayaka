"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable

import discord
from discord import app_commands
from discord.ext import commands, menus
from fuzzywuzzy import process

from utils import time
from utils.paginator import RoboPages


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import Context, GuildContext


class TZMenuSource(menus.ListPageSource):
    def __init__(self, data: Iterable[int], embeds: list[discord.Embed]) -> None:
        self.data = data
        self.embeds = embeds
        super().__init__(data, per_page=1)

    async def format_page(self, _, page: int) -> discord.Embed:
        return self.embeds[page]


class TimezoneConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> zoneinfo.ZoneInfo:
        query = process.extract(query=argument.lower(), choices=zoneinfo.available_timezones(), limit=5)
        if argument.lower() not in {timezone.lower() for timezone in zoneinfo.available_timezones()}:
            try:
                result = await ctx.disambiguate(query, lambda t: t[0])
            except ValueError as e:
                raise commands.BadArgument(str(e))
            return zoneinfo.ZoneInfo(result[0])
        return zoneinfo.ZoneInfo(query[0][0])


class Time(commands.Cog):
    """Time cog for fun time stuff."""

    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name='Get Current Time', callback=self.now_ctx_menu)
        self.bot.tree.add_command(self.ctx_menu, override=True)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CLOCK FACE TEN OCLOCK}')

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        query = """WITH corrected AS (
                       SELECT user_id, array_agg(guild_id) new_guild_ids
                       FROM tz_store, unnest(guild_ids) WITH ORDINALITY guild_id
                       WHERE guild_id != $1
                       GROUP BY user_id
                   )
                   UPDATE tz_store
                   SET guild_ids = new_guild_ids
                   FROM corrected
                   WHERE guild_ids <> new_guild_ids
                   AND tz_store.user_id = corrected.user_id;
                """
        return await self.bot.pool.execute(query, guild.id)

    async def cog_command_error(self, ctx: Context, error: commands.CommandError):
        error = getattr(error, 'original', error)
        if isinstance(error, commands.BadArgument):
            return await ctx.send(str(error))

    def _gen_tz_embeds(self, requester: str, iterable: list[list[str]]) -> list[discord.Embed]:
        embeds = []
        for item in iterable:
            embed = discord.Embed(title='Timezone lists', colour=discord.Colour.green())
            embed.description = '\n'.join(item)
            fmt = f'Page {iterable.index(item) + 1}/{len(iterable)}'
            embed.set_footer(text=f'{fmt} | Requested by: {requester}')
            embeds.append(embed)
        return embeds

    def _curr_tz_time(self, curr_timezone: zoneinfo.ZoneInfo, *, ret_datetime: bool = False):
        # We assume it's a good TZ here
        dt_obj = datetime.now(curr_timezone)
        if ret_datetime:
            return dt_obj
        return time.hf_time(dt_obj)

    @commands.hybrid_group(name='timezone', aliases=['tz'])
    async def timezone(self, ctx: Context, *, timezone: zoneinfo.ZoneInfo = commands.param(converter=TimezoneConverter)):
        """This will return the time in a specified timezone."""
        embed = discord.Embed(
            title=f'Current time in {timezone}', description=f'```\n{self._curr_tz_time(timezone, ret_datetime=False)}\n```'
        )
        embed.set_footer(text=f'Requested by: {ctx.author}')
        embed.timestamp = datetime.utcnow()
        return await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=['tzs'])
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def timezones(self, ctx):
        tz_list = [
            list(zoneinfo.available_timezones())[x : x + 15] for x in range(0, len(zoneinfo.available_timezones()), 15)
        ]
        embeds = self._gen_tz_embeds(str(ctx.author), tz_list)
        pages = RoboPages(source=TZMenuSource(range(0, 40), embeds), ctx=ctx)
        await pages.start()

    async def get_time_for(self, member: discord.Member) -> discord.Embed:
        if member.id == self.bot.user.id:
            tz = zoneinfo.ZoneInfo('UTC')
        else:
            query = """SELECT *
                       FROM tz_store
                       WHERE user_id = $1
                       AND $2 = ANY(guild_ids)
                    """
            result = await self.bot.pool.fetchrow(query, member.id, member.guild.id)
            if not result:
                raise commands.BadArgument(f"No timezone for {member} set or it's not public in this guild.")
            member_timezone = result['tz']
            query = process.extract(query=member_timezone.lower(), choices=zoneinfo.available_timezones(), limit=5)
            tz = zoneinfo.ZoneInfo(query[0][0])

        current_time = self._curr_tz_time(tz, ret_datetime=False)
        embed = discord.Embed(title=f'Time for {member}', description=f'```\n{current_time}\n```')
        delta: timedelta = tz.utcoffset(discord.utils.utcnow())  # type: ignore # this is literally a timezone
        seconds = int(delta.total_seconds())
        utc_offset = f'UTC{seconds // 3600:+03}:{abs(seconds) // 60 % 60:02}'
        embed.set_footer(text=f'{tz} ({utc_offset})')
        embed.timestamp = discord.utils.utcnow()
        return embed

    @commands.hybrid_command(name='now', invoke_without_command=True)
    @commands.guild_only()
    async def _now(self, ctx: GuildContext, *, member: discord.Member = commands.Author):
        """Current time for a member."""
        try:
            embed = await self.get_time_for(member)
        except commands.BadArgument as e:
            await ctx.send(str(e))
            return
        return await ctx.send(embed=embed)

    @app_commands.guild_only()
    async def now_ctx_menu(self, interaction: discord.Interaction, member: discord.Member) -> None:
        try:
            embed = await self.get_time_for(member)
        except commands.BadArgument as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @timezone.command(name='set')
    @commands.guild_only()
    async def time_set(
        self, ctx: GuildContext, *, timezone: zoneinfo.ZoneInfo = commands.param(converter=TimezoneConverter)
    ):
        """Add your timezone, with a warning about public info."""

        query = """INSERT INTO tz_store (user_id, guild_ids, tz)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id) DO UPDATE
                   SET guild_ids = tz_store.guild_ids || $2, tz = $3
                   WHERE tz_store.user_id = $1;
                """
        confirm = await ctx.prompt('This will make your timezone public in this guild. confirm?', reacquire=False)
        if not confirm:
            return
        await self.bot.pool.execute(query, ctx.author.id, [ctx.guild.id], timezone.key)
        return await ctx.send(ctx.tick(True), ephemeral=True)

    @time_set.autocomplete(name='timezone')
    async def timezone_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=c, value=c) for c in zoneinfo.available_timezones() if current.lower() in c.lower()
        ][:25]

    @timezone.command(name='remove')
    @commands.guild_only()
    async def time_remove(self, ctx: GuildContext):
        """Remove your timezone from this guild."""
        query = """WITH corrected AS (
                       SELECT user_id, array_agg(guild_id) new_guild_ids
                       FROM tz_store, unnest(guild_ids) WITH ORDINALITY guild_id
                       WHERE guild_id != $2
                       AND user_id = $1
                       GROUP BY user_id
                   )
                   UPDATE tz_store
                   SET guild_ids = new_guild_ids
                   FROM corrected
                   WHERE guild_ids <> new_guild_ids
                   AND tz_store.user_id = corrected.user_id;
                """
        await self.bot.pool.execute(query, ctx.author.id, ctx.guild.id)
        return await ctx.send(ctx.tick(True), ephemeral=True)

    @timezone.command(name='clear')
    async def time_clear(self, ctx: GuildContext):
        """Clears your timezones from all guilds."""
        query = 'DELETE FROM tz_store WHERE user_id = $1;'
        confirm = await ctx.prompt('Are you sure you wish to purge your timezone from all guilds?')
        if not confirm:
            return
        await self.bot.pool.execute(query, ctx.author.id)
        return await ctx.send(ctx.tick(True), ephemeral=True)

    async def _time_error(self, ctx: Context, error):
        error = getattr(error, 'original', error)
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send("How am I supposed to do this if you don't supply the timezone?")


async def setup(bot):
    await bot.add_cog(Time(bot))
