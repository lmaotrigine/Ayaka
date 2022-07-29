"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Callable, Generator, Iterable, TypeVar

import asyncpg
import discord
from discord.ext import commands

from .ui import ConfirmationView, DisambiguationView


if TYPE_CHECKING:
    from aiohttp import ClientSession

    from bot import Ayaka


__all__ = ('Context',)

T = TypeVar('T')


class _ContextDBAcquire:
    __slots__ = ('ctx', 'timeout')

    def __init__(self, ctx: Context, timeout: float | None) -> None:
        self.ctx = ctx
        self.timeout = timeout

    def __await__(self) -> Generator[Any, None, asyncpg.Connection]:
        return self.ctx._acquire(timeout=self.timeout).__await__()

    async def __aenter__(self) -> asyncpg.Pool | asyncpg.Connection:
        await self.ctx._acquire(timeout=self.timeout)
        return self.ctx.db

    async def __aexit__(self, *_) -> None:
        await self.ctx.release()


class Context(commands.Context['Ayaka']):
    _db: asyncpg.Connection | None
    prefix: str
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | discord.DMChannel
    command: commands.Command[Any, ..., Any]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pool = self.bot.pool
        self._db = None

    async def entry_to_code(self, entries: Iterable[tuple[str, str]]) -> None:
        width = max(len(a) for a, _ in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'{name:<{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    async def indented_entry_to_code(self, entries: Iterable[tuple[str, str]]) -> None:
        width = max(len(a) for a, _ in entries)
        output = ['```']
        for name, entry in entries:
            output.append(f'\u200b{name:<{width}}: {entry}')
        output.append('```')
        await self.send('\n'.join(output))

    def __repr__(self) -> str:
        # we need this for out cache key strategy
        return '<Context>'

    @property
    def session(self) -> ClientSession:
        return self.bot.session

    @discord.utils.cached_property
    def replied_reference(self) -> discord.MessageReference | None:
        ref = self.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            return ref.resolved.to_reference()
        return None

    async def disambiguate(self, matches: list[T], entry: Callable[[T], Any]) -> T:
        if len(matches) == 0:
            raise ValueError('No results found.')

        if len(matches) == 1:
            return matches[0]

        matches_ = {i: (m, entry(m)) for i, m in enumerate(matches)}
        view = DisambiguationView(matches_, self.author.id, self)

        await self.release()

        view.message = await self.send('There are too many matches... Which one did you mean?', view=view)
        try:
            if await view.wait():
                raise ValueError('Took too long, goodbye.')
            else:
                assert view.value is not None
                return view.value
        finally:
            await self.release()

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        reacquire: bool = True,
        author_id: int | None = None,
    ) -> bool | None:
        author_id = author_id or self.author.id
        view = ConfirmationView(
            timeout=timeout, author_id=author_id, reacquire=reacquire, ctx=self, delete_after=delete_after
        )
        view.message = await self.send(message, view=view)
        await view.wait()
        return view.value

    def tick(self, opt: bool | None, label: str | None = None) -> str:
        lookup = self.bot.emoji
        emoji = lookup.get(opt, '❌')
        if label is not None:
            return f'{emoji}: {label}'
        return emoji

    @property
    def db(self) -> asyncpg.Connection | asyncpg.Pool:
        return self._db if self._db else self.pool

    async def _acquire(self, timeout: float | None) -> asyncpg.Connection:
        if self._db is None:
            self._db = await self.pool.acquire(timeout=timeout)
        return self._db

    def acquire(self) -> _ContextDBAcquire:
        return _ContextDBAcquire(self, timeout=None)

    async def release(self) -> None:
        # from source digging asyncpg, releasing an already
        # release connection does nothing

        if self._db is not None:
            await self.bot.pool.release(self._db)
            self._db = None

    async def show_help(self, command: Any = None) -> None:
        cmd = self.bot.get_command('help')
        command = command or self.command.qualified_name
        await self.invoke(cmd, command=command)  # type: ignore

    async def safe_send(self, content: str, *, escape_mentions: bool = True, **kwargs) -> discord.Message:
        if escape_mentions:
            content = discord.utils.escape_mentions(content)

        if len(content) > 2000:
            fp = io.BytesIO(content.encode())
            kwargs.pop('file', None)
            return await self.send(file=discord.File(fp, filename='message_too_long.txt'), **kwargs)
        else:
            return await self.send(content)


class GuildContext(Context):
    author: discord.Member
    guild: discord.Guild
    channel: discord.VoiceChannel | discord.TextChannel | discord.Thread
    me: discord.Member
    prefix: str
