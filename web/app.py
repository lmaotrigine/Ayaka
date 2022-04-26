"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import pathlib
import secrets

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

import config
from ipc import Client

from .oauth import Oauth2
from .routes.discord import router as discord_router
from .routes.index import router as index_router
from .routes.pokemon import router as pokemon_router


dirname = pathlib.Path(__file__).parent.parent


class MyAPI(FastAPI):
    def __init__(self):
        self.client = Client(port=3456, secret_key=config.ipc_key)
        self.redis = aioredis.from_url(config.redis)
        super().__init__(docs_url=None, redoc_url=None, openapi_url=None)


app = MyAPI()
app.add_middleware(SessionMiddleware, secret_key=secrets.token_urlsafe(64))
app.include_router(discord_router, prefix='/discord')
app.include_router(index_router)
app.include_router(pokemon_router)
app.mount('/static', StaticFiles(directory=dirname / 'static'), name='static')
oauth = Oauth2(config.application_id, config.client_secret, f'{config.base_url}/discord/callback')


@app.middleware('http')  # type: ignore
async def add_oauth_middleware(req: Request, call_next):
    req.state.oauth = oauth
    return await call_next(req)


@app.on_event('shutdown')  # type: ignore
async def close_oauth():
    await oauth.close()
