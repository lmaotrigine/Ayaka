"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import TYPE_CHECKING

import dice_parser
import discord
from discord import app_commands
from discord.ext import commands

from utils.dice import PersistentRollContext, string_search_adv, VerboseMDStringifier


if TYPE_CHECKING:
    from bot import Ayaka
    from utils._types.dnd import DndClassTopLevel
    from utils.context import Context


CLASS_PATH = pathlib.Path(__file__).parent.parent / '5e.tools' / 'class'


class DnD(commands.GroupCog, name='dnd', command_attrs=dict(hidden=True)):
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
        dice: str = '1d20',
    ) -> None:
        """Roll any combination of dice in the `XdY` format. (`1d6`, `2d8`, etc)
        
        Multiple rolls can be added together as an equation.
        Standard mathematical operators and parentheses can be used: `() + - / *`
        
        This command also accepts `adv` and `dis` for Advantage and Disadvantage.
        Rolls can be tagged with `[text]` for informational purposes.
        Any text after the roll will assign  the name of the roll.
        
        __Examples__
        `roll` or `roll 1d20` - Roll a single d20, just like at the table
        `roll 1d20+4` - A skill check or attack roll
        `roll 1d8+2+1d6` - Longbow damage with Hunter's Mark
        
        `roll 1d20+1 adv` - A skill check or attack roll with Advantage
        `roll 1d20-3 dis` - A skill check or attack roll with Disadvantage
        
        `roll (1d8+4)*2` - Warhammer damage against bludgeoning vulnerability
        
        `roll 1d10[cold]+2d6[piercing] Ice Knife` - The Ice Knife Spell does cold and piercing damage
        
        **Advanced Options**
        __Operators__
        Operators are always followed by a selector, and operate on the items in the set that match the selector.
        A set can be made of single or multiple entries i.e., `1d20` or `(1d6,1d8,1d10)`.
        
        These operations work on dice and sets of numbers
        `k` - keep - keeps all matched values.
        `p` - drop - drops all matched values.
        
        These operations only work on dice rolls.
        `rr` - reroll - Rerolls all matched die values until none match.
        `ro` - reroll once - Rerolls all matched die values once.
        `ra` - reroll and add - Rerolls up to one matched value once, add to the roll.
        `mi` - minimum - Sets the minimum value of each die.
        `ma` - maximum - Sets the maximum value of each die.
        `e` - explode on - Rolls an additional die for each matched value. Exploded dice can explode.
        
        __Selectors__
        Selectors select from the remaining kept values in a set.
        `X`  | literal X
        `lX` | lowest X
        `hX` | highest X
        `>X` | greater than X
        `<X` | less than X
        
        __Examples__
        `roll 2d20kh1+4` - Advantage roll, using Keep Highest format
        `roll 2d20kl1-2` - Disadvantage roll, using Keep Lowest format
        `roll 4d6mi2[fire]` - Elemental Adept, Fire
        `roll 10d6ra6` - Wild Magic Sorcerer Spell Bombardment
        `roll 4d6ro<3` - Great Weapon Fighting
        `roll 2d6e6` - Explode on 6
        `roll (1d6,1d8,1d10)kh2` - Keep 2 highest rolls of a set of dice
        
        **Additional information can be found at:**
        https://github.com/lmaotrigine/dice-parser/blob/main/README.md#dice-syntax
        """
        
        if dice == '0/0':
            await ctx.send('What do you expect me to do, destroy the universe?')
            return
        dice, adv = string_search_adv(dice)
        res = dice_parser.roll(dice, advantage=adv, allow_comments=True, stringifier=VerboseMDStringifier())
        embed = discord.Embed(colour=discord.Colour.random())
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        out = f':game_die:\n{res}'
        if len(out) > 3999:
            out = f':game_die:\n{str(res)[:400]}...\n**Total**: {res.total}'
        embed.description = out
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=[ctx.author]))
    
    @commands.hybrid_command(name='multiroll', aliases=['rr'])
    async def rr(self, ctx: Context, iterations: int, *, dice: str) -> None:
        """Rolls dice in xdy format a given number of times."""
        dice, adv = string_search_adv(dice)
        await self._roll_many(ctx, iterations, dice, adv=adv)
    
    @commands.hybrid_command(name='iterroll', aliases=['rrr'])
    async def rrr(self, ctx: Context, iterations: int, dice: str, dc: int | None = None, *, args: str = '') -> None:
        """Rolls dice in xdy format, given a set DC."""
        _, adv = string_search_adv(args)
        await self._roll_many(ctx, iterations, dice, dc, adv)
        
    @staticmethod
    async def _roll_many(ctx: Context, iteratons: int, roll_str: str, dc: int | None = None, adv: dice_parser.AdvType | None = None) -> None:
        if iteratons < 1 or iteratons > 100:
            await ctx.send('Too many or too few iterations.')
            return
        if adv is None:
            adv = dice_parser.AdvType.NONE
        results = []
        successes = 0
        ast = dice_parser.parse(roll_str, allow_comments=True)
        roller = dice_parser.Roller(context=PersistentRollContext())
        
        for _ in range(iteratons):
            res = roller.roll(ast, advantage=adv)
            if dc is not None and res.total >= dc:
                successes += 1
            results.append(res)
        
        if dc is None:
            header = f'Rolling {iteratons} iterations...'
            footer = f'{sum(o.total for o in results)} total.'
        else:
            header = f'Rolling {iteratons} iterations, DC {dc}...'
            footer = f'{successes} successes, {sum(o.total for o in results)} total.'
        if ast.comment:
            header = f'{ast.comment}: {header}'
        
        result_strs = '\n'.join(str(o) for o in results)
        embed = discord.Embed(colour=discord.Colour.random())
        embed.title = header
        embed.set_footer(text=footer)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        out = result_strs
        if len(out) > 3500:
            one_result = str(results[0])
            out = f'{one_result}\n[{len(results) - 1} results omitted for output size.]'
        embed.description = out
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=[ctx.author]))
    
    @roll.error
    @rr.error
    @rrr.error
    async def roll_error(self, ctx: Context, error: BaseException) -> None:
        error = getattr(error, 'original', error)
        if isinstance(error, dice_parser.RollError):
            await ctx.send(str(error), delete_after=5)
            return


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(DnD(bot))
