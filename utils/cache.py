"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import time
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from functools import wraps
from typing import Any, Protocol, TypeVar

from lru import LRU


R = TypeVar('R')

# Can't use ParamSpec due to https://github.com/python/typing/discussions/946
class CacheProtocol(Protocol[R]):
    cache: MutableMapping[str, R]

    def __call__(self, *args: Any, **kwds: Any) -> R:
        ...

    def get_key(self, *args: Any, **kwargs: Any) -> str:
        ...

    def invalidate(self, *args: Any, **kwargs: Any) -> bool:
        ...

    def invalidate_containing(self, key: str) -> None:
        ...

    def get_stats(self) -> tuple[int, int]:
        ...


def _wrap_and_store_coroutine(cache: MutableMapping[str, R], key: str, coro: Awaitable[R]) -> Coroutine[Any, Any, R]:
    async def func() -> R:
        value = await coro
        cache[key] = value
        return value

    return func()


def _wrap_new_coroutine(value: R) -> Coroutine[Any, Any, R]:
    async def new_coroutine() -> R:
        return value

    return new_coroutine()


class ExpiringCache(dict[str, tuple[R, float]]):
    def __init__(self, seconds: float) -> None:
        self.__ttl = seconds
        super().__init__()

    def __verify_cache_integrity(self) -> None:
        # have to do this in two steps...
        current_time = time.monotonic()
        to_remove = [k for k, (_, t) in self.items() if current_time > (t + self.__ttl)]
        for k in to_remove:
            del self[k]

    def __contains__(self, key: str) -> bool:
        self.__verify_cache_integrity()
        return super().__contains__(key)

    def __getitem__(self, key: str) -> tuple[R, float]:
        self.__verify_cache_integrity()
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: R) -> None:
        super().__setitem__(key, (value, time.monotonic()))


class Strategy(enum.Enum):
    lru = 1
    raw = 2
    timed = 3


def cache(
    maxsize: int = 128, strategy: Strategy = Strategy.lru, ignore_kwargs: bool = False
) -> Callable[[Callable[..., R]], CacheProtocol[R]]:
    def decorator(func: Callable[..., R]) -> CacheProtocol[R]:
        if strategy is Strategy.lru:
            _internal_cache = LRU(maxsize)
            _stats = _internal_cache.get_stats
        elif strategy is Strategy.raw:
            _internal_cache = {}
            _stats = lambda: (0, 0)
        elif strategy is Strategy.timed:
            _internal_cache = ExpiringCache(maxsize)
            _stats = lambda: (0, 0)

        def _make_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            # this is a bit of a clusterfuck
            # we do not care what 'self' parameter is when we __repr__ it
            def _true_repr(o):
                if o.__class__.__repr__ is object.__repr__:
                    return f'<{o.__class__.__module__}.{o.__class__.__name__}>'
                return repr(o)

            key = [f'{func.__module__}.{func.__name__}']
            key.extend(_true_repr(a) for a in args)
            if not ignore_kwargs:
                for k, v in kwargs.items():
                    # note: this only really works for this use case in particular
                    # I want to pass asyncpg.Connection objects to the parameters
                    # however, they use default __repr__ and I do not care what
                    # connection is passed in so I needed a bypass.
                    if k == 'connection':
                        continue
                    key.append(_true_repr(k))
                    key.append(_true_repr(v))
            return ':'.join(key)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> R | Coroutine[Any, Any, R | tuple[R, float]]:
            key = _make_key(args, kwargs)
            try:
                value = _internal_cache[key]
            except KeyError:
                value = func(*args, **kwargs)
                if inspect.isawaitable(value):
                    return _wrap_and_store_coroutine(_internal_cache, key, value)
                _internal_cache[key] = value
                return value
            else:
                if asyncio.iscoroutinefunction(func):
                    return _wrap_new_coroutine(value)
                return value

        def _invalidate(*args: Any, **kwargs: Any) -> bool:
            try:
                del _internal_cache[_make_key(args, kwargs)]
            except KeyError:
                return False
            else:
                return True

        def _invalidate_containing(key: str) -> None:
            to_remove = []
            for k in _internal_cache.keys():
                if key in k:
                    to_remove.append(k)
            for k in to_remove:
                try:
                    del _internal_cache[key]
                except KeyError:
                    continue

        wrapper.cache = _internal_cache
        wrapper.get_key = lambda *args, **kwargs: _make_key(args, kwargs)
        wrapper.invalidate = _invalidate
        wrapper.get_stats = _stats
        wrapper.invalidate_containing = _invalidate_containing
        return wrapper  # type: ignore # can't be done

    return decorator
