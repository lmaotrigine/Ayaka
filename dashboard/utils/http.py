from __future__ import annotations

import asyncio
import datetime
import weakref
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar
from urllib.parse import quote

import aiohttp
import discord
import orjson


if TYPE_CHECKING:
    from types import TracebackType
    from typing_extensions import Self
    from bot import Ayaka
    
    BE = TypeVar('BE', bound=BaseException)


__all__ = ('Route', 'HTTPClient')


class Route:
    
    BASE: ClassVar[str] = 'https://discord.com/api/v10'
    
    def __init__(self, method: str, path: str, token: str, **parameters: Any) -> None:
        self.method: str = method
        self.path: str = path
        self.token: str = token
        url = self.BASE + self.path
        if parameters:
            url = url.format_map({k: quote(v) if isinstance(v, str) else v for k, v in parameters.items()})
        self.url: str = url
        self.channel_id: str | int | None = parameters.get('channel_id')
        self.guild_id: str | int | None = parameters.get('guild_id')
        self.webhook_id: str | int | None = parameters.get('webhook_id')
        self.webhook_token: str | None = parameters.get('webhook_token')
    
    @property
    def bucket(self) -> str:
        return f'{self.channel_id}:{self.guild_id}:{self.path}'


class MaybeUnlock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock: asyncio.Lock = lock
        self._unlock: bool = True
    
    def __enter__(self) -> Self:
        return self
    
    def defer(self) -> None:
        self._unlock = False
        
    def __exit__(self, exc_type: type[BE] | None, exc: BE | None, traceback: TracebackType) -> None:
        if self._unlock:
            self.lock.release()


async def json_or_text(response: aiohttp.ClientResponse) -> dict[str, Any] | str:
    text = await response.text(encoding='utf-8')
    try:
        if response.headers['content-type'] == 'application/json':
            return orjson.loads(text)
    except KeyError:
        # thanks Cloudflare
        pass
    return text


def _parse_ratelimit_header(request: Any, *, use_clock: bool = False) -> float:
    reset_after: str | None = request.headers.get('X-Ratelimit-Reset-After')
    if use_clock or not reset_after:
        utc = datetime.timezone.utc
        now = datetime.datetime.now(utc)
        reset = datetime.datetime.fromtimestamp(float(request.headers['X-Ratelimit-Reset']), utc)
        return (reset - now).total_seconds()
    else:
        return float(reset_after)


class HTTPClient:
    
    def __init__(self, bot: Ayaka) -> None:
        self.bot: Ayaka = bot
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._global_over: asyncio.Event = asyncio.Event()
        self._global_over.set()
    
    async def request(self, route: Route, **kwargs: Any) -> Any:
        bucket = route.bucket
        
        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            if bucket is not None:
                self._locks[bucket] = lock
        
        kwargs['headers'] = {'Authorization': f'Bearer {route.token}'}
        
        if not self._global_over.is_set():
            await self._global_over.wait()
        
        resp: aiohttp.ClientResponse | None = None
        data: dict[str, Any] | str | None = None
        
        await lock.acquire()
        
        with MaybeUnlock(lock) as maybe_lock:
            for tries in range(5):
                try:
                    async with self.bot.session.request(route.method, route.url, **kwargs) as resp:
                        data = await json_or_text(resp)
                        if resp.headers.get('X-Ratelimit-Remaining') == '0' and resp.status != 429:
                            maybe_lock.defer()
                            self.bot.loop.call_later(_parse_ratelimit_header(resp, use_clock=False), lock.release)
                        if 300 > resp.status >= 200:
                            return data
                        if resp.status == 429:
                            if not resp.headers.get('Via') or isinstance(data, str):
                                raise discord.HTTPException(resp, data)
                            retry_after: float = data['retry_after']
                            if is_global := data.get('global', False):
                                self._global_over.clear()
                            await asyncio.sleep(retry_after)
                            if is_global:
                                self._global_over.set()
                            continue
                        if resp.status in {500, 502, 504}:
                            await asyncio.sleep(1 + tries * 2)
                            continue
                        if resp.status == 403:
                            raise discord.Forbidden(resp, data)
                        elif resp.status == 404:
                            raise discord.NotFound(resp, data)
                        elif resp.status >= 500:
                            raise discord.DiscordServerError(resp, data)
                        else:
                            raise discord.HTTPException(resp, data)
                except OSError as e:
                    if tries < 4 and e.errno in (54, 10054):
                        await asyncio.sleep(1 + tries * 2)
                        continue
                    raise
            if resp is not None:
                if resp.status >= 500:
                    raise discord.DiscordServerError(resp, data)
                raise discord.HTTPException(resp, data)
            raise RuntimeError('Unreachable code in HTTP handling')
