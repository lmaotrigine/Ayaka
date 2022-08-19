"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
from collections import deque
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

    def __init__(self, method: str, path: str, token: str, *, metadata: str | None = None, **parameters: Any) -> None:
        self.method: str = method
        self.path: str = path
        # Metadata is a special string used to differentiate between known sub ratelimits
        # Since these can't be handled generically, this is the next best way to do so
        self.metadata: str | None = metadata
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
    def key(self) -> str:
        if self.metadata:
            return f'{self.method} {self.path}:{self.metadata}'
        return f'{self.method} {self.path}'

    @property
    def major_parameters(self) -> str:
        return '+'.join(
            str(k) for k in (self.channel_id, self.guild_id, self.webhook_id, self.webhook_token) if k is not None
        )


class Ratelimit:

    __slots__ = (
        'limit',
        'remaining',
        'outgoing',
        'reset_after',
        'expires',
        'dirty',
        '_last_request',
        '_loop',
        '_pending_requests',
        '_sleeping',
    )

    def __init__(self) -> None:
        self.limit: int = 1
        self.remaining: int = self.limit
        self.outgoing: int = 0
        self.reset_after: float = 0.0
        self.expires: float | None = None
        self.dirty: bool = False
        self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._pending_requests: deque[asyncio.Future[Any]] = deque()
        # Only a single rate limit object should be sleeping at a time.
        # The object that is sleeping is ultimately responsible for freeing the semaphore
        # for the requests currently pending.
        self._sleeping: asyncio.Lock = asyncio.Lock()
        self._last_request: float = self._loop.time()

    def __repr__(self) -> str:
        return (
            f'<RateLimitBucket limit={self.limit} remaining={self.remaining} pending_requests={len(self._pending_requests)}>'
        )

    def reset(self) -> None:
        self.remaining = self.limit - self.outgoing
        self.expires = None
        self.reset_after = 0.0
        self.dirty = False

    def update(self, response: aiohttp.ClientResponse, *, use_clock: bool = False) -> None:
        headers = response.headers
        self.limit = int(headers.get('X-Ratelimit-Limit', 1))

        if self.dirty:
            self.remaining = min(int(headers.get('X-Ratelimit-Remaining', 0)), self.limit - self.outgoing)
        else:
            self.remaining = int(headers.get('X-Ratelimit-Remaining', 0))
            self.dirty = True

        reset_after = headers.get('X-Ratelimit-Reset-After')
        if use_clock or not reset_after:
            utc = datetime.timezone.utc
            now = datetime.datetime.now(utc)
            reset = datetime.datetime.fromtimestamp(float(headers['X-Ratelimit-Reset']), utc)
            self.reset_after = (reset - now).total_seconds()
        else:
            self.reset_after = float(reset_after)

        self.expires = self._loop.time() + self.reset_after

    def _wake_next(self) -> None:
        while self._pending_requests:
            future = self._pending_requests.popleft()
            if not future.done():
                future.set_result(None)
                break

    def _wake(self, count: int = 1) -> None:
        awaken = 0
        while self._pending_requests:
            future = self._pending_requests.popleft()
            if not future.done():
                future.set_result(None)
                awaken += 1

            if awaken >= count:
                break

    async def _refresh(self) -> None:
        async with self._sleeping:
            await asyncio.sleep(self.reset_after)
        self.reset()
        self._wake(self.remaining)

    def is_expired(self) -> bool:
        return self.expires is not None and self._loop.time() > self.expires

    def is_inactive(self) -> bool:
        delta = self._loop.time() - self._last_request
        return delta >= 300 and self.outgoing == 0 and len(self._pending_requests) == 0

    async def acquire(self) -> None:
        self._last_request = self._loop.time()
        if self.is_expired():
            self.reset()

        while self.remaining <= 0:
            future = self._loop.create_future()
            self._pending_requests.append(future)
            try:
                await future
            except:
                future.cancel()
                if self.remaining > 0 and not future.cancelled():
                    self._wake_next()
                raise

        self.remaining -= 1
        self.outgoing += 1

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(self, type: type[BE], value: BE, traceback: TracebackType) -> None:
        self.outgoing -= 1
        tokens = self.remaining - self.outgoing
        # Check whether the rate limit needs to be preemptively slept on
        # Note that this is a Lock to prevent multiple rate limit objects from sleeping at once
        if not self._sleeping.locked():
            if tokens <= 0:
                await self._refresh()
            elif self._pending_requests:
                self._wake(tokens)


