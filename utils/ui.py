"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Generic, Sequence, TypeVar

import discord


if TYPE_CHECKING:
    from .context import Context

T = TypeVar('T')


class ConfirmationView(discord.ui.View):
    def __init__(self, *, timeout: float, author_id: int, delete_after: bool) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self.delete_after: bool = delete_after
        self.author_id: int = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        else:
            await interaction.response.send_message('This confirmation dialog is not for you.', ephemeral=True)
            return False

    async def on_timeout(self) -> None:
        if self.delete_after and self.message:
            await self.message.delete()

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()
        self.stop()


class DisambiguatorView(discord.ui.View, Generic[T]):
    message: discord.Message
    selected: T

    def __init__(self, ctx: Context, data: list[T], entry: Callable[[T], Any]):
        super().__init__()
        self.ctx = ctx
        self.data = data

        options = []
        for i, x in enumerate(data):
            opt = entry(x)
            if not isinstance(opt, discord.SelectOption):
                opt = discord.SelectOption(label=str(opt))
            opt.value = str(i)
            options.append(opt)
        
        select = discord.ui.Select(options=options)

        select.callback = self.on_select_submit
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.ctx.author.id, self.ctx.bot.owner.id):
            await interaction.response.send_message('This select menu is not meant for you, sorry!', ephemeral=True)
            return False
        return True

    async def on_select_submit(self, interaction: discord.Interaction):
        index = int(self.select.values[0])
        self.selected = self.data[index]
        await interaction.response.defer()
        await self.message.delete()
        self.stop()


class AvatarView(discord.ui.View):
    def __init__(self, member: discord.Member, author_id: int) -> None:
        super().__init__()
        self.member = member
        self.author_id = author_id
        self.labels = ('View Server Avatar', 'View Global Avatar')
        assert member.guild_avatar is not None
        self.avatars: Sequence[discord.Asset] = (member.avatar or member.default_avatar, member.guild_avatar)
        self.index = 1
        self.embed: discord.Embed = discord.utils.MISSING

    @discord.ui.button(label='View Global Avatar', style=discord.ButtonStyle.blurple)
    async def button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index ^= 1
        self.button.label = self.labels[self.index]
        avatar = self.avatars[self.index].with_static_format('png')
        self.embed.set_author(name=self.member, url=avatar)
        self.embed.set_image(url=avatar)
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id in (self.author_id, interaction.client.owner_id):  # type: ignore # pain
            return True
        else:
            await interaction.response.send_message('This view cannot be controlled by you.', ephemeral=True)
            return False
