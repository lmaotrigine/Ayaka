"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import abc
import asyncio
import binascii
import os
from urllib.parse import urlencode

import discord
import orjson

import config
from ..utils.handlers import HTTPHandler, Token


__all__ = ('DiscordLogin', 'DiscordLogout', 'DiscordIndex', 'DiscordInviteBot', 'DiscordAvatarHistory', 'DiscordAvatarHistoryUser')


class DiscordIndex(HTTPHandler, abc.ABC):
    
    async def get(self) -> None:
        return self.redirect('https://discord.gg/s44CFagYN2')


class DiscordLogin(HTTPHandler, abc.ABC):
    
    async def get(self) -> None:
        identifier = self.get_identifier()
        user_state = self.get_secure_cookie("state")
        auth_state = self.get_query_argument("state", None)
        code = self.get_query_argument("code", None)
        auth_token = self.request.headers.get('Authorization', None) or self.get_secure_cookie('auth_token')

        if not code or not auth_state or not user_state:

            state = binascii.hexlify(os.urandom(16)).decode()

            self.set_secure_cookie(
                "state",
                state
            )

            return self.redirect(
                f"https://discord.com/api/oauth2/authorize?"
                f"client_id={config.application_id}&"
                f"response_type=code&"
                f"scope=identify%20guilds&"
                f"redirect_uri={config.base_url + '/discord/login'}&"
                f"state={state}"
            )

        if auth_state != user_state.decode():
            self.set_status(400)
            return await self.finish({"error": "user state and server state must match."})

        self.clear_cookie("state")
        
        guild_id = self.get_query_argument('guild_id', None)
        if guild_id is not None:
            user = await self.get_user()
            if user is None or user.id != self.bot.owner.id:
                guild_id = int(guild_id)
                query = 'SELECT token FROM auth_tokens WHERE guild_id = $1;'
                res = await self.bot.pool.fetchval(query, guild_id)
                if not res:
                    return self.redirect('/not_gonna_happen')
        async with self.bot.session.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_secret": config.client_secret,
                    "client_id":     config.application_id,
                    "redirect_uri":  config.base_url + '/discord/login',
                    "code":          code,
                    "grant_type":    "authorization_code",
                    "scope":         "identify guilds connections",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded"
                }
        ) as response:
            if 200 < response.status > 206:
                raise discord.HTTPException(response, orjson.dumps(await response.json()).decode('utf-8'))

            data = await response.json()
            print('moew', data)

        if data.get("error"):
            raise discord.HTTPException(response, orjson.dumps(data).decode('utf-8'))

        token_response = Token(data)
        await self.bot.redis.hset("tokens", identifier, token_response.json)
        
        if 'bot' in token_response.scope:
            if guild_id is None:
                    # thanks discord
                    return
            if auth_token is not None:
                query = 'DELETE FROM auth_tokens WHERE token = $1;'
                res = await self.bot.pool.execute(query, auth_token)
            else:
                user = await self.get_user()
                if not user:
                    await self.leave_guild(int(guild_id))
                    return
                if user.id == self.bot.owner.id:
                    return
                query = 'DELETE FROM auth_tokens WHERE token in (SELECT token FROM auth_tokens WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC LIMIT 1);'
                res = await self.bot.pool.execute(query, user.id, guild_id)
            if res == 'DELETE 0':
                await self.leave_guild(int(guild_id))
        print('hi')
        return self.redirect('/')
    
    async def leave_guild(self, guild_id: int) -> None:
        for _ in range(10):
            if guild := self.bot.get_guild(guild_id):
                if guild.get_member(self.bot.owner.id):
                    return
                members = len(guild.members)
                bots = sum(m.bot for m in guild.members)
                humans = members - bots
                if humans > 10 and humans > bots:
                    return
                return await guild.leave()
            await asyncio.sleep(0.2)


class DiscordLogout(HTTPHandler, abc.ABC):
    
    async def get(self) -> None:
        await self.bot.redis.hdel('tokens', self.get_identifier())
        self.clear_cookie('identifier')
        return self.redirect('/')
    

class DiscordInviteBot(HTTPHandler, abc.ABC):
    
    async def get(self) -> None:
        user = await self.get_user()
        auth_token = self.request.headers.get('Authorization', None)
        if user is None and auth_token is None:
            return self.redirect('/discord/login')
        if user is not None:
            if user.id != self.bot.owner.id:
                query = 'SELECT token FROM auth_tokens WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1);'
                res = await self.bot.pool.fetchval(query, user.id)
                if res:
                    self.set_secure_cookie('auth_token', res)
            else:
                res = True
        else:
            res = auth_token
        if not res:
            return self.redirect('/not_gonna_happen')
        state = binascii.hexlify(os.urandom(32)).decode('utf-8')
        self.set_secure_cookie('state', state)
        params = {
            'client_id': config.application_id,
            'client_secret': config.client_secret,
            'response_type': 'code',
            'scope': 'identify guilds connections bot applications.commands',
            'redirect_uri': config.base_url + '/discord/login',
            'state': state,
        }
        return self.redirect(f'https://discord.com/api/oauth2/authorize?{urlencode(params)}')


class DiscordAvatarHistory(HTTPHandler, abc.ABC):
    async def get(self) -> None:
        user = await self.get_user()
        if not user:
            return self.redirect('/discord/login')
        avys = await self.bot.get_user_avatars(user.id)
        if avys is None:
            self.set_status(500)
            return await self.finish({'error': 'this functionality is unavailable due to an internal error.'})
        if not avys['avatars']:
            self.set_status(404)
            return await self.finish('No avatar history recorded.')
        await self.render('avatarhistory.html', **avys)


class DiscordAvatarHistoryUser(HTTPHandler, abc.ABC):
    async def get(self, user_id: str) -> None:
        try:
            _user_id = int(user_id)
        except ValueError:
            self.set_status(400)
            await self.finish('Invalid discord ID provided or no avatar history recorded.')
            return
        avys = await self.bot.get_user_avatars(_user_id)
        if avys is None:
            self.set_status(500)
            return await self.finish({'error': 'this functionality is unavailable due to an internal error.'})
        if not avys['avatars']:
            self.set_status(404)
            return await self.finish('Invalid discord ID provided or no avatar history recorded.')
        await self.render('avatarhistory.html', **avys)
