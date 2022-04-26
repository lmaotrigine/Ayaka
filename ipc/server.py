"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Coroutine, Optional, TypedDict


T = Callable[..., Coroutine]

import aiohttp.web

from .errors import *


if TYPE_CHECKING:
    from bot import Ayaka

__all__ = ('Server', 'route')

log = logging.getLogger(__name__)


class IpcPayload(TypedDict):
    endpoint: str
    data: dict[str, Any]


class IpcServerResponse:
    def __init__(self, data: IpcPayload):
        self._json = data
        self.length = len(data)
        self.endpoint = data['endpoint']
        for key, val in data['data'].items():
            setattr(self, key, val)

    def get(self, key: str, default: Any) -> Any:
        return getattr(self, key) or default

    def to_json(self):
        return self._json

    def __repr__(self) -> str:
        return f'<IpcServerResponse length={self.length}>'

    def __str__(self) -> str:
        return self.__repr__()


class Server:
    ROUTES: ClassVar[dict[str, T]] = {}

    def __init__(
        self,
        bot: Ayaka,
        *,
        host: str = 'localhost',
        port: int = 8765,
        secret_key: Optional[str] = None,
    ):
        self.bot = bot
        self.secret_key = secret_key
        self.host = host
        self.port = port
        self._server = None
        self._multicast_server = None
        self.endpoints = {}

    def __update_endpoints(self):
        self.endpoints = {**self.ROUTES}

    async def handle_accept(self, request: aiohttp.web.Request) -> None:
        self.__update_endpoints()
        log.info('Initiating IPC Server.')

        websocket = aiohttp.web.WebSocketResponse()
        await websocket.prepare(request)

        async for message in websocket:
            data = message.json()
            log.debug('IPC Server < %r', data)
            endpoint = data.get('endpoint')
            headers = data.get('headers')
            if not headers or headers.get('Authorization') != self.secret_key:
                log.info('Received unauthorized request (Invalid or no token provided).')
                response = {'error': 'Invalid or no token provided.', 'code': 403}
            else:
                if not endpoint or endpoint not in self.endpoints:
                    log.info('Received invalid request (Invalid or no endpoint given).')
                    response = {'error': 'Invalid or no endpoint given.', 'code': 400}
                else:
                    server_response = IpcServerResponse(data)
                    attempted_cls = self.bot.cogs.get(self.endpoints[endpoint].__qualname__.split('.')[0])
                    if attempted_cls:
                        arguments = (attempted_cls, server_response)
                    else:
                        arguments = (server_response,)
                    try:
                        ret = await self.endpoints[endpoint](*arguments)
                        response = ret
                    except Exception as e:
                        log.error('Received error while executing %r with %r', endpoint, request)
                        self.bot.dispatch('ipc_error', endpoint, e)
                        response = {'error': f'IPC route raised error of type {type(e).__name__}', 'code': 500}

            try:
                await websocket.send_json(response)
                log.debug('IPC Server > %r', response)
            except TypeError as e:
                response = {'error': str(e), 'code': 500}
                await websocket.send_json(response)
                log.debug('IPC Server > %r', response)
                raise JSONEncodeError(str(e))

    async def start(self) -> None:
        self._server = aiohttp.web.Application()
        self._server.router.add_route('GET', '/', self.handle_accept)  # type: ignore
        runner = aiohttp.web.AppRunner(self._server)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, self.host, self.port)
        await site.start()


def route(name: str = '') -> Callable[[T], T]:
    def decorator(func: T) -> T:
        if not name:
            Server.ROUTES[func.__name__] = func
        else:
            Server.ROUTES[name] = func
        return func

    return decorator
