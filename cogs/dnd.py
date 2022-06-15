"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands


if TYPE_CHECKING:
    from bot import Ayaka
    from utils._types import DndClassTopLevel
    from utils.context import Context


CLASS_PATH = pathlib.Path(__file__).parent.parent / '5e.tools' / 'class'
DICE_RE = re.compile(r'(?P<rolls>[0-9]+)d(?P<die>[0-9]+)(?P<mod>[\+\-][0-9]+)?')


class Roll:
    def __init__(self, *, die: int, rolls: int, mod: int | None = None) -> None:
        self.die = die
        self.rolls = rolls
        self.mod = mod

    def __str__(self) -> str:
        fmt = f'{self.rolls}d{self.die}'
        if self.mod is not None:
            fmt += f'{self.mod:+}'
        return fmt

    def __repr__(self) -> str:
        return f'<Roll die={self.die} rolls={self.rolls} mod={self.mod:+}>'


class DiceRoll(commands.Converter[Roll]):
    async def convert(self, _: Context, argument: str) -> list[Roll]:
        search: list[tuple[str, str, str]] = DICE_RE.findall(argument)
        if not search:
            raise commands.BadArgument("Dice roll doesn't seem valid, please use it in the format of `2d20` or `2d20+8`")
        ret: list[Roll] = []
        for match in search:
            rolls: int = int(match[0])
            die: int = int(match[1])
            if match[2]:
                mod = int(match[2])
            else:
                mod = None
            ret.append(Roll(die=die, rolls=rolls, mod=mod))
        return ret


class DnD(commands.GroupCog, name='dnd'):
    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self._classes: list[str] = discord.utils.MISSING
        super().__init__()

    @app_commands.command(name='data-for')
    @app_commands.rename(class_='class')
    async def dnd_data_for(self, interaction: discord.Interaction, class_: str) -> None:
        """Returns data for the specified DnD class."""
        if class_ not in self._classes:
            await interaction.response.send_message(f'`{class_}` is not a valid DnD Class choice.')
            return
        await interaction.response.defer()
        class_path = CLASS_PATH / f'class-{class_}.json'
        with open(class_path, 'r') as fp:
            data: DndClassTopLevel = json.load(fp)
        possible_subclasses = '\n'.join(x['name'] for x in data['subclass'])
        await interaction.followup.send(f'Possible subclasses for {class_.title()}:\n\n{possible_subclasses}')

    @dnd_data_for.autocomplete(name='class_')
    async def data_for_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if self._classes is discord.utils.MISSING:
            ret: list[str] = []
            class_data_path = CLASS_PATH
            for path in class_data_path.glob('*.json'):
                class_ = path.stem
                if class_ in {'foundry', 'index', 'class-generic'}:
                    continue
                name = re.sub(r'class\-', '', class_)
                ret.append(name)
            self._classes = ret
        if not current:
            return [app_commands.Choice(name=class_.title(), value=class_) for class_ in self._classes][:25]
        return [
            app_commands.Choice(name=class_.title(), value=class_) for class_ in self._classes if current.lower() in class_
        ][:25]

    @commands.hybrid_command()
    async def roll(
        self,
        ctx: Context,
        *,
        dice: list[Roll] = commands.param(converter=DiceRoll, default=None, displayed_default='1d20+0'),
    ) -> None:
        """Roll DnD dice!

        Rolls a DnD die in the format of `1d10+0`, this includes `+` or `-` modifiers.

        Examples:
            `1d10+2`
            `2d8-12`

        You can also roll multiple dice at once, in the format of `2d10+2 1d12`.
        """
        dice = dice or [Roll(die=20, rolls=1)]
        if len(dice) > 25:
            await ctx.send('No more than 25 dice per invoke, please.')
            return

        embed = discord.Embed(title='Rolls', colour=discord.Colour.random())

        for die in dice:
            _choices: list[int] = []
            for _ in range(die.rolls):
                _choices.append(random.randint(1, die.die))
            _current_total: int = sum(_choices)
            fmt = ''
            for i, amount in enumerate(_choices, 1):
                fmt += f'Roll {i}: {amount}'
            fmt += f'\nTotal: {_current_total}'
            if die.mod:
                _current_total += die.mod
                fmt += f'\nTotal incl mod: {abs(_current_total)}'
            embed.add_field(name=f'{die}', value=f'```prolog\n{fmt}\n```')
            _current_total = 0
        embed.set_footer(text=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @roll.error
    async def roll_error(self, ctx: Context, error: BaseException) -> None:
        error = getattr(error, 'original', error)
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error), delete_after=5)
            return


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(DnD(bot))
