#!/usr/bin/env python
"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""


from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Callable, Coroutine, Generator, Iterable, ParamSpec, TypeVar

import click

import config
from bot import EXTENSIONS, Ayaka
from utils.db import Table


if TYPE_CHECKING:
    import asyncpg


T = TypeVar('T')
P = ParamSpec('P')

try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


def coroutine(func: Callable[P, Coroutine[None, None, T]]) -> Callable[P, T]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


class RemoveNoise(logging.Filter):
    def __init__(self) -> None:
        super().__init__(name='discord.state')

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelname == 'WARNING' and 'referencing an unknown' in record.msg:
            return False
        return True


@contextlib.contextmanager
def setup_logging(name='bot') -> Generator[None, None, None]:
    log = logging.getLogger()
    try:
        # __enter__
        if name == 'bot':
            logging.getLogger('discord').setLevel(logging.INFO)
            logging.getLogger('discord.http').setLevel(logging.WARNING)
            logging.getLogger('discord.state').addFilter(RemoveNoise())
            logging.getLogger('mangadex.http').setLevel(logging.DEBUG)
            handler = RotatingFileHandler(
                filename='ayaka.log', encoding='utf-8', mode='w', maxBytes=32 * 1024 * 1024, backupCount=5
            )
        else:
            handler = RotatingFileHandler(
                filename='ayaka-web.log', encoding='utf-8', mode='w', maxBytes=32 * 1024 * 1024, backupCount=5
            )
        log.setLevel(logging.INFO)
        dt_fmt = '%Y-%m-%d %H:%M:%S'
        fmt = logging.Formatter('[{asctime}] [{levelname:<7}] {name}: {message}', dt_fmt, style='{')
        handler.setFormatter(fmt)
        log.addHandler(handler)
        yield
    finally:
        # __exit__
        handlers = log.handlers[:]
        for hdlr in handlers:
            hdlr.close()
            log.removeHandler(hdlr)


async def run_bot() -> None:
    log = logging.getLogger()
    async with Ayaka() as bot:
        try:
            pool = await Table.create_pool(config.postgresql, command_timeout=60, max_inactive_connection_lifetime=0)
        except Exception:
            click.echo('Could not set up PostgreSQL. Exiting.', file=sys.stderr)
            log.exception('Could not set up PostgreSQL. Exiting.')
            raise RuntimeError('Could not set up PostgreSQL. Exiting.')

        if pool is None:
            raise RuntimeError('Setting up PostgreSQL pool failed.')
        bot.pool = pool
        for extension in EXTENSIONS:
            try:
                await bot.load_extension(extension)
            except Exception:
                print(f'Failed to load extension {extension}.', file=sys.stderr)
                traceback.print_exc()
        await bot.start()


@click.group(invoke_without_command=True, options_metavar='[options]')
@click.pass_context
def main(ctx: click.Context) -> None:
    """Launches the bot."""
    if ctx.invoked_subcommand is None:
        with setup_logging():
            asyncio.run(run_bot())


@main.group(short_help='database stuff', options_metavar='[options]')
def db() -> None:
    pass


@db.command(short_help='initialises the database for the bot', options_metavar='[options]')
@click.argument('cogs', nargs=-1, metavar='[cogs]')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
@coroutine
async def init(cogs: Iterable[str], quiet: bool) -> None:
    """This manages the migrations and database creation system for you."""

    try:
        await Table.create_pool(config.postgresql)
    except Exception:
        click.echo(f'Could not create PostgreSQL conenction pool.\n{traceback.format_exc()}', err=True)
        return

    if not cogs:
        cogs = EXTENSIONS
    else:
        cogs = [f'cogs.{e}' if not e.startswith('cogs.') else e for e in cogs]

    for ext in cogs:
        try:
            importlib.import_module(ext)
        except Exception:
            click.echo(f'Could not load {ext}.\n{traceback.format_exc()}', err=False)
            continue

    for table in Table.all_tables():
        try:
            created = await table.create(verbose=not quiet, run_migrations=False)
        except Exception:
            click.echo(f'Could not create {table.__tablename__}.\n{traceback.format_exc()}', err=True)
        else:
            if created:
                click.echo(f'[{table.__module__}] Created {table.__tablename__}.')
            else:
                click.echo(f'[{table.__module__}] No work needed for {table.__tablename__}')


