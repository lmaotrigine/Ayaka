"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import io
import logging
import re
import shlex
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Callable, Literal, List, MutableMapping, Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from typing_extensions import Annotated

from utils import cache, checks, flags, time
from utils.formats import plural


if TYPE_CHECKING:
    from bot import Ayaka
    from cogs.reminders import Timer
    from utils.context import Context, GuildContext

    class ModGuildContext(GuildContext):
        cog: Mod
        guild_config: ModConfig


log = logging.getLogger(__name__)

## Misc utilities


class Arguments(argparse.ArgumentParser):
    def error(self, message: str):
        raise RuntimeError(message)


class AutoModFlags(flags.BaseFlags):
    @flags.flag_value
    def joins(self) -> int:
        """Whether the server is broadcasting joins"""
        return 1
    
    @flags.flag_value
    def raid(self) -> int:
        """Whether the server is autobanning spammers"""
        return 2


## Configuration


class ModConfig:
    __slots__ = (
        'automod_flags',
        'id',
        'bot',
        'broadcast_channel_id',
        'broadcast_webhook_url',
        'mention_count',
        'safe_automod_channel_ids',
        'mute_role_id',
        'muted_members',
        '_cs_broadcast_webhook',
    )

    bot: Ayaka
    automod_flags: AutoModFlags
    id: int
    broadcast_channel_id: Optional[int]
    broadcast_webhook_url: Optional[str]
    mention_count: Optional[int]
    safe_automod_channel_ids: set[int]
    muted_members: set[int]
    mute_role_id: Optional[int]

    @classmethod
    def from_record(cls, record: Any, bot: Ayaka):
        self = cls()

        # the basic configuration
        self.bot = bot
        self.automod_flags = AutoModFlags(record['automod_flags'] or 0)
        self.id = record['id']
        self.broadcast_channel_id = record['broadcast_channel']
        self.broadcast_webhook_url = record['broadcast_webhook_url']
        self.mention_count = record['mention_count']
        self.safe_automod_channel_ids = set(record['safe_automod_channel_ids'] or [])
        self.muted_members = set(record['muted_members'] or [])
        self.mute_role_id = record['mute_role_id']
        return self

    @property
    def broadcast_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.broadcast_channel_id)  # type: ignore
    
    @property
    def requires_migration(self) -> bool:
        return self.broadcast_channel_id is not None and self.broadcast_webhook_url is None
    
    @discord.utils.cached_slot_property('_cs_broadcast_webhook')
    def broadcast_webhook(self) -> discord.Webhook | None:
        if self.broadcast_webhook_url is None:
            return None
        return discord.Webhook.from_url(self.broadcast_webhook_url, session=self.bot.session)

    @property
    def mute_role(self) -> Optional[discord.Role]:
        guild = self.bot.get_guild(self.id)
        return guild and self.mute_role_id and guild.get_role(self.mute_role_id)  # type: ignore

    def is_muted(self, member: discord.abc.Snowflake) -> bool:
        return member.id in self.muted_members

    async def apply_mute(self, member: discord.Member, reason: Optional[str]):
        if self.mute_role_id:
            await member.add_roles(discord.Object(id=self.mute_role_id), reason=reason)


## Views


class MigrateJoinLogView(discord.ui.View):
    def __init__(self, cog: Mod) -> None:
        super().__init__(timeout=None)
        self.cog = cog
    
    @discord.ui.button(label='Migrate', custom_id='migrate_robomod_join_logs', style=discord.ButtonStyle.green)
    async def migrate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.message is not None
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            await self.cog.migrate_automod_broadcast(interaction.user, interaction.channel, interaction.guild_id)  # type: ignore
        except RuntimeError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        else:
            await interaction.message.edit(content=None, view=None)
            await interaction.followup.send('Successfully migrated to new join logs!', ephemeral=True)


class PreExistingMuteRoleView(discord.ui.View):
    message: discord.Message

    def __init__(self, user: discord.abc.User) -> None:
        super().__init__(timeout=120.0)
        self.user = user
        self.merge: bool | None = None
    
    async def on_timeout(self) -> None:
        try:
            await self.message.reply('Aborting.')
            await self.message.delete()
        except:
            pass
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Sorry, these buttons aren't for you", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label='Merge', style=discord.ButtonStyle.blurple)
    async def merge_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.merge = True

    @discord.ui.button(label='Replace', style=discord.ButtonStyle.grey)
    async def replace_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.merge = False

    @discord.ui.button(label='Quit', style=discord.ButtonStyle.red)
    async def abort_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message('Aborting', ephemeral=True)
        self.merge = None
        await self.message.delete()


## Converters


def can_execute_action(ctx: GuildContext, user: discord.Member, target: discord.Member) -> bool:
    return user.id == ctx.bot.owner_id or user == ctx.guild.owner or user.top_role > target.top_role


