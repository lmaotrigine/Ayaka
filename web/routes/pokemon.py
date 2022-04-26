"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import os
import pathlib

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import HTMLResponse

from ..template import get_template


router = APIRouter()

static = pathlib.Path(__file__).parent.parent.parent / 'static'


@router.get('/sprites')
async def showdown_trainer_sprites(req: Request) -> HTMLResponse:
    file = static / 'sprites' / 'showdown.txt'
    with open(file) as f:
        sprites = f.read().splitlines()
    return HTMLResponse(get_template('sprites.html').render(sprites=sprites))


@router.get('/sprites/pokemon')
async def pokemon_sprites(req: Request) -> HTMLResponse:
    reg = static / 'sprites' / 'pokemon' / 'regular'
    shiny = static / 'sprites' / 'pokemon' / 'shiny'
    regs = sorted(os.listdir(reg))
    shinies = sorted(os.listdir(shiny))
    sprites = list(zip(regs, shinies))
    return HTMLResponse(get_template('pokemon.html').render(sprites=sprites))
