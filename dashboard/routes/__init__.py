from __future__ import annotations

from typing import Any

from .discord import *
from .pages import *
from .pokemon import *


def setup_routes(**kwargs: Any) -> Any:
    return [
        (r'/', Index, kwargs),
        (r'/ip', IP, kwargs),
        (r'/voice', VoiceRecognition, kwargs),
        (r'/discord', DiscordIndex, kwargs),
        (r'/discord/login', DiscordLogin, kwargs),
        (r'/discord/logout', DiscordLogout, kwargs),
        (r'/discord/invite-bot', DiscordInviteBot, kwargs),
        (r'/discord/avatarhistory', DiscordAvatarHistory, kwargs),
        (r'/discord/avatarhistory/([^/]+)', DiscordAvatarHistoryUser, kwargs),
        (r'/sprites', ShowdownTrainerSprites, kwargs),
        (r'/sprites/pokemon', PokemonSprites, kwargs),
    ]