async def json_or_text(response: aiohttp.ClientResponse) -> dict[str, Any] | str:
    text = await response.text(encoding='utf-8')
    try:
        if response.headers['content-type'] == 'application/json':
            return orjson.loads(text)
    except KeyError:
        # thanks Cloudflare
        pass
    return text


class HTTPClient:
    def __init__(self, bot: Ayaka) -> None:
        self.bot: Ayaka = bot
        # Route key -> bucket hash
        self._bucket_hashes: dict[str, str] = {}
        # Bucket hash + Major parameters -> Rate limit
        # or
        # Route key + Major parameters -> Rate limit
        # When the key is the latter, it is used for temporary
        # one shot requests that don't have a bucket hash
        # When this reaches 256 elements, it will try to evict based off of expiry
        self._buckets: dict[str, Ratelimit] = {}
        self._global_over: asyncio.Event = asyncio.Event()
        self._global_over.set()

    def _try_clear_expired_ratelimits(self) -> None:
        if len(self._buckets) < 256:
            return

        keys = [key for key, bucket in self._buckets.items() if bucket.is_inactive()]
        for key in keys:
            del self._buckets[key]

    def get_ratelimit(self, key: str) -> Ratelimit:
        try:
            value = self._buckets[key]
        except KeyError:
            self._buckets[key] = value = Ratelimit()
            self._try_clear_expired_ratelimits()
        return value

    async def request(self, route: Route, **kwargs: Any) -> Any:
        route_key = route.key
        bucket_hash = None
        try:
            bucket_hash = self._bucket_hashes[route_key]
        except KeyError:
            key = f'{route_key}:{route.major_parameters}'
        else:
            key = f'{bucket_hash}:{route.major_parameters}'

        ratelimit = self.get_ratelimit(key)

        kwargs['headers'] = {'Authorization': f'Bearer {route.token}'}

        if not self._global_over.is_set():
            await self._global_over.wait()

        resp: aiohttp.ClientResponse | None = None
        data: dict[str, Any] | str | None = None

        async with ratelimit:
            for tries in range(5):
                try:
                    async with self.bot.session.request(route.method, route.url, **kwargs) as resp:
                        data = await json_or_text(resp)
                        # Update and use rate limit information if the bucket header is present
                        discord_hash = resp.headers.get('X-Ratelimit-Bucket')
                        # I am unsure if X-Ratelimit-Bucket is always available
                        # However, X-Ratelimit-Remaining has been a consistent cornerstone that worked
                        has_ratelimit_headers = 'X-Ratelimit-Remaining' in resp.headers
                        if discord_hash is not None:
                            # If the hash Discord has provided is somehow different from our current hash something changed
                            if bucket_hash != discord_hash:
                                if bucket_hash is not None:
                                    # If the previous hash was an actual Discord hash then this means the
                                    # hash has changed sporadically.
                                    # This can be due to two reasons
                                    # 1. It's a sub-ratelimit which is hard to handle
                                    # 2. The ratelimit information genuinely changed
                                    # There is no good way to discern these, Discord doesn't provide a way to do so.
                                    # At best, there will be some form of logging to help catch it.
                                    # Alternating sub-ratelimits means that the requests oscillate between
                                    # different underlying rate limits -- this can lead to unexpected 429s
                                    # It is unavoidable.
                                    self._bucket_hashes[route_key] = discord_hash
                                    recalculated_key = discord_hash + route.major_parameters
                                    self._buckets[recalculated_key] = ratelimit
                                    self._buckets.pop(key, None)
                                elif route_key not in self._bucket_hashes:
                                    self._bucket_hashes[route_key] = discord_hash
                                    self._buckets[discord_hash + route.major_parameters] = ratelimit

                        if has_ratelimit_headers:
                            if resp.status != 429:
                                ratelimit.update(resp, use_clock=False)

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
