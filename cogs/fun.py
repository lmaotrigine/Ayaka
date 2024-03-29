"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import random
import re
import time
from functools import partial
from textwrap import fill
from typing import TYPE_CHECKING, Annotated, Callable, NamedTuple

import bottom
import discord
import googletrans
from currency_converter import CurrencyConverter
from discord import app_commands
from discord.ext import commands
from lru import LRU
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from utils import checks, translator
from utils._types.discord_ import MessageableGuildChannel
from utils.context import Context, GuildContext
from utils.converters import MessageOrCleanContent, MessageOrContent, RedditMediaURL
from utils.formats import human_join, plural


if TYPE_CHECKING:
    from bot import Ayaka

MESSAGE_LINK_RE = re.compile(
    r'^(?:https?://)(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(?P<guild>\d{16,20})/(?P<channel>\d{16,20})/(?P<message>\d{16,20})/?$'
)
SPOILER_EMOJI_ID = 956843179213209620


log = logging.getLogger(__name__)


class ConvertibleUnit(NamedTuple):
    # (value) -> (converted, unit)
    formula: Callable[[float], tuple[float, str]]
    capture: str


UNIT_CONVERSIONS: dict[str, ConvertibleUnit] = {
    'km': ConvertibleUnit(lambda v: (v * 0.621371, 'mi'), r'km|(?:kilometer|kilometre)s?'),
    'm': ConvertibleUnit(lambda v: (v * 3.28084, 'ft'), r'm|(?:meter|metre)s?'),
    'ft': ConvertibleUnit(lambda v: (v * 0.3048, 'm'), r'ft|foot|feet'),
    'cm': ConvertibleUnit(lambda v: (v * 0.393701, 'in'), r'cm|(?:centimeter|centimetre)s?'),
    'in': ConvertibleUnit(lambda v: (v * 2.54, 'cm'), r'in|inch|inches'),
    'mi': ConvertibleUnit(lambda v: (v * 1.60934, 'km'), r'mi|mile|miles'),
    'kg': ConvertibleUnit(lambda v: (v * 2.20462, 'lb'), r'kg|kilogram|kilograms'),
    'lb': ConvertibleUnit(lambda v: (v * 0.453592, 'kg'), r'(?:lb|pound)s?'),
    'L': ConvertibleUnit(lambda v: (v * 0.264172, 'gal'), r'l|(?:liter|litre)s?'),
    'gal': ConvertibleUnit(lambda v: (v * 3.78541, 'L'), r'gal|gallon|gallons'),
    'C': ConvertibleUnit(lambda v: (v * 1.8 + 32, 'F'), r'c|°c|celsius'),
    'F': ConvertibleUnit(lambda v: ((v - 32) / 1.8, 'C'), r'f|°f|fahrenheit'),
}

UNIT_CONVERSION_REGEX_COMPONENT = '|'.join(f'(?P<{name}>{unit.capture})' for name, unit in UNIT_CONVERSIONS.items())
UNIT_CONVERSION_REGEX = re.compile(
    rf'(?P<value>\-?[0-9]+(?:[,.][0-9]+)?)\s*(?:{UNIT_CONVERSION_REGEX_COMPONENT})\b', re.IGNORECASE
)


class Unit(NamedTuple):
    value: float
    unit: str

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Unit:
        match = UNIT_CONVERSION_REGEX.match(argument)
        if match is None:
            raise commands.BadArgument('Could not find a unit')
        value = float(match.group('value'))
        unit = match.lastgroup
        if unit is None:
            raise commands.BadArgument('Could not find a unit')
        return cls(value, unit)

    def converted(self) -> Unit:
        return Unit(*UNIT_CONVERSIONS[self.unit].formula(self.value))

    @property
    def display_unit(self) -> str:
        # work around the fact that ° can't be used in group names
        if self.unit in ('F', 'C'):
            return f'°{self.unit}'
        return f' {self.unit}'


