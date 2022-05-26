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


ydl = yt_dlp.YoutubeDL({'outtmpl': 'buffer/%(id)s.%(ext)s', 'quiet': True})

MOBILE_PATTERN = re.compile(r'https?://(?:vm\.)?tiktok\.com/[a-zA-Z0-9]+(?:/\?.*)?')
DESKTOP_PATTERN = re.compile(r'(https?://(?:www\.)?tiktok\.com/\@[a-zA-Z0-9_]+/video/[0-9]+)')
# INSTAGRAM_PATTERN = re.compile(r'(?:https?://)?(?:www\.)?instagram\.com/reel/[a-zA-Z\-0-9]+/')
INSTAGRAM_PATTERN = re.compile(r'(?:https?://)?(?:www\.)?instagram\.com/reel/[a-zA-Z0-9\-\_]+/(?:\?.*)?\=')


class TikTok(commands.Cog):
    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.guild.id != 932533101530349568:
            return
        
        matches = MOBILE_PATTERN.findall(message.content) or DESKTOP_PATTERN.findall(message.content) or INSTAGRAM_PATTERN.findall(message.content)
        if not matches:
            return
        print(f'Processing {len(matches)} detected TikToks...')
        
        async with message.channel.typing():
            loop = asyncio.get_running_loop()
            for idx, url in enumerate(matches, start=1):
                if not url.endswith('/'):
                    url += '/'
                fn = functools.partial(ydl.extract_info, url, download=True)
                info = await loop.run_in_executor(None, fn)
                file_loc = pathlib.Path(f'buffer/{info["id"]}.{info["ext"]}')
                fixed_file_loc = pathlib.Path(f'buffer/{info["id"]}_fixed.{info["ext"]}')
                
                stat = file_loc.stat()
                if stat.st_size > message.guild.filesize_limit:
                    file_loc.unlink(missing_ok=True)
                    await message.reply(f'TikTok link #{idx} in your message exceeded the file size limit.')
                    continue
                proc = await asyncio.create_subprocess_exec('ffmpeg', '-y', '-i', f'{file_loc}', f'{fixed_file_loc}')
                await proc.communicate()
                file = discord.File(str(fixed_file_loc), filename=fixed_file_loc.name)
                content = f'{info["uploader"]}\n\n' * bool(info['uploader'])
                content += f'{info["description"]}'
                if message.mentions:
                    content = ' '.join(m.mention for m in message.mentions) + '\n\n' + content
                try:
                    await message.reply(content[:1000], file=file)
                except discord.HTTPException:
                    await message.reply(f'This link exceeded the file size limit.')
                else:
                    if any([INSTAGRAM_PATTERN.fullmatch(message.content), DESKTOP_PATTERN.fullmatch(message.content), MOBILE_PATTERN.fullmatch(message.content)]):
                        await message.delete()
                finally:
                    file_loc.unlink(missing_ok=True)
                    fixed_file_loc.unlink(missing_ok=True)


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(TikTok(bot))