class MemberID(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                member_id = int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(f"{argument} is not a valid member or member ID.") from None
            else:
                m = await ctx.bot.get_or_fetch_member(ctx.guild, member_id)
                if m is None:
                    # hackban case
                    return type('_Hackban', (), {'id': member_id, '__str__': lambda s: f'Member ID {s.id}'})()

        if not can_execute_action(ctx, ctx.author, m):
            raise commands.BadArgument('You cannot do this action on this user due to role hierarchy.')
        return m


class BannedMember(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        if argument.isdigit():
            member_id = int(argument, base=10)
            try:
                return await ctx.guild.fetch_ban(discord.Object(id=member_id))
            except discord.NotFound:
                raise commands.BadArgument('This member has not been banned before.') from None

        entity = await discord.utils.find(lambda u: str(u.user) == argument, ctx.guild.bans(limit=None))

        if entity is None:
            raise commands.BadArgument('This member has not been banned before.')
        return entity


class ActionReason(commands.Converter):
    async def convert(self, ctx: GuildContext, argument: str):
        ret = f'{ctx.author} (ID: {ctx.author.id}): {argument}'

        if len(ret) > 512:
            reason_max = 512 - len(ret) + len(argument)
            raise commands.BadArgument(f'Reason is too long ({len(argument)}/{reason_max})')
        return ret


def safe_reason_append(base: str, to_append: str) -> str:
    appended = base + f'({to_append})'
    if len(appended) > 512:
        return base
    return appended


## Spam detector

# TODO: add this to d.py maybe
class CooldownByContent(commands.CooldownMapping):
    def _bucket_key(self, message: discord.Message) -> tuple[int, str]:
        return (message.channel.id, message.content)


class SpamChecker:
    """This spam checker does a few things.

    1) It checks if a user has spammed more than 10 times in 12 seconds
    2) It checks if the content has been spammed 15 times in 17 seconds.
    3) It checks if new users have spammed 30 times in 35 seconds.
    4) It checks if "fast joiners" have spammed 10 times in 12 seconds.
    5) It checks if a member spammed `config.mention_count * 2` mentions in 12 seconds.

    The second case is meant to catch alternating spam bots while the first one
    just catches regular singular spam bots.

    From experience these values aren't reached unless someone is actively spamming.
    """

    def __init__(self):
        self.by_content = CooldownByContent.from_cooldown(15, 17.0, commands.BucketType.member)
        self.by_user = commands.CooldownMapping.from_cooldown(10, 12.0, commands.BucketType.user)
        self.last_join: Optional[datetime.datetime] = None
        self.new_user = commands.CooldownMapping.from_cooldown(30, 35.0, commands.BucketType.channel)
        self._by_mentions: Optional[commands.CooldownMapping] = None
        self._by_mentions_rate: Optional[int] = None

        # user_id flag mapping (for about 30 minutes)
        self.fast_joiners: MutableMapping[int, bool] = cache.ExpiringCache(seconds=1800.0)
        self.hit_and_run = commands.CooldownMapping.from_cooldown(10, 12, commands.BucketType.channel)

    def by_mentions(self, config: ModConfig) -> Optional[commands.CooldownMapping]:
        if not config.mention_count:
            return None

        mention_threshold = config.mention_count * 2
        if self._by_mentions_rate != mention_threshold:
            self._by_mentions = commands.CooldownMapping.from_cooldown(mention_threshold, 12, commands.BucketType.member)
            self._by_mentions_rate = mention_threshold
        return self._by_mentions

    def is_new(self, member: discord.Member) -> bool:
        now = discord.utils.utcnow()
        seven_days_ago = now - datetime.timedelta(days=7)
        ninety_days_ago = now - datetime.timedelta(days=90)
        return member.created_at > ninety_days_ago and member.joined_at is not None and member.joined_at > seven_days_ago

    def is_spamming(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False

        current = message.created_at.timestamp()

        # CooldownMapping.get_bucket never returns None if bucket_type is not default.
        if message.author.id in self.fast_joiners:
            bucket: commands.Cooldown = self.hit_and_run.get_bucket(message)  # type: ignore
            if bucket.update_rate_limit(current):
                return True

        if self.is_new(message.author):  # type: ignore  # guarded with the first if statement
            new_bucket: commands.Cooldown = self.new_user.get_bucket(message)  # type: ignore
            if new_bucket.update_rate_limit(current):
                return True

        user_bucket: commands.Cooldown = self.by_user.get_bucket(message)  # type: ignore
        if user_bucket.update_rate_limit(current):
            return True

        content_bucket: commands.Cooldown = self.by_content.get_bucket(message)  # type: ignore
        if content_bucket.update_rate_limit(current):
            return True

        return False

    def is_fast_join(self, member: discord.Member) -> bool:
        joined = member.joined_at or discord.utils.utcnow()
        if self.last_join is None:
            self.last_join = joined
            return False
        is_fast = (joined - self.last_join).total_seconds() <= 2.0
        self.last_join = joined
        if is_fast:
            self.fast_joiners[member.id] = True
        return is_fast

    def is_mention_spam(self, message: discord.Message, config: ModConfig) -> bool:
        mapping = self.by_mentions(config)
        if mapping is None:
            return False
        
        current = message.created_at.timestamp()
        # get_bucket can only return None if bucket type is default.
        mention_bucket: commands.Cooldown = mapping.get_bucket(message, current)  # type: ignore
        mention_count = sum(not m.bot and m.id != message.author.id for m in message.mentions)
        return mention_bucket.update_rate_limit(current, tokens=mention_count) is not None


## Checks


class NoMuteRole(commands.CommandError):
    def __init__(self):
        super().__init__('This server does not have a mute role set up.')


def can_mute():
    async def predicate(ctx: ModGuildContext) -> bool:
        is_owner = await ctx.bot.is_owner(ctx.author)
        if ctx.guild is None:
            return False

        if not ctx.author.guild_permissions.manage_roles and not is_owner:
            return False

        # This will only be used within this cog.
        ctx.guild_config = config = await ctx.cog.get_guild_config(ctx.guild.id)  # type: ignore
        role = config and config.mute_role
        if role is None:
            raise NoMuteRole()
        return ctx.author.top_role > role

    return commands.check(predicate)


def can_use_block():
    async def predicate(ctx: ModGuildContext) -> bool:
        is_owner = await ctx.bot.is_owner(ctx.author)
        if ctx.guild is None:
            return False
        if is_owner:
            return True
        if isinstance(ctx.channel, discord.Thread):
            return ctx.author.id == ctx.channel.owner_id or ctx.channel.permissions_for(ctx.author).manage_threads
        return ctx.channel.permissions_for(ctx.author).manage_roles

    return commands.check(predicate)


## The actual cog


class Mod(commands.Cog):
    """Moderation related commands."""

    def __init__(self, bot: Ayaka):
        self.bot: Ayaka = bot

        # guild_id: SpamChecker
        self._spam_check: defaultdict[int, SpamChecker] = defaultdict(SpamChecker)

        # guild_id: List[(member_id, insertion)]
        # A batch of data for bulk inserting mute role changes
        # True - insert, False - remove
        self._data_batch: defaultdict[int, list[tuple[int, Any]]] = defaultdict(list)
        self._batch_lock = asyncio.Lock()
        self._disable_lock = asyncio.Lock()
        self.batch_updates.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_updates.start()

        # (guild_id, channel_id): List[str]
        # A batch list of message content for message
        self.message_batches: defaultdict[tuple[int, int], list[str]] = defaultdict(list)
        self._batch_message_lock = asyncio.Lock()
        self.bulk_send_messages.start()

        self._automod_migration_view = MigrateJoinLogView(self)
        bot.add_view(self._automod_migration_view)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='DiscordCertifiedModerator', id=847961544124923945)

    def __repr__(self) -> str:
        return '<cogs.Mod>'
    
    async def cog_load(self) -> None:
        self._avatar: bytes = await self.bot.user.display_avatar.read()

    def cog_unload(self) -> None:
        self.batch_updates.stop()
        self.bulk_send_messages.stop()
        self._automod_migration_view.stop()

    async def cog_command_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                await ctx.send('I do not have permission to execute this action.')
            elif isinstance(original, discord.NotFound):
                await ctx.send(f'This entity does not exist: {original.text}')
            elif isinstance(original, discord.HTTPException):
                await ctx.send('Somehow, an unexpected error occurred. Try again later?')
        elif isinstance(error, NoMuteRole):
            await ctx.send(str(error))
        elif isinstance(error, commands.UserInputError):
            await ctx.send(str(error))

    async def bulk_insert(self):
        query = """UPDATE guild_mod_config
                   SET muted_members = x.result_array
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(guild_id BIGINT, result_array BIGINT[])
                   WHERE guild_mod_config.id = x.guild_id;
                """

        if not self._data_batch:
            return

        final_data = []
        for guild_id, data in self._data_batch.items():
            # If it's touched this function then chances are that this has hit cache before
            # so it's not actually doing a query, hopefully.
            config = await self.get_guild_config(guild_id)

            # Unsure what happened here, but this should be rare.
            if config is None:
                continue

            as_set = config.muted_members
            for member_id, insertion in data:
                func = as_set.add if insertion else as_set.discard
                func(member_id)

            final_data.append({'guild_id': guild_id, 'result_array': list(as_set)})
            self.get_guild_config.invalidate(self, guild_id)

        await self.bot.pool.execute(query, final_data)
        self._data_batch.clear()

    @tasks.loop(seconds=15.0)
    async def batch_updates(self):
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=10.0)
    async def bulk_send_messages(self):
        async with self._batch_message_lock:
            for ((guild_id, channel_id), messages) in self.message_batches.items():
                guild = self.bot.get_guild(guild_id)
                channel: Optional[discord.abc.Messageable] = guild and guild.get_channel(channel_id)  # type: ignore
                if channel is None:
                    continue

                paginator = commands.Paginator(suffix='', prefix='')
                for message in messages:
                    paginator.add_line(message)

                for page in paginator.pages:
                    try:
                        await channel.send(page)
                    except discord.HTTPException:
                        pass

            self.message_batches.clear()

    @cache.cache()
    async def get_guild_config(self, guild_id: int) -> Optional[ModConfig]:
        query = """SELECT * FROM guild_mod_config WHERE id=$1;"""
        async with self.bot.pool.acquire(timeout=300.0) as con:
            record = await con.fetchrow(query, guild_id)
            if record is not None:
                return ModConfig.from_record(record, self.bot)
            return None

    async def check_raid(self, config: ModConfig, guild_id: int, member: discord.Member, message: discord.Message) -> None:
        if not config.automod_flags.raid:
            return

        checker = self._spam_check[guild_id]
        if not checker.is_spamming(message):
            return

        try:
            await member.ban(reason='Auto-ban for spamming')
        except discord.HTTPException:
            log.info('[Robomod] Failed to ban %s (ID: %s) from server %s.', member, member.id, member.guild)
        else:
            log.info('[Robomod] Banned %s (ID: %s) from server %s.', member, member.id, member.guild)
    
    async def ban_for_mention_spam(
        self,
        mention_count: int,
        guild_id: int,
        message: discord.Message,
        member: discord.Member,
        multiple: bool = False,
    ) -> None:
        
        if multiple:
            reason = f'Spamming mentions over multiple messages ({mention_count} mentions)'
        else:
            reason = f'Spamming mentions ({mention_count} mentions)'
        
        try:
            await member.ban(reason=reason)
        except Exception:
            log.info('[Mention Spam] Failed to ban member %s (ID: %s) in guild ID %s', member, member.id, guild_id)
        else:
            to_send = f'Banned {member} (ID: {member.id}) for spamming {mention_count} mentions.'
            async with self._batch_message_lock:
                self.message_batches[(guild_id, message.channel.id)].append(to_send)
            
            log.info('[Mention Spam] Member %s (ID: %s) has been banned from guild ID %s', member, member.id, guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        author = message.author
        if author.id in (self.bot.user.id, self.bot.owner_id):
            return

        if message.guild is None:
            return

        if not isinstance(author, discord.Member):
            return

        if author.bot:
            return

        # we're going to ignore members with manage messages
        if author.guild_permissions.manage_messages:
            return

        guild_id = message.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return
        
        if message.channel.id in config.safe_automod_channel_ids:
            return

        # check for raid mode stuff
        await self.check_raid(config, guild_id, author, message)

        if not config.mention_count:
            return

        checker = self._spam_check[guild_id]
        if checker.is_mention_spam(message, config):
            await self.ban_for_mention_spam(config.mention_count, guild_id, message, author, multiple=True)
            return
        
        # auto-ban tracking for mention spams begin here
        if len(message.mentions) <= 3:
            return

        # check if it meets the thresholds required
        mention_count = sum(not m.bot and m.id != author.id for m in message.mentions)
        if mention_count < config.mention_count:
            return

        await self.ban_for_mention_spam(mention_count, guild_id, message, author)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = member.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.is_muted(member):
            return await config.apply_mute(member, 'Member was previously muted.')

        if not config.automod_flags.joins:
            return

        now = discord.utils.utcnow()

        is_new = member.created_at > (now - datetime.timedelta(days=7))
        checker = self._spam_check[guild_id]

        # Do the broadcasted message to the channel
        title = 'Member Joined'
        if checker.is_fast_join(member):
            colour = 0xDD5F53  # red
            if is_new:
                title = 'Member Joined (Very New Member)'
        else:
            colour = 0x53DDA4  # green

            if is_new:
                colour = 0xDDA453  # yellow
                title = 'Member Joined (Very New Member)'

        e = discord.Embed(title=title, colour=colour)
        e.timestamp = now
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name='ID', value=member.id)
        assert member.joined_at is not None
        e.add_field(name='Joined', value=time.format_dt(member.joined_at, "F"))
        e.add_field(name='Created', value=time.format_relative(member.created_at), inline=False)

        if config.requires_migration:
            await self.suggest_automod_migration(config, e, guild_id)
            return
        
        if config.broadcast_webhook:
            try:
                await config.broadcast_webhook.send(embed=e)
            except discord.Forbidden:
                async with self._disable_lock:
                    await self.disable_automod_broadcast(guild_id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Comparing roles in memory is faster than potentially fetching from
        # database, even if there's a cache layer
        if before.roles == after.roles:
            return

        guild_id = after.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None:
            return

        if config.mute_role_id is None:
            return

        before_has = before.get_role(config.mute_role_id)
        after_has = after.get_role(config.mute_role_id)

        # No change in the mute role
        # both didn't have it or both did have it
        if before_has == after_has:
            return

        async with self._batch_lock:
            # If `after_has` is true, then it's an insertion operation
            # if it's false, then the role for removed
            self._data_batch[guild_id].append((after.id, after_has))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild_id = role.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role_id != role.id:
            return

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)

    @commands.command(aliases=['newmembers'])
    @commands.guild_only()
    async def newusers(self, ctx: GuildContext, *, count: int = 5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have
        joined.

        The count parameter can only be up to 25.
        """
        count = max(min(count, 25), 5)

        if not ctx.guild.chunked:
            members = await ctx.guild.chunk(cache=True)

        members = sorted(ctx.guild.members, key=lambda m: m.joined_at or ctx.guild.created_at, reverse=True)[:count]

        e = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            joined = member.joined_at or datetime.datetime(1970, 1, 1)
            body = f'Joined {time.format_relative(joined)}\nCreated {time.format_relative(member.created_at)}'
            e.add_field(name=f'{member} (ID: {member.id})', value=body, inline=False)

        await ctx.send(embed=e)

    async def suggest_automod_migration(self, config: ModConfig, embed: discord.Embed, guild_id: int) -> None:
        channel = config.broadcast_channel

        async with self._disable_lock:
            await self.disable_automod_broadcast(guild_id)
        
        if channel is None:
            return
        
        msg = (
            '**Notice**\n\n'
            'Join logs have been updated to use a webhook to prevent the bot from being '
            'heavily rate limited during join raids. As a result, **migration needs to be done '
            'in order for joins to start being broadcasted again**. Sorry for the inconvenience.\n\n'
            'For the migration to succeed, **the bot must have Manage Webhooks permission** both in '
            'the server *and* the channel.\n\n'
            'In order to migrate, **please press the button below**.'
        )

        try:
            await channel.send(embed=embed, content=msg, view=self._automod_migration_view)
        except discord.Forbidden:
            pass
    
    @commands.hybrid_group(aliases=['automod'], fallback='info')
    @checks.is_mod()
    async def robomod(self, ctx: GuildContext):
        """Show current RoboMod (automatic moderation) behaviour on the server.

        You must have Ban Members and Manage Messages permissions to use this
        command or its subcommands.
        """

        config = await self.get_guild_config(ctx.guild.id)
        if config is None:
            await ctx.send('This server does not have RoboMod set up!')
            return
        
        e = discord.Embed(title='RoboMod information')
        if config.automod_flags.joins:
            channel = f'<#{config.broadcast_channel_id}>'
            if config.requires_migration:
                broadcast = (
                    f'{channel}\n\n\N{WARNING SIGN}\ufe0f '
                    'This server requires migration for this feature to continue working.\n'
                    f'Run "{ctx.prefix}robomod disable joins" followed by "{ctx.prefix}robomod join {channel}" '
                    'to ensure this feature continues working.'
                )
            else:
                broadcast = f'Enabled on {channel}'
        else:
            broadcast = 'Disabled'
        
        e.add_field(name='Join Logs', value=broadcast)
        e.add_field(name='Raid Protection', value='Enabled' if config.automod_flags.raid else 'Disabled')

        mention_spam = f'{config.mention_count} mentions' if config.mention_count else 'Disabled'
        e.add_field(name='Mention Spam Protection', value=mention_spam)

        if config.safe_automod_channel_ids:
            if len(config.safe_automod_channel_ids) <= 5:
                ignored = '\n'.join(f'<#{c}>' for c in config.safe_automod_channel_ids)
            else:
                sliced = list(config.safe_automod_channel_ids)[:5]
                channels = '\n'.join(f'<#{c}>' for c in sliced)
                ignored = f'{channels}\n{len(config.safe_automod_channel_ids) - 5} more...'
        else:
            ignored = 'Nothing'

        e.add_field(name='Ignored Channels', value=ignored, inline=False)
        await ctx.send(embed=e)

    @robomod.command(name='joins')
    @checks.is_mod()
    @app_commands.describe(
        channel='The channel to broadcast join messages to. The bot must be able to create webhooks in it.'
    )
    async def robomod_joins(self, ctx: GuildContext, *, channel: discord.TextChannel) -> None:
        """Enables join message logging in the given channel.

        The bot must have the ability to create webhooks in the given channel.
        """

        await ctx.defer()
        config = await self.get_guild_config(ctx.guild.id)
        if config and config.automod_flags.joins:
            await ctx.send(f'You already have join message loggin enabled. To disable, use "{ctx.prefix}robomod disable joins"')
            return
        
        channel_id = channel.id

        reason = f'{ctx.author} (ID: {ctx.author.id}) enabled RoboMod join logs'

        try:
            webhook = await channel.create_webhook(name='RoboMod Join Logs', avatar=self._avatar, reason=reason)
        except discord.Forbidden:
            await ctx.send(f'The not does not have permissions to create webhooks in {channel.mention}.')
            return
        except discord.HTTPException:
            await ctx.send('An error occurred while creating the webhook. Note you can only have 10 wevhooks per channel.')
            return

        query = """INSERT INTO guild_mod_config (id, automod_flags, broadcast_channel, broadcast_webhook_url)
                   VALUES ($1, $2, $3, $4) ON CONFLICT (id)
                   DO UPDATE SET
                        automod_flags = guild_mod_config.automod_flags | EXCLUDED.automod_flags,
                        broadcast_channel = EXCLUDED.broadcast_channel,
                        broadcast_webhook_url = EXCLUDED.broadcast_webhook_url;
                """

        flags = AutoModFlags()
        flags.joins = True
        await ctx.db.execute(query, ctx.guild.id, flags.value, channel_id, webhook.url)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Join logs enabled. Broadcasting join messages to <#{channel_id}>.')

    async def disable_automod_broadcast(self, guild_id: int):
         # Note: This is called when the webhook has been deleted
        query = """INSERT INTO guild_mod_config (id, automod_flags, broadcast_channel, broadcast_webhook_url)
                   VALUES ($1, 0, NULL, NULL) ON CONFLICT (id)
                   DO UPDATE SET
                        automod_flags = guild_mod_config.automod_flags & ~$2,
                        broadcast_channel = NULL,
                        broadcast_webhook_url = NULL;
                """

        await self.bot.pool.execute(query, guild_id, AutoModFlags.joins.flag)
        self.get_guild_config.invalidate(self, guild_id)

    async def migrate_automod_broadcast(self, user: discord.abc.User, channel: discord.TextChannel, guild_id: int) -> None:
        reason = f'{user} (ID: {user.id}) migrated RoboMod join logs'

        config = await self.get_guild_config(guild_id)
        if config and not config.requires_migration:
            # If someone successfully migrated somehow, just return early
            # The message will hopefully edit
            return

        try:
            webhook = await channel.create_webhook(name='RoboMod Join Logs', avatar=self._avatar, reason=reason)
        except discord.Forbidden:
            raise RuntimeError(f'The bot does not have permissions to create webhooks in {channel.mention}.') from None
        except discord.HTTPException:
            raise RuntimeError('An error occurred while creating the webhook. Note you can only have 10 wevhooks per channel.') from None

        query = 'UPDATE guild_mod_config SET broadcast_webhook_url = $2 WHERE id = $1'
        await self.bot.pool.execute(query, guild_id, webhook.url)
        self.get_guild_config.invalidate(self, guild_id)

    @robomod.command(name='disable', aliases=['off'])
    @checks.is_mod()
    @app_commands.describe(protection='The protection to disable')
    @app_commands.choices(protection=[
        app_commands.Choice(name='Everything', value='all'),
        app_commands.Choice(name='Join logging', value='joins'),
        app_commands.Choice(name='Raid protection', value='raid'),
        app_commands.Choice(name='Mention spam protection', value='mentions'),
    ])
    async def robomod_disable(self, ctx: GuildContext, *, protection: Literal['all', 'joins', 'raid', 'mentions'] = 'all') -> None:
        """Disables RoboMod on this server.

        This can be one of these settings:

         - "all" to disable everything
        - "joins" to disable join logging
        - "raid" to disable raid protection
        - "mentions" to disable mention spam protection
        
        If not given then it defaults to "all".
        """

        if protection == 'all':
            updates = 'automod_flags = 0, mention_count = 0, broadcast_channel = NULL'
            message = 'RoboMod has been disabled.'
        elif protection == 'joins':
            updates = (
                f'automod_flags = guild_mod_config.automod_flags & ~{AutoModFlags.joins.flag}, broadcast_channel = NULL'
            )
            message = 'Join logs have been disabled.'
        elif protection == 'raid':
            updates = f'automod_flags = guild_mod_config.automod_flags & ~{AutoModFlags.raid.flag}'
            message = 'Raid protection has been disabled.'
        elif protection == 'mentions':
            updates = 'mention_count = NULL'
            message = 'Mention spam protection has been disabled'

        query = f'UPDATE guild_mod_config SET {updates} WHERE id=$1 RETURNING broadcast_webhook_url'

        guild_id = ctx.guild.id
        record: Optional[tuple[Optional[str]]] = await self.bot.pool.fetchrow(query, guild_id)
        self._spam_check.pop(guild_id, None)
        self.get_guild_config.invalidate(self, guild_id)
        if record is not None and record[0] is not None and protection in ('all', 'joins'):
            wh = discord.Webhook.from_url(record[0], session=self.bot.session)
            try:
                await wh.delete(reason=message)
            except discord.HTTPException:
                await ctx.send(f'{message} However the webhook could not be deleted for some reason.')
                return

        await ctx.send(message)
    
    @robomod.command(name='raid')
    @checks.is_mod()
    @app_commands.describe(enabled='Whether raid protection should be enabled or not, toggles if not given.')
    async def robomod_raid(self, ctx: GuildContext, enabled: bool | None = None) -> None:
        """Toggles raid protection on the server.
        
        Raid protection automatically bans members that spam messages in your server.
        """

        perms = ctx.me.guild_permissions
        if not perms.ban_members:
            await ctx.send('\N{NO ENTRY SIGN} I do not have permissions to ban members.')
            return

        query = """INSERT INTO guild_mod_config (id, automod_flags)
                   VALUES ($1, $2) ON CONFLICT (id)
                   DO UPDATE SET
                        -- If we're toggling then we need to negate the previous result
                        automod_flags = CASE COALESCE($3, NOT (guild_mod_config.automod_flags & $2 = $2))
                                        WHEN TRUE THEN guild_mod_config.automod_flags | $2
                                        WHEN FALSE THEN guild_mod_config.automod_flags & ~$2
                                        END
                   RETURNING COALESCE($3, (automod_flags & $2 = $2));
                """

        row: tuple[bool] | None = await ctx.db.fetchrow(query, ctx.guild.id, AutoModFlags.raid.flag, enabled)
        enabled = row and row[0]
        self.get_guild_config.invalidate(self, ctx.guild.id)
        fmt = 'enabled' if enabled else 'disabled'
        await ctx.send(f'Raid protection {fmt}.')
    
    @robomod.command(name='mentions')
    @commands.guild_only()
    @app_commands.describe(count='The maximum amount of mentions before banning.')
    async def robomod_mentions(self, ctx: GuildContext, count: commands.Range[int, 3]):
        """Enables auto-banning accounts that spam more than "count" mentions.

         If a message contains `count` or more mentions then the
        bot will automatically attempt to auto-ban the member.
        The `count` must be greater than 3.

        This only applies for user mentions. Everyone or Role
        mentions are not included.
        """

        query = """INSERT INTO guild_mod_config (id, mention_count, safe_automod_channel_ids)
                   VALUES ($1, $2, '{}')
                   ON CONFLICT (id) DO UPDATE SET
                       mention_count = $2;
                """
        await ctx.db.execute(query, ctx.guild.id, count)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Mention spam protection threshold set to {count}.')
    
    @robomod_mentions.error
    async def robomod_mentions_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.RangeError):
            await ctx.send('\N{NO ENTRY SIGN} Mention spam protection threshold must be greater than three.')
    
    @robomod.command(name='ignore')
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def robomod_ignore(self, ctx: GuildContext, channels: commands.Greedy[discord.TextChannel]):
        """Specifies what channels ignore RoboMod auto-bans.
    
        If a channel is given then that channel will no longer be protected
        by RoboMod.
    
        To use this command you must have the Ban Members permission.
        """

        query = """UPDATE guild_mod_config
                   SET safe_automod_channel_ids =
                       ARRAY(SELECT DISTINCT * FROM unnest(COALESCE(safe_automod_channel_ids, '{}') || $2::bigint[]))
                   WHERE id = $1;
                """

        if len(channels) == 0:
            return await ctx.send('Missing channels to ignore.')

        channel_ids = [c.id for c in channels]
        await ctx.db.execute(query, ctx.guild.id, channel_ids)
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send(f'Mentions are now ignored on {", ".join(c.mention for c in channels)}.')

    @robomod.command(name='unignore')
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def robomod_unignore(self, ctx: GuildContext, channels: commands.Greedy[discord.TextChannel]):
        """Specifies what channels to take off the RoboMod ignore list.
       
        To use this command you must have the Ban Members permission.
        """

        if len(channels) == 0:
            return await ctx.send('Missing channels to protect.')

        query = """UPDATE guild_mod_config
                   SET safe_automod_channel_ids =
                       ARRAY(SELECT element FROM unnest(safe_automod_channel_ids) AS element
                             WHERE NOT(element = ANY($2::bigint[])))
                   WHERE id = $1;
                """

        await ctx.db.execute(query, ctx.guild.id, [c.id for c in channels])
        self.get_guild_config.invalidate(self, ctx.guild.id)
        await ctx.send('Updated mentionspam ignore list.')

    async def _basic_cleanup_strategy(self, ctx: GuildContext, search: int):
        count = 0
        async for msg in ctx.history(limit=search, before=ctx.message):
            if msg.author == ctx.me and not (msg.mentions or msg.role_mentions):
                await msg.delete()
                count += 1
        return {'Bot': count}

    async def _complex_cleanup_strategy(self, ctx: GuildContext, search: int):
        prefixes = tuple(self.bot.get_guild_prefixes(ctx.guild))  # thanks startswith

        def check(m):
            return m.author == ctx.me or m.content.startswith(prefixes)

        deleted = await ctx.channel.purge(limit=search, check=check, before=ctx.message)
        return Counter(m.author.display_name for m in deleted)

    async def _regular_user_cleanup_strategy(self, ctx: GuildContext, search: int):
        prefixes = tuple(self.bot.get_guild_prefixes(ctx.guild))

        def check(m):
            return (m.author == ctx.me or m.content.startswith(prefixes)) and not (m.mentions or m.role_mentions)

        deleted = await ctx.channel.purge(limit=search, check=check, before=ctx.message)
        return Counter(m.author.display_name for m in deleted)

    @commands.command()
    async def cleanup(self, ctx: GuildContext, search: int = 100):
        """Cleans up the bot's messages from the channel.

        If a search number is specified, it searches that many messages to delete.
        If the bot has Manage Messages permissions then it will try to delete
        messages that look like they invoked the bot as well.

        After the cleanup is completed, the bot will send you a message with
        which people got their messages deleted and their count. This is useful
        to see which users are spammers.

        Members with Manage Messages can search up to 1000 messages.
        Members without can search up to 25 messages.
        """

        strategy = self._basic_cleanup_strategy
        is_mod = ctx.channel.permissions_for(ctx.author).manage_messages
        if ctx.channel.permissions_for(ctx.me).manage_messages:
            if is_mod:
                strategy = self._complex_cleanup_strategy
            else:
                strategy = self._regular_user_cleanup_strategy

        if is_mod:
            search = min(max(2, search), 1000)
        else:
            search = min(max(2, search), 25)

        spammers = await strategy(ctx, search)
        deleted = sum(spammers.values())
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'- **{author}**: {count}' for author, count in spammers)

        await ctx.send('\n'.join(messages), delete_after=10)

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def kick(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Kicks a member from the server.

        In order for this to work, the bot must have Kick Member permissions.

        To use this command you must have Kick Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.kick(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Bans a member from the server.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def multiban(
        self,
        ctx: GuildContext,
        members: Annotated[List[discord.abc.Snowflake], commands.Greedy[MemberID]],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Bans multiple members from the server.

        This only works through banning via ID.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        total_members = len(members)
        if total_members == 0:
            return await ctx.send('Missing members to ban.')

        confirm = await ctx.prompt(f'This will ban **{plural(total_members):member}**. Are you sure?')
        if not confirm:
            return await ctx.send('Aborting.')

        failed = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                failed += 1

        await ctx.send(f'Banned {total_members - failed}/{total_members} members.')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def massban(self, ctx: GuildContext, *, arguments: str):
        """Mass bans multiple members from the server.

        This command has a powerful "command line" syntax. To use this command
        you and the bot must both have Ban Members permission. **Every option is optional.**

        Users are only banned **if and only if** all conditions are met.

        The following options are valid.

        `--channel` or `-c`: Channel to search for message history.
        `--reason` or `-r`: The reason for the ban.
        `--regex`: Regex that usernames must match.
        `--created`: Matches users whose accounts were created less than specified minutes ago.
        `--joined`: Matches users that joined less than specified minutes ago.
        `--joined-before`: Matches users who joined before the member ID given.
        `--joined-after`: Matches users who joined after the member ID given.
        `--no-avatar`: Matches users who have no avatar. (no arguments)
        `--no-roles`: Matches users that have no role. (no arguments)
        `--show`: Show members instead of banning them (no arguments).

        Message history filters (Requires `--channel`):

        `--contains`: A substring to search for in the message.
        `--starts`: A substring to search if the message starts with.
        `--ends`: A substring to search if the message ends with.
        `--match`: A regex to match the message content to.
        `--search`: How many messages to search. Default 100. Max 2000.
        `--after`: Messages must come after this message ID.
        `--before`: Messages must come before this message ID.
        `--files`: Checks if the message has attachments (no arguments).
        `--embeds`: Checks if the message has embeds (no arguments).
        """

        # For some reason there are cases due to caching that ctx.author
        # can be a User even in a guild only context
        # Rather than trying to work out the kink with it
        # Just upgrade the member itself.
        if not isinstance(ctx.author, discord.Member):
            try:
                author = await ctx.guild.fetch_member(ctx.author.id)
            except discord.HTTPException:
                return await ctx.send('Somehow, Discord does not seem to think you are in this server.')
        else:
            author = ctx.author

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('--channel', '-c')
        parser.add_argument('--reason', '-r')
        parser.add_argument('--search', type=int, default=100)
        parser.add_argument('--regex')
        parser.add_argument('--no-avatar', action='store_true')
        parser.add_argument('--no-roles', action='store_true')
        parser.add_argument('--created', type=int)
        parser.add_argument('--joined', type=int)
        parser.add_argument('--joined-before', type=int)
        parser.add_argument('--joined-after', type=int)
        parser.add_argument('--contains')
        parser.add_argument('--starts')
        parser.add_argument('--ends')
        parser.add_argument('--match')
        parser.add_argument('--show', action='store_true')
        parser.add_argument('--embeds', action='store_const', const=lambda m: len(m.embeds))
        parser.add_argument('--files', action='store_const', const=lambda m: len(m.attachments))
        parser.add_argument('--after', type=int)
        parser.add_argument('--before', type=int)

        try:
            args = parser.parse_args(shlex.split(arguments))
        except Exception as e:
            return await ctx.send(str(e))

        members = []

        if args.channel:
            channel = await commands.TextChannelConverter().convert(ctx, args.channel)
            before = args.before and discord.Object(id=args.before)
            after = args.after and discord.Object(id=args.after)
            predicates_: list[Callable[[discord.Message], bool]] = []
            if args.contains:
                predicates_.append(lambda m: args.contains in m.content)
            if args.starts:
                predicates_.append(lambda m: m.content.startswith(args.starts))
            if args.ends:
                predicates_.append(lambda m: m.content.endswith(args.ends))
            if args.match:
                try:
                    _match = re.compile(args.match)
                except re.error as e:
                    return await ctx.send(f'Invalid regex passed to `--match`: {e}')
                else:
                    predicates_.append(lambda m, x=_match: x.match(m.content))
            if args.embeds:
                predicates_.append(args.embeds)
            if args.files:
                predicates_.append(args.files)

            async for message in channel.history(limit=min(max(1, args.search), 2000), before=before, after=after):
                if all(p(message) for p in predicates_):
                    members.append(message.author)
        else:
            if ctx.guild.chunked:
                members = ctx.guild.members
            else:
                async with ctx.typing():
                    await ctx.guild.chunk(cache=True)
                members = ctx.guild.members

        # member filters
        predicates: list[Callable[[discord.Member], bool]] = [
            lambda m: isinstance(m, discord.Member) and can_execute_action(ctx, author, m),  # Only if applicable
            lambda m: not m.bot,  # No bots
            lambda m: m.discriminator != '0000',  # No deleted users
        ]

        converter = commands.MemberConverter()

        if args.regex:
            try:
                _regex = re.compile(args.regex)
            except re.error as e:
                return await ctx.send(f'Invalid regex passed to `--regex`: {e}')
            else:
                predicates.append(lambda m, x=_regex: x.match(m.name))

        if args.no_avatar:
            predicates.append(lambda m: m.avatar is None)
        if args.no_roles:
            predicates.append(lambda m: len(getattr(m, 'roles', [])) <= 1)

        now = discord.utils.utcnow()
        if args.created:

            def created(member, *, offset=now - datetime.timedelta(minutes=args.created)):
                return member.created_at > offset

            predicates.append(created)
        if args.joined:

            def joined(member, *, offset=now - datetime.timedelta(minutes=args.joined)):
                if isinstance(member, discord.User):
                    # If the member is a user then they left already
                    return True
                return member.joined_at and member.joined_at > offset

            predicates.append(joined)
        if args.joined_after:
            _joined_after_member = await converter.convert(ctx, str(args.joined_after))

            def joined_after(member, *, _other=_joined_after_member):
                return member.joined_at is not None and _other.joined_at is not None and member.joined_at > _other.joined_at

            predicates.append(joined_after)
        if args.joined_before:
            _joined_before_member = await converter.convert(ctx, str(args.joined_before))

            def joined_before(member, *, _other=_joined_before_member):
                return member.joined_at is not None and _other.joined_at is not None and member.joined_at < _other.joined_at

            predicates.append(joined_before)

        members = {m for m in members if all(p(m) for p in predicates)}
        if len(members) == 0:
            return await ctx.send('No members found matching criteria.')

        if args.show:
            members = sorted(members, key=lambda m: m.joined_at or now)
            fmt = "\n".join(f'{m.id}\tJoined: {m.joined_at}\tCreated: {m.created_at}\t{m}' for m in members)
            content = f'Current Time: {discord.utils.utcnow()}\nTotal members: {len(members)}\n{fmt}'
            file = discord.File(io.BytesIO(content.encode('utf-8')), filename='members.txt')
            return await ctx.send(file=file)

        if args.reason is None:
            return await ctx.send('--reason flag is required.')
        else:
            reason = await ActionReason().convert(ctx, args.reason)

        confirm = await ctx.prompt(f'This will ban **{plural(len(members)):member}**. Are you sure?')
        if not confirm:
            return await ctx.send('Aborting.')

        count = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await ctx.send(f'Banned {count}/{len(members)}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(kick_members=True)
    async def softban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Soft bans a member from the server.

        A softban is basically banning the member from the server but
        then unbanning the member as well. This allows you to essentially
        kick the member while removing their messages.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Kick Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.ban(member, reason=reason)
        await ctx.guild.unban(member, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: GuildContext,
        member: Annotated[discord.guild.BanEntry, BannedMember],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Unbans a member from the server.

        You can pass either the ID of the banned member or the Name#Discrim
        combination of the member. Typically the ID is easiest to use.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permissions.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        await ctx.guild.unban(member.user, reason=reason)
        if member.reason:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}), previously banned for {member.reason}.')
        else:
            await ctx.send(f'Unbanned {member.user} (ID: {member.user.id}).')

    @commands.command()
    @commands.guild_only()
    @checks.has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx: GuildContext,
        duration: time.FutureTime,
        member: Annotated[discord.abc.Snowflake, MemberID],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Temporarily bans a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC.

        You can also ban from ID to ban regardless whether they're
        in the server or not.

        In order for this to work, the bot must have Ban Member permissions.

        To use this command you must have Ban Members permission.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        until = f'until {time.format_dt(duration.dt, "F")}'
        heads_up_message = f'You have been banned from {ctx.guild.name} {until}. Reason: {reason}'

        try:
            await member.send(heads_up_message)  # type: ignore  # Guarded by AttributeError
        except (AttributeError, discord.HTTPException):
            # best attempt, oh well.
            pass

        reason = safe_reason_append(reason, until)
        await ctx.guild.ban(member, reason=reason)
        await reminder.create_timer(
            duration.dt, 'tempban', ctx.guild.id, ctx.author.id, member.id, created=ctx.message.created_at
        )
        await ctx.send(f'Banned {member} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempban_timer_complete(self, timer: Timer):
        guild_id, mod_id, member_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        moderator = await self.bot.get_or_fetch_member(guild, mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.fetch_user(mod_id)
            except:
                # request failed somehow
                moderator = f'Mod ID {mod_id}'
            else:
                moderator = f'{moderator} (ID: {mod_id})'
        else:
            moderator = f'{moderator} (ID: {mod_id})'

        reason = f'Automatic unban from timer made on {timer.created_at} by {moderator}.'
        await guild.unban(discord.Object(id=member_id), reason=reason)

    @commands.group(aliases=['purge'])
    @commands.guild_only()
    @checks.has_permissions(manage_messages=True)
    async def remove(self, ctx: GuildContext):
        """Removes messages that meet a criteria.

        In order to use this command, you must have Manage Messages permissions.
        Note that the bot needs Manage Messages as well. These commands cannot
        be used in a private message.

        When the command is done doing its work, you will get a message
        detailing which users got removed and how many messages got removed.
        """

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    async def do_removal(
        self,
        ctx: GuildContext,
        limit: int,
        predicate: Callable[[discord.Message], Any],
        *,
        before: Optional[int] = None,
        after: Optional[int] = None,
    ):
        if limit > 2000:
            return await ctx.send(f'Too many messages to search given ({limit}/2000)')

        if before is None:
            passed_before = ctx.message
        else:
            passed_before = discord.Object(id=before)

        if after is not None:
            passed_after = discord.Object(id=after)
        else:
            passed_after = None

        try:
            deleted = await ctx.channel.purge(limit=limit, before=passed_before, after=passed_after, check=predicate)
        except discord.Forbidden as e:
            return await ctx.send('I do not have permissions to delete messages.')
        except discord.HTTPException as e:
            return await ctx.send(f'Error: {e} (try a smaller search?)')

        spammers = Counter(m.author.display_name for m in deleted)
        deleted = len(deleted)
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'**{name}**: {count}' for name, count in spammers)

        to_send = '\n'.join(messages)

        if len(to_send) > 2000:
            await ctx.send(f'Successfully removed {deleted} messages.', delete_after=10)
        else:
            await ctx.send(to_send, delete_after=10)

    @remove.command()
    async def embeds(self, ctx: GuildContext, search: int = 100):
        """Removes messages that have embeds in them."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds))

    @remove.command()
    async def files(self, ctx: GuildContext, search: int = 100):
        """Removes messages that have attachments in them."""
        await self.do_removal(ctx, search, lambda e: len(e.attachments))

    @remove.command()
    async def images(self, ctx: GuildContext, search: int = 100):
        """Removes messages that have embeds or attachments."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds) or len(e.attachments))

    @remove.command(name='all')
    async def _remove_all(self, ctx: GuildContext, search: int = 100):
        """Removes all messages."""
        await self.do_removal(ctx, search, lambda e: True)

    @remove.command()
    async def user(self, ctx: GuildContext, member: discord.Member, search: int = 100):
        """Removes all messages by the member."""
        await self.do_removal(ctx, search, lambda e: e.author == member)

    @remove.command()
    async def contains(self, ctx: GuildContext, *, substr: str):
        """Removes all messages containing a substring.

        The substring must be at least 3 characters long.
        """
        if len(substr) < 3:
            await ctx.send('The substring length must be at least 3 characters.')
        else:
            await self.do_removal(ctx, 100, lambda e: substr in e.content)

    @remove.command(name='bot', aliases=['bots'])
    async def _bot(self, ctx: GuildContext, prefix: Optional[str] = None, search: int = 100):
        """Removes a bot user's messages and messages with their optional prefix."""

        def predicate(m):
            return (m.webhook_id is None and m.author.bot) or (prefix and m.content.startswith(prefix))

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='emoji', aliases=['emojis'])
    async def _emoji(self, ctx: GuildContext, search: int = 100):
        """Removes all messages containing custom emoji."""
        custom_emoji = re.compile(r'<a?:[a-zA-Z0-9\_]+:([0-9]+)>')

        def predicate(m):
            return custom_emoji.search(m.content)

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='reactions')
    async def _reactions(self, ctx: GuildContext, search: int = 100):
        """Removes all reactions from messages that have them."""

        if search > 2000:
            return await ctx.send(f'Too many messages to search for ({search}/2000)')

        total_reactions = 0
        async for message in ctx.history(limit=search, before=ctx.message):
            if len(message.reactions):
                total_reactions += sum(r.count for r in message.reactions)
                await message.clear_reactions()

        await ctx.send(f'Successfully removed {total_reactions} reactions.')

    @remove.command()
    async def custom(self, ctx: GuildContext, *, arguments: str):
        """A more advanced purge command.

        This command uses a powerful "command line" syntax.
        Most options support multiple values to indicate 'any' match.
        If the value has spaces it must be quoted.

        The messages are only deleted if all options are met unless
        the `--or` flag is passed, in which case only if any is met.

        The following options are valid.

        `--user`: A mention or name of the user to remove.
        `--contains`: A substring to search for in the message.
        `--starts`: A substring to search if the message starts with.
        `--ends`: A substring to search if the message ends with.
        `--search`: How many messages to search. Default 100. Max 2000.
        `--after`: Messages must come after this message ID.
        `--before`: Messages must come before this message ID.

        Flag options (no arguments):

        `--bot`: Check if it's a bot user.
        `--embeds`: Check if the message has embeds.
        `--files`: Check if the message has attachments.
        `--emoji`: Check if the message has custom emoji.
        `--reactions`: Check if the message has reactions
        `--or`: Use logical OR for all options.
        `--not`: Use logical NOT for all options.
        """
        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('--user', nargs='+')
        parser.add_argument('--contains', nargs='+')
        parser.add_argument('--starts', nargs='+')
        parser.add_argument('--ends', nargs='+')
        parser.add_argument('--or', action='store_true', dest='_or')
        parser.add_argument('--not', action='store_true', dest='_not')
        parser.add_argument('--emoji', action='store_true')
        parser.add_argument('--bot', action='store_const', const=lambda m: m.author.bot)
        parser.add_argument('--embeds', action='store_const', const=lambda m: len(m.embeds))
        parser.add_argument('--files', action='store_const', const=lambda m: len(m.attachments))
        parser.add_argument('--reactions', action='store_const', const=lambda m: len(m.reactions))
        parser.add_argument('--search', type=int)
        parser.add_argument('--after', type=int)
        parser.add_argument('--before', type=int)

        try:
            args = parser.parse_args(shlex.split(arguments))
        except Exception as e:
            await ctx.send(str(e))
            return

        predicates = []
        if args.bot:
            predicates.append(args.bot)

        if args.embeds:
            predicates.append(args.embeds)

        if args.files:
            predicates.append(args.files)

        if args.reactions:
            predicates.append(args.reactions)

        if args.emoji:
            custom_emoji = re.compile(r'<:(\w+):(\d+)>')
            predicates.append(lambda m: custom_emoji.search(m.content))

        if args.user:
            users = []
            converter = commands.MemberConverter()
            for u in args.user:
                try:
                    user = await converter.convert(ctx, u)
                    users.append(user)
                except Exception as e:
                    await ctx.send(str(e))
                    return

            predicates.append(lambda m: m.author in users)

        if args.contains:
            predicates.append(lambda m: any(sub in m.content for sub in args.contains))

        if args.starts:
            predicates.append(lambda m: any(m.content.startswith(s) for s in args.starts))

        if args.ends:
            predicates.append(lambda m: any(m.content.endswith(s) for s in args.ends))

        op = all if not args._or else any

        def predicate(m):
            r = op(p(m) for p in predicates)
            if args._not:
                return not r
            return r

        if args.after:
            if args.search is None:
                args.search = 2000

        if args.search is None:
            args.search = 100

        args.search = max(0, min(2000, args.search))  # clamp from 0-2000
        await self.do_removal(ctx, args.search, predicate, before=args.before, after=args.after)

    # Mute related stuff

    async def update_mute_role(
        self, ctx: GuildContext, config: Optional[ModConfig], role: discord.Role, *, merge: bool = False
    ) -> None:
        guild = ctx.guild
        if config and merge:
            members = config.muted_members
            # If the roles are being merged then the old members should get the new role
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id}): Merging mute roles'
            async for member in self.bot.resolve_member_ids(guild, members):
                if not member._roles.has(role.id):
                    try:
                        await member.add_roles(role, reason=reason)
                    except discord.HTTPException:
                        pass
        else:
            members = set()

        members.update(map(lambda m: m.id, role.members))
        query = """INSERT INTO guild_mod_config (id, mute_role_id, muted_members)
                   VALUES ($1, $2, $3::bigint[]) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id,
                       muted_members = EXCLUDED.muted_members
                """
        await self.bot.pool.execute(query, guild.id, role.id, list(members))
        self.get_guild_config.invalidate(self, guild.id)

    @staticmethod
    async def update_mute_role_permissions(
        role: discord.Role, guild: discord.Guild, invoker: discord.abc.User
    ) -> tuple[int, int, int]:
        success = 0
        failure = 0
        skipped = 0
        reason = f'Action done by {invoker} (ID: {invoker.id})'
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.manage_roles:
                overwrite = channel.overwrites_for(role)
                overwrite.send_messages = False
                overwrite.add_reactions = False
                overwrite.use_application_commands = False
                overwrite.create_private_threads = False
                overwrite.create_public_threads = False
                overwrite.send_messages_in_threads = False
                try:
                    await channel.set_permissions(role, overwrite=overwrite, reason=reason)
                except discord.HTTPException:
                    failure += 1
                else:
                    success += 1
            else:
                skipped += 1
        return success, failure, skipped

    @commands.group(name='mute', invoke_without_command=True)
    @can_mute()
    async def _mute(
        self,
        ctx: ModGuildContext,
        members: commands.Greedy[discord.Member],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Mutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        assert ctx.guild_config.mute_role_id is not None
        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to mute.')

        failed = 0
        for member in members:
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Muted [{total - failed}/{total}]')

    @commands.command(name='unmute')
    @can_mute()
    async def _unmute(
        self,
        ctx: ModGuildContext,
        members: commands.Greedy[discord.Member],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Unmutes members using the configured mute role.

        The bot must have Manage Roles permission and be
        above the muted role in the hierarchy.

        To use this command you need to be higher than the
        mute role in the hierarchy and have Manage Roles
        permission at the server level.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        assert ctx.guild_config.mute_role_id is not None
        role = discord.Object(id=ctx.guild_config.mute_role_id)
        total = len(members)
        if total == 0:
            return await ctx.send('Missing members to unmute.')

        failed = 0
        for member in members:
            try:
                await member.remove_roles(role, reason=reason)
            except discord.HTTPException:
                failed += 1

        if failed == 0:
            await ctx.send('\N{THUMBS UP SIGN}')
        else:
            await ctx.send(f'Unmuted [{total - failed}/{total}]')

    @commands.command()
    @can_mute()
    async def tempmute(
        self,
        ctx: ModGuildContext,
        duration: time.FutureTime,
        member: discord.Member,
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Temporarily mutes a member for the specified duration.

        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2024-12-31".

        Note that times are in UTC.

        This has the same permissions as the `mute` command.
        """

        if reason is None:
            reason = f'Action done by {ctx.author} (ID: {ctx.author.id})'

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        assert ctx.guild_config.mute_role_id is not None
        role_id = ctx.guild_config.mute_role_id
        await member.add_roles(discord.Object(id=role_id), reason=reason)
        await reminder.create_timer(
            duration.dt, 'tempmute', ctx.guild.id, ctx.author.id, member.id, role_id, created=ctx.message.created_at
        )
        await ctx.send(f'Muted {discord.utils.escape_mentions(str(member))} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempmute_timer_complete(self, timer):
        guild_id, mod_id, member_id, role_id = timer.args
        await self.bot.wait_until_ready()

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return

        member = await self.bot.get_or_fetch_member(guild, member_id)
        if member is None or not member._roles.has(role_id):
            # They left or don't have the role any more so it has to be manually changed in the SQL
            # if applicable, of course
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))
            return

        if mod_id != member_id:
            moderator = await self.bot.get_or_fetch_member(guild, mod_id)
            if moderator is None:
                try:
                    moderator = await self.bot.fetch_user(mod_id)
                except:
                    # request failed somehow
                    moderator = f'Mod ID {mod_id}'
                else:
                    moderator = f'{moderator} (ID: {mod_id})'
            else:
                moderator = f'{moderator} (ID: {mod_id})'

            reason = f'Automatic unmute from timer made on {timer.created_at} by {moderator}.'
        else:
            reason = f'Expiring self-mute made on {timer.created_at} by {member}'

        try:
            await member.remove_roles(discord.Object(id=role_id), reason=reason)
        except discord.HTTPException:
            # if the request failed then just do it manually
            async with self._batch_lock:
                self._data_batch[guild_id].append((member_id, False))

    @_mute.group(name='role', invoke_without_command=True)
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def _mute_role(self, ctx: GuildContext):
        """Shows configuration of the mute role.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is not None:
            members = config.muted_members.copy()  # type: ignore  # This is already narrowed
            members.update(map(lambda r: r.id, role.members))
            total = len(members)
            role = f'{role} (ID: {role.id})'
        else:
            total = 0
        await ctx.send(f'Role: {role}\nMembers Muted: {total}')

    @_mute_role.command(name='set')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    @commands.cooldown(1, 60.0, commands.BucketType.guild)
    async def mute_role_set(self, ctx: GuildContext, *, role: discord.Role):
        """Sets the mute role to a pre-existing role.

        This command can only be used once every minute.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        if role.is_default():
            return await ctx.send('Cannot use the @\u200beveryone role.')

        if role > ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send('This role is higher than your highest role.')

        if role > ctx.me.top_role:
            return await ctx.send('This role is higher than my highest role.')

        config = await self.get_guild_config(ctx.guild.id)
        has_pre_existing = config is not None and config.mute_role is not None
        merge: bool | None = False

        if has_pre_existing:
            msg = (
                '\N{WARNING SIGN} **There seems to be a pre-existing mute role set up.**\n\n'
                'If you want to merge the pre-existing member data with the new member data press the Merge button.\n'
                'If you want to replace pre-existing member data with the new member data press the Replace button.\n\n'
                '**Note: Merging is __slow__. It will also add the role to every possible member that needs it.**'
            )

            view = PreExistingMuteRoleView(ctx.author)
            view.message = await ctx.send(msg, view=view)
            await view.wait()
            if view.merge is None:
                return
            merge = view.merge
        else:
            muted_members = len(role.members)
            if muted_members > 0:
                msg = f'Are you sure you want to make this the mute role? It has {plural(muted_members):member}.'
                confirm = await ctx.prompt(msg)
                if not confirm:
                    merge = None

        if merge is None:
            return await ctx.send('Aborting.')

        async with ctx.typing():
            await self.update_mute_role(ctx, config, role, merge=merge)
            escaped = discord.utils.escape_mentions(role.name)
            await ctx.send(
                f'Successfully set the {escaped} role as the mute role.\n\n'
                '**Note: Permission overwrites have not been changed.**'
            )

    @_mute_role.command(name='update', aliases=['sync'])
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_update(self, ctx: GuildContext):
        """Updates the permission overwrites of the mute role.

        This works by blocking the Send Messages and Add Reactions
        permission on every text channel that the bot can do.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        config = await self.get_guild_config(ctx.guild.id)
        role = config and config.mute_role
        if role is None:
            return await ctx.send('No mute role has been set up to update.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            total = success + failure + skipped
            await ctx.send(
                f'Attempted to update {total} channel permissions. '
                f'[Updated: {success}, Failed: {failure}, Skipped (no permissions): {skipped}]'
            )

    @_mute_role.command(name='create')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_create(self, ctx: GuildContext, *, name):
        """Creates a mute role with the given name.

        This also updates the channel overwrites accordingly
        if wanted.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """

        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is not None and config.mute_role is not None:
            return await ctx.send('A mute role already exists.')

        try:
            role = await ctx.guild.create_role(name=name, reason=f'Mute Role Created By {ctx.author} (ID: {ctx.author.id})')
        except discord.HTTPException as e:
            return await ctx.send(f'An error happened: {e}')

        query = """INSERT INTO guild_mod_config (id, mute_role_id)
                   VALUES ($1, $2) ON CONFLICT (id)
                   DO UPDATE SET
                       mute_role_id = EXCLUDED.mute_role_id;
                """
        await ctx.db.execute(query, guild_id, role.id)
        self.get_guild_config.invalidate(self, guild_id)

        confirm = await ctx.prompt('Would you like to update the channel overwrites as well?')
        if not confirm:
            return await ctx.send('Mute role successfully created.')

        async with ctx.typing():
            success, failure, skipped = await self.update_mute_role_permissions(role, ctx.guild, ctx.author)
            await ctx.send(
                'Mute role successfully created. Overwrites: ' f'[Updated: {success}, Failed: {failure}, Skipped: {skipped}]'
            )

    @_mute_role.command(name='unbind')
    @checks.has_guild_permissions(moderate_members=True, manage_roles=True)
    async def mute_role_unbind(self, ctx: GuildContext):
        """Unbinds a mute role without deleting it.

        To use these commands you need to have Manage Roles
        and Manage Server permission at the server level.
        """
        guild_id = ctx.guild.id
        config = await self.get_guild_config(guild_id)
        if config is None or config.mute_role is None:
            return await ctx.send('No mute role has been set up.')

        muted_members = len(config.muted_members)
        if muted_members > 0:
            msg = f'Are you sure you want to unbind and unmute {plural(muted_members):member}?'
            confirm = await ctx.prompt(msg)
            if not confirm:
                return await ctx.send('Aborting.')

        query = """UPDATE guild_mod_config SET (mute_role_id, muted_members) = (NULL, '{}'::bigint[]) WHERE id=$1;"""
        await self.bot.pool.execute(query, guild_id)
        self.get_guild_config.invalidate(self, guild_id)
        await ctx.send('Successfully unbound mute role.')

    @commands.command()
    @commands.guild_only()
    async def selfmute(self, ctx: GuildContext, *, duration: time.ShortTime):
        """Temporarily mutes yourself for the specified duration.

        The duration must be in a short time form, e.g. 4h. Can
        only mute yourself for a maximum of 24 hours and a minimum
        of 5 minutes.

        Do not ask a moderator to unmute you.
        """

        reminder = self.bot.reminder
        if reminder is None:
            return await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')

        config = await self.get_guild_config(ctx.guild.id)
        role_id = config and config.mute_role_id
        if role_id is None:
            raise NoMuteRole()

        if ctx.author._roles.has(role_id):
            return await ctx.send('Somehow you are already muted <:rooThink:596576798351949847>')

        created_at = ctx.message.created_at
        if duration.dt > (created_at + datetime.timedelta(days=1)):
            return await ctx.send('Duration is too long. Must be at most 24 hours.')

        if duration.dt < (created_at + datetime.timedelta(minutes=5)):
            return await ctx.send('Duration is too short. Must be at least 5 minutes.')

        delta = time.human_timedelta(duration.dt, source=created_at)
        warning = f'Are you sure you want to be muted for {delta}?\n**Do not ask the moderators to undo this!**'
        confirm = await ctx.prompt(warning)
        if not confirm:
            return await ctx.send('Aborting', delete_after=5.0)

        reason = f'Self-mute for {ctx.author} (ID: {ctx.author.id}) for {delta}'
        await ctx.author.add_roles(discord.Object(id=role_id), reason=reason)
        await reminder.create_timer(
            duration.dt, 'tempmute', ctx.guild.id, ctx.author.id, ctx.author.id, role_id, created=created_at
        )

        await ctx.send(f'\N{OK HAND SIGN} Muted for {delta}. Be sure not to bother anyone about it.')

    @selfmute.error
    async def on_selfmute_error(self, ctx: GuildContext, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('Missing a duration to selfmute for.')

    def _hoisters_magic(self, guild: discord.Guild) -> Optional[discord.File]:
        fmt = []
        for member in guild.members:
            character = ord(member.display_name[0])
            if (character < 65) or (90 < character < 97):
                fmt.append(member)

        if not fmt:
            return
        formatted = '{0:32} || {1:<32} ({2})\n\n'.format('~~ Name ~~', '~~ Nickname ~~', '~~ ID ~~')
        formatted += '\n'.join(f'{member.name:32} || {member.display_name:<32} ({member.id})' for member in fmt)

        out = io.BytesIO(formatted.encode())

        return discord.File(out, filename='hoisters.txt', spoiler=False)

    @commands.hybrid_command(name='hoisters')
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def hoisters_(
        self, ctx: Context, guild: discord.Guild | None = commands.param(converter=commands.GuildConverter(), default=None)
    ) -> None:
        """List the members who are currently hosting in the member list.

        This is currently any punctuation character.
        """

        if guild is not None:
            owner = await ctx.bot.is_owner(ctx.author)
            if owner is False:
                guild = ctx.guild
        else:
            guild = ctx.guild

        if guild is None:
            raise commands.BadArgument('Please be in a guild when running this.')

        file = self._hoisters_magic(guild)
        if file is None:
            await ctx.send('No hoisters here!')
            return

        if ctx.interaction is not None:
            await ctx.send(file=file, ephemeral=True)
            return

        try:
            await ctx.author.send(file=file)
        except discord.Forbidden:
            await ctx.send("I couldn't DM you so here it is...", file=file)

    @commands.command(enabled=False)
    @commands.guild_only()
    @commands.bot_has_guild_permissions(ban_members=True)
    async def selfban(self, ctx: GuildContext) -> None:
        """This is a totally destructive Ban. It won't be undone without begging moderators. By agreeing you agree you're gone forever."""
        confirm = await ctx.prompt('This is a self **ban**. There is no undoing this.')
        if confirm:
            return await ctx.author.ban(reason='Suicide.', delete_message_days=0)

    @commands.command()
    @can_use_block()
    async def block(self, ctx: ModGuildContext, *, member: discord.Member) -> None:
        """Blocks a user from your channel."""

        if member.top_role >= ctx.author.top_role and not self.bot.is_owner(ctx.author):
            return
        reason = f'Block by {ctx.author} (ID: {ctx.author.id})'
        if isinstance(ctx.channel, discord.Thread):
            try:
                await ctx.channel.remove_user(member)
            except:
                await ctx.send('\N{THUMBS DOWN SIGN}')
            else:
                await ctx.send('\N{THUMBS UP SIGN}')
            return
        try:
            await ctx.channel.set_permissions(
                member,
                send_messages=False,
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                reason=reason,
            )
        except:
            await ctx.channel.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.channel.send('\N{THUMBS UP SIGN}')

    @commands.command()
    @can_use_block()
    async def unblock(self, ctx: ModGuildContext, *, member: discord.Member) -> None:
        """Unblocks a user from your channel."""

        if member.top_role >= ctx.author.top_role and not self.bot.is_owner(ctx.author):
            return
        reason = f'Unblock by {ctx.author} (ID: {ctx.author.id})'
        if isinstance(ctx.channel, discord.Thread):
            try:
                await ctx.channel.add_user(member)
            except:
                await ctx.send('\N{THUMBS DOWN SIGN}')
            else:
                await ctx.send('\N{THUMBS UP SIGN}')
            return
        try:
            await ctx.channel.set_permissions(
                member,
                send_messages=None,
                add_reactions=None,
                create_public_threads=None,
                create_private_threads=None,
                send_messages_in_threads=None,
                reason=reason,
            )
        except:
            await ctx.channel.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.channel.send('\N{THUMBS UP SIGN}')

    @commands.command()
    @can_use_block()
    async def tempblock(self, ctx: ModGuildContext, duration: time.FutureTime, *, member: discord.Member) -> None:
        """Temporarily blocks a user from your channel.

        The duration can be a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2017-12-31".

        Note that times are in UTC.
        """

        if member.top_role >= ctx.author.top_role and not self.bot.is_owner(ctx.author):
            return

        created_at = ctx.message.created_at

        reminder = self.bot.reminder
        if reminder is None:
            await ctx.send('Sorry, this functionality is currently unavailable. Try again later?')
            return
        if isinstance(ctx.channel, discord.Thread):
            await ctx.send('Cannot execute tempblock in threads. Use block instead.')
            return
        await reminder.create_timer(
            duration.dt,
            'tempblock',
            ctx.guild.id,
            ctx.author.id,
            ctx.channel.id,
            member.id,
            created=created_at,
        )
        reason = f'Tempblock by {ctx.author} (ID: {ctx.author.id}) until {duration.dt}'
        try:
            await ctx.channel.set_permissions(
                member,
                send_messages=False,
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                reason=reason,
            )
        except:
            await ctx.channel.send('\N{THUMBS DOWN SIGN}')
        else:
            await ctx.channel.send(f'Blocked {member} for {time.format_relative(duration.dt)}.')

    @commands.Cog.listener()
    async def on_tempblock_timer_complete(self, timer: Timer) -> None:
        guild_id, mod_id, channel_id, member_id = timer.args
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # RIP
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            # RIP x2
            return
        to_unblock = await self.bot.get_or_fetch_member(guild, member_id)
        if to_unblock is None:
            # RIP x3
            return
        moderator = await self.bot.get_or_fetch_member(guild, mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.fetch_user(mod_id)
            except:
                # request failed somehow
                moderator = f'Mod ID {mod_id}'
            else:
                moderator = f'{moderator} (ID: {mod_id})'
        else:
            moderator = f'{moderator} (ID: {mod_id})'
        reason = f'Automatic unblock from timer made on {timer.created_at} by {moderator}.'
        try:
            await channel.set_permissions(
                to_unblock,
                send_messages=None,
                add_reactions=None,
                create_public_threads=None,
                create_private_threads=None,
                send_messages_in_threads=None,
                reason=reason,
            )
        except:
            pass


async def setup(bot: Ayaka):
    await bot.add_cog(Mod(bot))
