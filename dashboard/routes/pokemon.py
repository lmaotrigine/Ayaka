from __future__ import annotations

import abc
import os
import pathlib

from ..utils.handlers import HTTPHandler

static = pathlib.Path(__file__).parent.parent.parent / 'static'


class ShowdownTrainerSprites(HTTPHandler, abc.ABC):
    async def get(self) -> None:
        file = static / 'sprites' / 'showdown.txt'
        with open(file) as f:
            sprites = f.read().splitlines()
        self.render('sprites.html', sprites=sprites)
    

class PokemonSprites(HTTPHandler, abc.ABC):
    async def get(self) -> None:
        reg = static / 'sprites' / 'pokemon' / 'regular'
        shiny = static / 'sprites' / 'pokemon' / 'shiny'
        regs = sorted(os.listdir(reg))
        shinies = sorted(os.listdir(shiny))
        sprites = list(zip(regs, shinies))
        self.render('pokemon.html', sprites=sprites)