@db.command(short_help='migrates the database')
@click.argument('cog', nargs=1, metavar='cog')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
@click.pass_context
def migrate(ctx: click.Context, cog: str, quiet: bool):
    """Update the migration file with the newest schema."""

    if not cog.startswith('cogs.'):
        cog = f'cogs.{cog}'

    try:
        importlib.import_module(cog)
    except Exception:
        click.echo(f'Could not load {cog}.\n{traceback.format_exc()}', err=True)
        return

    def work(table: type[Table], *, invoked: bool = False) -> None:
        try:
            actually_migrated = table.write_migration()
        except RuntimeError as e:
            click.echo(f'Could not migrate {table.__tablename__}: {e}', err=True)
            if not invoked:
                click.confirm('do you want to create the table?', abort=True)
                ctx.invoke(init, cogs=[cog], quiet=quiet)
                work(table, invoked=True)
            sys.exit(-1)
        else:
            if actually_migrated:
                click.echo(f'Successfully updated migrations for {table.__tablename__}.')
            else:
                click.echo(f'Found no changes for {table.__tablename__}.')

    for table in Table.all_tables():
        work(table)

    click.echo(f'Done migrating {cog}.')


async def apply_migration(cog: str, quiet: bool, index: int, *, downgrade: bool = False) -> None:
    try:
        pool = await Table.create_pool(config.postgresql)
    except Exception:
        click.echo(f'Could not create PostgreSQL connection pool.\n{traceback.format_exc()}', err=True)
        return
    assert pool is not None  # thanks, asyncpg

    if not cog.startswith('cogs.'):
        cog = f'cogs.{cog}'

    try:
        importlib.import_module(cog)
    except Exception:
        click.echo(f'Could not load {cog}.\n{traceback.format_exc()}', err=True)
        return

    async with pool.acquire() as con:
        tr = con.transaction()
        await tr.start()
        for table in Table.all_tables():
            try:
                await table.migrate(index=index, downgrade=downgrade, verbose=not quiet, connection=con)
            except RuntimeError as e:
                click.echo(f'Could not migrate {table.__tablename__}: {e}', err=True)
                await tr.rollback()
                break
        else:
            await tr.commit()


@db.command(short_help='upgrades from a migration')
@click.argument('cog', nargs=1, metavar='[cog]')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
@click.option('--index', help='the index to use', default=-1)
@coroutine
async def upgrade(cog: str, quiet: bool, index: int) -> None:
    """Runs an upgrade from a migration."""
    await apply_migration(cog, quiet, index)


@db.command(short_help='downgrades from a migration')
@click.argument('cog', nargs=1, metavar='[cog]')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
@click.option('--index', help='the index to use', default=-1)
@coroutine
async def downgrade(cog: str, quiet: bool, index: int) -> None:
    """Runs a downgrade from a migration."""
    await apply_migration(cog, quiet, index, downgrade=True)


async def remove_tables(pool: asyncpg.Pool, cog: str, quiet: bool) -> None:
    async with pool.acquire() as con:
        tr = con.transaction()
        await tr.start()
        for table in Table.all_tables():
            try:
                await table.drop(verbose=not quiet, connection=con)
            except RuntimeError as e:
                click.echo(f'Could not drop {table.__tablename__}: {e}', err=True)
                await tr.rollback()
                break
            else:
                click.echo(f'Dropped {table.__tablename__}')
        else:
            await tr.commit()
            click.echo(f'Successfully removed {cog} tables.')


@db.command(short_help="removes a cog's tables", options_metavar='[options]')
@click.argument('cog', metavar='<cog>')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
@coroutine
async def drop(cog: str, quiet: bool) -> None:
    """This removes a table and all its migrations.

    You must be pretty sure about this before you do it,
    as once you do it there's no coming back.

    Also note that the name must be the table name, not
    the cog name.
    """

    click.confirm('do you really want to do this?', abort=True)

    try:
        pool = await Table.create_pool(config.postgresql)
    except Exception:
        click.echo(f'Could not created PostgreSQL connection pool.\n{traceback.format_exc()}', err=True)
        return
    assert pool is not None  # thanks asyncpg

    if not cog.startswith('cogs.'):
        cog = f'cogs.{cog}'

    try:
        importlib.import_module(cog)
    except Exception:
        click.echo(f'Could not load {cog}.\n{traceback.format_exc()}', err=True)
        return

    await remove_tables(pool, cog, quiet)


if __name__ == '__main__':
    main()
