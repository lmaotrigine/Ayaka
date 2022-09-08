"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import datetime
import logging
from textwrap import shorten
from typing import TYPE_CHECKING, Callable, Coroutine

import discord
import mangadex
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import as_chunks
from mangadex.query import FeedOrderQuery, MangaListOrderQuery, Order

from utils import formats
from utils.context import Context
from utils.paginator import MangadexEmbed


if TYPE_CHECKING:
    from bot import Ayaka

LOG = logging.getLogger(__name__)


class MangadexConverter(commands.Converter):
    def lookup(
        self, bot: Ayaka, item: str
    ) -> Callable[[str], Coroutine[None, None, mangadex.Manga | mangadex.Chapter | mangadex.Author]] | None:
        table = {
            'title': bot.manga_client.view_manga,
            'chapter': bot.manga_client.get_chapter,
            'author': bot.manga_client.get_author,
        }

        return table.get(item, None)

    async def convert(self, ctx: Context, argument: str) -> mangadex.Manga | mangadex.Chapter | mangadex.Author | None:
        search = mangadex.MANGADEX_URL_REGEX.search(argument)
        if search is None:
            return None

        item = self.lookup(ctx.bot, search['type'])
        if item is None:
            return None

        true_item = await item(search['ID'])
        return true_item


class MangaView(discord.ui.View):
    def __init__(self, user: discord.abc.Snowflake, bot: Ayaka, manga: list[mangadex.Manga], /) -> None:
        self.user = user
        self.bot = bot
        self.manga_id: str | None = None
        options = []
        for idx, mango in enumerate(manga, start=1):
            options.append(
                discord.SelectOption(label=f'[{idx}] {shorten(mango.title, width=95)}', description=mango.id, value=mango.id)
            )
        self._lookup = {m.id: m for m in manga}
        super().__init__()
        self.select.options = options

    @discord.ui.select(min_values=1, max_values=1, options=[])
    async def select(self, interaction: discord.Interaction, item) -> None:
        assert interaction.user is not None
        assert interaction.channel is not None
        assert not isinstance(interaction.channel, discord.PartialMessageable)
        embed = await MangadexEmbed.from_manga(self._lookup[item.values[0]], nsfw_allowed=interaction.channel.is_nsfw())
        self.manga_id = item.values[0]
        if await self.bot.is_owner(interaction.user):
            self.follow.disabled = False

        await interaction.response.edit_message(content=None, embed=embed, view=self)

    @discord.ui.button(label='Follow?', disabled=True)
    async def follow(self, interaction: discord.Interaction, _) -> None:
        assert interaction.user is not None
        if not await self.bot.is_owner(interaction.user):
            raise commands.CheckFailure("You can't follow manga unless you're VJ.")

        assert self.manga_id is not None
        await self.bot.manga_client.follow_manga(self.manga_id)
        await interaction.response.send_message('You now follow this!', ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        assert interaction.user is not None
        if self.user.id != interaction.user.id:
            raise app_commands.CheckFailure('boo')
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, _: discord.ui.Select) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return await interaction.response.send_message("You can't choose someone else's Manga", ephemeral=True)
        else:
            raise error


