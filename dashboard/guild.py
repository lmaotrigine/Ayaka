"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import time
from typing import TypedDict

import discord
import orjson
from typing_extensions import NotRequired


__all__ = ('GuildData', 'Guild')


class GuildData(TypedDict):
    id: int
    name: str
    icon: str | None
    owner: bool
    permissions: int
    features: list[str]
    
    fetch_time: NotRequired[float]


class Guild:
    
    def __init__(self, data: GuildData) -> None:
        self.data: GuildData = data
        
        self.id: int = int(data['id'])
        self.name: str = data['name']
        self._icon: str | None = data['icon']
        self.owner: bool = data['owner']
        self.permissions: discord.Permissions = discord.Permissions(int(data['permissions']))
        self.features: list[str] = data['features']
        
    @property
    def icon(self) -> str | None:
        if not (icon := self._icon):
            return None
        _format = 'gif' if icon.startswith('a_') else 'png'
        return f'https://cdn.discordapp.com/icons/{self.id}/{icon}.{_format}?size=4096'
    
    @property
    def fetch_time(self) -> float:
        return self.data.get('fetch_time') or time.time()
    
    @property
    def expired(self) -> bool:
        return (time.time() - self.fetch_time) > 20
    
    @property
    def json(self) -> str:
        data = self.data.copy()
        data['fetch_time'] = self.fetch_time
        return orjson.dumps(data).decode('utf-8')
