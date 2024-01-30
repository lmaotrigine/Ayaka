"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Callable, Coroutine, Iterator, MutableMapping
from functools import wraps
from typing import Any, Protocol, TypeVar

from lru import LRU


R = TypeVar('R')


# Can't use ParamSpec due to https://github.com/python/typing/discussions/946
class CacheProtocol(Protocol[R]):
    cache: MutableMapping[str, asyncio.Task[R]]

    def __call__(self, *args: Any, **kwds: Any) -> asyncio.Task[R]: ...

    def get_key(self, *args: Any, **kwargs: Any) -> str: ...

    def invalidate(self, *args: Any, **kwargs: Any) -> bool: ...

    def invalidate_containing(self, key: str) -> None: ...

    def get_stats(self) -> tuple[int, int]: ...


class ExpiringCache(dict[str, tuple[R, float]]):
    def __init__(self, seconds: float) -> None:
        self.__ttl = seconds
        super().__init__()

    def __verify_cache_integrity(self) -> None:
        # have to do this in two steps...
        current_time = time.monotonic()
        to_remove = [k for k, (_, t) in super().items() if current_time > (t + self.__ttl)]
        for k in to_remove:
            del self[k]

    def __contains__(self, key: str) -> bool:
        self.__verify_cache_integrity()
        return super().__contains__(key)

    def __getitem__(self, key: str) -> R:
        self.__verify_cache_integrity()
        tup = super().__getitem__(key)
        return tup[0]

    def get(self, key: str, default: Any = None):
        v = super().get(key, default)
        if v is default:
            return default
        return v[0]

    def __setitem__(self, key: str, value: R) -> None:
        super().__setitem__(key, (value, time.monotonic()))

    def values(self) -> Iterator[R]:
        return map(lambda x: x[0], super().values())

    def items(self) -> Iterator[tuple[str, R]]:
        return map(lambda x: (x[0], x[1][0]), super().items())


class Strategy(enum.Enum):
    lru = 1
    raw = 2
    timed = 3


def cache(
    maxsize: int = 128, strategy: Strategy = Strategy.lru, ignore_kwargs: bool = False
) -> Callable[[Callable[..., Coroutine[Any, Any, R]]], CacheProtocol[R]]:
    def decorator(func: Callable[..., Coroutine[Any, Any, R]]) -> CacheProtocol[R]:
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
                    if k == 'connection' or k == 'pool':
                        continue
                    key.append(_true_repr(k))
                    key.append(_true_repr(v))
            return ':'.join(key)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> asyncio.Task[R]:
            key = _make_key(args, kwargs)
            try:
                task = _internal_cache[key]
            except KeyError:
                _internal_cache[key] = task = asyncio.create_task(func(*args, **kwargs))
                return task
            else:
                return task

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

        wrapper.cache = _internal_cache  # type: ignore # can't be done
        wrapper.get_key = lambda *args, **kwargs: _make_key(args, kwargs)  # type: ignore # can't be done
        wrapper.invalidate = _invalidate  # type: ignore # can't be done
        wrapper.get_stats = _stats  # type: ignore # can't be done
        wrapper.invalidate_containing = _invalidate_containing  # type: ignore # can't be done
        return wrapper  # type: ignore # can't be done

    return decorator
