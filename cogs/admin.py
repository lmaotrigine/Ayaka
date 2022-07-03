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
import logging
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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generator, Literal, Optional, Union

import discord
from discord import app_commands, ui
from discord.ext import commands

from bot import EXTENSIONS
from utils import db, formats
from utils.context import Context, GuildContext
from utils.paginator import RoboPages, TextPageSource


if TYPE_CHECKING:
    from types import ModuleType

    from asyncpg import Record
    from typing_extensions import Self

    from bot import Ayaka


class AuthTokens(db.Table, table_name='auth_tokens'):
    id = db.PrimaryKeyColumn()
    user_id = db.Column(db.Integer(big=True), index=True)
    guild_id = db.Column(db.Integer(big=True), index=True)
    token = db.Column(db.String, unique=True, nullable=False)
    created_at = db.Column(db.Datetime(timezone=True), default="(now() at time zone 'utc')", nullable=False)

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


MISSING = discord.utils.MISSING

log = logging.getLogger(__name__)


def trim(text: str, *, max: int = MISSING, code_block: bool = False, lang: str = 'py', end: str = '…') -> str:
    new_max: int = max or (1900 if code_block else 1990)

    trimmed = text[:new_max]

    if len(text) >= new_max:
        trimmed += end

    if code_block:
        return f'```{lang}\n{trimmed}\n```'
    return trimmed


REMOVE_CODEBLOCK = re.compile(r'```([a-zA-Z\-]+)?([\s\S]+?)```')


def remove_codeblock(text: str) -> tuple[str | None, str]:
    find = REMOVE_CODEBLOCK.match(text)
    if not find:
        return None, text
    lang, txt = find.groups()
    return lang, txt


def full_command_name(
    command: app_commands.Command[Any, Any, Any] | app_commands.Group | app_commands.ContextMenu
) -> Generator[str, Any, Any]:
    if not isinstance(command, app_commands.ContextMenu):
        if command.parent:
            yield from full_command_name(command.parent)
    yield command.name


class EvalModal(ui.Modal):
    def __init__(self, *, sql: bool = False, prev_code: str | None = None, prev_extras: str | None = None):
        super().__init__(title=('SQL' if sql else 'Python') + ' Evaluation')
        self.code = ui.TextInput(
            label='Code to evaluate', style=discord.TextStyle.paragraph, required=True, default=prev_code
        )
        self.add_item(self.code)
        self.interaction: discord.Interaction = MISSING
        self.extras: ui.TextInput = MISSING

        if sql:
            self.extras = ui.TextInput(label='Semicolon-separated SQL args', required=False, default=prev_extras)
            self.add_item(self.extras)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.stop()


