"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from collections.abc import Iterable
from typing import Any, Callable, TypeVar

from discord import app_commands
from discord.ext import commands

from utils.context import Context, GuildContext


T = TypeVar('T')


async def check_permissions(
    ctx: GuildContext, perms: dict[str, bool], *, check: Callable[[Iterable[Any]], bool] = all
) -> bool:
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True
    resolved = ctx.channel.permissions_for(ctx.author)
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_permissions(*, check: Callable[[Iterable[Any]], bool] = all, **perms: bool) -> Callable[[T], T]:
    async def pred(ctx) -> bool:
        return await check_permissions(ctx, perms, check=check)

    return commands.check(pred)


async def check_guild_permissions(
    ctx: GuildContext, perms: dict[str, bool], *, check: Callable[[Iterable[Any]], bool] = all
) -> bool:
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    if ctx.guild is None:
        return False

    resolved = ctx.author.guild_permissions
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_guild_permissions(*, check: Callable[[Iterable[Any]], bool] = all, **perms: bool) -> Callable[[T], T]:
    async def pred(ctx) -> bool:
        return await check_guild_permissions(ctx, perms, check=check)

    return commands.check(pred)


# These do not take channel overrides into account


def hybrid_permissions_check(**perms: bool) -> Callable[[T], T]:
    async def pred(ctx: GuildContext):
        return await check_guild_permissions(ctx, perms)

    def decorator(func: T) -> T:
        commands.check(pred)(func)
        app_commands.default_permissions(**perms)(func)
        return func

    return decorator


def is_manager() -> Callable[[T], T]:
    return hybrid_permissions_check(manage_guild=True)


def is_mod() -> Callable[[T], T]:
    return hybrid_permissions_check(ban_members=True, manage_messages=True)


def is_admin() -> Callable[[T], T]:
    return hybrid_permissions_check(administrator=True)


def mod_or_permissions(**perms: bool) -> Callable[[T], T]:
    perms['ban_members'] = True
    perms['manage_messages'] = True

    async def pred(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, perms, check=any)

    return commands.check(pred)


def admin_or_permissions(**perms: bool) -> Callable[[T], T]:
    perms['administrator'] = True

    async def pred(ctx: GuildContext) -> bool:
        return await check_guild_permissions(ctx, perms, check=any)

    return commands.check(pred)


def is_in_guilds(*guild_ids: int) -> Callable[[T], T]:
    def pred(ctx: Context) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        return guild.id in guild_ids

    return commands.check(pred)


def can_use_spoiler() -> Callable[[T], T]:
    def pred(ctx: Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        my_permissions = ctx.channel.permissions_for(ctx.guild.me)
        if not (my_permissions.read_message_history and my_permissions.manage_messages and my_permissions.add_reactions):
            raise commands.BadArgument(
                'Need Read Message History, Add Reactions, and Manage Messages permissions to use this. Sorry if I spoiled you.'
            )
        return True

    return commands.check(pred)
