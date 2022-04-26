"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

from .errors import *


__all__ = ('Client',)

log = logging.getLogger(__name__)


class Client:
    def __init__(
        self,
        port: int,
        host: str = 'localhost',
        secret_key: Optional[str] = None,
    ):
        self.secret_key = secret_key
        self.host = host
        self.port = port
        self.session = None
        self.websocket = None

    @property
    def url(self) -> str:
        return f'ws://{self.host}:{self.port}'

    async def init_sock(self) -> aiohttp.ClientWebSocketResponse:
        log.info('Initiating WebSocket connection.')
        self.session = aiohttp.ClientSession()
        self.websocket = await self.session.ws_connect(self.url, autoping=False, autoclose=False)
        log.info('Client connected to %s', self.url)
        return self.websocket

    async def request(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        log.info('Requesting IPC Server for %r with %r', endpoint, kwargs)
        if not self.session:
            await self.init_sock()
        assert self.websocket is not None and self.session is not None
        payload = {'endpoint': endpoint, 'data': kwargs, 'headers': {'Authorization': self.secret_key}}
        await self.websocket.send_json(payload)
        log.debug('Client > %r', payload)
        recv = await self.websocket.receive()
        log.debug('Client < %r', recv)

        if recv.type is aiohttp.WSMsgType.PING:
            log.info('Received request to PING')
            await self.websocket.ping()
            return await self.request(endpoint, **kwargs)

        if recv.type is aiohttp.WSMsgType.CLOSED:
            log.error(
                'WebSocket connection closed unexpectedly. IPC Server is unreachable. Attempting reconnection in 5 seconds.'
            )
            await self.session.close()
            await asyncio.sleep(5)
            await self.init_sock()
            return await self.request(endpoint, **kwargs)
        return recv.json()
