"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

import discord


if TYPE_CHECKING:
    from .context import Context

T = TypeVar('T')

class ConfirmationView(discord.ui.View):
    def __init__(self, *, timeout: float, author_id: int, reacquire: bool, ctx: Context, delete_after: bool) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self.delete_after: bool = delete_after
        self.author_id: int = author_id
        self.ctx: Context = ctx
        self.reacquire: bool = reacquire
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        else:
            await interaction.response.send_message('This confirmation dialog is not for you.', ephemeral=True)
            return False

    async def on_timeout(self) -> None:
        if self.reacquire:
            await self.ctx.acquire()
        if self.delete_after and self.message:
            await self.message.delete()

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_message()
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_message()
        self.stop()


class DisambiguationView(discord.ui.View, Generic[T]):
    def __init__(self, matches: dict[int, tuple[T, Any]], author_id: int, ctx: Context) -> None:
        super().__init__()
        self.matches = matches
        self.value: T | None = None
        self.message: discord.Message | None = None
        for k, v in matches.items():
            self.select.add_option(label=str(v[1]), value=str(k))
        self.author_id = author_id
        self.ctx = ctx
        
    @discord.ui.select(options=[])
    async def select(self, interaction: discord.Interaction, item: discord.ui.Select) -> None:
        self.value = self.matches[int(item.values[0])][0]
        if self.message:
            await self.message.delete()
            self.message = None
        await interaction.response.send_message(self.ctx.tick(True), ephemeral=True)
        self.stop()
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id in (self.author_id, self.ctx.bot.owner_id):
            return True
        else:
            await interaction.response.send_message('This disambiguation dialog is not for you.', ephemeral=True)
            return False
    
    async def on_timeout(self) -> None:
        if self.message:
            await self.message.delete()
        self.stop()
