from typing import TypedDict

class _Booru(TypedDict):
    user_id: str
    api_key: str

class _Mangadex(TypedDict):
    username: str
    password: str
    refresh_token: str | None

# core
token: str
application_id: int
mangadex_webhook: str
avatar_webhook: str
redis: str
postgresql: str
mangadex_auth: _Mangadex
stat_webhook: str
gelbooru_api: _Booru
danbooru_api: _Booru
twitter_api_key: str
twitter_secret: str
twitter_access_token: str
twitter_access_token_secret: str

# core: optional
cdn_key: str
audio_postgresql: str

# dashboard
client_secret: str
base_url: str
cookie_secret: str

# private extensions
github_token: str
chess_hook: str
cotd_salt: str
ims_news_hook: str
open_collective_token: str
oc_discord_client_id: int
oc_discord_client_secret: str
