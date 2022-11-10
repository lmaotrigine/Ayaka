"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import pathlib
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Coroutine, Iterable, TypeVar

import aiohttp
import discord
import mangadex
import nhentai
import redis.asyncio as aioredis
from discord.ext import commands

import config
from dashboard.routes import setup_routes
from dashboard.utils.http import HTTPClient
from utils.config import Config
from utils.context import Context


try:
    import tornado.httpserver
    import tornado.web
except ImportError:
    SETUP_WEB = False
else:
    SETUP_WEB = True


if TYPE_CHECKING:
    from asyncpg import Pool

    from cogs.config import Config as ConfigCog
    from cogs.logging import Logging as LoggingCog
    from cogs.reminders import Reminder
    
    ContextT = TypeVar('ContextT', bound=Context)

DESCRIPTION = """
Hello! I'm a bot written by VJ#5945 to provide some nice utilities.
"""

log = logging.getLogger(__name__)

EXTENSIONS: tuple[str, ...] = (
    'jishaku',
    'cogs.admin',
    'cogs.anime',
    'cogs.ayaka',
    'cogs.config',
    'cogs.dnd',
    'cogs.emoji',
    'cogs.feeds',
    'cogs.fun',
    'cogs.lewd',
    'cogs.logging',
    'cogs.manga',
    'cogs.meta',
    'cogs.minigames',
    'cogs.mod',
    'cogs.nihongo',
    'cogs.poll',
    'cogs.reminders',
    'cogs.rng',
    'cogs.rtfx',
    'cogs.snipe',
    'cogs.stalking',
    'cogs.stars',
    'cogs.stats',
    'cogs.synth',
    'cogs.tags',
    'cogs.tiktok',
    'cogs.time',
    'cogs.todo',
    # 'cogs.private.quiz',
    'cogs.private.cotd',
    'cogs.private.ims',
    'cogs.private.private',
    'cogs.private.logging',
    'cogs.private.games',
    'cogs.private.roles',
    'cogs.private.scotrail',
    'cogs.private.dungeon',
)


def _prefix_callable(bot: Ayaka, msg: discord.Message) -> list[str]:
    if msg.guild is None:
        return commands.when_mentioned_or('hey babe ')(bot, msg)
    else:
        prefs: list[str] | None = bot.prefixes.get(msg.guild.id)
        if prefs is None:
            prefs = ['hey babe ']
        return commands.when_mentioned_or(*prefs)(bot, msg)


class ProxyObject(discord.Object):
    def __init__(self, guild: discord.abc.Snowflake | None) -> None:
        super().__init__(id=0)
        self.guild: discord.abc.Snowflake | None = guild


