"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import colorsys
import datetime
import inspect
import itertools
import json
import os
import pathlib
import textwrap
import unicodedata
from collections import Counter, defaultdict
from io import BytesIO
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands, menus

from utils import checks, formats, time
from utils._types.discord_ import MessageableGuildChannel
from utils.context import Context, GuildContext
from utils.paginator import RoboPages, TextPageSource
from utils.ui import AvatarView


if TYPE_CHECKING:
    from bot import Ayaka


GuildChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.CategoryChannel | discord.Thread


class Prefix(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> str:
        user_id = ctx.bot.user.id
        if argument.startswith((f'<@{user_id}>', f'<@!{user_id}>')):
            raise commands.BadArgument('That is a reserved prefix already in use.')
        return argument


class GroupHelpPageSource(menus.ListPageSource):
    def __init__(self, group: commands.Group | commands.Cog, commands: list[commands.Command], *, prefix: str):
        super().__init__(entries=commands, per_page=6)
        self.group = group
        self.prefix = prefix
        self.title = f'{self.group.qualified_name} Commands'
        self.description = self.group.description

    async def format_page(self, menu, commands):
        embed = discord.Embed(title=self.title, description=self.description, color=discord.Colour.blurple())

        for command in commands:
            signature = f'{command.qualified_name} {command.signature}'
            embed.add_field(name=signature, value=command.short_doc or 'No help given...', inline=False)

        maximum = self.get_max_pages()
        if maximum > 1:
            embed.set_author(name=f'Page {menu.current_page + 1}/{maximum} ({len(self.entries)} commands)')

        embed.set_footer(text=f'Use "{self.prefix}help command" for more info on a command.')
        return embed


class HelpSelectMenu(discord.ui.Select['HelpMenu']):
    def __init__(self, commands: dict[commands.Cog, list[commands.Command]], bot: Ayaka, *, private: bool | None = None):
        if private is False:
            placeholder = 'Public Categories...'
        elif private is True:
            placeholder = 'Private Categories...'
        else:
            placeholder = 'Select a category...'
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=int(private or 0))
        self.commands = commands
        self.bot = bot
        self.private = private
        self.__fill_options()

    def __fill_options(self) -> None:
        if self.row == 0:
            self.add_option(
                label='Index',
                emoji='\N{WAVING HAND SIGN}',
                value='__index',
                description='The help page showing how to use the bot.',
            )
        for cog, commands in self.commands.items():
            if not commands:
                continue
            if self.private is False and 'private' in cog.__module__:
                continue
            if self.private is True and 'private' not in cog.__module__:
                continue
            description = cog.description.split('\n', 1)[0] or None
            emoji = getattr(cog, 'display_emoji', None)
            self.add_option(label=cog.qualified_name, value=cog.qualified_name, description=description, emoji=emoji)

    async def callback(self, interaction: discord.Interaction):
        assert self.view is not None
        value = self.values[0]
        if value == '__index':
            await self.view.rebind(FrontPageSource(), interaction)
        else:
            cog = self.bot.get_cog(value)
            if cog is None:
                await interaction.response.send_message('Somehow this category does not exit?', ephemeral=True)
                return

            commands = self.commands[cog]
            if not commands:
                await interaction.response.send_message('This category has no commands for you', ephemeral=True)
                return

            source = GroupHelpPageSource(cog, commands, prefix=self.view.ctx.clean_prefix)
            await self.view.rebind(source, interaction)


