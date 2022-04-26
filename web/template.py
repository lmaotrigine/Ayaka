"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from jinja2 import Environment
from jinja2.loaders import FileSystemLoader


if TYPE_CHECKING:
    from jinja2.environment import Template
dirname = pathlib.Path(__file__).parent
ENVIRONMENT = Environment(loader=FileSystemLoader(dirname / 'templates'))


def get_template(name: str) -> Template:
    return ENVIRONMENT.get_template(name)
