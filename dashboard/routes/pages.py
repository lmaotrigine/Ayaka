from __future__ import annotations

import abc
from typing import Any

import discord

from ..utils.handlers import HTTPHandler
from ..guild import Guild


class BasePage(HTTPHandler, abc.ABC):
    async def get_page_render_info(self, guild: Guild | discord.Guild | None = None) -> dict[str, Any]:
        user = await self.get_user()
        related_guilds = await self.get_related_guilds() if user is not None else {}
        return {'bot': self.bot, 'user': user, 'guild': guild, **related_guilds}


class Index(BasePage, abc.ABC):
    async def get(self) -> None:
        data = await self.get_page_render_info()
        total = await self.bot.redis.incrby('hits', 1)
        remote = self.request.headers.get('X-Real-IP') or self.request.headers.get('X-Forwarded-For') or self.request.remote_ip
        you = await self.bot.redis.incrby(f'hits:{remote}', 1)
        self.render('index.html', total=total, you=you, **data)


class IP(BasePage, abc.ABC):
    async def get(self) -> None:
        remote = self.request.headers.get('X-Real-IP') or self.request.headers.get('X-Forwarded-For') or self.request.remote_ip
        self.set_header('Content-Type', 'text/plain; charset=utf-8')
        await self.finish(remote)


class VoiceRecognition(BasePage, abc.ABC):
    async def get(self) -> None:
        self.render('voice_recognition.html')


class NotGonnaHappen(BasePage, abc.ABC):
    async def get(self) -> None:
        self.set_header('Content-Type', 'text/plain; charset=utf-8')
        await self.finish("you don't have a token, ask VJ. tokens are one time use, so if you had one and used it you'll need to apply for another")