class FrontPageSource(menus.PageSource):
    def is_paginating(self) -> bool:
        # This forces the buttons to appear even in the front page
        return True

    def get_max_pages(self) -> int | None:
        # There's only one actual page in the front page
        # However we need at least 2 to show all the buttons
        return 2

    async def get_page(self, page_number: int) -> Any:
        # The front page is a dummy
        self.index = page_number
        return self

    def format_page(self, menu: HelpMenu, page):
        embed = discord.Embed(title='Bot Help', colour=discord.Colour.blurple())
        embed.description = inspect.cleandoc(
            f"""
            Hello! Welcome to the help page.
            
            Use '{menu.ctx.clean_prefix}help command' for more info on a command.
            Use '{menu.ctx.clean_prefix}help category' for more info on a category.
            Use the dropdown menu below to select a category.
            """
        )
        embed.add_field(
            name='Support Server',
            value='For more help, consider joining the official server over at https://discord.gg/s44CFagYN2',
            inline=False,
        )

        if self.index == 0:
            embed.add_field(
                name='Who are you?',
                value=(
                    "I'm a bot made by VJ#5945. I'm a fork of R. Danny#6348 with some edits. "
                    'I have features such as moderation, starboard, tags, reminders, and more. '
                    'You can get more information on my commands by using the dropdown below.\n\n'
                    "I'm also open source. You can see my code on [GitHub](https://github.com/lmaotrigine/Ayaka)!"
                ),
                inline=False,
            )
        elif self.index == 1:
            entries = (
                ('<argument>', 'This means the argument is __**required**__.'),
                ('[argument]', 'This means the argument is __**optional**__.'),
                ('[A|B]', 'This means the it can be __**either A or B**__.'),
                (
                    '[argument...]',
                    'This means you can have multiple arguments.\n'
                    'Now that you know the basics, it should be noted that...\n'
                    '__**You do not type in the brackets!**__',
                ),
            )

            embed.add_field(name='How do I use this bot?', value='Reading the bot signature is pretty simple.')

            for name, value in entries:
                embed.add_field(name=name, value=value, inline=False)
        return embed


class HelpMenu(RoboPages):
    def __init__(self, source: menus.PageSource, *, ctx: Context):
        super().__init__(source, ctx=ctx, compact=True)

    def add_categories(self, commands: dict[commands.Cog, list[commands.Command]]) -> None:
        self.clear_items()
        if len(commands) > 25:
            self.add_item(HelpSelectMenu(commands, self.ctx.bot, private=False))
            self.add_item(HelpSelectMenu(commands, self.ctx.bot, private=True))
        else:
            self.add_item(HelpSelectMenu(commands, self.ctx.bot))
        self.fill_items()

    async def rebind(self, source: menus.PageSource, interaction: discord.Interaction) -> None:
        self.source = source
        self.current_page = 0

        await self.source._prepare_once()
        page = await self.source.get_page(0)
        kwargs = await self._get_kwargs_from_page(page)
        self._update_labels(0)
        await interaction.response.edit_message(**kwargs, view=self)


class PaginatedHelpCommand(commands.HelpCommand):
    context: Context

    def __init__(self):
        super().__init__(
            command_attrs={
                'cooldown': commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.member),
                'help': 'Shows help about the bot, a command, or a category',
            }
        )

    async def on_help_command_error(self, ctx: Context, error: Exception):
        if isinstance(error, commands.CommandInvokeError):
            # Ignore missing permission errors
            if isinstance(error.original, discord.HTTPException) and error.original.code == 50013:
                return
            await ctx.send(str(error.original))

    def get_command_signature(self, command: commands.Group | commands.Command) -> str:
        parent = command.full_parent_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            fmt = f'[{command.name}|{aliases}]'
            if parent:
                fmt = f'{parent} {fmt}'
            alias = fmt
        else:
            alias = command.name if not parent else f'{parent} {command.name}'
        return f'{alias} {command.signature}'

    async def send_bot_help(self, mapping):
        bot = self.context.bot

        def key(command: commands.Command) -> str:
            cog = command.cog
            return cog.qualified_name if cog else '\U0010ffff'

        entries: list[commands.Command] = await self.filter_commands(bot.commands, sort=True, key=key)

        all_commands: dict[commands.Cog, list[commands.Command]] = defaultdict(list)
        for name, children in itertools.groupby(entries, key=key):
            if name == '\U0010ffff':
                continue

            if name.startswith('__Private__'):
                cog = bot.get_cog('Private')
            else:
                cog = bot.get_cog(name)
            all_commands[cog] += sorted(children, key=lambda c: c.qualified_name)  # type: ignore

        menu = HelpMenu(FrontPageSource(), ctx=self.context)
        menu.add_categories(all_commands)
        await menu.start()

    async def send_cog_help(self, cog: commands.Cog):
        entries = await self.filter_commands(cog.get_commands(), sort=True)
        menu = HelpMenu(GroupHelpPageSource(cog, entries, prefix=self.context.clean_prefix), ctx=self.context)
        await menu.start()

    def common_command_formatting(self, embed_like, command):
        embed_like.title = self.get_command_signature(command)
        if command.description:
            embed_like.description = f'{command.description}\n\n{command.help}'
        else:
            embed_like.description = command.help or 'No help found...'

    async def send_command_help(self, command: commands.Command):
        # No pagination necessary for a single command.
        embed = discord.Embed(colour=discord.Colour.blurple())
        self.common_command_formatting(embed, command)
        await self.context.send(embed=embed)

    async def send_group_help(self, group: commands.Group):
        subcommands = group.commands
        if len(subcommands) == 0:
            return await self.send_command_help(group)

        entries = await self.filter_commands(subcommands, sort=True)
        if len(entries) == 0:
            return await self.send_command_help(group)

        source = GroupHelpPageSource(group, entries, prefix=self.context.clean_prefix)
        self.common_command_formatting(source, group)
        menu = HelpMenu(source, ctx=self.context)
        await menu.start()