class UnitCollector(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> set[Unit]:
        units = set()
        for match in UNIT_CONVERSION_REGEX.finditer(argument):
            value = float(match.group('value'))
            unit = match.lastgroup
            if unit is None:
                raise commands.BadArgument('Could not find a unit')
            units.add(Unit(value, unit))
        if not units:
            raise commands.BadArgument('Could not find a unit')
        return units


class SpoilerCache:
    __slots__ = ('author_id', 'channel_id', 'title', 'text', 'attachments')

    def __init__(self, data):
        self.author_id = data['author_id']
        self.channel_id = data['channel_id']
        self.title = data['title']
        self.text = data['text']
        self.attachments = data['attachments']

    def has_single_image(self):
        return self.attachments and self.attachments[0].filename.lower().endswith(('.gif', '.png', '.jpg', '.jpeg'))

    def to_embed(self, bot: Ayaka):
        embed = discord.Embed(title=f'{self.title} Spoiler', colour=0x01AEEE)
        if self.text:
            embed.description = self.text

        if self.has_single_image():
            if self.text is None:
                embed.title = f'{self.title} Spoiler Image'
            embed.set_image(url=self.attachments[0].url)
            attachments = self.attachments[1:]
        else:
            attachments = self.attachments

        if attachments:
            value = '\n'.join(f'[{a.filename}]({a.url})' for a in attachments)
            embed.add_field(name='Attachments', value=value, inline=False)

        user = bot.get_user(self.author_id)
        if user:
            embed.set_author(name=str(user), icon_url=user.display_avatar.with_format('png'))
        return embed

    def to_spoiler_embed(self, ctx: Context, storage_message: discord.Message):
        description = 'This spoiler has been hidden. Press the button to reveal it!'
        embed = discord.Embed(title=f'{self.title} Spoiler', description=description)
        if self.has_single_image() and self.text is None:
            embed.title = f'{self.title} Spoiler Image'

        embed.set_footer(text=storage_message.id)
        embed.colour = 0x01AEEE
        embed.set_author(name=ctx.author, icon_url=ctx.author.display_avatar.with_format('png'))
        return embed


class SpoilerView(discord.ui.View):
    def __init__(self, cog: Fun) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label='Reveal Spoiler',
        style=discord.ButtonStyle.grey,
        emoji=discord.PartialEmoji(name='spoiler', id=956843179213209620),
        custom_id='cogs:buttons:reveal_spoiler',
    )
    async def reveal_spoiler(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.message is not None
        assert interaction.channel_id is not None

        cache = await self.cog.get_spoiler_cache(interaction.channel_id, interaction.message.id)
        if cache is not None:
            embed = cache.to_embed(self.cog.bot)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Jump to Spoiler', url=interaction.message.jump_url))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message('Could not find this message in storage', ephemeral=True)


class SpoilerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(1, 10.0), commands.BucketType.user)

    def _bucket_key(self, tup):
        return tup

    def is_rate_limited(self, message_id, user_id):
        bucket = self.get_bucket((message_id, user_id))
        return bucket is not None and bucket.update_rate_limit() is not None


class TranslateFlags(commands.FlagConverter):
    source: str = commands.flag(name='from', description='The language to translate from', default='auto')
    dest: str = commands.flag(name='to', description='The language to translate to', default='en')


