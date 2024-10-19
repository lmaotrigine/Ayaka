from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, TypedDict

import aiohttp
import discord
import yarl
from discord import app_commands
from discord.ext import commands
from lru import LRU


if TYPE_CHECKING:
    from bot import Ayaka


class PasteData(TypedDict):
    key: str
    data: str


class PasteResponse(TypedDict):
    key: str
    url: str


class Codeblock:
    __slots__ = ('lang', 'code')

    def __init__(self, *, lang: str | None, code: str):
        self.lang = lang
        self.code = code


class CodeblockConverter:
    def __init__(self, blocks: list[Codeblock]) -> None:
        self.blocks = blocks

    @classmethod
    def convert(cls, argument: str) -> CodeblockConverter:
        lines = argument.split('\n')
        inside = False
        lang: str | None = None
        blocks: list[Codeblock] = []
        code: list[str] = []
        for line in lines:
            if inside and '```' not in line:
                code.append(line)
            elif not inside and '```' in line:
                lang = line[line.index('`') :].split()[0].strip('```') or None
                line = line.replace('```', '', 1)
                if '```' in line:
                    data = line.replace('```', '')
                    if data:
                        blocks.append(Codeblock(lang=None, code=data))
                    lang = None
                    code = []
                    continue
                inside = True
            elif inside and '```' in line:
                if not code:
                    continue
                joined = '\n'.join(code)
                blocks.append(Codeblock(lang=lang, code=joined))
                lang = None
                code = []
                inside = False
        return cls(blocks)


class Node:
    __slots__ = ('keys', 'last_edit')

    def __init__(self, *, keys: list[str], last_edit: datetime.datetime | None) -> None:
        self.keys = keys
        self.last_edit = last_edit


ALLOWED_INSTALLS = app_commands.AppInstallationType(user=True, guild=True)
ALLOWED_CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
BASE_URL = yarl.URL('https://paste.5ht2.me')


class Paste(commands.Cog, command_attrs={'hidden': True}):
    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name='Message to Paste',
            callback=self.convert_paste,
            allowed_installs=ALLOWED_INSTALLS,
            allowed_contexts=ALLOWED_CONTEXTS,
        )
        self._cache: LRU[int, Node] = LRU(50)

    async def cog_load(self) -> None:
        self.ctx_menu.on_error = self.paste_error
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @app_commands.checks.cooldown(2, 10)
    async def convert_paste(self, interaction: discord.Interaction[Ayaka], message: discord.Message) -> None:
        await interaction.response.defer()
        cached = self._cache.get(message.id)
        if cached is not None and cached.last_edit == message.edited_at:
            out: list[str] = []
            for key in cached.keys:
                try:
                    async with self.bot.session.get(BASE_URL / 'documents' / key) as resp:
                        resp.raise_for_status()
                        paste: PasteData = await resp.json()
                except aiohttp.ClientResponseError as e:
                    if e.status != 404:
                        await interaction.followup.send(f'An unknown error occurred while fetching this paste: `{e.status}`')
                        return
                else:
                    out.append(f'<{BASE_URL / paste["key"]}>')
            await interaction.followup.send('\n'.join(out))
        parsed = CodeblockConverter.convert(message.content)
        files: list[str] = []
        for attachment in message.attachments:
            content_type = attachment.content_type or ''
            if content_type.startswith('text/') or content_type == 'application/json':
                content = (await attachment.read()).decode('utf-8')
                files.append(content[:400_000])
        for block in parsed.blocks:
            files.append(block.code)
        if len(files) < 5:
            content = (
                f'{message.author}({message.author.id}) in {message.channel}({message.channel.id})\n'
                f'{message.created_at}\n\n{message.content}'
            )
            files.append(content)
        keys: list[str] = []
        for file in files[:5]:
            async with self.bot.session.post(BASE_URL / 'documents', data=file) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f'An error occurred while creating the paste: `{resp.status}`')
                    return
                data: PasteResponse = await resp.json()
                keys.append(data['key'])
        self._cache[message.id] = Node(keys=keys, last_edit=message.edited_at)
        await interaction.followup.send('\n'.join(f'<{BASE_URL / key}>' for key in keys))

    async def paste_error(self, interaction: discord.Interaction[Ayaka], error: app_commands.AppCommandError) -> None:
        s = interaction.response.send_message
        if interaction.response.is_done():
            s = interaction.followup.send
        await s(f'An error occurred: {error}', ephemeral=True)


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(Paste(bot))
