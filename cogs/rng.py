"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils.context import Context
from utils.formats import plural


if TYPE_CHECKING:
    from bot import Ayaka

    from .tags import Tags


class RNG(commands.Cog):
    """Utilities that provide pseudo-RNG."""

    def __init__(self, bot: Ayaka):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{GAME DIE}')

    @commands.group()
    async def random(self, ctx: Context):
        """Displays a random thing you request."""
        if ctx.invoked_subcommand is None:
            await ctx.send(f'Incorrect random subcommand passed. Try {ctx.prefix}help random')

    @random.command()
    async def tag(self, ctx: Context):
        """Displays a random tag.

        A tag showing up in this does not get its usage count increased.
        """
        tags: Tags = self.bot.get_cog('Tags')  # type: ignore # ???
        if tags is None:
            return await ctx.send('Tag commands currently disabled.')

        tag = await tags.get_random_tag(ctx.guild, connection=ctx.db)
        if tag is None:
            return await ctx.send('This server has no tags.')

        await ctx.send(f'Random tag found: {tag["name"]}\n{tag["content"]}')

    @random.command()
    async def number(self, ctx: Context, minimum: int = 0, maximum: int = 100):
        """Displays a random number within an optional range.

        The minimum must be smaller than the maximum and the maximum number
        accepted is 1000.
        """

        maximum = min(maximum, 1000)
        if minimum >= maximum:
            await ctx.send('Maximum is smaller than minimum.')
            return

        await ctx.send(f'{random.randint(minimum, maximum)}')

    @commands.command()
    async def choose(self, ctx, *choices: commands.clean_content):
        """Chooses between multiple choices.

        To denote multiple choices, you should use double quotes.
        """
        if len(choices) < 2:
            return await ctx.send('Not enough choices to pick from.')

        await ctx.send(random.choice(choices))

    @commands.command()
    async def choosebestof(self, ctx, times: int | None, *choices: commands.clean_content):
        """Chooses between multiple choices N times.

        To denote multiple choices, you should use double quotes.

        You can only choose up to 10001 times and only the top 10 results are shown.
        """
        if len(choices) < 2:
            return await ctx.send('Not enough choices to pick from.')

        if times is None:
            times = (len(choices) ** 2) + 1

        times = min(10001, max(1, times))
        results = Counter(random.choice(choices) for _ in range(times))
        builder = []
        if len(results) > 10:
            builder.append('Only showing top 10 results...')
        for index, (elem, count) in enumerate(results.most_common(10), start=1):
            builder.append(f'{index}. {elem} ({plural(count):time}, {count/times:.2%})')

        await ctx.send('\n'.join(builder))


async def setup(bot: Ayaka):
    await bot.add_cog(RNG(bot))
