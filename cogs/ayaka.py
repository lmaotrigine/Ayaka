"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import timeit
import traceback
from collections import namedtuple
from functools import partial
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks
from jishaku.codeblocks import Codeblock, codeblock_converter

from utils.context import Context
from utils.formats import to_codeblock


ProfileState = namedtuple('ProfileState', 'path name')


if TYPE_CHECKING:
    from bot import Ayaka
    from cogs.stats import Stats


class AyakaCore(commands.Cog, name='Ayaka'):
    """Ayaka specific commands."""

    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.ayaka_task.start()
        self.ayaka_details = {
            False: ProfileState('static/dusk.png', 'Ayaka Dusk'),
            True: ProfileState('static/dawn.png', 'Ayaka Dawn'),
        }

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        now = discord.utils.utcnow()
        dawn = now.hour >= 6 and now.hour < 18
        if dawn:
            return discord.PartialEmoji(name='ayaka_dawn', id=992019469016772639)
        else:
            return discord.PartialEmoji(name='ayaka_dusk', id=992019472321892352)

    def cog_unload(self):
        self.ayaka_task.cancel()

    @commands.command(name='hello')
    async def hello(self, ctx: Context) -> None:
        """Say hello to Ayaka."""
        now = discord.utils.utcnow()
        time = now.hour >= 6 and now.hour < 18
        path = self.ayaka_details[time].path

        file = discord.File(path, filename='ayaka.jpg')
        embed = discord.Embed(colour=0xEC9FED)
        embed.set_image(url='attachment://ayaka.jpg')
        embed.description = (
            f'Hello, I am {self.ayaka_details[time].name}, written by isis (`5ht2`).\n\nYou should see my other side~'
        )

        await ctx.send(embed=embed, file=file)

    @commands.group(invoke_without_command=True)
    async def ayaka(self, ctx: Context) -> None:
        """This is purely for subcommands."""

    @ayaka.command()
    @commands.is_owner()
    async def core(self, ctx: Context, *, body: Codeblock = commands.param(converter=codeblock_converter)) -> None:
        """Directly evaluate Ayaka core code."""
        jsk = self.bot.get_command('jishaku python')
        assert jsk is not None

        await jsk(ctx, argument=body)

    @ayaka.command()
    @commands.is_owner()
    async def system(self, ctx: Context, *, body: Codeblock = commands.param(converter=codeblock_converter)) -> None:
        """Directly evaluate Ayaka system code."""
        jsk = self.bot.get_command('jishaku shell')
        assert jsk is not None
        await jsk(ctx, argument=body)

    @ayaka.command()
    @commands.is_owner()
    async def timeit(
        self,
        ctx: Context,
        iterations: int = commands.parameter(default=100),
        *,
        body: Codeblock = commands.param(converter=codeblock_converter),
    ) -> None:
        await ctx.message.add_reaction(self.bot.emoji[None])
        timeit_globals = {
            'ctx': ctx,
            'guild': ctx.guild,
            'author': ctx.author,
            'bot': ctx.bot,
            'channel': ctx.channel,
            'discord': discord,
            'commands': commands,
        }
        timeit_globals.update(globals())

        func = partial(timeit.timeit, body.content, number=iterations, globals=timeit_globals)
        run = await self.bot.loop.run_in_executor(None, func)
        await ctx.message.add_reaction(self.bot.emoji[True])

        embed = discord.Embed(
            title=f'timeit of {iterations} iterations took {run:.20f}.',
            colour=0xEC9FED,
        )
        embed.add_field(
            name='Body',
            value=to_codeblock(body.content, language=body.language or '', escape_md=False),
        )

        await ctx.send(embed=embed)

    @ayaka.command(aliases=['sauce'])
    @commands.is_owner()
    async def source(self, ctx: Context, *, command: str) -> None:
        """Show Ayaka system code."""
        jsk = self.bot.get_command('jishaku source')
        assert jsk is not None
        await jsk(ctx, command_name=command)

    @ayaka.command(aliases=['debug'])
    @commands.is_owner()
    async def diagnose(self, ctx: Context, *, command_name: str) -> None:
        """Diagnose ayaka features."""
        jsk = self.bot.get_command('jishaku debug')
        assert jsk is not None
        await jsk(ctx, command_string=command_name)

    @ayaka.command()
    @commands.is_owner()
    async def sleep(self, ctx: Context) -> None:
        """Ayaka naptime."""
        await ctx.send('さようなら!')
        await self.bot.close()

    @property
    def light(self) -> bool:
        now = discord.utils.utcnow()
        light = now.hour >= 6 and now.hour < 18
        return light

    @tasks.loop(
        time=[
            datetime.time(hour=6, minute=0, tzinfo=datetime.timezone.utc),
            datetime.time(hour=18, minute=0, tzinfo=datetime.timezone.utc),
        ]
    )
    async def ayaka_task(self) -> None:
        profile = self.ayaka_details[self.light]
        name = profile.name
        path = profile.path
        with open(path, 'rb') as buffer:
            await self.webhook_send(f'Performing change to: {name}')
            await self.bot.user.edit(username=name, avatar=buffer.read())

    @ayaka_task.before_loop
    async def before_ayaka(self) -> None:
        await self.bot.wait_until_ready()

        profile = self.ayaka_details[self.light]
        name = profile.name
        path = profile.path

        if (self.light and self.bot.user.name != 'Ayaka Dawn') or (not self.light and self.bot.user.name != 'Ayaka Dusk'):
            with open(path, 'rb') as buffer:
                await self.webhook_send(f'Drift - changing to: {name}.')
                await self.bot.user.edit(username=name, avatar=buffer.read())

    @ayaka_task.error
    async def ayaka_error(self, error: BaseException):
        error = getattr(error, 'original', error)

        if isinstance(error, discord.HTTPException):
            await self.webhook_send('You are ratelimited on profile edits.')
            self.ayaka_task.cancel()
            self.ayaka_task.start()
        else:
            embed = discord.Embed(title='Ayaka Error', colour=discord.Colour.red())
            lines = traceback.format_exception(type(error), error, error.__traceback__, 4)
            embed.description = to_codeblock(''.join(lines), escape_md=False)
            await self.webhook_send(embed=embed)

    async def webhook_send(self, message: str = 'Error', *, embed: discord.Embed = discord.utils.MISSING):
        cog: Stats = self.bot.get_cog('Stats')  # type: ignore # ???
        if not cog:
            await asyncio.sleep(5)
            return await self.webhook_send(message, embed=embed)
        wh = cog.webhook
        await wh.send(message, embed=embed)


async def setup(bot):
    await bot.add_cog(AyakaCore(bot))
