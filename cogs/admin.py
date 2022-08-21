"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import copy
import datetime  # type: ignore eval
import importlib
import inspect
import io
import os
import re
import secrets
import subprocess
import sys
import textwrap
import time
import traceback
from collections import Counter  # type: ignore eval
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

import discord
from discord.ext import commands

from bot import EXTENSIONS
from utils import formats
from utils.context import Context, GuildContext
from utils.paginator import RoboPages, TextPageSource


if TYPE_CHECKING:
    from types import ModuleType

    from asyncpg import Record
    from typing_extensions import Self

    from bot import Ayaka


class PerformanceMocker:
    """A mock object that can also be used in await expressions."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()

    def permissions_for(self, obj: Any) -> discord.Permissions:
        # Lie and say we don't have permissions to embed
        # This just makes it so pagination sessions just abruptly end on __init__
        # Most checks based on permission have a bypass for the owner anyway
        # So this lie will not affect the actual command invocation.
        perms = discord.Permissions.all()
        perms.administrator = False
        perms.embed_links = False
        perms.add_reactions = False
        return perms

    def __getattr__(self, attr: str) -> Self:
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __repr__(self) -> str:
        return '<PerformanceMocker>'

    def __await__(self):
        future: asyncio.Future[Self] = self.loop.create_future()
        future.set_result(self)
        return future.__await__()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> Self:
        return self

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False


class Admin(commands.Cog):
    """Admin-only commands that make the bot dynamic."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self._last_result = None
        self.sessions = set()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='stafftools', id=957327255825178706)

    async def run_process(self, command: str) -> list[str]:
        process = await asyncio.create_subprocess_shell(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = await process.communicate()
        return [output.decode() for output in result]

    def cleanup_code(self, content: str) -> str:
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    async def cog_check(self, ctx: Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    def get_syntax_error(self, err: SyntaxError) -> str:
        if err.text is None:
            return f'```py\n{err.__class__.__name__}: {err}\n```'
        return f'```py\n{err.text}{"^":>{err.offset}}\n{err.__class__.__name__}: {err}\n```'

    @commands.command(usage='[guild]')
    async def leave(self, ctx: GuildContext, *, guild: discord.Guild = discord.utils.MISSING) -> None:
        """Leaves a guild guild."""
        if guild is discord.utils.MISSING and ctx.guild is None:
            raise commands.NoPrivateMessage()
        guild = guild or ctx.guild
        await guild.leave()

    @leave.error
    async def leave_Error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))

    @commands.command()
    async def load(self, ctx: Context, *, module: str) -> None:
        """Loads a module."""
        if module != 'jishaku' and not module.startswith('cogs.'):
            module = f'cogs.{module}'

        try:
            await self.bot.load_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f'{err.__class__.__name__}: {err}')
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.command()
    async def unload(self, ctx: Context, *, module: str) -> None:
        """Unloads a module."""
        if module != 'jishaku' and not module.startswith('cogs.'):
            module = f'cogs.{module}'

        try:
            await self.bot.unload_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f'{err.__class__.__name__}: {err}')
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.command(aliases=['cogs'])
    async def extensions(self, ctx: Context) -> None:
        """Lists the bot's extensions"""
        loaded = set(self.bot.extensions.keys())
        _all = set(EXTENSIONS)
        unloaded = _all - loaded
        embed = discord.Embed(colour=discord.Colour.og_blurple())
        embed.add_field(name='\U0001f4e6 Loaded', value='\n'.join(sorted(loaded)))
        embed.add_field(name='\U0001f4e7 Unloaded', value='\n'.join(sorted(unloaded)))
        await ctx.send(embed=embed)

    def get_submodules_from_extension(self, module: ModuleType) -> set[str]:
        submodules = set()
        for _, obj in inspect.getmembers(module):
            if inspect.ismodule(obj):
                submodules.add(obj.__name__)
            else:
                try:
                    mod = obj.__module__
                except AttributeError:
                    pass
                else:
                    submodules.add(mod)
        submodules.discard(module.__name__)
        return submodules

    @commands.group(invoke_without_command=True)
    async def reload(self, ctx: Context, *, module: str) -> None:
        """Reloads a module."""
        if module != 'jishaku' and not module.startswith('cogs.'):
            module = f'cogs.{module}'
        try:
            ext = self.bot.extensions[module]
        except KeyError:
            return await ctx.invoke(self.load, module=module)

        submodules = self.get_submodules_from_extension(ext)
        for mod in submodules:
            if not mod.startswith('utils.'):
                continue

            try:
                importlib.reload(sys.modules[mod])
            except KeyError:
                pass
        try:
            await self.bot.reload_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f'{err.__class__.__name__}: {err}')
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    _GIT_PULL_REGEX = re.compile(r'\s*(?P<filename>.+?)\s*\|\s*[0-9]+\s*[+-]+')

    def find_modules_from_git(self, output: str) -> list[tuple[int, str]]:
        files = self._GIT_PULL_REGEX.findall(output)
        ret = []
        for file in files:
            root, ext = os.path.splitext(file)
            if ext != '.py':
                continue
            if root.startswith('utils/'):
                ret.append((1, root.replace('/', '.')))
            elif root.startswith('cogs/'):
                ret.append((0, root.replace('/', '.')))
        ret.sort(reverse=True)
        return ret

    async def reload_or_load_extension(self, module: str) -> None:
        try:
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(module)

    @reload.command(name='all')
    async def reload_all(self, ctx: Context) -> None:
        """Reloads all modules, while pulling from git."""

        async with ctx.typing():
            stdout, _ = await self.run_process('git pull')

        if stdout.startswith('Already up to date.'):
            await ctx.send(stdout)
            return

        modules = self.find_modules_from_git(stdout)
        mods_text = '\n'.join(f'{index}. `{module}`' for index, (_, module) in enumerate(modules, start=1))
        prompt_text = f'This will update the following modules. Are you sure?\n{mods_text}'
        confirm = await ctx.prompt(prompt_text)
        if not confirm:
            await ctx.send('Aborting.')
            return

        statuses = []
        for is_submodule, module in modules:
            if is_submodule:
                try:
                    actual_module = sys.modules[module]
                except KeyError:
                    statuses.append((ctx.tick(None), module))
                else:
                    try:
                        importlib.reload(actual_module)
                    except Exception:
                        statuses.append((ctx.tick(False), module))
                    else:
                        statuses.append((ctx.tick(True), module))
            else:
                try:
                    await self.reload_or_load_extension(module)
                except commands.ExtensionError:
                    statuses.append((ctx.tick(False), module))
                else:
                    statuses.append((ctx.tick(True), module))
        await ctx.send('\n'.join(f'{status}: `{module}`' for status, module in statuses))

    @commands.command(name='eval')
    async def _eval(self, ctx: Context, *, body: str) -> None:
        """Evaluates a code"""

        env = {
            'bot': self.bot,
            'ctx': ctx,
            'channel': ctx.channel,
            'author': ctx.author,
            'guild': ctx.guild,
            'message': ctx.message,
            '_': self._last_result,
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:
            await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')
            return

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()  # type: ignore
        except Exception as e:
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except:
                pass

            if ret is None:
                if value:
                    await ctx.send(f'```py\n{value}\n```')
            else:
                self._last_result = ret
                await ctx.send(f'```py\n{value}{ret}\n```')

    @commands.command()
    async def repl(self, ctx: Context) -> None:
        """Launches an interactive REPL session."""
        variables = {
            'ctx': ctx,
            'bot': self.bot,
            'message': ctx.message,
            'guild': ctx.guild,
            'channel': ctx.channel,
            'author': ctx.author,
            '_': None,
        }

        if ctx.channel.id in self.sessions:
            await ctx.send('Already running a REPL session in this channel. Exit it with `quit`.')
            return

        self.sessions.add(ctx.channel.id)
        await ctx.send('Enter code to execute or evaluate. `exit()` or `quit` to exit.')

        def check(m: discord.Message) -> bool:
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and m.content.startswith('`')

        while True:
            try:
                response = await self.bot.wait_for('message', check=check, timeout=10.0 * 60.0)
            except asyncio.TimeoutError:
                await ctx.send('Exiting REPL session.')
                self.sessions.remove(ctx.channel.id)
                break

            cleaned = self.cleanup_code(response.content)

            if cleaned in ('quit', 'exit', 'exit()'):
                await ctx.send('Exiting.')
                self.sessions.remove(ctx.channel.id)
                return

            executor = exec
            if cleaned.count('\n') == 0:
                # single statement, potentially 'eval'
                try:
                    code = compile(cleaned, '<repl session>', 'eval')
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is exec:
                try:
                    code = compile(cleaned, '<repl session>', 'exec')
                except SyntaxError as e:
                    await ctx.send(self.get_syntax_error(e))
                    continue

            variables['message'] = response

            fmt = None
            stdout = io.StringIO()

            try:
                with redirect_stdout(stdout):
                    result = executor(code, variables)  # type: ignore
                    if inspect.isawaitable(result):
                        result = await result
            except Exception as e:
                value = stdout.getvalue()
                fmt = f'```py\n{value}{traceback.format_exc()}\n```'
            else:
                value = stdout.getvalue()
                if result is not None:
                    fmt = f'```py\n{value}{result}\n```'
                    variables['_'] = result
                elif value:
                    fmt = f'```py\n{value}\n```'

            try:
                if fmt is not None:
                    if len(fmt) > 2000:
                        await ctx.send('Content too big to be printed.')
                    else:
                        await ctx.send(fmt)
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(f'Unexpected error: `{e}`')

    @commands.group(hidden=True, invoke_without_command=True)
    async def sql(self, ctx: Context, *, query: str) -> None:
        """Run some SQL."""
        query = self.cleanup_code(query)

        is_multistatement = query.count(';') > 1
        strategy: Callable[[str], Awaitable[list[Record]] | Awaitable[str]]
        if is_multistatement:
            # fetch does not support multiple statements
            strategy = ctx.db.execute
        else:
            strategy = ctx.db.fetch

        try:
            start = time.perf_counter()
            results = await strategy(query)
            dati = (time.perf_counter() - start) * 1000.0
        except Exception:
            await ctx.send(f'```py\n{traceback.format_exc()}\n```')
            return

        rows = len(results)
        if isinstance(results, str) or rows == 0:
            await ctx.send(f'`{dati:.2f}ms: {results}`')
            return

        headers = list(results[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in results)
        render = table.render()

        fmt = f'```\n{render}\n```\n*Returned {formats.plural(rows):row} in {dati:.2f}ms*'
        if len(fmt) > 2000:
            fp = io.BytesIO(fmt.encode('utf-8'))
            await ctx.send('Too many results...', file=discord.File(fp, 'results.txt'))
        else:
            await ctx.send(fmt)

    async def send_sql_results(self, ctx: Context, records: list[Any]):
        headers = list(records[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in records)
        render = table.render()

        fmt = f'```\n{render}\n```'
        if len(fmt) > 2000:
            fp = io.BytesIO(fmt.encode('utf-8'))
            await ctx.send('Too many results...', file=discord.File(fp, 'results.txt'))
        else:
            await ctx.send(fmt)

    @sql.command(name='schema', hidden=True)
    async def sql_schema(self, ctx: Context, *, table_name: str) -> None:
        """Runs a query describing the table schema."""
        query = """SELECT column_name, data_type, column_default, is_nullable
                   FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE table_name =$1;
                """

        results = await ctx.db.fetch(query, table_name)

        if len(results) == 0:
            await ctx.send('Could not find a table with that name.')
            return

        await self.send_sql_results(ctx, results)

    @sql.command(name='tables', hidden=True)
    async def sql_tables(self, ctx: Context) -> None:
        """Lists all SQL tables in the database."""

        query = """SELECT table_name
                   FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
                """

        results = await ctx.db.fetch(query)

        if len(results) == 0:
            await ctx.send('Could not find any tables')
            return

        await self.send_sql_results(ctx, results)

    @sql.command(name='sizes', hidden=True)
    async def sql_sizes(self, ctx: Context) -> None:
        """Display how much space the database is taking up."""

        # Credit: https://wiki.postgresql.org/wiki/Disk_Usage
        query = """
            SELECT nspname || '.' || relname AS "relation",
                pg_size_pretty(pg_relation_size(C.oid)) AS "size"
            FROM pg_class C
            LEFT JOIN pg_namespace N ON (N.oid = C.relnamespace)
            WHERE nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY pg_relation_size(C.oid) DESC;
            LIMIT 20;
        """

        results = await ctx.db.fetch(query)

        if len(results) == 0:
            await ctx.send('Could not find any tables')
            return

        await self.send_sql_results(ctx, results)

    @commands.group()
    @commands.guild_only()
    async def sync(self, ctx: GuildContext, guild_id: int | None, copy: bool = False) -> None:
        """Syncs the slash commands with the given guild."""

        if guild_id:
            guild = discord.Object(id=guild_id)
        else:
            guild = ctx.guild

        if copy:
            self.bot.tree.copy_global_to(guild=guild)

        commands = await self.bot.tree.sync(guild=guild)
        await ctx.send(f'Successfully synced {len(commands)} commands.')

    @sync.command(name='global')
    async def sync_global(self, ctx: Context) -> None:
        """Syncs the slash commands globally."""

        commands = await self.bot.tree.sync(guild=None)
        await ctx.send(f'Successfully synced {len(commands)} commands.')

    @commands.command()
    async def sudo(
        self, ctx: Context, channel: discord.TextChannel | None, who: Union[discord.Member, discord.User], *, command: str
    ) -> None:
        """Run a command as another user optionally in another channel."""
        msg = copy.copy(ctx.message)
        new_channel = channel or ctx.channel
        msg.channel = new_channel
        msg.author = who
        msg.content = ctx.prefix + command
        new_ctx = await self.bot.get_context(msg)
        await self.bot.invoke(new_ctx)

    @commands.command()
    async def do(self, ctx: Context, times: int, *, command: str) -> None:
        """Repeats a command a specified number of times."""
        msg = copy.copy(ctx.message)
        msg.content = ctx.prefix + ' '.join(command)
        new_ctx = await self.bot.get_context(msg)
        for _ in range(times):
            await new_ctx.reinvoke()

    @commands.command()
    async def sh(self, ctx: Context, *, command: str) -> None:
        """Runs a shell command."""

        async with ctx.typing():
            stdout, stderr = await self.run_process(command)

        if stderr:
            text = f'stdout:\n{stdout}\nstderr:\n{stderr}'
        else:
            text = stdout

        pages = RoboPages(TextPageSource(text), ctx=ctx)
        await pages.start()

    @commands.command()
    async def perf(self, ctx: Context, *, command: str) -> None:
        """Checks the timing of a command, attempting to suppress HTTP and DB calls."""

        msg = copy.copy(ctx.message)
        msg.content = ctx.prefix + command

        new_ctx = await self.bot.get_context(msg)

        # Intercepts the Messageable interface a bit
        new_ctx._state = PerformanceMocker()  # type: ignore
        new_ctx.channel = PerformanceMocker()  # type: ignore

        if new_ctx.command is None:
            await ctx.send('No command found.')
            return

        start = time.perf_counter()
        try:
            await new_ctx.command.invoke(new_ctx)
        except commands.CommandError:
            end = time.perf_counter()
            success = False
            try:
                await ctx.send(f'```py\n{traceback.format_exc()}\n```')
            except discord.HTTPException:
                pass
        else:
            end = time.perf_counter()
            success = True

        await ctx.send(f'Status: {ctx.tick(success)} Time: {(end - start) * 1000:.2f}ms')

    @commands.group()
    async def whitelist(self, ctx: Context) -> None:
        """Manages the guild whitelist."""

    @whitelist.command()
    async def gen(self, ctx: Context, guild_id: int, *, user: discord.User) -> None:
        """Generates a token for this guild and user."""
        token = secrets.token_hex(64)
        query = """
                INSERT INTO auth_tokens (guild_id, user_id, token, created_at)
                VALUES ($1, $2, $3, $4);
                """
        await ctx.db.execute(query, guild_id, user.id, token, discord.utils.utcnow())
        await ctx.message.add_reaction(ctx.tick(True))


async def setup(bot: Ayaka):
    await bot.add_cog(Admin(bot))
