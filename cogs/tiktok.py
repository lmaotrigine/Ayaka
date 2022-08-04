"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import functools
import pathlib
import re
from typing import TYPE_CHECKING

import discord
import yt_dlp
from discord.ext import commands


if TYPE_CHECKING:
    from bot import Ayaka
    from utils.context import Context


ydl = yt_dlp.YoutubeDL({'outtmpl': 'buffer/%(id)s.%(ext)s', 'quiet': True})

MOBILE_PATTERN = re.compile(r'(https?://(?:vm|www)\.tiktok\.com/(?:t/)?[a-zA-Z0-9]+)(?:/\?.*)?')
DESKTOP_PATTERN = re.compile(r'(https?://(?:www\.)?tiktok\.com/@(?P<user>.*)/video/(?P<video_id>[0-9]+))(\?(?:.*))?')
INSTAGRAM_PATTERN = re.compile(r'(?:https?://)?(?:www\.)?instagram\.com/reel/[a-zA-Z0-9\-\_]+/\?.*?\=')


class NeedsLogin(commands.CommandError):
    pass


class TikTok(commands.Cog, command_attrs=dict(hidden=True)):
    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot

    async def process_url(self, url: str, max_len: int = 8388608) -> tuple[discord.File, str]:
        loop = asyncio.get_running_loop()
        if not url.endswith('/'):
            url += '/'
        fn = functools.partial(ydl.extract_info, url, download=True)
        try:
            info = await loop.run_in_executor(None, fn)
        except yt_dlp.DownloadError as e:
            if 'You need to log in' in str(e):
                raise NeedsLogin('Need to log in.')
            raise
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
        file_loc.unlink(missing_ok=True)
        fixed_file_loc.unlink(missing_ok=True)
        content = f'**Uploader:**: {info["uploader"]}\n\n' * bool(info['uploader'])
        content += f'**Description:** {info["description"]}' * bool(info['description'])
        return file, content

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
            MOBILE_PATTERN.findall(message.content)
            or DESKTOP_PATTERN.findall(message.content)
            or INSTAGRAM_PATTERN.findall(message.content)
        )
        if not matches:
            return
        print(f'Processing {len(matches)} detected TikToks...')

        async with message.channel.typing():
            for idx, url in enumerate(matches, start=1):
                if isinstance(url, str):
                    exposed_url = url
                else:
                    exposed_url = url[0]
                try:
                    file, content = await self.process_url(exposed_url, message.guild.filesize_limit)
                except NeedsLogin as e:
                    await message.channel.send(str(e))
                    return
                except ValueError:
                    await message.reply(f'TikTok link #{idx} in your message exceeded the file size limit.')
                    continue

                if message.mentions:
                    content = ' '.join(m.mention for m in message.mentions) + '\n\n' + content
                await message.reply(content[:1000], file=file)
                if message.channel.permissions_for(message.guild.me).manage_messages and any(
                    [
                        INSTAGRAM_PATTERN.fullmatch(message.content),
                        MOBILE_PATTERN.fullmatch(message.content),
                        DESKTOP_PATTERN.fullmatch(message.content),
                    ]
                ):
                    await message.delete()

    @commands.hybrid_command(name='tiktok', aliases=['tt'])
    async def tt(self, ctx: Context, url: str) -> None:
        """Download a TikTok video or an Instagram reel."""
        if not MOBILE_PATTERN.fullmatch(url) and not INSTAGRAM_PATTERN.fullmatch(url) and not DESKTOP_PATTERN.fullmatch(url):
            await ctx.send('Invalid TikTok link.', ephemeral=True)
            return
        await ctx.defer()
        try:
            if ctx.guild:
                file, content = await self.process_url(url, ctx.guild.filesize_limit)
            else:
                file, content = await self.process_url(url)
        except NeedsLogin as e:
            await ctx.send(str(e))
            return
        except ValueError:
            await ctx.send('TikTok link exceeded the file size limit.')
            return
        await ctx.send(content[:1000], file=file)


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(TikTok(bot))
