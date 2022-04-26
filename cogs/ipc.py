"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, Union

import discord
from discord import User
from discord.ext import commands

import ipc


if TYPE_CHECKING:
    from bot import Ayaka
    from ipc.server import IpcPayload
    from utils.context import Context


class IPC(commands.Cog, command_attrs=dict(hidden=True)):
    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.redis = self.bot.redis

    async def cog_check(self, ctx: Context) -> bool:
        return await commands.is_owner().predicate(ctx)

    @discord.utils.cached_property
    def webhook(self):
        return discord.Webhook.from_url(self.bot.config.stat_webhook, session=self.bot.session)

    @ipc.route()
    async def join(self, data: IpcPayload) -> dict[str, str]:
        leave = False
        if data.get('user_id') == self.bot.owner_id:
            await self.webhook.send('invited by me')
            return {'status': 'success'}

        if (guild_id := data.get('guild_id')) is None:
            # discord fucked up
            await self.webhook.send('discord fucked up')
            {'status': 'success'}

        if (user_id := data.get('user_id')) is None:
            # oauth was bypassed. we blacklist this guild.
            await self.webhook.send(f'No user_id in join payload. Guild ID: {guild_id}')
            leave = True

        valid = data.get('valid')
        if valid:
            user = f'{data.get("username")}#{data.get("discriminator")}'
            await self.webhook.send(f'{user} ({user_id}) used a token to join.')
            return {'status': 'success'}

        for _ in range(5):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await asyncio.sleep(0.4)
            else:
                break
        else:
            return {}  # unreachable

        if leave:
            await guild.leave()
            return {'status': 'fail'}

        # user_id will not be None from here on.

        if await self.redis.get(f'guild:{guild.id}') is not None:
            await self.webhook.send(f'{guild.name} (ID: {guild.id}) is in the whitelist.')
            return {'status': 'success'}

        if not guild.chunked:
            await guild.chunk()
        if guild.member_count is None:
            await self.webhook.send('guild.member_count is None')
            # thanks discord very cool
            return {'status': 'success'}

        assert user_id is not None
        user = f'{data.get("username")}#{data.get("discriminator")}'
        await self.webhook.send(f'Added to {guild.name} (ID: {guild.id}) by {user} ({user_id}) without a token.')

        assert self.bot.owner_id is not None
        if await self.bot.get_or_fetch_member(guild, self.bot.owner_id) is not None:
            await self.webhook.send(f'Found VJ in {guild.name} (ID: {guild.id}).')
            return {'status': 'success'}

        count = Counter(m.bot for m in guild.members)
        if count[True] > count[False] or count[False] < 10:
            await self.webhook.send(
                f'Leaving {guild.name} (ID: {guild.id}) because it has {count[True]} bots and {count[False]} humans.'
            )
            await guild.leave()
            return {'status': 'fail'}
        return {'status': 'success'}

    async def cog_unload(self) -> None:
        await self.redis.close()

    @commands.group()
    async def whitelist(self, ctx):
        """Whitelist users/guilds from adding the bot if they don't meet requirements."""

    @whitelist.command(name='user')
    async def user_whitelist(self, ctx: Context, user: User):
        """Whitelist a user."""
        ret = await self.redis.incrby(f'user:{user.id}', 1)
        await ctx.send(f'{user}: {ret}')

    @whitelist.command(name='guild')
    async def guild_whitelist(self, ctx: Context, guild_id: int):
        """Whitelist a guild."""
        ret = await self.redis.set(f'guild:{guild_id}', 1)
        await ctx.send(f'{guild_id}: {ret}')

    @whitelist.command(name='remove', aliases=['delete'])
    async def delete_whitelist(self, ctx: Context, obj: Union[User, int]):
        """Remove an entity from the whitelist."""
        if isinstance(obj, User):
            ret = await self.redis.incrby(f'user:{obj.id}', -1)
            if ret <= 0:
                ret = await self.redis.delete(f'user:{obj.id}')
            await ctx.send(f'User {obj}: {ret}')
        else:
            ret = await self.redis.delete(f'guild:{obj}')
            await ctx.send(f'Guild {obj}: {ret}')


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(IPC(bot))
