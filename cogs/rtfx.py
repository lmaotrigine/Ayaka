"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import pathlib
import re
import sys
import zlib
from textwrap import dedent
from typing import TYPE_CHECKING, Callable, Generator

import asyncpg  # type: ignore # rtfs
import discord
from discord import app_commands
from discord.ext import commands, menus, tasks  # type: ignore # rtfs
from jishaku.codeblocks import Codeblock, codeblock_converter
from jishaku.shell import ShellReader
from lxml import etree

from utils import fuzzy
from utils.context import Context
from utils.formats import to_codeblock
from utils.paginator import RoboPages, TextPageSource


if TYPE_CHECKING:
    from bot import Ayaka


RTFS = ('discord', 'discord.ext.commands', 'discord.ext.tasks', 'discord.ext.menus', 'asyncpg')

RTFM_PAGES = {
    'discord.py': 'https://discordpy.readthedocs.io/en/stable',
    'discord.py-master': 'https://discordpy.readthedocs.io/en/latest',
    'python': 'https://docs.python.org/3',
    'asyncpg': 'https://magicstack.github.io/asyncpg/current',
    'aiohttp': 'https://docs.aiohttp.org/en/stable',
}


class BadSource(commands.CommandError):
    pass


class SourceConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> str | None:
        args = argument.split('.')
        top_level = args[0]
        if top_level in ('commands', 'menus', 'tasks'):
            top_level = f'discord.ext.{top_level}'

        if top_level not in RTFS:
            raise BadSource(f'`{top_level}`is not an allowed sourceable module.')

        recur = sys.modules[top_level]  # type: ignore

        if len(args) == 1:
            return inspect.getsource(recur)

        for item in args[1:]:
            if item == '':
                raise BadSource("Don't even try.")

            recur = inspect.getattr_static(recur, item, None)

            if recur is None:
                raise BadSource(f'{argument} is not a valid module path.')

        if isinstance(recur, property):
            recur: Callable[..., None] = recur.fget
        return inspect.getsource(recur)


class SphinxObjectFileReader:
    # Inspired by Sphinx's InventoryFileReader
    BUFSIZE = 16 * 1024

    def __init__(self, buffer: bytes):
        self.stream = io.BytesIO(buffer)

    def readline(self) -> str:
        return self.stream.readline().decode('utf-8')

    def skipline(self) -> None:
        self.stream.readline()

    def read_compressed_chunks(self) -> Generator[bytes, None, None]:
        decompressor = zlib.decompressobj()
        while True:
            chunk = self.stream.read(self.BUFSIZE)
            if len(chunk) == 0:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()

    def read_compressed_lines(self) -> Generator[str, None, None]:
        buf = b''
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b'\n')
            while pos != -1:
                yield buf[:pos].decode('utf-8')
                buf = buf[pos + 1 :]
                pos = buf.find(b'\n')


