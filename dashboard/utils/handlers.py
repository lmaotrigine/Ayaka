from __future__ import annotations

import abc
import binascii
import os
from typing import TYPE_CHECKING

import discord
import orjson
import tornado.web

import config
from .http import Route
from ..guild import Guild
from ..token import Token
from ..user import User

if TYPE_CHECKING:
    from bot import Ayaka


__all__ = ('HTTPHandler',)


class HTTPHandler(tornado.web.RequestHandler, abc.ABC):
    
    def initialize(self, bot: Ayaka) -> None:
        self.bot: Ayaka = bot
        
    def get_identifier(self) -> str:
        if _identifier := self.get_secure_cookie('identifier'):
            return _identifier.decode('utf-8')
        identifier: str = binascii.hexlify(os.urandom(32)).decode('utf-8')
        self.set_secure_cookie('identifier', identifier)
        return identifier
    
    async def get_token(self) -> Token | None:
        identifier = self.get_identifier()
        token_data: str | None = await self.bot.redis.hget('tokens', identifier)
        if not token_data:
            return None
        token = Token(orjson.loads(token_data))
        if token.expired:
            data = {
                'client_secret': config.client_secret,
                'client_id': config.application_id,
                'redirect_uri': config.base_url + '/discord/login',
                'refresh_token': token.refresh_token,
                'grant_type': 'refresh_token',
            }
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            async with self.bot.session.post('https://discord.com/api/oauth2/token', data=data, headers=headers) as resp:
                if 200 < resp.status > 206:
                    raise discord.HTTPException(resp, await resp.text())
                data = orjson.loads(await resp.text())
            if data.get('error'):
                raise discord.HTTPException(resp, orjson.dumps(data).decode('utf-8'))
            token = Token(data)
            await self.bot.redis.hset('tokens', identifier, token.json)
        return token
    
    async def fetch_user(self) -> User | None:
        if not (token := await self.get_token()):
            return None
        
        data = await self.bot.dashboard_client.request(Route('GET', '/users/@me', token=token.access_token))
        user = User(data)
        identifier = self.get_identifier()
        await self.bot.redis.hset('users', identifier, user.json)
        return user
    
    async def get_user(self) -> User | None:
        identifier = self.get_identifier()
        data: str | None = await self.bot.redis.hget('users', identifier)
        if data:
            user = User(orjson.loads(data))
            if user.expired:
                user = await self.fetch_user()
        else:
            user = await self.fetch_user()
        return user
    
    async def fetch_guilds(self) -> list[Guild] | None:
        if not (token := await self.get_token()):
            return None
        if not (user := await self.get_user()):
            return
        data = await self.bot.dashboard_client.request(Route('GET', '/users/@me/guilds', token=token.access_token))
        guilds = [Guild(guild) for guild in data]
        await self.bot.redis.hset('guilds', str(user.id), orjson.dumps([guild.json for guild in guilds]).decode('utf-8'))
        return guilds
    
    async def get_guilds(self) -> list[Guild] | None:
        if not (user := await self.get_user()):
            return
        
        data: str | None = await self.bot.redis.hget('guilds', str(user.id))
        
        if data:
            guilds = [Guild(orjson.loads(guild)) for guild in orjson.loads(data)]
            if any(guild.expired for guild in guilds):
                guilds = await self.fetch_guilds()
        else:
            guilds = await self.fetch_guilds()
        return guilds
    
    async def get_related_guilds(self) -> dict[str, list[Guild]]:
        user_guilds = await self.get_guilds() or []
        bot_guild_ids = [guild.id for guild in self.bot.guilds]
        return {
            'shared_guilds': [guild for guild in user_guilds if guild.id in bot_guild_ids],
            'non_shared_guilds': [guild for guild in user_guilds if guild.id not in bot_guild_ids]
        }