class FeedbackModal(discord.ui.Modal, title='Submit Feedback'):
    summary = discord.ui.TextInput(label='Summary', placeholder='A brief explanation of what you want')
    details = discord.ui.TextInput(label='Details', style=discord.TextStyle.long, required=False)

    def __init__(self, cog: Meta) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = self.cog.feedback_channel
        if channel is None:
            await interaction.response.send_message('Could not submit your feedback, sorry about this.', ephemeral=True)
            return

        embed = self.cog.get_feedback_embed(interaction, summary=str(self.summary), details=self.details.value)
        await channel.send(embed=embed)
        await interaction.response.send_message('Successfully submitted feedback', ephemeral=True)


class Meta(commands.Cog):
    """Commands for utilities related to Discord or the Bot itself."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self.bot._original_help_command = bot.help_command
        self.bot.help_command = PaginatedHelpCommand()
        self.bot.help_command.cog = self

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{WHITE QUESTION MARK ORNAMENT}')

    def cog_unload(self) -> None:
        assert self.bot._original_help_command is not None
        self.bot.help_command = self.bot._original_help_command

    async def cog_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))

    @commands.command()
    async def charinfo(self, ctx: Context, *, characters: str) -> None:
        """Shows you information about a number of characters.

        Only up to 25 characters at a time.
        """
        if len(characters) > 25:
            raise commands.BadArgument('Too many characters.')

        def to_string(c):
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'

        msg = '\n'.join(map(to_string, characters))
        if len(msg) > 2000:
            await ctx.send('Output too long to display.')
            return
        await ctx.send(msg)

    @commands.command()
    @checks.mod_or_permissions(manage_nicknames=True)
    async def decancer(self, ctx: GuildContext, *, user: discord.Member | None = None) -> None:
        """Normalises username to make it mentionable."""

        def ensure_user(user: discord.Member | None) -> discord.Member:
            if user is None:
                if ctx.message.reference:
                    ref = ctx.message.reference.resolved
                    if isinstance(ref, discord.Message):
                        return ref.author  # type: ignore
                raise commands.MissingRequiredArgument(commands.Parameter('user', kind=inspect.Parameter.KEYWORD_ONLY))
            return user

        user = ensure_user(user)
        async with self.bot.session.get('https://api.5ht2.me/decancer', params={'text': user.display_name}) as resp:
            if resp.status != 200:
                await ctx.send(f'Something went wrong with the API. Tell VJ: {await resp.text()}')
                return
            js = await resp.json()
        decancered = js['decancered']
        await user.edit(nick=decancered, reason=f'decancer by {ctx.author}')
        await ctx.send('\N{OK HAND SIGN}')

    @commands.group(name='prefix', invoke_without_command=True)
    async def prefix(self, ctx: GuildContext) -> None:
        """Manages the server's prefixes.

        If called without a subcommand, this will list the currently set
        prefixes.
        """

        prefixes = self.bot.get_guild_prefixes(ctx.guild)

        # we want to remove prefix #2, because it's the 2nd form of the mention
        # and to the end user, this would end up making them confused why the
        # mention is there twice
        del prefixes[1]

        e = discord.Embed(title='Prefixes', colour=discord.Colour.blurple())
        e.set_footer(text=f'{len(prefixes)} prefixes')
        e.description = '\n'.join(f'{index}. {elem}' for index, elem in enumerate(prefixes, start=1))
        await ctx.send(embed=e)

    @app_commands.command(name='prefix')
    async def slash_prefix(self, interaction: discord.Interaction) -> None:
        """Lists currently configured prefixes."""

        prefixes = self.bot.get_guild_prefixes(interaction.guild)
        del prefixes[1]
        e = discord.Embed(title='Prefixes', colour=discord.Colour.blurple())
        e.set_footer(text=f'{len(prefixes)} prefixes')
        e.description = '\n'.join(f'{index}. {elem}' for index, elem in enumerate(prefixes, start=1))
        await interaction.response.send_message(embed=e)

    @prefix.command(name='add', ignore_extra=False)
    @checks.is_manager()
    async def prefix_add(self, ctx: GuildContext, prefix: str = commands.param(converter=Prefix)) -> None:
        """Appends a prefix to the list of custom prefixes.

        Previously set prefixes are not overridden.

        To have a word prefix, you should quote it and end it with
        a space, e.g. 'hello ' to set the prefix to 'hello '. This
        is because Discord removes spaces when sending messages so
        the spaces are not preserved.

        Multi-word prefixes must be quoted also.

        You must have Manage Server permission to use this command.
        """

        current_prefixes = self.bot.get_raw_guild_prefixes(ctx.guild.id)
        current_prefixes.append(prefix)
        try:
            await self.bot.set_guild_prefixes(ctx.guild, current_prefixes)
        except Exception as e:
            await ctx.send(f'{ctx.tick(False)} {e}')
        else:
            await ctx.send(ctx.tick(True))

    @prefix_add.error
    async def prefix_add_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.TooManyArguments):
            await ctx.send("You've given too many prefixes. Either quote it or only do it one by one.")

    @prefix.command(name='remove', aliases=['delete'], ignore_extra=False)
    @checks.is_manager()
    async def prefix_remove(self, ctx: GuildContext, prefix: str = commands.param(converter=Prefix)) -> None:
        """Removes a prefix from the list of custom prefixes.

        This is the inverse of the 'prefix add' command. You can
        use this to remove prefixes from the default set as well.

        You must have Manage Server permission to use this command.
        """

        current_prefixes = self.bot.get_raw_guild_prefixes(ctx.guild.id)

        try:
            current_prefixes.remove(prefix)
        except ValueError:
            await ctx.send('I do not have this preix registered.')
            return

        try:
            await self.bot.set_guild_prefixes(ctx.guild, current_prefixes)
        except Exception as e:
            await ctx.send(f'{ctx.tick(False)} {e}')
        else:
            await ctx.send(ctx.tick(True))

    @prefix.command(name='clear')
    @checks.is_manager()
    async def prefix_clear(self, ctx: GuildContext) -> None:
        """Removes all custom prefixes.

        After this, the bot will listen to only mention prefixes.

        You must have Manage Server permission to use this command.
        """

        await self.bot.set_guild_prefixes(ctx.guild, [])
        await ctx.send(ctx.tick(True))

    @commands.command(name='quit', aliases=['shutdown', 'logout', 'sleep', 'die', 'restart'])
    @commands.is_owner()
    async def _quit(self, ctx: Context) -> None:
        """Quits the bot."""

        await ctx.send('さようなら')
        await self.bot.close()

    @staticmethod
    def _iterate_source_line_counts(root: pathlib.Path) -> Iterator[int]:
        for child in root.iterdir():
            if child.name.startswith('.'):
                continue
            if child.is_dir():
                yield from Meta._iterate_source_line_counts(child)
            else:
                if child.suffix in ('.py', '.html', '.css'):
                    with child.open(encoding='utf-8') as f:
                        yield len(f.readlines())

    @staticmethod
    def count_source_lines(root: pathlib.Path) -> int:
        return sum(Meta._iterate_source_line_counts(root))

    @commands.command()
    async def cloc(self, ctx: Context) -> None:
        """."""
        await ctx.send(f'I am made of only {self.count_source_lines(pathlib.Path(__file__).parent.parent):,} lines.')

    @commands.command()
    async def source(self, ctx: Context, *, command: str | None = None) -> None:
        """Displays my full source code or for a specific command.

        To display the source code of a subcommand you can separate it by
        periods, e.g. tag.create for the create subcommand of the tag command
        or by spaces.
        """
        source_url = 'https://github.com/lmaotrigine/Ayaka'
        branch = 'v2'
        if command is None:
            await ctx.send(source_url)
            return

        if command == 'help':
            src = type(self.bot.help_command)
            module = src.__module__
            filename = inspect.getsourcefile(src)
        else:
            obj = self.bot.get_command(command.replace('.', ' '))
            if obj is None:
                await ctx.send('Could not find comamnd.')
                return

            # since we found the command we're looking for, presumably anyway, let's
            # try to access the code itself
            src = obj.callback.__code__
            module = obj.callback.__module__
            filename = src.co_filename

        lines, firstlineno = inspect.getsourcelines(src)
        if not module.startswith('discord'):
            # not a built-in command
            if 'cogs.private' in module:
                # private commands
                source_url = 'https://github.com/lmaotrigine/ayaka-private'
                location = module[13:].replace('.', '/') + '.py'
                branch = 'main'
            else:
                location = os.path.relpath(filename).replace('\\', '/')  # type: ignore
        else:
            location = module.replace('.', '/') + '.py'
            source_url = 'https://github.com/Rapptz/discord.py'
            branch = 'master'
        final_url = f'<{source_url}/blob/{branch}/{location}#L{firstlineno}-L{firstlineno + len(lines) - 1}>'
        await ctx.send(final_url)

    @commands.command()
    async def avatar(self, ctx: Context, *, user: discord.Member | discord.User | None = None) -> None:
        """Shows a user's enlarged avatar(if possible)."""

        embed = discord.Embed()
        user = user or ctx.author
        avatar = user.display_avatar.with_static_format('png')
        embed.set_author(name=str(user), url=avatar)
        embed.set_image(url=avatar)
        if isinstance(user, discord.Member) and user.guild_avatar is not None:
            view = AvatarView(user, ctx.author.id)
            view.embed = embed
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)

    @commands.command()
    async def avatarhistory(self, ctx: Context, *, user: discord.Member | discord.User | None = None) -> None:
        """Gives a link with a user's avatar history.

        This only contains global avatars and no guild level changes.
        """

        user = user or ctx.author
        link = f'https://{self.bot.config.base_url}/discord/avatarhistory/{user.id}'
        embed = discord.Embed(colour=discord.Colour.og_blurple())
        embed.description = f'[Avatar history for {user.mention}]({link})'
        await ctx.send(embed=embed)

    @commands.command(aliases=['userinfo'])
    async def info(self, ctx: Context, *, user: discord.Member | discord.User | None = None):
        """Shows info about a user."""

        user = user or ctx.author
        if ctx.guild and isinstance(user, discord.User):
            user = ctx.guild.get_member(user.id) or user

        e = discord.Embed()
        roles = [role.mention for role in user.roles[1:]] if isinstance(user, discord.Member) else ['N/A']
        shared = sum(g.get_member(user.id) is not None for g in self.bot.guilds)
        e.set_author(name=str(user))

        def format_date(dt: datetime.datetime | None):
            if dt is None:
                return 'N/A'
            return f'{dt:%Y-%m-%d %H:%M} ({discord.utils.format_dt(dt, "R")})'

        e.add_field(name='ID', value=user.id, inline=False)
        e.add_field(name='Servers', value=f'{shared} shared', inline=False)
        e.add_field(name='Joined', value=format_date(getattr(user, 'joined_at', None)), inline=False)
        e.add_field(name='Created', value=format_date(user.created_at), inline=False)

        voice = getattr(user, 'voice', None)
        if voice is not None:
            vc = voice.channel
            other_people = len(vc.members) - 1
            voice = f'{vc.name} with {other_people} others' if other_people else f'{vc.name} by themselves'
            e.add_field(name='Voice', value=voice, inline=False)

        if roles:
            e.add_field(name='Roles', value=', '.join(roles) if len(roles) < 10 else f'{len(roles)} roles', inline=False)

        colour = user.colour
        if colour.value:
            e.colour = colour

        if user.avatar:
            e.set_thumbnail(url=user.avatar.url)

        if isinstance(user, discord.User):
            e.set_footer(text='This member is not in this server.')

        await ctx.send(embed=e)

    @commands.command(aliases=['guildinfo'], usage='')
    @commands.guild_only()
    async def serverinfo(self, ctx: GuildContext, *, guild: discord.Guild | None = None) -> None:
        """Shows info about the current server."""

        if await self.bot.is_owner(ctx.author):
            guild = guild or ctx.guild
        else:
            guild = ctx.guild

        roles = [role.mention for role in guild.roles[1:]]
        roles = roles or ['No extra roles']

        # figure out what channels are 'secret'
        everyone = guild.default_role
        everyone_perms = everyone.permissions.value
        secret = Counter()
        totals = Counter()
        for channel in guild.channels:
            allow, deny = channel.overwrites_for(everyone).pair()
            perms = discord.Permissions((everyone_perms & ~deny.value) | allow.value)
            channel_type = type(channel)
            totals[channel_type] += 1
            if not perms.read_messages:
                secret[channel_type] += 1
            elif isinstance(channel, discord.VoiceChannel) and (not perms.connect or not perms.speak):
                secret[channel_type] += 1

        member_by_status = Counter(str(m.status) for m in guild.members)

        e = discord.Embed()
        e.title = guild.name
        e.description = f'**ID**: {guild.id}\n**Owner**: {guild.owner}'
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)

        channel_info = []
        key_to_emoji = {
            discord.TextChannel: '<:text_channel:956843179687174204>',
            discord.VoiceChannel: '<:voice_channel:956843179146108948>',
        }
        for key, total in totals.items():
            secrets = secret[key]
            try:
                emoji = key_to_emoji[key]
            except KeyError:
                continue

            if secrets:
                channel_info.append(f'{emoji} {total} ({secrets} locked)')
            else:
                channel_info.append(f'{emoji} {total}')

        info = []
        features = set(guild.features)

        for feature in features:
            info.append(f'{ctx.tick(True)}: {feature.replace("_", " ").title()}')

        if info:
            e.add_field(name='Features', value='\n'.join(info))

        e.add_field(name='Channels', value='\n'.join(channel_info))

        if guild.premium_tier != 0:
            boosts = f'Level {guild.premium_tier}\n{guild.premium_subscription_count} boosts'
            last_boost = max(guild.members, key=lambda m: m.premium_since or guild.created_at)
            if last_boost.premium_since is not None:
                boosts = f'{boosts}\nLast boost: {last_boost} ({time.human_timedelta(last_boost.premium_since, accuracy=2)})'
            e.add_field(name='Boosts', value=boosts, inline=False)

        bots = sum(m.bot for m in guild.members)
        fmt = (
            f'<:online:957185052234641429> {member_by_status["online"]} '
            f'<:idle:956843179213209621> {member_by_status["idle"]} '
            f'<:dnd:956843179724927016> {member_by_status["dnd"]} '
            f'<:offline:956843179188060190> {member_by_status["offline"]}\n'
            f'Total: {guild.member_count} ({formats.plural(bots):bot})'
        )

        e.add_field(name='Members', value=fmt, inline=False)
        e.add_field(name='Roles', value=', '.join(roles) if len(roles) < 10 else f'{len(roles)} roles')

        emoji_stats = Counter()
        for emoji in guild.emojis:
            if emoji.animated:
                emoji_stats['animated'] += 1
                emoji_stats['animated_disabled'] += not emoji.available
            else:
                emoji_stats['regular'] += 1
                emoji_stats['disabled'] += not emoji.available

        fmt = (
            f'Regular: {emoji_stats["regular"]}/{guild.emoji_limit}\n'
            f'Animated: {emoji_stats["animated"]}/{guild.emoji_limit}\n'
        )
        if emoji_stats['disabled'] or emoji_stats['animated_disabled']:
            fmt = f'{fmt}Disabled: {emoji_stats["disabled"]} regular, {emoji_stats["animated_disabled"]} animated\n'

        fmt = f'{fmt}Total Emoji: {len(guild.emojis)}/{guild.emoji_limit * 2}'
        e.add_field(name='Emoji', value=fmt, inline=False)
        e.set_footer(text='Created').timestamp = guild.created_at
        await ctx.send(embed=e)

    async def say_permissions(self, ctx: Context, member: discord.Member, channel: MessageableGuildChannel) -> None:
        permissions = channel.permissions_for(member)
        e = discord.Embed(colour=member.colour)
        avatar = member.display_avatar.with_static_format('png')
        e.set_author(name=str(member), icon_url=avatar)
        allowed, denied = [], []

        for name, value in permissions:
            name = name.replace('_', ' ').replace('guild', 'server').title()
            if value:
                allowed.append(name)
            else:
                denied.append(name)

        e.add_field(name='Allowed', value='\n'.join(allowed))
        e.add_field(name='Denied', value='\n'.join(denied))
        await ctx.send(embed=e)

    @commands.command()
    @commands.guild_only()
    async def permissions(
        self, ctx: GuildContext, member: discord.Member | None = None, channel: MessageableGuildChannel | None = None
    ) -> None:
        """Shows a member's permissions in a specific channel.

        If no channel is given, then it uses the current one.

        You cannot use this in private messages. If no member is given, then
        the info returned will be yours.
        """
        channel = channel or ctx.channel

        person = member or ctx.author

        await self.say_permissions(ctx, person, channel)

    @commands.command()
    @commands.guild_only()
    async def botpermissions(self, ctx: GuildContext, *, channel: MessageableGuildChannel | None = None) -> None:
        """hows the bot's permissions in a specific channel.

        If no channel is given then it uses the current one.

        This is a good way of checking if the bot has the permissions needed
        to execute the commands it wants to execute.

        You cannot use this in private messages.
        """
        channel = channel or ctx.channel
        member = ctx.guild.me
        await self.say_permissions(ctx, member, channel)

    @commands.command()
    @commands.is_owner()
    async def debugpermissions(
        self,
        ctx: GuildContext,
        channel: MessageableGuildChannel if TYPE_CHECKING else GuildChannel,
        author: discord.Member | None = None,
    ):
        """Shows permission resolution for a channel and an optional author."""
        person = author or ctx.me

        await self.say_permissions(ctx, person, channel)

    @commands.command(aliases=['invite'])
    async def join(self, ctx: Context) -> None:
        """Joins a server."""
        r = """
            [Support](https://discord.gg/s44CFagYN2)
            
            Ask VJ.
            
            (Use the link in the profile if your server has more than 10 members and less bots than humans.)
            """
        embed = discord.Embed(colour=discord.Colour.blurple())
        embed.description = textwrap.dedent(r)
        s = """
            I dislike asking for managed roles or permissions.
            **Some commands may not work unless you give me the correct permissions.**
            """
        embed.add_field(name='No permissions enabled by default', value=textwrap.dedent(s), inline=False)
        await ctx.send(embed=embed)

    def get_feedback_embed(
        self, obj: Context | discord.Interaction, *, summary: str, details: str | None = None
    ) -> discord.Embed:
        e = discord.Embed(title='Feedback', colour=0x738BD7)

        if details is not None:
            e.description = details
            e.title = summary[:256]
        else:
            e.description = summary

        if obj.guild is not None:
            e.add_field(name='Server', value=f'{obj.guild.name} (ID: {obj.guild.id})', inline=False)

        if obj.channel is not None:
            e.add_field(name='Channel', value=f'{obj.channel} (ID: {obj.channel.id})', inline=False)

        if isinstance(obj, discord.Interaction):
            e.timestamp = obj.created_at
            user = obj.user
        else:
            e.timestamp = obj.message.created_at
            user = obj.author

        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_footer(text=f'Author ID: {user.id}')
        return e

    @property
    def feedback_channel(self) -> discord.TextChannel | None:
        guild = self.bot.get_guild(932533101530349568)
        if guild is None:
            return None
        return guild.get_channel(956838318937636894)  # type: ignore

    @commands.command()
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.user)
    async def feedback(self, ctx: Context, *, content: str) -> None:
        """Gives feedback about the bot.

        This is a quick way to request features or bug fixes
        without being in the bot's server.

        The bot will communicate with you via DM about the status
        of your request if possible.

        You can only request feedback once a minute.
        """

        channel = self.feedback_channel
        if channel is None:
            return

        embed = self.get_feedback_embed(ctx, summary=content)
        await channel.send(embed=embed)
        await ctx.send(f'{ctx.tick(True)} Successfully sent feedback')

    @app_commands.command(name='feedback')
    async def feedback_slash(self, interaction: discord.Interaction) -> None:
        """Give feedback about the bot directly to the owner."""

        await interaction.response.send_modal(FeedbackModal(self))

    @commands.command(name='pm', hidden=True)
    @commands.is_owner()
    async def _pm(self, ctx: Context, user: discord.User, *, content: str) -> None:
        """PMs requested users."""
        fmt = (
            content + '\n\n*This is a DM sent because you had previously requested'
            ' feedback or I found a bug'
            ' in a command you used, I do not monitor this DM.*'
        )
        try:
            await user.send(fmt)
        except discord.HTTPException:
            await ctx.send(f'Could not PM user by ID {user.id}.')
        else:
            await ctx.send('PM successfully sent.')

    @commands.command(name='msgraw', aliases=['msgr', 'rawm'])
    @commands.cooldown(1, 15.0, commands.BucketType.user)
    async def raw_message(self, ctx: Context, message: discord.Message) -> None:
        """Quickly return the raw content of the specific message."""
        try:
            msg = await ctx.bot.http.get_message(message.channel.id, message.id)
        except discord.NotFound as err:
            raise commands.BadArgument(
                f'Message with the ID of {message.id} cannot be found in {message.channel.mention}.'  # type: ignore
            ) from err
        source = TextPageSource(
            formats.clean_triple_backtick(
                formats.escape_invis_chars(json.dumps(msg, indent=2, ensure_ascii=False, sort_keys=True))
            ),
            prefix='```json',
            suffix='```',
        )
        pages = RoboPages(source, ctx=ctx)
        await pages.start()

    @commands.command(name='disconnect')
    @commands.check(lambda ctx: bool(ctx.guild and ctx.guild.voice_client))
    async def disconnect_(self, ctx: GuildContext) -> None:
        """Disconnects the bot from the voice channel."""
        v_client: discord.VoiceClient = ctx.guild.voice_client  # type: ignore
        v_client.stop()
        await v_client.disconnect(force=True)

    @commands.hybrid_command()
    async def colour(self, ctx: Context, *, colour: str | None = None) -> None:
        """Information about a colour"""
        if colour is None:
            new_colour = discord.Colour.random()
        else:
            new_colour = await commands.ColourConverter().convert(ctx, colour)
        hsv = colorsys.rgb_to_hsv(*map(lambda x: x / 255, new_colour.to_rgb()))
        hsv = f'{hsv[0] * 360:.0f}°, {hsv[1] * 100:.0f}%, {hsv[2] * 100:.0f}%'
        hls = colorsys.rgb_to_hls(*map(lambda x: x / 255, new_colour.to_rgb()))
        hsl = f'{hls[0] * 360:.0f}°, {hls[2] * 100:.0f}%, {hls[1] * 100:.0f}%'

        def rgb_to_cmyk(r, g, b):
            if (r, g, b) == (0, 0, 0):
                # black
                return 0, 0, 0, 100

            # rgb [0,255] -> cmy [0,1]
            c = 1 - r / 255
            m = 1 - g / 255
            y = 1 - b / 255

            # extract out k [0, 1]
            min_cmy = min(c, m, y)
            c = (c - min_cmy) / (1 - min_cmy)
            m = (m - min_cmy) / (1 - min_cmy)
            y = (y - min_cmy) / (1 - min_cmy)
            k = min_cmy

            # rescale to the range [0,CMYK_SCALE]
            return f'{c * 100:.0f}%, {m * 100:.0f}%, {y * 100:.0f}%, {k * 100:.0f}%'

        cmyk = rgb_to_cmyk(*new_colour.to_rgb())
        embed = discord.Embed(colour=new_colour)
        embed.set_author(name=f'Information on {new_colour}')
        embed.title = f'Hex: {new_colour}'
        embed.add_field(name='RGB:', value=', '.join(str(x) for x in new_colour.to_rgb()) + '\u2000\u2000')
        embed.add_field(name='HSV:', value=hsv + '\u2000\u2000')
        embed.add_field(name='HSL:', value=hsl + '\u2000\u2000')
        embed.add_field(name='CMYK:', value=cmyk)
        embed.set_image(url='https://api.5ht2.me/colour?' + urlencode({'colour': str(new_colour)}))
        await ctx.send(embed=embed)


@app_commands.context_menu(name='Raw Message')
@app_commands.checks.cooldown(1, 15)
async def raw_message(interaction: discord.Interaction, message: discord.Message) -> None:
    await interaction.response.defer()
    msg = await interaction.client.http.get_message(message.channel.id, message.id)
    fmt = formats.clean_triple_backtick(
        formats.escape_invis_chars(json.dumps(msg, indent=2, ensure_ascii=False, sort_keys=True))
    )
    if len(fmt) > 1985:
        fp = BytesIO(fmt.encode('utf-8'))
        await interaction.followup.send('output too long...', file=discord.File(fp, 'raw_message.json'))
        return
    await interaction.followup.send(f'```json\n{fmt}\n```')


async def setup(bot: Ayaka):
    await bot.add_cog(Meta(bot))
    bot.tree.add_command(raw_message)


async def teardown(bot: Ayaka):
    bot.tree.remove_command(raw_message.name, type=raw_message.type)
