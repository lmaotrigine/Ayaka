"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
import pathlib
import re
from io import BytesIO
from typing import TYPE_CHECKING, Annotated

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from utils.cache import ExpiringCache
from utils.fuzzy import extract
from utils.time import ordinal


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import Context


ydl = yt_dlp.YoutubeDL({'outtmpl': 'buffer/%(id)s.%(ext)s', 'quiet': True, 'cookiefile': 'configs/insta_cookies.txt'})

log = logging.getLogger(__name__)

MOBILE_PATTERN = re.compile(r'\<?(https?://(?:vt|vm|www)\.tiktok\.com/(?:t/)?[a-zA-Z0-9]+/?)(?:/\?.*\>)?\>?')
DESKTOP_PATTERN = re.compile(r'\<?(https?://(?:www\.)?tiktok\.com/@(?P<user>.*)/video/(?P<video_id>[0-9]+))(\?(?:.*))?\>?')
INSTAGRAM_PATTERN = re.compile(
    rf'\<?(?P<url>https?://(?:www\.)?instagram\.com(?:/[^/]+)?/(?:p|tv|reel)/(?P<id>[^/?#&]+))\>?'
)

BASE_URLS = [
    'api16-normal-useast5.us.tiktokv.com',
    'api16-normal-c-alisg.tiktokv.com',
    'api19-normal-useast1a.tiktokv.com',
    'api22-normal-c-useast1a.tiktokv.com',
]
VOICES: dict[str, str] = {
    # Default
    'default': 'Default',
    # Disney
    'en_us_ghostface': 'Ghost Face',
    'en_us_chewbacca': 'Chewbacca',
    'en_us_c3po': 'C3PO',
    'en_us_stitch': 'Stitch',
    'en_us_stormtrooper': 'Stormtrooper',
    'en_us_rocket': 'Rocket',
    # English
    'en_au_001': 'English AU - Female',
    'en_au_002': 'English AU - Male',
    'en_uk_001': 'English UK - Male 1',
    'en_uk_003': 'English UK - Male 2',
    'en_us_001': 'English US - Female (Int. 1)',
    'en_us_002': 'English US - Female (Int. 2)',
    'en_us_006': 'English US - Male 1',
    'en_us_007': 'English US - Male 2',
    'en_us_009': 'English US - Male 3',
    'en_us_010': 'English US - Male 4',
    # Europe
    'fr_001': 'French - Male 1',
    'fr_002': 'French - Male 2',
    'de_001': 'German - Female',
    'de_002': 'German - Male',
    'es_002': 'Spanish - Male',
    # Europe
    'es_mx_002': 'Spanish MX - Male',
    'br_001': 'Portuguese BR - Female 1',
    'br_003': 'Portuguese BR - Female 2',
    'br_004': 'Portuguese BR - Female 3',
    'br_005': 'Portuguese BR - Male',
    # Asia
    'id_001': 'Indonesian - Female',
    'jp_001': 'Japanese - Female 1',
    'jp_003': 'Japanese - Female 2',
    'jp_005': 'Japanese - Female 3',
    'jp_006': 'Japanese - Male',
    'kr_002': 'Korean - Male 1',
    'kr_003': 'Korean - Female',
    'kr_004': 'Korean - Male 2',
}


def get_voice(argument: str) -> str:
    if argument in VOICES:
        return argument.lower()
    raise commands.BadArgument('Invalid Voice')


log = logging.getLogger(__name__)


class NeedsLogin(commands.CommandError):
    pass


class TiktokError(Exception):
    def __init__(self, resp: aiohttp.ClientResponse) -> None:
        self.resp = resp

    async def log(self, ctx: Context) -> None:
        e = discord.Embed(title='Tiktok Error', colour=0xCC3366)
        e.description = f'```json\n{await self.resp.text()}\n```'
        e.timestamp = discord.utils.utcnow()
        await ctx.bot.stat_webhook.send(embed=e)

    def __str__(self) -> str:
        return 'Tiktok broke, sorry.'


class FilesizeLimitExceeded(Exception):
    def __init__(self, post: bool) -> None:
        self.post = post
        super().__init__('The filesize limit was exceeded for this guild.')