class MangaCog(commands.Cog, name='Manga'):
    """Cog to assist with Mangadex related things."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self.webhook = discord.Webhook.from_url(bot.config.mangadex_webhook, session=bot.session)

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{SILHOUETTE OF JAPAN}')

    async def cog_load(self):
        self.get_personal_feed.add_exception_type(mangadex.APIException)
        self.get_personal_feed.start()

    @commands.hybrid_group(name='mangadex', aliases=['dex'])
    async def mangadex(self, ctx: Context) -> None:
        """commands for interacting with MangaDex!"""
        if not ctx.invoked_subcommand:
            await ctx.send_help(self)

    @mangadex.command(name='get')
    async def get_(
        self, ctx: Context, *, item: mangadex.Manga | mangadex.Chapter = commands.param(converter=MangadexConverter)
    ) -> None:
        """
        This command takes a mangadex link to a chapter or manga and returns the data.
        """
        nsfw_allowed = isinstance(ctx.channel, discord.DMChannel) or ctx.channel.is_nsfw()
        if isinstance(item, mangadex.Manga):
            embed = await MangadexEmbed.from_manga(item, nsfw_allowed=nsfw_allowed)
        elif isinstance(item, mangadex.Chapter):
            if item.chapter is None:
                await item.get_parent_manga()
            embed = await MangadexEmbed.from_chapter(item, nsfw_allowed=nsfw_allowed)
        else:
            await ctx.send('Not found?')
            return
        await ctx.send(embed=embed)

    async def perform_search(self, search_query: str) -> list[mangadex.Manga] | None:
        order = MangaListOrderQuery(relevance=Order.descending)

        collection = await self.bot.manga_client.manga_list(limit=5, title=search_query, order=order)

        if not collection.manga:
            return

        return collection.manga

    @mangadex.command(name='search')
    @app_commands.describe(query='The manga name to search for')
    async def search_(self, ctx: Context, *, query: str) -> None:
        """Search mangadex for a manga given its name."""
        manga = await self.perform_search(query)
        if manga is None:
            await ctx.send('No results found!', ephemeral=True)
            return

        view = MangaView(ctx.author, self.bot, manga)
        await ctx.send(view=view, ephemeral=True)

    @search_.error
    async def search_error(self, ctx: Context, error: commands.CommandError) -> None:
        error = getattr(error, 'original', error)
        if isinstance(error, ValueError):
            await ctx.send('You did not format the command flags properly.')
            return

    @mangadex.command(name='manga')
    @app_commands.describe(manga_id='The ID of the manga')
    async def manga_(self, ctx: Context, *, manga_id: str) -> None:
        """
        Uses a MangaDex UUID (for manga) to retrieve the data for it.
        """
        manga = await self.bot.manga_client.view_manga(manga_id)

        if manga.content_rating in (
            mangadex.ContentRating.pornographic,
            mangadex.ContentRating.suggestive,
            mangadex.ContentRating.erotica,
        ):
            if not getattr(ctx.channel, 'is_nsfw', lambda: True)():
                await ctx.send('This manga is a bit too lewd for a non-lewd channel.')
                return

        embed = await MangadexEmbed.from_manga(manga)
        await ctx.send(embed=embed)

    @mangadex.command(name='chapter')
    async def chapter_(self, ctx: Context, *, chapter_id: str) -> None:
        """
        Returns data on a MangaDex chapter.
        """
        chapter = await self.bot.manga_client.get_chapter(chapter_id)

        if chapter.manga is None:
            await chapter.get_parent_manga()

        assert chapter.manga is not None

        nsfw_allowed = isinstance(ctx.channel, discord.DMChannel) or ctx.channel.is_nsfw()

        embed = await MangadexEmbed.from_chapter(chapter, nsfw_allowed=nsfw_allowed)
        await ctx.send(embed=embed)

    @tasks.loop(minutes=10)
    async def get_personal_feed(self) -> None:
        order = FeedOrderQuery(created_at=Order.descending)
        ten_m_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        feed = await self.bot.manga_client.get_my_feed(
            limit=32,
            translated_language=['en', 'ja'],
            order=order,
            created_at_since=ten_m_ago,
            content_rating=[
                mangadex.ContentRating.pornographic,
                mangadex.ContentRating.safe,
                mangadex.ContentRating.suggestive,
                mangadex.ContentRating.erotica,
            ],
        )
        if not feed.chapters:
            return

        embeds = []
        for chapter in feed.chapters:
            if chapter.manga is not None:
                await chapter.get_parent_manga()
            embed = await MangadexEmbed.from_chapter(chapter, nsfw_allowed=True)
            embeds.append(embed)

        for embeds in as_chunks(embeds, 10):
            await self.webhook.send(
                '<@!411166117084528640>', embeds=embeds, allowed_mentions=discord.AllowedMentions(users=True)
            )
        self.bot.manga_client.dump_refresh_token()

    @get_personal_feed.before_loop
    async def before_feed(self) -> None:
        await self.bot.wait_until_ready()

    @get_personal_feed.error
    async def on_loop_error(self, error: BaseException) -> None:
        import traceback

        error = getattr(error, 'original', error)
        lines = traceback.format_exception(type(error), error, error.__traceback__)
        fmt = '<@!411166117084528640> \n'
        to_send = formats.to_codeblock(''.join(lines), escape_md=False)

        await self.webhook.send(fmt + to_send, allowed_mentions=discord.AllowedMentions(users=True))
        self.bot.manga_client.dump_refresh_token()

    def cog_unload(self) -> None:
        self.get_personal_feed.cancel()


async def setup(bot: Ayaka) -> None:
    await bot.add_cog(MangaCog(bot))