class RTFX(commands.Cog):
    """Python source and documentation for some libraries."""

    def __init__(self, bot: Ayaka):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='python', id=957325544951799848)

    def parse_object_inv(self, stream: SphinxObjectFileReader, url: str) -> dict[str, str]:
        # key: url
        # n.b.: Key doesn't have `discord` or `discord.ext.commands` namespaces
        result = {}

        # first line is version info
        inv_version = stream.readline().rstrip()

        if inv_version != '# Sphinx inventory version 2':
            raise RuntimeError('Invalid objects.inv file version.')

        # next line is '# Project: <name>'
        # then after that is '# Version: <version>'
        projname = stream.readline().rstrip()[11:]
        _ = stream.readline().rstrip()[11:]

        # next line says if it's a zlib header
        line = stream.readline()
        if 'zlib' not in line:
            raise RuntimeError('Invalid objects.inv file, not z-lib compatible.')

        # This code mostly comes form the Sphinx repository
        entry_regex = re.compile(r'(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)')
        for line in stream.read_compressed_lines():
            match = entry_regex.match(line.rstrip())
            if not match:
                continue

            name, directive, _, location, dispname = match.groups()
            domain, _, subdirective = directive.partition(':')
            if directive == 'py:module' and name in result:
                # From the Sphinx Repository:
                # due to a bug in 1.1 and below,
                # two inventory entries are created
                # for Python modules, and the first
                # one is correct
                continue

            # Most documentation pages have a label
            if directive == 'std:doc':
                subdirective = 'label'

            if location.endswith('$'):
                location = location[:-1] + name

            key = name if dispname == '-' else dispname
            prefix = f'{subdirective}:' if domain == 'std' else ''

            if projname == 'discord.py':
                key = key.replace('discord.ext.commands.', '').replace('discord.', '')
            elif projname == 'asyncpg':
                key = key.replace('asyncpg.', '')

            result[f'{prefix}{key}'] = os.path.join(url, location)
        return result

    async def build_rtfm_lookup_table(self) -> None:
        cache = {}
        for key, page in RTFM_PAGES.items():
            _ = cache[key] = {}
            async with self.bot.session.get(page + '/objects.inv') as resp:
                if resp.status != 200:
                    raise RuntimeError('Cannot build rtfm lookup table, try again later.')

                stream = SphinxObjectFileReader(await resp.read())
                cache[key] = self.parse_object_inv(stream, page)
        self._rtfm_cache = cache

    async def do_rtfm(self, ctx: Context, key: str, obj: str | None) -> None:

        if obj is None:
            await ctx.send(RTFM_PAGES[key])
            return

        if not hasattr(self, '_rtfm_cache'):
            await ctx.typing()
            await self.build_rtfm_lookup_table()

        obj = re.sub(r'^(?:discord\.(?:ext\.)?)?(?:commands\.)?(.+)', r'\1', obj)

        if key.startswith('discord.'):
            # point the abc.Messageable types properly
            q = obj.lower()
            for name in dir(discord.abc.Messageable):
                if name[0] == '_':
                    continue
                if q == name:
                    obj = f'abc.Messageable.{name}'
                    break

        cache = list(self._rtfm_cache[key].items())

        matches = fuzzy.finder(obj, cache, key=lambda t: t[0], lazy=False)

        e = discord.Embed(colour=self.bot.colour)
        if not matches:
            await ctx.send('Could not find anything. Sorry.')
            return
        e.title = f'RTFM for __**`{key}`**__: {obj}'
        e.description = '\n'.join(f'[`{key}`]({url})' for key, url in matches[:8])
        e.set_footer(text=f'{len(matches)} possible results.')
        await ctx.send(embed=e)

    async def rtfm_slash_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Degenerate case: not having built caching yet
        if not hasattr(self, '_rtfm_cache'):
            await interaction.response.autocomplete([])
            await self.build_rtfm_lookup_table()
            return []
        
        if not current:
            return []
        
        if len(current) < 3:
            return [app_commands.Choice(name=current, value=current)]
        
        assert interaction.command is not None
        key = interaction.command.name
        matches = fuzzy.finder(current, self._rtfm_cache[key], lazy=False)[:10]
        return [app_commands.Choice(name=m, value=m) for m in matches]
    
    @commands.hybrid_group(aliases=['rtfd'], fallback='python')
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm(self, ctx: Context, *, entity: str | None = None) -> None:
        """Gives you a documentation link for a Python entity."""
        await self.do_rtfm(ctx, 'python', entity)

    @rtfm.command(name='discord.py', aliases=['dpy', 'dpys'])
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_dpy(self, ctx: Context, *, entity: str | None = None) -> None:
        """Gives you a documentation link for a discord.py entity.

        Events, objects, and functions are all supported through a
        a cruddy fuzzy algorithm.
        """
        await self.do_rtfm(ctx, 'discord.py', entity)

    @rtfm.command(name='discord.py-latest', aliases=['dpym', 'dpy-latest', 'dpyl', 'dpy-master'])
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_dpy_master(self, ctx: Context, *, entity: str | None = None) -> None:
        """Gives you a documentation link for a discord.py entity (master branch)."""
        await self.do_rtfm(ctx, 'discord.py-master', entity)

    @rtfm.command(name='asyncpg')
    @app_commands.describe(entity='The object to search for')
    @app_commands.autocomplete(entity=rtfm_slash_autocomplete)
    async def rtfm_asyncpg(self, ctx: Context, *, entity: str | None = None) -> None:
        """Gives you the documentation link for an `asyncpg` entity."""
        await self.do_rtfm(ctx, 'asyncpg', entity)

    @rtfm.command(name='aiohttp')
    async def rtfm_aiohttp(self, ctx: Context, *, entity: str | None = None) -> None:
        """Gives you the documentation link for an `aiohttp` entity."""
        await self.do_rtfm(ctx, 'aiohttp', entity)

    @rtfm.command(name='refresh', with_app_command=False)
    @commands.is_owner()
    async def rtfm_refresh(self, ctx: Context) -> None:
        """Refreshes the RTFM cache."""

        async with ctx.typing():
            await self.build_rtfm_lookup_table()
        await ctx.send('\N{THUMBS UP SIGN}')

    @commands.command(name='rtfs')
    async def rtfs(
        self, ctx: Context, *, target: str | None = commands.param(converter=SourceConverter, default=None)
    ) -> None:
        if target is None:
            await ctx.send(embed=discord.Embed(title='Available sources for rtfs', description='\n'.join(RTFS)))
            return

        new_target = dedent(target)
        pages = TextPageSource(new_target, prefix='```py')
        menu = RoboPages(pages, ctx=ctx)
        await menu.start()

    @rtfs.error
    async def rtfs_error(self, ctx: Context, error: commands.CommandError) -> None:
        error = getattr(error, 'original', error)

        if isinstance(error, TypeError):
            await ctx.send('Not a valid source-able type or path.')

    @commands.command(name='pyright', aliases=['pr'])
    async def _pyright(self, ctx: Context, *, codeblock: Codeblock = commands.param(converter=codeblock_converter)) -> None:
        """
        Evaluates Python code through the latest (installed) version of Pyright on my system.
        """
        code = codeblock.content
        pyright_dump = pathlib.Path('./_pyright/')
        if not pyright_dump.exists():
            pyright_dump.mkdir(mode=0o0755, parents=True, exist_ok=True)
            conf = pyright_dump / 'pyrightconfig.json'
            conf.touch()
            with open(conf, 'w') as f:
                f.write(
                    json.dumps(
                        {
                            'pythonVersion': '3.9',
                            'typeCheckingMode': 'basic',
                            'useLibraryCodeForTypes': False,
                            'reportMissingImports': True,
                        }
                    )
                )
        await ctx.typing()
        rand = os.urandom(16).hex()
        with_file = pyright_dump / f'{rand}_tmp_pyright.py'
        with_file.touch(mode=0o0777, exist_ok=True)

        with open(with_file, 'w') as f:
            f.write(code)

        output: str = ''
        with ShellReader(f'cd _pyright && pyright --outputjson {with_file.name}') as reader:
            async for line in reader:
                if not line.startswith('[stderr] '):
                    output += line

        with_file.unlink(missing_ok=True)

        counts = {'error': 0, 'warn': 0, 'info': 0}

        data = json.loads(output)

        diagnostics = []
        for diagnostic in data['generalDiagnostics']:
            start = diagnostic['range']['start']
            start = f'{start["line"]}:{start["character"]}'

            severity = diagnostic['severity']
            if severity != 'error':
                severity = severity[:4]
            counts[severity] += 1

            prefix = ' ' if severity == 'info' else '-'
            message = diagnostic['message'].replace('\n', f'\n{prefix}')

            diagnostics.append(f'{prefix} {start} - {severity}: {message}')

        version = data['version']
        diagnostics = '\n'.join(diagnostics)
        totals = ', '.join(f'{count} {name}' for name, count in counts.items())

        fmt = to_codeblock(f'Pyright v{version}:\n\n{diagnostics}\n\n{totals}\n', language='diff', escape_md=False)
        await ctx.send(fmt)

    @commands.command()
    async def cpp(self, ctx: Context, *, query: str) -> None:
        """Search something on cppreference."""

        url = 'https://en.cppreference.com/mwiki/index.php'
        params = {
            'title': 'Special:Search',
            'search': query,
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:78.0) Gecko/20100101 Firefox/78.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
        }
        async with ctx.session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                await ctx.send(f'An error occurred (status code: {resp.status}). Retry later.')
                return
            if resp.url.path != '/mwiki/index.php':
                await ctx.send(f'<{resp.url}>')
            e = discord.Embed()
            root = etree.fromstring(await resp.text(), etree.HTMLParser())
            nodes = root.findall(".//div[@class='mw-search-result-heading']/a")
            description = []
            special_pages = []
            for node in nodes:
                href = node.attrib['href']
                if not href.startswith('/w/cpp'):
                    continue
                if href.startswith(('/w/cpp/language', '/w/cpp/concept')):
                    # special page
                    special_pages.append(f'[{node.text}](http://en.cppreference.com{href})')
                else:
                    description.append(f'[`{node.text}`](http://en.cppreference.com{href})')
            if len(special_pages) > 0:
                e.add_field(name='Language Results', value='\n'.join(special_pages), inline=False)
                if len(description):
                    e.add_field(name='Library Results', value='\n'.join(description[:10]), inline=False)
            else:
                if not len(description):
                    await ctx.send('No results found.')
                    return
                e.title = 'Search Results'
                e.description = '\n'.join(description[:15])
            e.add_field(name='See More', value=f'[`{discord.utils.escape_markdown(query)}` results]({resp.url})')
            await ctx.send(embed=e)


async def setup(bot: Ayaka):
    await bot.add_cog(RTFX(bot))