class Ayaka(commands.AutoShardedBot):
    pool: Pool
    _original_help_command: commands.HelpCommand | None
    user: discord.ClientUser  # typechecker lie
    command_stats: Counter[str]
    socket_stats: Counter[str]
    command_types_used: Counter[bool]
    logging_handler: Any
    bot_app_info: discord.AppInfo
    old_tree_error: Callable[[discord.Interaction, discord.app_commands.AppCommandError], Coroutine[Any, Any, None]]

    def __init__(self):
        intents = discord.Intents.all()

        super().__init__(
            command_prefix=_prefix_callable,
            description=DESCRIPTION,
            allowed_mentions=discord.AllowedMentions.none(),
            pm_help=None,
            help_attrs=dict(hidden=True),
            heartbeat_timeout=150.0,
            intents=intents,
            application_id=config.application_id,
            enable_debug_events=True,
        )
        self.session = aiohttp.ClientSession()
        self.dashboard_client = HTTPClient(self)
        self.redis = aioredis.from_url(config.redis, encoding='utf-8', decode_responses=True)
        self.hentai_client = nhentai.Client()
        md_user = config.mangadex_auth['username']
        assert md_user is not None
        md_pass = config.mangadex_auth['password']
        assert md_pass is not None
        md_token = config.mangadex_auth['refresh_token']
        self.manga_client = mangadex.Client(username=md_user, password=md_pass, refresh_token=md_token)
        self.resumes: defaultdict[int, list[datetime.datetime]] = defaultdict(list)
        self.identifies: defaultdict[int, list[datetime.datetime]] = defaultdict(list)
        if SETUP_WEB:
            self.dashboard = tornado.web.Application(
                setup_routes(bot=self),
                static_path=str(pathlib.Path(__file__).parent / 'static'),
                template_path=str(pathlib.Path(__file__).parent / 'dashboard' / 'templates'),
                cookie_secret=config.cookie_secret,
                debug=False,
                default_host=config.base_url,
            )
            self.server = tornado.httpserver.HTTPServer(self.dashboard, xheaders=True)

        self.emoji = {
            True: '<:yes:956843604620476457>',
            False: '<:no:956843604972826664>',
            None: '<:none:956843605010567178>',
        }
        self.colour: discord.Colour = discord.Colour(0xEC9FED)

        # in case of further spam, add a cooldown mapping
        # for people who excessively spam commands
        self.spam_control = commands.CooldownMapping.from_cooldown(10, 12.0, commands.BucketType.user)

        # A counter to auto ban frequent spammers
        # Triggering the rate limit 5 times in a row will auto-ban the user from the bot.
        self._auto_spam_count = Counter()

    async def setup_hook(self) -> None:
        self.prefixes: Config[list[str]] = Config(pathlib.Path('configs/prefixes.json'))
        self.blacklist: Config[bool] = Config(pathlib.Path('configs/blacklist.json'))
        self.bot_app_info = await self.application_info()
        self.owner_id = self.bot_app_info.owner.id
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
            except Exception:
                log.exception('Failed to load extension %s', extension)
        if SETUP_WEB:
            self.server.bind(6789)
            self.server.start()

    @property
    def owner(self) -> discord.User:
        return self.bot_app_info.owner

    def _clear_gateway_data(self) -> None:
        one_week_ago = discord.utils.utcnow() - datetime.timedelta(days=7)
        for _, dates in self.identifies.items():
            to_remove = [index for index, dt in enumerate(dates) if dt < one_week_ago]
            for index in reversed(to_remove):
                del dates[index]

        for _, dates in self.resumes.items():
            to_remove = [index for index, dt in enumerate(dates) if dt < one_week_ago]
            for index in reversed(to_remove):
                del dates[index]

    async def before_identify_hook(self, shard_id: int, *, initial: bool = False) -> None:
        self._clear_gateway_data()
        self.identifies[shard_id].append(discord.utils.utcnow())
        await super().before_identify_hook(shard_id, initial=initial)

    async def on_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send('This command cannot be used in private messages.')
        elif isinstance(error, commands.DisabledCommand):
            await ctx.send('Sorry. This command is disabled and cannot be used.')
        elif isinstance(error, commands.MissingRequiredArgument):
            if ctx.command.extras.get('handled') is not True:
                await ctx.send(f'Missing required argument `{error.param.name}` for command `{ctx.command}`')
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if not isinstance(original, discord.HTTPException):
                log.exception('In %s:', ctx.command.qualified_name, exc_info=original)
        elif isinstance(error, commands.ArgumentParsingError):
            await ctx.send(str(error))

    def get_guild_prefixes(
        self,
        guild: discord.abc.Snowflake | None,
        *,
        local_inject: Callable[[Ayaka, discord.Message], list[str]] = _prefix_callable,
    ) -> list[str]:
        proxy_msg = ProxyObject(guild=guild)
        return local_inject(self, proxy_msg)  # type: ignore # can't narrow?

    def get_raw_guild_prefixes(self, guild_id: int) -> list[str]:
        return self.prefixes.get(guild_id, ['hey babe '])

    async def set_guild_prefixes(self, guild: discord.abc.Snowflake, prefixes: list[str]) -> None:
        if not prefixes:
            await self.prefixes.put(str(guild.id), [])
        elif len(prefixes) > 10:
            raise RuntimeError('Cannot have more than 10 custom prefixes.')
        else:
            await self.prefixes.put(str(guild.id), sorted(set(prefixes), reverse=True))

    async def add_to_blacklist(self, object_id: int) -> None:
        await self.blacklist.put(str(object_id), True)

    async def remove_from_blacklist(self, object_id: int) -> None:
        try:
            await self.blacklist.remove(str(object_id))
        except KeyError:
            pass

    async def get_or_fetch_member(self, guild: discord.Guild, member_id: int) -> discord.Member | None:
        member = guild.get_member(member_id)
        if member is not None:
            return member

        shard: discord.ShardInfo = self.get_shard(guild.shard_id)  # type: ignore  # will never be None
        if shard.is_ws_ratelimited():
            try:
                member = await guild.fetch_member(member_id)
            except discord.HTTPException:
                return None
            else:
                return member

        members = await guild.query_members(limit=1, user_ids=[member_id], cache=True)
        if not members:
            return None
        return members[0]

    async def resolve_member_ids(self, guild: discord.Guild, member_ids: Iterable[int]) -> AsyncIterator[discord.Member]:
        needs_resolution = []
        for member_id in member_ids:
            member = guild.get_member(member_id)
            if member is not None:
                yield member
            else:
                needs_resolution.append(member)

        total_needs_resolution = len(needs_resolution)
        if total_needs_resolution == 1:
            shard = self.get_shard(guild.shard_id)
            assert shard is not None
            if shard.is_ws_ratelimited():
                try:
                    member = await guild.fetch_member(needs_resolution[0])
                except discord.HTTPException:
                    pass
                else:
                    yield member
            else:
                members = await guild.query_members(limit=1, user_ids=needs_resolution, cache=True)
                if members:
                    yield members[0]
        elif total_needs_resolution <= 100:
            # Only a single resolution call needed here
            resolved = await guild.query_members(limit=100, user_ids=needs_resolution, cache=True)
            for member in resolved:
                yield member
        else:
            # We need to chunk these in bits of 100...
            for index in range(0, total_needs_resolution, 100):
                to_resolve = needs_resolution[index : index + 100]
                members = await guild.query_members(limit=100, user_ids=to_resolve, cache=True)
                for member in members:
                    yield member

    async def on_ready(self) -> None:
        if not hasattr(self, 'uptime'):
            self.uptime = discord.utils.utcnow()
        log.info('Ready: %s (ID: %s)', self.user, self.user.id)

    async def on_shard_resumed(self, shard_id: int):
        print('Shard ID %s has resumed...', shard_id)
        self.resumes[shard_id].append(discord.utils.utcnow())

    @discord.utils.cached_property
    def stat_webhook(self) -> discord.Webhook:
        hook = discord.Webhook.from_url(config.stat_webhook, session=self.session)
        return hook

    async def log_spammer(
        self, ctx: Context, message: discord.Message, retry_after: float, *, autoblock: bool = False
    ) -> discord.WebhookMessage | None:
        guild_name = getattr(ctx.guild, 'name', 'No guild (DMs)')
        guild_id = getattr(ctx.guild, 'id', None)
        fmt = 'User %s (ID %s) in guild %r (ID %s) spamming, retry_after: %.2fs'
        log.warning(fmt, message.author, message.author.id, guild_name, guild_id, retry_after)
        if not autoblock:
            return

        webhook = self.stat_webhook
        embed = discord.Embed(title='Auto-blocked Member', colour=0xDDA453)
        embed.add_field(name='Member', value=f'{message.author} (ID: {message.author.id})', inline=False)
        embed.add_field(name='Guild Info', value=f'{guild_name} (ID: {guild_id}', inline=False)
        embed.add_field(name='Channel Info', value=f'{message.channel} (ID: {message.channel.id})', inline=False)
        embed.timestamp = discord.utils.utcnow()
        return await webhook.send(embed=embed, wait=True)

    async def get_context(self, origin: discord.Message | discord.Interaction, /, *, cls: type[ContextT]=Context) -> ContextT:
        return await super().get_context(origin, cls=cls)  # type: ignore # not sure

    async def process_commands(self, message: discord.Message) -> None:
        ctx: Context = await self.get_context(message)

        if ctx.command is None:
            return

        if ctx.author.id in self.blacklist:
            return

        if ctx.guild is not None and ctx.guild.id in self.blacklist:
            return

        bucket = self.spam_control.get_bucket(message)
        current = message.created_at.replace(tzinfo=datetime.timezone.utc).timestamp()
        retry_after = bucket and bucket.update_rate_limit(current)
        author_id = message.author.id
        if retry_after and author_id != self.owner_id:
            self._auto_spam_count[author_id] += 1
            if self._auto_spam_count[author_id] >= 5:
                await self.add_to_blacklist(author_id)
                del self._auto_spam_count[author_id]
                await self.log_spammer(ctx, message, retry_after, autoblock=True)
            else:
                await self.log_spammer(ctx, message, retry_after)
            return
        else:
            self._auto_spam_count.pop(author_id, None)

        await self.invoke(ctx)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self.process_commands(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.id == self.owner_id:
            if not before.embeds and after.embeds:
                return
            await self.process_commands(after)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id in self.blacklist:
            await guild.leave()

    async def close(self) -> None:
        await asyncio.gather(
            super().close(),
            self.session.close(),
            self.manga_client.close(),
            self.hentai_client.close(),
        )
        await super().close()

    def run(self) -> None:
        raise NotImplementedError('Use `start` instead.')

    async def start(self) -> None:
        await super().start(config.token, reconnect=True)

    async def get_user_avatars(self, user_id: int) -> dict[str, str | list[str]] | None:
        if self.logging_cog is None:
            return None
        return await self.logging_cog.avatar_history(user_id)

    @property
    def config(self) -> config:  # type: ignore # this can be used as a type but idk if it's best practice
        return __import__('config')

    @property
    def reminder(self) -> Reminder | None:
        return self.get_cog('Reminder')  # type: ignore # ???

    @property
    def config_cog(self) -> ConfigCog | None:
        return self.get_cog('Config')  # type: ignore # ???

    @property
    def logging_cog(self) -> LoggingCog | None:
        return self.get_cog('Logging')  # type: ignore # ???