class Fun(commands.Cog):
    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.translator = googletrans.Translator()
        self._spoiler_cache = LRU(128)
        self._spoiler_cooldown = SpoilerCooldown()
        self._spoiler_view = SpoilerView(self)
        bot.add_view(self._spoiler_view)
        self.currency_conv = CurrencyConverter()
        self.valid_langs = googletrans.LANGCODES.keys() | googletrans.LANGUAGES.keys()
        self.valid_source = self.valid_langs | set(['auto'])
        self.currency_codes = json.loads(open('utils/currency_codes.json').read())
        self.ctx_menu = app_commands.ContextMenu(name='View Pronouns', callback=self.view_pronouns_callback)
        self.bot.tree.add_command(self.ctx_menu)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        self._spoiler_view.stop()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{MAPLE LEAF}')

    # @commands.Cog.listener('on_message')
    async def quote(self, message: discord.Message) -> None:
        if message.author.bot or message.embeds or message.guild is None:
            return

        assert isinstance(message.channel, (discord.TextChannel, discord.Thread))
        perms = message.channel.permissions_for(message.guild.me)
        if perms.send_messages is False or perms.embed_links is False:
            return

        if not (match := MESSAGE_LINK_RE.search(message.content)):
            return

        data = match.groupdict()
        guild_id = int(data['guild'])
        channel_id = int(data['channel'])
        message_id = int(data['message_id'])

        if guild_id != message.guild.id:
            return

        channel = message.guild.get_channel(channel_id)
        if channel is None:
            # deleted or private?
            return

        assert isinstance(channel, (discord.TextChannel, discord.Thread))
        try:
            quote_message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            # Bot has no access I guess.
            return

        embed = discord.Embed(title=f'Quote from {quote_message.author} in {channel.name}')
        embed.set_author(name=quote_message.author.name, icon_url=quote_message.author.display_avatar.url)
        embed.description = quote_message.content or 'No message content.'
        fmt = 'This message had:\n'
        if quote_message.embeds:
            fmt += 'one or more embeds\n'
        if quote_message.attachments:
            fmt += 'one or more attachments\n'

        if len(fmt.split('\n')) >= 3:
            embed.add_field(name='Also...', value=fmt)

        embed.timestamp = quote_message.created_at

        await message.channel.send(embed=embed)

    async def view_pronouns_callback(self, interaction: discord.Interaction, member: discord.Member) -> None:
        # fetches from pronoundb.org for people without client mods
        lookup = {
            'he': ['he', 'him'],
            'it': ['it', 'its'],
            'she': ['she', 'her'],
            'they': ['they', 'them'],
        }
        is_author = member.id == interaction.user.id

        def formatter(s: list[str]) -> str:
            if not s:
                err = 'not specified their pronouns on PronounDB.'
                if is_author:
                    return f'have {err}\nYou can do so by linking your Discord account on https://pronoundb.org'
                return f'has {err}'
            if s[0] == 'ask':
                verb = 'prefer' if is_author else 'prefers'
                return f'{verb} that people ask their pronouns.'
            if s[0] == 'avoid':
                verb = 'want' if is_author else 'wants'
                return f'{verb} to avoid pronouns.'
            if s[0] == 'other':
                verb = 'go' if is_author else 'goes'
                return f'{verb} by pronouns not available on PronounDB.'
            ret = 'go by ' if is_author else 'goes by '
            l = []
            for p in s:
                if p == 'ask':
                    if not is_author:
                        return f'{ret} pronouns. You may also ask this person for additional info.'
                    return f'{ret} pronouns. People may also ask you for additional info.'
                if p == 'other':
                    prefix = 'You also go' if is_author else 'This person also goes'
                    return f'{ret} pronouns. {prefix} by pronouns not available on PronounDB.'
                if p == 'any':
                    l.append('any pronouns')
                else:
                    l.append('"' + '/'.join(lookup[p]) + '"')
            return f'{ret}{human_join(l)}'

        await interaction.response.defer(ephemeral=True)
        url = f'https://pronoundb.org/api/v2/lookup?platform=discord&ids={member.id}'
        if member.bot:
            await interaction.followup.send('beep boop?', ephemeral=True)
            return
        else:
            async with self.bot.session.get(url) as resp:
                try:
                    pronouns = (await resp.json())[str(member.id)]['sets']['en']
                except KeyError:  # likely not found
                    pronouns = []
        author = 'You' if is_author else str(member)
        await interaction.followup.send(f'{author} {formatter(pronouns)}', ephemeral=True)

    @commands.command(hidden=True)
    async def feelgood(self, ctx: Context) -> None:
        """press"""
        await ctx.send('*pressed*')

    @commands.command(hidden=True)
    async def feelbad(self, ctx: Context) -> None:
        """depress"""
        await ctx.send('*depressed*')

    @commands.command()
    async def love(self, ctx: Context) -> None:
        """What is love?"""
        responses = [
            'https://www.youtube.com/watch?v=HEXWRTEbj1I',
            'https://www.youtube.com/watch?v=i0p1bmr0EmE',
            'an intense feeling of deep affection',
            'something we don\'t have',
        ]

        response = random.choice(responses)
        await ctx.send(response)

    @commands.command(hidden=True)
    async def bored(self, ctx: Context) -> None:
        """boredom looms"""
        await ctx.send('https://i.imgur.com/BuTKSzf.png')

    async def do_translate(
        self,
        ctx: Context,
        message: discord.Message | str | None,
        *,
        from_: str | None = 'auto',
        to: str | None = 'en',
    ):
        reply = ctx.replied_message
        if message is None:
            if reply is not None:
                message = reply.clean_content
            else:
                return await ctx.send('No message to translate.')

        if isinstance(message, discord.Message):
            message = message.clean_content
        loop = self.bot.loop
        try:
            ret = await loop.run_in_executor(None, self.translator.translate, message, to, from_)
        except Exception as e:
            return await ctx.send(f'An error occurred: {e.__class__.__name__}: {e}')
        assert not isinstance(ret, list)
        embed = discord.Embed(title='Translated', colour=0x4284F3)
        src = googletrans.LANGUAGES.get(ret.src, '(auto-detected)').title()  # type: ignore
        dest = googletrans.LANGUAGES.get(ret.dest, 'Unknown').title()
        embed.add_field(name=f'From {translator.LANG_TO_FLAG.get(ret.src, "")} {src}', value=ret.origin, inline=False)  # type: ignore
        embed.add_field(name=f'To {translator.LANG_TO_FLAG.get(ret.dest, "")} {dest}', value=ret.text, inline=False)
        if ret.pronunciation and ret.pronunciation != ret.text:
            embed.add_field(name='Pronunciation', value=ret.pronunciation)

        await ctx.send(embed=embed)

    @commands.command()
    async def translate(self, ctx: Context, *, message: MessageOrCleanContent | None = None) -> None:
        """Translates a message to English using Google Translate."""
        """
        To avoid parsing ambiguities, the message will have to be prefixed with `text:`.

        For best results, optional flags should precede the `text:` flag.

        The following optional flags are allowed:

        `from:`: The language to translate from, defaults to auto-detect.
        `to:`: The language to translate to, defaults to English.
        """

        # src = flags.source.lower()
        # dest = flags.dest.lower()
        # if src not in self.valid_source:
        #    await ctx.send('Invalid source language.')
        #    return
        # if dest not in self.valid_langs:
        #    await ctx.send('Invalid destination language.')
        #    return
        if not isinstance(message, discord.Message) and message is not None:
            if message is not None:
                try:
                    message = await commands.MessageConverter().convert(ctx, message)  # type: ignore
                except commands.BadArgument:
                    pass
                else:
                    message = message.clean_content  # type: ignore
        await self.do_translate(ctx, message, from_='auto', to='en')  # type: ignore

    @staticmethod
    def uwu_aliases(text: str) -> str:
        aliases = {
            'hello': ['hyaaaa', 'haiii'],
            'bye': ['baiiii', 'bui', 'bai'],
            'this': ['dis'],
            'that': ['dat'],
            'what': ['wat', 'waa'],
            'because': ['cuz'],
            'and': ['&', 'annnd', 'n'],
            'cry': ['cri'],
            'no': ['nu', 'noooo'],
            'why': ['wai'],
        }
        l = []
        for w in text.split():
            if w.lower in aliases:
                l.append(random.choice(aliases[w.lower()]))
                continue
            l.append(w)
        return ' '.join(l)

    @staticmethod
    def initial_uwu(text: str) -> str:
        t = []
        for w in text.split():
            if 'r' in w:
                w = w.replace('r', 'w')
            if 'ng' in w and random.random() > 0.5:
                w = w.replace('ng', 'n')
            if 'l' in w and random.random() > 0.5:
                w = w.replace('l', 'w')
            t.append(w)
        return ' '.join(t)

    @staticmethod
    def stut(text: str, factor: int) -> str:
        nt = []
        sp = text.split()
        for p, w in enumerate(sp):
            if p % 2 == 0:
                if int(len(sp) * (random.randint(1, 5) / 10)) * factor * 2 < len(sp) and len(w) > 2 and w[0] != '\n':
                    nt.append(f'{w[0]}-{w}')
                    continue
            nt.append(w)
        return ' '.join(nt)

    @staticmethod
    def cute(text: str, factor: int) -> str:
        emoji = (
            'uwu',
            'owo',
            'ʕ•́ᴥ•̀ʔっ',
            '≧◠ᴥ◠≦',
            '>_<',
            '(◕ ˬ ◕✿)',
            '(・ω ・✿)',
            '(◕ㅅ◕✿)',
            ' (◠‿◠✿)',
            ' (◠‿◠)',
            ' ̑̑ෆ(⸝⸝⸝◉⸝ ｡ ⸝◉⸝✿⸝⸝)',
            '(இ__இ✿)',
            '✧w✧',
            'ಇ( ꈍᴗꈍ)ಇ',
            '( ᴜ ω ᴜ )',
            'ଘ(੭ ˘ ᵕ˘)━☆ﾟ.*･｡ﾟᵕ꒳ᵕ~',
            'ʕ ꈍᴥꈍʔ',
            '（´•(ｪ)•｀）',
            '(=^･ω･^=)',
            '/ᐠ . ֑ . ᐟﾉ',
            'චᆽච',
            '♡(˶╹̆ ▿╹̆˵)و✧♡',
            '( o͡ ꒳ o͡ )',
            '(´・ω・｀)',
            'Ꮚ･ω･Ꮚ',
            '꒰(͏ʻัꈊʻั)꒱',
            '꒰(͏ˊ•ꈊ•ˋ)꒱',
            'ʕᴥ·　ʔ',
            'ʕ º ᴥ ºʔ',
            'ʕ≧ᴥ≦ʔ',
            '▼・ᴥ・▼',
            '૮ ˘ﻌ˘ ა',
            '(ᵔᴥᵔ)',
            '꒰꒡ꆚ꒡꒱',
        )
        s = text.split(' ')
        emotes = math.ceil((len([x for x in s if x[-1:] in (',', '.') and x[-2:] != '..']) + 1) * (factor / 10))
        t = []
        for p, w in enumerate(s):
            if emotes > 0:
                if (w[-1:] in (',', '.') and w[-2:] != '..' and random.random() > 0.5) or p + 1 == len(s):
                    t.append(
                        f'{w[:len(w) - (1 if w[-1] in (",", ".") else 0)]} {random.choice(emoji)}{w[-1] if p != len(s) - 1 else ""}'
                    )
                    emotes -= 1
                    continue
            t.append(w)
        return ' '.join(t)

    def build_uwu(self, text: str, cute: int = 5, stut: int = 3) -> str:
        text = self.uwu_aliases(text)
        text = self.stut(self.initial_uwu(text), stut)
        return self.cute(text, cute)

    @commands.hybrid_command(aliases=['uwu', 'owo', 'owofy'], hidden=True)
    async def uwufy(self, ctx: Context, *, text: str) -> None:
        """UwU"""
        await ctx.send(self.build_uwu(text, cute=3))

    @commands.hybrid_group(name='bottom')
    async def bottom_group(self, ctx: Context) -> None:
        """💖✨✨✨✨🥺,,,👉👈💖💖✨,👉👈💖💖✨🥺,👉👈💖💖✨🥺,👉👈💖💖✨,👉👈💖💖🥺,,,,👉👈"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @bottom_group.command(name='encode')
    async def _bottom_encode(
        self,
        ctx: Context,
        *,
        text: discord.Message | str | None = commands.param(converter=MessageOrContent, default=None, displayed_default=''),
    ) -> None:
        """Encode text to bottom."""
        if isinstance(text, discord.Message):
            text = text.content
        if not text:
            reply = ctx.replied_message
            if reply is not None:
                text = reply.content
            else:
                await ctx.send('Missing text to encode.', ephemeral=True)
                return
        await ctx.send(bottom.encode(text))  # type: ignore # pyright is high

    @bottom_group.command(name='decode')
    async def _bottom_decode(
        self,
        ctx: Context,
        *,
        text: discord.Message | str | None = commands.param(converter=MessageOrContent, default=None, displayed_default=''),
    ) -> None:
        """Decode text from bottom."""
        if isinstance(text, discord.Message):
            text = text.content
        if not text:
            reply = ctx.replied_message
            if reply is not None:
                text = reply.content
            else:
                await ctx.send('Missing text to decode.', ephemeral=True)
                return
        try:
            await ctx.send(bottom.decode(text))  # type: ignore # pyright is high
        except ValueError:
            await ctx.send('Invalid bottom text.', ephemeral=True)

    @commands.command(hidden=True)
    async def cat(self, ctx: Context) -> None:
        """Gives you a random cat."""
        async with ctx.session.get('https://api.thecatapi.com/v1/images/search') as resp:
            if resp.status != 200:
                await ctx.send('No cat found :(')
                return
            js = await resp.json()
        await ctx.send(embed=discord.Embed(title='Random Cat').set_image(url=js[0]['url']))

    @commands.command(hidden=True)
    async def dog(self, ctx: Context) -> None:
        """Gives you a random dog."""
        async with ctx.session.get('https://random.dog/woof') as resp:
            if resp.status != 200:
                await ctx.send('No dog found :(')
                return

            filename = await resp.text()
            url = f'https://random.dog/{filename}'
            filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
            if filename.endswith(('.mp4', '.webm')):
                async with ctx.typing():
                    async with ctx.session.get(url) as other:
                        if other.status != 200:
                            await ctx.send('Could not download dog video :(')
                            return

                        if int(other.headers['Content-Length']) >= filesize:
                            await ctx.send(f'Video was too big to upload... See it here: {url} instead.')
                            return

                        fp = io.BytesIO(await other.read())
                        await ctx.send(file=discord.File(fp, filename))
            else:
                await ctx.send(embed=discord.Embed(title='Random Dog').set_image(url=url))

    def _draw_words(self, text: str) -> io.BytesIO:
        text = fill(text, 25)
        font = ImageFont.truetype('static/W6.ttc', 60)
        padding = 50

        images = [Image.new('RGBA', (1, 1), color=0) for _ in range(2)]
        for index, (image, colour) in enumerate(zip(images, ((47, 49, 54), 'white'))):
            draw = ImageDraw.Draw(image)
            left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font)
            w, h = right - left, bottom - top
            images[index] = image = image.resize((w + padding, h + padding))
            draw = ImageDraw.Draw(image)
            draw.multiline_text((padding / 2, padding / 2), text=text, fill=colour, font=font)
        background, foreground = images

        background = background.filter(ImageFilter.GaussianBlur(radius=7))
        background.paste(foreground, (0, 0), foreground)
        buf = io.BytesIO()
        background.save(buf, 'png')
        buf.seek(0)
        return buf

    def random_words(self, amount: int) -> list[str]:
        with open('static/words.txt', 'r') as fp:
            words = fp.readlines()
        return random.sample(words, amount)

    @commands.command(aliases=['typerace'])
    @commands.cooldown(1, 10.0, commands.BucketType.channel)
    @commands.max_concurrency(1, commands.BucketType.channel, wait=False)
    async def typeracer(self, ctx: Context, amount: int = 5) -> None:
        """
        Type racing.

        This command will send an image of words of [amount] length.
        Please type and send this Kana in the same channel to qualify.
        """

        amount = max(min(amount, 50), 1)

        await ctx.send('Type-racing begins in 5 seconds.')
        await asyncio.sleep(5)

        words = self.random_words(amount)
        randomised_words = (' '.join(words)).replace('\n', '').strip().lower()

        func = partial(self._draw_words, randomised_words)
        image = await ctx.bot.loop.run_in_executor(None, func)
        file = discord.File(fp=image, filename='typerace.png')
        await ctx.send(file=file)

        winners = dict()
        is_ended = asyncio.Event()

        start = time.time()

        def check(message: discord.Message) -> bool:
            if (
                message.channel == ctx.channel
                and not message.author.bot
                and message.content.lower() == randomised_words
                and message.author not in winners
            ):
                winners[message.author] = time.time() - start
                is_ended.set()
                ctx.bot.loop.create_task(message.add_reaction(ctx.bot.emoji[True]))
            return False

        task = ctx.bot.loop.create_task(ctx.bot.wait_for('message', check=check))

        try:
            await asyncio.wait_for(is_ended.wait(), timeout=60)
        except asyncio.TimeoutError:
            await ctx.send('No participants matched the output.')
        else:
            await ctx.send('Input accepted... Other players have 10 seconds left.')
            await asyncio.sleep(10)
            embed = discord.Embed(title=f'{plural(len(winners)):Winner}', colour=discord.Colour.random())
            embed.description = '\n'.join(
                f'{idx}: {person.mention} - {time:.4f} seconds for {len(randomised_words) / time * 12:.2f}WPM'
                for idx, (person, time) in enumerate(winners.items(), start=1)
            )
            await ctx.send(embed=embed)
        finally:
            task.cancel()

    @commands.command()
    async def currency(self, ctx: Context, amount: float, source: str, dest: str) -> None:
        """Currency converter."""
        source = source.upper()
        dest = dest.upper()
        try:
            new_amount = self.currency_conv.convert(amount, source, dest)
        except ValueError as e:
            await ctx.send(str(e))
            return
        prefix = next((cur for cur in self.currency_codes if cur['cc'] == dest), {}).get('symbol')
        await ctx.send(f'{prefix}{new_amount:.2f}')

    async def redirect_post(self, ctx: Context, title: str, text: str | None) -> tuple[discord.Message, SpoilerCache]:
        storage: discord.TextChannel = self.bot.get_guild(932533101530349568).get_channel(956988935538614312)  # type: ignore # this exists

        supported_attachments = ('.png', '.jpg', '.jpeg', '.webm', '.gif', '.mp4', '.txt')
        if not all(attach.filename.lower().endswith(supported_attachments) for attach in ctx.message.attachments):
            raise RuntimeError(f'Unsupported file in attachments. Only {", ".join(supported_attachments)} supported.')

        files = []
        total_bytes = 0
        max_mb = 25 * 1024 * 1024
        for attach in ctx.message.attachments:
            async with ctx.session.get(attach.url) as resp:
                if resp.status != 200:
                    continue

                content_length = int(resp.headers.get('Content-Length', ''))

                # file too big, skip it
                if (total_bytes + content_length) > max_mb:
                    continue

                total_bytes += content_length
                fp = io.BytesIO(await resp.read())
                files.append(discord.File(fp, filename=attach.filename))

            if total_bytes >= max_mb:
                break

        # on mobile, messages that are deleted immediately sometimes persist client side
        await asyncio.sleep(0.2)
        await ctx.message.delete()
        data = discord.Embed(title=title)
        if text:
            data.description = text

        data.set_author(name=ctx.author.id)
        data.set_footer(text=ctx.channel.id)

        try:
            message = await storage.send(embed=data, files=files)
        except discord.HTTPException as e:
            raise RuntimeError(f'Sorry. Could not store message due to {e.__class__.__name__}: {e}.') from e

        to_dict = {
            'author_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'attachments': message.attachments,
            'title': title,
            'text': text,
        }

        cache = SpoilerCache(to_dict)
        return message, cache

    async def get_spoiler_cache(self, channel_id, message_id):
        try:
            return self._spoiler_cache[message_id]
        except KeyError:
            pass

        storage: discord.TextChannel = self.bot.get_guild(932533101530349568).get_channel(956988935538614312)  # type: ignore # this exists

        # slow path requires 2 lookups
        # first is looking up the message_id of the original post
        # to get the embed footer information which points to the storage message ID
        # the second is getting the storage message ID and extracting the information from it
        channel: MessageableGuildChannel = self.bot.get_channel(channel_id)  # type: ignore  # yeah it won't be any of those
        if not channel:
            return None

        try:
            original_message = await channel.fetch_message(message_id)
            storage_message_id = int(original_message.embeds[0].footer.text)  # type: ignore # this exists
            message = await storage.fetch_message(storage_message_id)
        except:
            # this message is probably not the proper format or the storage died
            return None

        data = message.embeds[0]
        to_dict = {
            'author_id': int(data.author.name),  # type: ignore # this exists
            'channel_id': int(data.footer.text),  # type: ignore # this exists
            'attachments': message.attachments,
            'title': data.title,
            'text': None if not data.description else data.description,
        }
        cache = SpoilerCache(to_dict)
        self._spoiler_cache[message_id] = cache
        return cache

    @commands.Cog.listener('on_raw_reaction_add')
    async def spoiler_listener(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.id != SPOILER_EMOJI_ID:
            return

        if self._spoiler_cooldown.is_rate_limited(payload.message_id, payload.user_id):
            return

        user = self.bot.get_user(payload.user_id) or (await self.bot.fetch_user(payload.user_id))
        if not user or user.bot:
            return

        cache = await self.get_spoiler_cache(payload.channel_id, payload.message_id)
        assert cache is not None
        embed = cache.to_embed(self.bot)
        await user.send(embed=embed)

    @commands.command()
    @checks.can_use_spoiler()
    async def spoiler(self, ctx: Context, title: str, *, text: str | None = None):
        """Marks your post a spoiler with a title.

        Once your post is marked as a spoiler it will be
        automatically deleted and the bot will send a message
        to those who opt-in to view the spoiler.

        The only media types supported are png, gif, jpeg, mp4,
        and webm.

        Only 25MiB of total media can be uploaded at once.
        Sorry, Discord limitation.

        To opt-in to a post's spoiler you must press the button.
        """

        if len(title) > 100:
            return await ctx.send('Sorry. Title has to be shorter than 100 characters.')

        try:
            storage_message, cache = await self.redirect_post(ctx, title, text)
        except Exception as e:
            return await ctx.send(str(e))

        spoiler_message = await ctx.send(embed=cache.to_spoiler_embed(ctx, storage_message), view=self._spoiler_view)
        self._spoiler_cache[spoiler_message.id] = cache

    @commands.command(usage='<url>')
    @commands.cooldown(1, 5.0, commands.BucketType.member)
    async def vreddit(self, ctx: Context, *, reddit: RedditMediaURL) -> None:
        """Downloads a v.redd.it submission.

        Regular reddit URLs or v.redd.it URLs are supported.
        """

        filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
        async with ctx.session.get(reddit.url) as resp:
            if resp.status != 200:
                await ctx.send('Could not download video.')
                return

            if int(resp.headers['Content-Length']) >= filesize:
                await ctx.send('Video is too big to be uploaded.')
                return

            data = await resp.read()
            await ctx.send(file=discord.File(io.BytesIO(data), filename=reddit.filename))

    @vreddit.error
    async def on_vreddit_error(self, ctx: Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await ctx.send(f'{error}')

    def safe_chan(self, member: discord.Member, channels: list[discord.VoiceChannel]) -> discord.VoiceChannel | None:
        random.shuffle(channels)
        for channel in channels:
            if channel.permissions_for(member).connect:
                return channel
        return None

    @commands.command(hidden=True, name='scatter', aliases=['scattertheweak'])
    @checks.is_admin()
    async def scatter(self, ctx: GuildContext, voice_channel: discord.VoiceChannel | None = None) -> None:
        if voice_channel:
            channel = voice_channel
        else:
            if ctx.author.voice:
                channel = ctx.author.voice.channel
            else:
                channel = None

        if channel is None:
            await ctx.send('No voice channel.')
            return

        members = channel.members

        for member in members:
            target = self.safe_chan(member, ctx.guild.voice_channels)
            if target is None:
                continue
            await member.move_to(target)

    @commands.command(hidden=True, name='snap')
    @checks.is_admin()
    async def snap(self, ctx: GuildContext) -> None:
        members = []
        for vc in ctx.guild.voice_channels:
            members.extend(vc.members)

        upper = math.ceil(len(members) / 2)
        choices = random.choices(members, k=upper)

        for m in choices:
            await m.move_to(None)

    @commands.command(name='convert')
    async def _convert(self, ctx: Context, *, values: Annotated[set[Unit], UnitCollector] | None = None) -> None:
        """Converts between various units.

        Supported unit conversions:

        - km <-> mi
        - m <-> ft
        - cm <-> in
        - kg <-> lb
        - L <-> gal
        - °C <-> °F
        """
        if values is None:
            reply = ctx.replied_message
            if reply is None:
                await ctx.send('You need to provide some values to convert or reply to a message with values.')
                return
            values = await UnitCollector().convert(ctx, reply.content)
        pairs: list[tuple[str, str]] = []
        for value in values:
            original = f'{value.value:g}{value.display_unit}'
            converted = value.converted()
            pairs.append((original, f'{converted.value:g}{converted.display_unit}'))

        # pad for width since this is monospace
        width = max(len(original) for original, _ in pairs)
        fmt = '\n'.join(f'{original:<{width}} -> {converted}' for original, converted in pairs)
        await ctx.send(f'```\n{fmt}\n```')

    @_convert.error
    async def on_convert_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))


async def setup(bot: Ayaka):
    await bot.add_cog(Fun(bot))