class TikTok(commands.Cog, command_attrs=dict(hidden=True)):
    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name='Process TikTok link',
            callback=self.tiktok_context_menu_callback,
            guild_ids=[932533101530349568, 714196770879438888],
        )
        self.bot.tree.add_command(self.ctx_menu)
        self._tasks: dict[str, asyncio.Task] = ExpiringCache(seconds=20)

    async def cog_load(self) -> None:
        ret: list[app_commands.Choice[str]] = []
        for value, name in VOICES.items():
            ret.append(app_commands.Choice(name=name, value=value))
        self.voice_choices = ret

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def tiktok_context_menu_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(thinking=True)

        if match := MOBILE_PATTERN.search(message.content):
            url = match[1]
        elif match := DESKTOP_PATTERN.search(message.content):
            url = match[1]
        elif match := INSTAGRAM_PATTERN.search(message.content):
            url = match['url']
        else:
            await interaction.followup.send("I couldn't find any processable links in this message.")
            return

        try:
            file, content = await self.process_url(url, interaction.guild)
        except (FilesizeLimitExceeded, NeedsLogin, RuntimeError) as e:
            await interaction.followup.send(str(e))
            return
        await interaction.followup.send(content, file=file)

    # async def _extract_info(self, url: str)
    async def process_url(self, url: str, guild: discord.Guild | None = None) -> tuple[discord.File, str]:
        max_len = guild and guild.filesize_limit or 8388608
        loop = asyncio.get_running_loop()
        fn = functools.partial(ydl.extract_info, url, download=True)
        try:
            info = await loop.run_in_executor(None, fn)
        except (yt_dlp.DownloadError, yt_dlp.utils.ExtractorError) as e:
            if 'You need to log in' in str(e) or 'login required' in str(e):
                raise NeedsLogin('Need to log in.')
            raise
        if not info:
            raise RuntimeError('This message could not be parsed. Are you sure it\'s a valid link?')
        file_loc = pathlib.Path(f'buffer/{info["id"]}.{info["ext"]}')
        fixed_file_loc = pathlib.Path(f'buffer/{info["id"]}_fixed.{info["ext"]}')

        stat = file_loc.stat()
        if stat.st_size > max_len:
            file_loc.unlink(missing_ok=True)
            raise ValueError('Video exceeded the file size limit.')
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-y', '-i', f'{file_loc}', f'{fixed_file_loc}', '-hide_banner', '-loglevel', 'warning'
        )
        await proc.communicate()
        if fixed_file_loc.stat().st_size > max_len:
            file_loc.unlink(missing_ok=True)
            raise ValueError('Video exceeded the file size limit.')
        file = discord.File(str(fixed_file_loc), filename=fixed_file_loc.name)
        content = f'**Uploader:**: {info["uploader"]}\n\n' * bool(info['uploader'])
        content += f'**Description:** {info["description"]}' * bool(info['description'])
        if file_loc.name in self._tasks:
            self._tasks[file_loc.name].cancel()
        task = loop.create_task(self._cleanup_paths(file_loc, fixed_file_loc))
        self._tasks[file_loc.name] = task
        return file, content

    async def _cleanup_paths(self, *args: pathlib.Path) -> None:
        await asyncio.sleep(20)
        for path in args:
            path.unlink(missing_ok=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.guild.id != 932533101530349568:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        matches = (
            list(MOBILE_PATTERN.finditer(message.content))
            + list(DESKTOP_PATTERN.finditer(message.content))
            + list(INSTAGRAM_PATTERN.finditer(message.content))
        )
        if not matches:
            return
        log.info(f'Processing {len(matches)} detected TikToks...')

        async with message.channel.typing():
            _errors = []
            for idx, url in enumerate(matches, start=1):
                exposed_url = url[1]
                try:
                    file, content = await self.process_url(exposed_url, message.guild)
                except NeedsLogin as e:
                    await message.channel.send(str(e))
                    return
                except ValueError:
                    await message.reply(f'TikTok link #{idx} in your message exceeded the file size limit.')
                    continue
                except (yt_dlp.DownloadError, yt_dlp.utils.ExtractorError):
                    _errors.append(idx)
                    continue

                if message.mentions:
                    content = ' '.join(m.mention for m in message.mentions) + '\n\n' + content

                content = content[:1000] + f'\nRequested by: {message.author} | Replying to: {message.jump_url}'

                await message.channel.send(content, file=file)
                if _errors:
                    formatted = (
                        'I had issues downloading the '
                        f'{", ".join(ordinal(idx) for idx in _errors)} '
                        'links in your message.'
                    )
                    await message.channel.send(formatted)
                if message.channel.permissions_for(message.guild.me).manage_messages and any(
                    [
                        INSTAGRAM_PATTERN.fullmatch(message.content),
                        MOBILE_PATTERN.fullmatch(message.content),
                        DESKTOP_PATTERN.fullmatch(message.content),
                    ]
                ):
                    await message.delete()

    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))

    @commands.hybrid_group(name='tiktok', aliases=['tt'], fallback='download')
    async def tt(self, ctx: Context, url: str) -> None:
        """Download a TikTok video or an Instagram reel."""
        if not MOBILE_PATTERN.fullmatch(url) and not INSTAGRAM_PATTERN.fullmatch(url) and not DESKTOP_PATTERN.fullmatch(url):
            await ctx.send('Invalid TikTok link.', ephemeral=True)
            return
        await ctx.typing()
        try:
            file, content = await self.process_url(url, ctx.guild)
        except NeedsLogin as e:
            await ctx.reply(str(e))
            return
        except ValueError:
            await ctx.reply('TikTok link exceeded the file size limit.')
            return
        await ctx.reply(content[:1000], file=file)

    async def process_voice(self, voice: str, text: str) -> BytesIO:
        voice = 'en_us_002' if voice.lower() == 'default' else voice
        params = {
            'speaker_map_type': 0,
            'text_speaker': voice,
            'req_text': text,
            'aid': 1233,
        }
        headers = {
            'User-Agent': 'com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; es_ES; SM-G988N; Build/NRD90M;tt-ok/3.12.13.1)',
            'Cookie': 'sessionid=57b7d8b3e04228a24cc1e6d25387603a',
        }
        data = {}
        for url in BASE_URLS:
            async with self.bot.session.post(
                f'https://{url}/media/api/text/speech/invoke/', params=params, headers=headers
            ) as resp:
                data = await resp.json()
            if data.get('message') == "Couldn't load speech. Try again.":
                continue
            try:
                res = data['data']['v_str']
            except KeyError:
                log.error(
                    'TikTok synth error.\nMessage: "%s"\nStatus Code: %d\nStatus Message: "%s"',
                    text,
                    data['status_code'],
                    data['status_msg'],
                )
                raise TiktokError(resp)
            padding = len(res) % 4
            res = res + ('=' * padding)
            bytes_ = base64.b64decode(res)
            fp = BytesIO(bytes_)
            fp.seek(0)
            return fp
        log.error(
            'TikTok synth error.\nMessage: "%s"\nStatus Code: %d\nStatus Message: "%s"',
            text,
            data['status_code'],
            data['status_msg'],
        )
        raise RuntimeError('Tiktok broke, sorry.')

    async def voice_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        all_choices = self.voice_choices
        if not current:
            return all_choices[:25]
        cleaned = extract(str(current), [choice.name for choice in all_choices], limit=5, score_cutoff=20)
        ret: list[app_commands.Choice[str]] = []
        for item, _ in cleaned:
            _x = discord.utils.get(all_choices, name=item)
            if _x:
                ret.append(_x)
        return ret

    @tt.command(name='voice')
    @app_commands.autocomplete(voice=voice_autocomplete)
    async def tiktok_voice(
        self,
        ctx: Context,
        voice: Annotated[str, get_voice],
        *,
        text: str,
    ) -> None:
        """Generate an audio file with a given Tiktok voice engine and text."""
        await ctx.typing()
        try:
            fp = await self.process_voice(voice, text)
        except RuntimeError as e:
            await ctx.reply(str(e))
            return
        except TiktokError as e:
            await e.log(ctx)
            await ctx.reply(str(e))
            raise e
        await ctx.reply(f'> {text}', file=discord.File(fp, filename='tiktok.mp3'))


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(TikTok(bot))
