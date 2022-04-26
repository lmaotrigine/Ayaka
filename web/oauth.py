"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import Any
from urllib.parse import urlencode

import aiohttp


class Oauth2:
    BASE_URL = 'https://discord.com/api/v9'

    def __init__(self, client_id: int | str, client_secret, redirect_uri: str):
        self.__session = None
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def close(self):
        if self.__session is not None:
            await self.__session.close()

    async def request(
        self,
        method: str = 'POST',
        route: str = '/',
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        if self.__session is None:
            self.__session = aiohttp.ClientSession()
        headers = (headers or {}) | {'Accept': 'application/json'}
        async with self.__session.request(
            method, f'{self.BASE_URL}{route}', headers=headers, params=params, **kwargs
        ) as response:
            return await response.json()

    def get_authorization_url(self, state: str | None = None, scope: str = 'identify') -> str:
        params = {
            'client_id': self.client_id,
            'scope': scope,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
        }
        if state is not None:
            params['state'] = state
        return f'https://discord.com/oauth2/authorize?{urlencode(params)}'

    async def get_access_token(self, code: str):
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.redirect_uri,
        }
        data = await self.request('POST', '/oauth2/token', data=data)
        return data

    async def get_identity(self, access_token: str | None = None):
        data = await self.request('GET', '/users/@me', headers={'Authorization': f'Bearer {access_token}'})
        return data | {'access_token': access_token}
