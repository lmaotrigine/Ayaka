"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord.ext import commands


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import GuildContext


def to_emoji(c: int) -> str:
    return chr(0x1F1E6 + c)


class Polls(commands.Cog):
    """Poll voting system."""

    def __init__(self, bot: Ayaka):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{BALLOT BOX WITH BALLOT}')

    @commands.command()
    @commands.guild_only()
    async def poll(self, ctx: GuildContext, *, question: str) -> None:
        """Interactively creates a poll with the given question.

        To vote, use reactions!
        """

        messages: list[discord.Message] = [ctx.message]
        answers = []

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel and len(m.content) <= 100

        for i in range(20):
            messages.append(await ctx.send(f'Say poll option or {ctx.prefix}cancel to publish poll.'))
            try:
                entry: discord.Message = await self.bot.wait_for('message', check=check, timeout=60.0)
            except asyncio.TimeoutError:
                break
            messages.append(entry)
            if entry.clean_content.startswith(f'{ctx.prefix}cancel'):
                break
            answers.append((to_emoji(i), entry.clean_content))
        try:
            await ctx.channel.delete_messages(messages)
        except:
            pass  # oh well

        answer = '\n'.join(f'{keycap}: {content}' for keycap, content in answers)
        actual_poll = await ctx.send(f'{ctx.author} asks: {question}\n\n{answer}')
        for emoji, _ in answers:
            await actual_poll.add_reaction(emoji)

    @poll.error
    async def poll_error(self, ctx: GuildContext, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('Missing the question.')
            ctx.command.extras['handled'] = True

    @commands.command()
    @commands.guild_only()
    async def quickpoll(self, ctx: GuildContext, *questions_and_choices: str) -> None:
        """Makes a poll quickly.

        The first argument is the question and the rest are the choices.
        """

        if len(questions_and_choices) < 3:
            await ctx.send('Need at least 1 question with 2 choices.')
            return
        elif len(questions_and_choices) > 21:
            await ctx.send('You can only have up to 20 choices.')
            return

        perms = ctx.channel.permissions_for(ctx.me)
        if not (perms.read_message_history or perms.add_reactions):
            await ctx.send('Need Read Message History and Add Reactions permissions.')
            return
        question = questions_and_choices[0]
        choices = [(to_emoji(e), v) for e, v in enumerate(questions_and_choices[1:])]
        try:
            await ctx.message.delete()
        except:
            pass

        body = '\n'.join(f'{key}: {c}' for key, c in choices)
        poll = await ctx.send(f'{ctx.author} asks: {question}\n\n{body}')
        for emoji, _ in choices:
            await poll.add_reaction(emoji)


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(Polls(bot))
