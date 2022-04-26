"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import secrets
from typing import Optional

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ..template import get_template


router = APIRouter()


async def broadcast_join(
    req: Request, valid: bool, guild_id: Optional[int], user_id: Optional[int] = None, **kwargs
) -> RedirectResponse:
    data = await req.app.client.request('join', valid=valid, guild_id=guild_id, user_id=user_id, **kwargs)
    success = data['success'].lower() == 'success'
    if success:
        return RedirectResponse('/')
    return RedirectResponse('/not_gonna_happen')


@router.get('/')
async def hi(req: Request) -> RedirectResponse:
    return RedirectResponse('https://discord.gg/s44CFagYN2')


@router.get('/login')
async def login(req: Request):
    if req.session.get('user') is not None:
        return RedirectResponse('/')
    state = secrets.token_urlsafe(32)
    req.session['state'] = state
    url = req.state.oauth.get_authorization_url(state)
    return RedirectResponse(url)


@router.get('/logout')
async def logout(req: Request):
    try:
        del req.session['user']
    except KeyError:
        pass
    return RedirectResponse('/')


@router.get('/invite-bot')
async def invite_bot(req: Request) -> RedirectResponse:
    try:
        state = req.session['state']
    except KeyError:
        req.session['state'] = state = secrets.token_urlsafe(32)
    try:
        user = req.session['user']
    except KeyError:
        url = req.state.oauth.get_authorization_url(state, scope='identify bot applications.commands')
        return RedirectResponse(url)
    tokens = await req.app.redis.get(f'user:{user["id"]}')
    if int(user['id']) != 411166117084528640 and tokens <= 0:
        await req.app.redis.delete(f'user:{user["id"]}')
        return RedirectResponse('/not_gonna_happen')
    return RedirectResponse(req.state.oauth.get_authorization_url(state, scope='identify bot applications.commands'))


@router.get('/callback')
async def callback(
    req: Request, state: Optional[str], code: Optional[str] = None, guild_id: Optional[int] = None
) -> RedirectResponse:
    if req.query_params.get('error'):
        return RedirectResponse('/')
    if req.session.get('state', secrets.token_urlsafe(32)) != state:
        raise HTTPException(401)
    data = await req.state.oauth.get_access_token(code)
    # by now, the bot might already be added.
    token = data['access_token']
    scope = data['scope']
    if 'identify' not in scope:
        if 'bot' in scope:
            return await broadcast_join(req, False, guild_id)
    try:
        user = req.session['user']
    except KeyError:
        req.session['user'] = user = await req.state.oauth.get_identity(token)
    if 'bot' not in scope:
        # this is likely a login request. Don't want to consume a token for that.
        # if invited with only applications.commands, nothing will work anyway
        # so no need to check whitelist
        return RedirectResponse('/')
    username, discriminator = user['username'], user['discriminator']
    tokens = await req.app.redis.incrby(f'user:{user["id"]}', -1)
    if tokens < 0:
        await req.app.redis.delete(f'user:{user["id"]}')
        return await broadcast_join(
            req, False, guild_id, user_id=int(user['id']), username=username, discriminator=discriminator
        )
    await broadcast_join(req, True, guild_id, user_id=int(user['id']), username=username, discriminator=discriminator)
    return RedirectResponse('/')


@router.get('/avatarhistory')
async def avy_history(req: Request) -> Response:
    try:
        user = req.session['user']
    except KeyError:
        return RedirectResponse('/discord/login')
    res = await req.app.client.request('avatar_history', user_id=int(user['id']))
    if not res['avatars']:
        return PlainTextResponse('No avatars history recorded.', status_code=404)
    return HTMLResponse(get_template('avatarhistory.html').render(**res))


@router.get('/avatarhistory/{id}')
async def avy_history_user(req: Request, id) -> Response:
    try:
        id = int(id, base=10)
    except ValueError:
        return PlainTextResponse('Invalid Discord ID provided or no avatar history recorded.', status_code=404)
    res = await req.app.client.request('avatar_history', user_id=id)
    if not res['avatars']:
        return PlainTextResponse('Invalid Discord ID provided or no avatar history recorded', status_code=404)
    return HTMLResponse(get_template('avatarhistory.html').render(**res))