class Admin(commands.GroupCog, group_name='dev'):
    """Admin-only commands that make the bot dynamic."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self._last_eval: str | None = None
        self._eval_globals: dict[str, Any] = {'bot': self.bot}
        self._last_sql: str | None = None
        self._last_sql_args: str | None = None
        self._last_result = None
        self.sessions = set()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='stafftools', id=957327255825178706)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.bot.is_owner(interaction.user):
            raise app_commands.CheckFailure('boo')
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return await interaction.response.send_message("Don't even try.", ephemeral=True)
        else:
            raise error

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.application_command:
            return

        if not interaction.command:
            name = interaction.data['name']  # type: ignore
            log.warn('Received command "%s" which was not found.', name)
            return

        command_name = ' '.join(full_command_name(interaction.command))
        command_args = ' '.join([f'{k}: {v!r}' for k, v in interaction.namespace.__dict__.items()])

        log.info('[%s/#%s/%s]: /%s %s', interaction.user, interaction.channel, interaction.guild, command_name, command_args)

    cog = app_commands.Group(name='cog', description='Cog related commands.')

    @cog.command(name='load')
    async def slash_load(self, interaction: discord.Interaction, extension: Optional[str] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        success: list[str] = []
        fail: list[str] = []

        if not extension:
            for ext in EXTENSIONS:
                if not ext in self.bot.extensions:
                    try:
                        await self.bot.load_extension(ext)
                    except Exception as e:
                        log.exception('Failed to load extension "%s"', ext, exc_info=e)
                        fail.append(f'{ext}: {e}')
                    else:
                        success.append(ext)
        else:
            try:
                await self.bot.load_extension(extension)
            except Exception as e:
                log.exception('Failed to load extension "%s"', extension, exc_info=e)
                fail.append(f'{extension}: {e}')
            else:
                success.append(extension)

        embed = discord.Embed(colour=discord.Colour.green() if not fail else discord.Colour.red())
        if success:
            embed.add_field(name='\U0001f4e5', value='\n'.join(success))
        if fail:
            embed.add_field(name='\U0001f4e4', value='\n'.join(fail))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @slash_load.autocomplete('extension')
    async def _load_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not await self.bot.is_owner(interaction.user):
            return []
        current = current.lower()
        unloaded = sorted(set(EXTENSIONS) - set(self.bot.extensions.keys()))
        return [app_commands.Choice(name=k, value=k) for k in unloaded if current in k]

    @cog.command(name='unload')
    async def slash_unload(self, interaction: discord.Interaction, extension: str) -> None:
        try:
            await self.bot.unload_extension(extension)
        except Exception as e:
            exc = traceback.format_exception(type(e), e, e.__traceback__)
            exc_text = trim(''.join(exc), code_block=True)
            await interaction.response.send_message(f'Failed to unload extension "{extension}{exc_text}"', ephemeral=True)
        else:
            await interaction.response.send_message(self.bot.emoji[True], ephemeral=True)

    @cog.command(name='reload')
    async def slash_reload(self, interaction: discord.Interaction, extension: Optional[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        success: list[str] = []
        fail: list[str] = []

        if not extension:
            for ext in EXTENSIONS:
                if not ext in self.bot.extensions:
                    try:
                        await self.bot.reload_extension(ext)
                    except Exception as e:
                        log.exception('Failed to reload extension "%s"', ext, exc_info=e)
                        fail.append(f'{ext}: {e}')
                    else:
                        success.append(ext)
        else:
            try:
                await self.bot.reload_extension(extension)
            except Exception as e:
                log.exception('Failed to reload extension "%s"', extension, exc_info=e)
                fail.append(f'{extension}: {e}')
            else:
                success.append(extension)

        embed = discord.Embed(colour=discord.Colour.green() if not fail else discord.Colour.red())
        if success:
            embed.add_field(name='\U0001f504', value='\n'.join(success))
        if fail:
            embed.add_field(name='\U0001f4e4', value='\n'.join(fail))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @slash_reload.autocomplete('extension')
    @slash_unload.autocomplete('extension')
    async def _reload_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not await self.bot.is_owner(interaction.user):
            return []
        current = current.lower()
        exts = list(self.bot.extensions.keys())
        return [app_commands.Choice(name=k, value=k) for k in sorted(exts) if current in k]

    @cog.command(name='list')
    async def _list(self, interaction: discord.Interaction) -> None:
        loaded = self.bot.extensions.keys()
        unloaded = set(EXTENSIONS) - set(loaded)
        embed = discord.Embed(colour=discord.Colour.og_blurple())
        embed.add_field(name='\U0001f4e5', value='\n'.join(loaded))
        if unloaded:
            embed.add_field(name='\U0001f4e4', value='\n'.join(unloaded))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='sync')
    async def slash_sync(self, interaction: discord.Interaction, guild_id: Optional[str] = None):
        guild: discord.Guild | None = None
        if guild_id is not None:
            new_id = int(guild_id)
            guild = self.bot.get_guild(new_id)

            if guild is None:
                await interaction.response.send_message(f'No guild with ID {guild_id} found.', ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            commands = await self.bot.tree.sync(guild=guild)
        except discord.HTTPException as e:
            exc = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            trimmed = trim(exc, code_block=True, max=1850)
            await interaction.followup.send(f'Failed to sync commands.\n{trimmed}')
        else:
            await interaction.followup.send(f'Synced `{len(commands)}` commands successfully.')

    @app_commands.command()
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message('さようなら')
        await self.bot.close()

    @app_commands.command(name='sql')
    async def slash_sql(self, interaction: discord.Interaction) -> None:
        modal = EvalModal(sql=True, prev_code=self._last_sql, prev_extras=self._last_sql_args)
        await interaction.response.send_modal(modal)
        if await modal.wait():
            return

        await modal.interaction.response.defer(ephemeral=True, thinking=True)
        self._last_sql = modal.code.value
        self._last_sql_args = modal.extras.value

        args: list[Any]
        if modal.extras.value:
            args = [
                eval(a.strip(), globals() | {'interaction': interaction, 'bot': self.bot}, {})
                for a in modal.extras.value.split(';')
            ]
        else:
            args = []

        try:
            async with self.bot.pool.acquire() as c, c.transaction():
                result = await c.fetch(modal.code.value, *args)
        except Exception as e:
            await modal.interaction.followup.send(f'```sql\n{e}\n```')
            return

        res = formats.TabularData()
        res.set_columns(list(result[0].keys()))
        res.add_rows(list(r.values()) for r in result)
        fmt = res.render() or '\u200b'
        if len(fmt) > 1900:
            await modal.interaction.followup.send(
                'Output too long...', file=discord.File(io.BytesIO(fmt.encode('utf-8')), filename='output.txt')
            )
        else:
            await modal.interaction.followup.send(f'```sql\n{fmt}\n```')

    @app_commands.command(name='eval')
    async def slash_eval(self, interaction: discord.Interaction) -> None:
        modal = EvalModal(sql=False, prev_code=self._last_eval)
        await interaction.response.send_modal(modal)
        if await modal.wait():
            return

        await modal.interaction.response.defer(ephemeral=True, thinking=True)
        self._last_eval = modal.code.value
        code = f'async def _eval_func0():\n{textwrap.indent(modal.code.value, "  ")}'  # type: ignore
        self._eval_globals['interaction'] = interaction

        lcls = {}
        try:
            exec(code, self._eval_globals | globals(), lcls)
        except Exception as e:
            fmt = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            await modal.interaction.followup.send(f'```py\n{fmt}\n```')
            return

        func: Callable[[], Awaitable[Any]] = lcls.pop('_eval_func0')
        try:
            result = await func()
        except Exception as e:
            fmt = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            if len(fmt) > 1900:
                await modal.interaction.followup.send(
                    'Error too long...', file=discord.File(io.BytesIO(fmt.encode('utf-8')), filename='error.txt')
                )
                return
            await modal.interaction.followup.send(f'```py\n{fmt}\n```')
            return

        self._eval_globals['_'] = result
        await modal.interaction.followup.send(f'```py\n{result}\n```')

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

    @commands.group(invoke_without_command=True)
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

    @sql.command(name='table')
    async def sql_table(self, ctx: Context, *, table_name: str) -> None:
        """Runs a query describing the table schema."""
        query = """SELECT column_name, data_type, column_default, is_nullable
                   FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE table_name =$1;
                """

        results = await ctx.db.fetch(query, table_name)

        headers = list(results[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in results)
        render = table.render()

        fmt = f'```\n{render}\n```'
        if len(fmt) > 2000:
            fp = io.BytesIO(fmt.encode('utf-8'))
            await ctx.send('Too many results...', file=discord.File(fp, 'results.txt'))
        else:
            await ctx.send(fmt)

    @commands.command()
    @commands.guild_only()
    async def sync(
        self, ctx: GuildContext, guilds: commands.Greedy[discord.Object], spec: Optional[Literal['~', '*', '^']] = None
    ) -> None:
        """Syncs the bot's command tree."""

        if not guilds:
            if spec == '~':
                fmt = await ctx.bot.tree.sync(guild=ctx.guild)
            elif spec == '*':
                ctx.bot.tree.copy_global_to(guild=ctx.guild)
                fmt = await self.bot.tree.sync(guild=ctx.guild)
            elif spec == '^':
                ctx.bot.tree.clear_commands(guild=ctx.guild)
                await ctx.bot.tree.sync(guild=ctx.guild)
                fmt = []
            else:
                fmt = await ctx.bot.tree.sync()
            await ctx.send(
                f'Synced {formats.plural(len(fmt)):command} {"globally" if spec is None else "to the current guild."}'
            )
            return
        fmt = 0
        for guild in guilds:
            try:
                await ctx.bot.tree.sync(guild=guild)
            except discord.HTTPException:
                pass
            else:
                fmt += 1
        await ctx.send(f'Synced the tree to {formats.plural(fmt):guild}.')

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
        new_ctx._db = ctx._db
        await self.bot.invoke(new_ctx)

    @commands.command()
    async def do(self, ctx: Context, times: int, *, command: str) -> None:
        """Repeats a command a specified number of times."""
        msg = copy.copy(ctx.message)
        msg.content = ctx.prefix + ' '.join(command)
        new_ctx = await self.bot.get_context(msg)
        new_ctx._db = ctx._db
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
        new_ctx._db = PerformanceMocker()  # type: ignore

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
