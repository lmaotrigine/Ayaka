"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import asyncio
import json
import os
import pathlib
import uuid
from typing import Any, Callable, Generic, TypeVar, overload


_T = TypeVar('_T')

ObjectHook = Callable[[dict[str, Any]], Any]


class Config(Generic[_T]):
    def __init__(
        self,
        name: pathlib.Path,
        *,
        object_hook: ObjectHook | None = None,
        encoder: type[json.JSONEncoder] | None = None,
        load_later: bool = False,
    ):
        self.name = name
        self.object_hook = object_hook
        self.encoder = encoder
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()
        self._db: dict[str, _T | Any] = {}
        if load_later:
            self.loop.create_task(self.load())
        else:
            self.load_from_file()

    def load_from_file(self):
        try:
            with open(self.name, 'r') as f:
                self._db = json.load(f, object_hook=self.object_hook)
        except FileNotFoundError:
            self._db = {}

    async def load(self):
        async with self.lock:
            await self.loop.run_in_executor(None, self.load_from_file)

    def _dump(self):
        temp = self.name.with_stem(f'{uuid.uuid4()}-{self.name.stem}').with_suffix('.tmp')
        with open(temp, 'w', encoding='utf-8') as tmp:
            json.dump(self._db.copy(), tmp, ensure_ascii=True, cls=self.encoder, separators=(',', ':'))

        # atomically move the file
        os.replace(temp, self.name)

    async def save(self) -> None:
        async with self.lock:
            await self.loop.run_in_executor(None, self._dump)

    @overload
    def get(self, key: Any) -> _T | Any | None:
        ...

    @overload
    def get(self, key: Any, default: Any) -> _T | Any:
        ...

    def get(self, key: Any, default: Any = None) -> _T | Any | None:
        return self._db.get(str(key), default)

    async def put(self, key: Any, value: _T | Any) -> None:
        self._db[str(key)] = value
        await self.save()

    async def remove(self, key: Any) -> None:
        del self._db[str(key)]
        await self.save()

    def __contains__(self, item: Any) -> bool:
        return str(item) in self._db

    def __getitem__(self, item: Any) -> _T | Any:
        return self._db[str(item)]

    def __len__(self) -> int:
        return len(self._db)

    def all(self) -> dict[str, _T | Any]:
        return self._db
