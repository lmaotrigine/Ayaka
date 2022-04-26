"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..template import get_template


router = APIRouter()


@router.get('/')
async def root(req: Request) -> HTMLResponse:
    total = await req.app.redis.incrby('hits', 1)
    you = await req.app.redis.incrby(f'hits:{req.client.host}', 1)
    now = datetime.datetime.now().hour
    name = 'Dawn' if 6 < now < 18 else 'Dusk'
    try:
        user = req.session['user']
        user = f'{user["username"]}#{user["discriminator"]}'
    except KeyError:
        user = ''
    return HTMLResponse(get_template('index.html').render(name=name, user=user, total=f'{total:,}', you=f'{you:,}'))


@router.get('/discord')
async def hi(_) -> RedirectResponse:
    return RedirectResponse('https://discord.gg/s44CFagYN2')


@router.get('/ip')
async def ip(req: Request) -> PlainTextResponse:
    return PlainTextResponse(req.client.host)


@router.get('/voice')
async def voice(_) -> HTMLResponse:
    return HTMLResponse(get_template('voice_recognition.html').render())


@router.get('/not_gonna_happen')
async def lmao(_) -> PlainTextResponse:
    return PlainTextResponse(
        "you don't have a token, ask VJ. tokens are one time use, so if you had one and used it you'll need to apply for another"
    )
