"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import html
import io
import logging
import re
import textwrap
import urllib.parse
import zoneinfo
from typing import TYPE_CHECKING

import aiohttp
import dateutil.parser
import dateutil.tz
import discord
import feedparser
import tweepy
import tweepy.asynchronous
from bs4 import BeautifulSoup
from discord.ext import commands, tasks

from utils import checks
from utils._types.discord_ import MessageableGuildChannel


if TYPE_CHECKING:
    import asyncpg

    from bot import Ayaka
    from utils.context import Context

log = logging.getLogger(__name__)


TWITTER_COLOUR = 0x00ACED
TWITTER_ICON = 'https://abs.twimg.com/icons/apple-touch-icon-192x192.png'


class TwitterStream(tweepy.asynchronous.AsyncStream):
    def __init__(self, bot: Ayaka):
        super().__init__(
            bot.config.twitter_api_key,
            bot.config.twitter_secret,
            bot.config.twitter_access_token,
            bot.config.twitter_access_token_secret,
        )
        self.bot = bot
        self.feeds = {}
        self.unique_feeds = set()
        self.reconnect_ready = asyncio.Event()
        self.reconnect_ready.set()
        self.reconnecting = False

    async def start_feeds(self, *, feeds=None):
        if self.reconnecting:
            return await self.reconnect_ready.wait()
        self.reconnecting = True
        await self.reconnect_ready.wait()
        self.reconnect_ready.clear()
        if feeds:
            self.feeds = feeds
            self.unique_feeds = set(id for feeds in self.feeds.values() for id in feeds)
        if self.task:
            self.disconnect()
            await self.task
        if self.feeds:
            self.filter(follow=self.unique_feeds)
        self.bot.loop.call_later(120, self.reconnect_ready.set)
        self.reconnecting = False

    async def add_feed(self, channel, handle):
        user_id = self.bot.get_cog('Feeds').twitter_api.get_user(screen_name=handle).id_str  # type: ignore
        self.feeds[channel.id] = self.feeds.get(channel.id, []) + [user_id]
        if user_id not in self.unique_feeds:
            self.unique_feeds.add(user_id)
            await self.start_feeds()

    async def remove_feed(self, channel, handle):
        self.feeds[channel.id].remove(self.bot.get_cog('Feeds').twitter_api.get_user(screen_name=handle).id_str)  # type: ignore
        self.unique_feeds = set(id for feeds in self.feeds.values() for id in feeds)
        await self.start_feeds()

    async def on_status(self, status):
        if status.in_reply_to_status_id:
            return
        if status.user.id_str in self.unique_feeds:
            for channel_id, channel_feeds in self.feeds.items():
                if status.user.id_str in channel_feeds:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        assert isinstance(channel, MessageableGuildChannel)
                        if hasattr(status, 'extended_tweet'):
                            text = status.extended_tweet['full_text']
                            entities = status.extended_tweet['entities']
                            extended_entities = status.extended_tweet.get('extended_entities')
                        else:
                            text = status.text
                            entities = status.entities
                            extended_entities = getattr(status, 'extended_entities', None)
                        embed = discord.Embed(
                            title=f'@{status.user.screen_name}',
                            url=f'https://twitter.com/{status.user.screen_name}/status/{status.id}',
                            description=self.bot.get_cog('Feeds').process_tweet_text(text, entities),  # type: ignore
                            timestamp=status.created_at,
                            colour=TWITTER_COLOUR,
                        )
                        embed.set_author(name=status.user.name, icon_url=status.user.profile_image_url)
                        if extended_entities and extended_entities['media'][0]['type'] == 'photo':
                            embed.set_image(url=extended_entities['media'][0]['media_url_https'])
                            embed.description = embed.description.replace(extended_entities['media'][0]['url'], '')  # type: ignore
                        embed.set_footer(text='Twitter', icon_url=TWITTER_ICON)
                        try:
                            await channel.send(embed=embed)  # TODO: Handle this better
                        except discord.Forbidden:
                            log.warning('Twitter Stream: Missing permissions to send embed in #%s in %s', channel.name, channel.guild.name)
                        except discord.DiscordServerError as e:
                            log.error('Twitter Stream Discord Server Error: %r', e, exc_info=e)

    async def on_request_error(self, status_code):
        log.error(f'Twitter Stream: Request error {status_code}')


class Feeds(commands.Cog):
    """Twitter and RSS"""

    def __init__(self, bot: Ayaka):
        self.bot = bot
        self.twitter_auth = tweepy.OAuth1UserHandler(bot.config.twitter_api_key, bot.config.twitter_secret)
        self.twitter_auth.set_access_token(bot.config.twitter_access_token, bot.config.twitter_access_token_secret)
        self.twitter_api = tweepy.API(self.twitter_auth)
        self.blacklisted_handles = []
        self.tzinfos = {}
        for timezone_abbreviation in ('EDT', 'EST'):
            matching_timezones = list(
                filter(
                    lambda t: datetime.datetime.now(zoneinfo.ZoneInfo(t)).strftime('%Z') == timezone_abbreviation,
                    zoneinfo.available_timezones(),
                )
            )
            matching_utc_offsets = set(
                datetime.datetime.now(zoneinfo.ZoneInfo(t)).strftime('%z') for t in matching_timezones
            )
            if len(matching_utc_offsets) == 1:
                self.tzinfos[timezone_abbreviation] = dateutil.tz.gettz(matching_timezones[0])
        self.new_feed = asyncio.Event()
        self.check_feeds.start().set_name('RSS')

    async def cog_load(self):
        try:
            twitter_account = self.twitter_api.verify_credentials()
            if twitter_account.protected:
                self.blacklisted_handles.append(twitter_account.screen_name.lower())
            twitter_friends = self.twitter_api.get_friend_ids(screen_name=twitter_account.screen_name)
            for interval in range(0, len(twitter_friends), 100):
                some_friends = self.twitter_api.lookup_users(user_id=twitter_friends[interval : interval + 100])
                for friend in some_friends:
                    if friend.protected:
                        self.blacklisted_handles.append(friend.screen_name.lower())
        except (AttributeError, tweepy.TweepyException) as e:
            log.error('Failed to initialise Twitter blacklist: %r', e, exc_info=e)
        self.stream = TwitterStream(self.bot)
        self.task = self.bot.loop.create_task(self.start_twitter_feeds(), name='Start Twitter Stream')

    def cog_unload(self):
        if self.stream:
            self.stream.disconnect()
        self.task.cancel()
        self.check_feeds.cancel()

    @commands.group(invoke_without_command=True)
    async def twitter(self, ctx: Context):
        """Twitter"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @twitter.command(name='status')
    async def twitter_status(self, ctx: Context, handle: str, replies: bool = False, retweets: bool = False):
        """Get Twitter status

        Excludes replies and retweets by default

        Limited to 3200 most recent tweets.
        """
        tweet = None
        if handle.lower().strip('@') in self.blacklisted_handles:
            await ctx.send('This account is protected and cannot be accessed.')
            return
        try:
            for status in tweepy.Cursor(
                self.twitter_api.user_timeline,
                screen_name=handle,
                exclude_replies=not replies,
                include_rts=retweets,
                tweet_mode='extended',
                count=200,
            ).items():
                tweet = status
                break
        except tweepy.NotFound:
            await ctx.send(f'@{handle} does not exist.')
            return
        except tweepy.TweepyException as e:
            await ctx.send(f'An unexpected error occurred: {e}')
            return
        if not tweet:
            await ctx.send('Status not found for this handle.')
            return
        text = self.process_tweet_text(tweet.full_text, tweet.entities)
        image_url = None
        if hasattr(tweet, 'extended_entities') and tweet.extended_entities['media'][0]['type'] == 'photo':
            image_url = tweet.extended_entities['media'][0]['media_url_https']
            text = text.replace(tweet.extended_entities['media'][0]['url'], '')
        e = discord.Embed(
            description=text,
            title=f'@{tweet.user.screen_name}',
            url=f'https://twitter.com/{tweet.user.screen_name}/status/{tweet.id}',
            colour=TWITTER_COLOUR,
        )
        e.timestamp = tweet.created_at
        e.set_footer(text=tweet.user.name, icon_url=tweet.user.profile_image_url)
        e.set_image(url=image_url)
        await ctx.send(embed=e)

    @twitter.command(name='add', aliases=['addhandle', 'handleadd'])
    @checks.is_mod()
    async def twitter_add(self, ctx: Context, handle: str):
        """Adds a Twitter handle to a text channel

        A delay of up to 2 minutes is possible due to Twitter rate limits.
        """
        if handle.startswith('@'):
            handle = handle[1:]
        embed = discord.Embed(colour=TWITTER_COLOUR)
        query = """SELECT EXISTS (
                   SELECT FROM twitter_handles
                   WHERE channel_id = $1 AND handle = $2
                   );
                """
        following = await ctx.db.fetchval(query, ctx.channel.id, handle)
        if following:
            await ctx.send('This text channel is already following that Twitter handle.')
            return
        embed.description = '\N{HOURGLASS} Please wait...'
        message = await ctx.send(embed=embed)
        try:
            await self.stream.add_feed(ctx.channel, handle)
        except tweepy.TweepyException as e:
            embed.description = f'\N{NO ENTRY SIGN} An unexpected error occurred: {e}'
            await message.edit(embed=embed)
            return
        query = """INSERT INTO twitter_handles (channel_id, handle)
                   VALUES ($1, $2);
                """
        await ctx.db.execute(query, ctx.channel.id, handle)
        embed.description = f'Added the Twitter handle [`@{handle}`](https://twitter.com/{handle}) to this text channel.'
        await message.edit(embed=embed)

    @twitter.command(name='remove', aliases=['delete', 'removehandle', 'handleremove', 'deletehandle', 'handledelete'])
    @checks.is_mod()
    async def twitter_remove(self, ctx: Context, handle: str):
        """Remove a Twitter handle from a text channel.

        A delay of up to 2 minutes is possible due to Twitter rate limits.
        """
        query = """DELETE FROM twitter_handles
                   WHERE channel_id = $1 AND handle = $2
                   RETURNING handle;
                """
        deleted = await ctx.db.fetchval(query, ctx.channel.id, handle)
        if not deleted:
            await ctx.send("This text channel isn't following that Twitter handle")
            return
        embed = discord.Embed(colour=TWITTER_COLOUR)
        embed.description = '\N{HOURGLASS} Please wait...'
        message = await ctx.send(embed=embed)
        await self.stream.remove_feed(ctx.channel, handle)
        embed.description = f'Removed the Twitter handle [`@{handle}`](https://twitter.com/{handle}) from this text channel.'
        await message.edit(embed=embed)

    @twitter.command(aliases=['handle', 'feeds', 'feed', 'list'])
    async def handles(self, ctx: Context):
        """Show Twitter handles being followed in this text channel."""
        query = """SELECT handle FROM twitter_handles
                     WHERE channel_id = $1;
                """
        records = await ctx.db.fetch(query, ctx.channel.id)
        desc = '\n'.join(sorted([record['handle'] for record in records], key=str.casefold))
        await ctx.send(embed=discord.Embed(colour=TWITTER_COLOUR, description=desc))

    def process_tweet_text(self, text, entities):
        mentions = {}
        for mention in entities['user_mentions']:
            mentions[text[mention['indices'][0] : mention['indices'][1]]] = mention['screen_name']
        for mention, screen_name in mentions.items():
            text = text.replace(mention, f'[{mention}](https://twitter.com/{screen_name})')
        for hashtag in entities['hashtags']:
            text = text.replace(
                f'#{hashtag["text"]}', f'[#{hashtag["text"]}](https://twitter.com/hashtag/{hashtag["text"]})'
            )
        for symbol in entities['symbols']:
            text = text.replace(f'${symbol["text"]}', f'[${symbol["text"]}](https://twitter.com/search?q=${symbol["text"]})')
        for url in entities['urls']:
            text = text.replace(url['url'], url['expanded_url'])
        return html.unescape(text.replace('\ufe0f', ''))

    async def start_twitter_feeds(self):
        await self.bot.wait_until_ready()
        feeds = {}
        try:
            self.twitter_api.wait_on_rate_limit = True
            async with self.bot.pool.acquire() as con:
                async with con.transaction():
                    async for record in con.cursor('SELECT * FROM twitter_handles'):
                        try:
                            partial = functools.partial(self.twitter_api.get_user, screen_name=record['handle'])
                            user = await self.bot.loop.run_in_executor(None, partial)
                            feeds[record['channel_id']] = feeds.get(record['channel_id'], []) + [user.id_str]
                        except (tweepy.Forbidden, tweepy.NotFound):
                            continue
            await self.stream.start_feeds(feeds=feeds)
        except Exception as e:
            log.error('Uncaught Twitter Task exception\n', exc_info=(type(e), e, e.__traceback__))
            return
        finally:
            self.twitter_api.wait_on_rate_limit = False

    @commands.group(aliases=['feed'], invoke_without_command=True)
    async def rss(self, ctx: Context):
        """RSS"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @rss.command(name='add')
    @checks.is_mod()
    async def rss_add(self, ctx: Context, url: str):
        """Add a feed to a channel."""
        query = """SELECT EXISTS (
            SELECT FROM rss_feeds
            WHERE channel_id = $1 AND feed = $2
        );
        """
        following = await ctx.db.fetchval(query, ctx.channel.id, url)
        if following:
            await ctx.send('This text channel is already following that RSS feed.')
            return
        async with ctx.session.get(url) as resp:
            feed_text = await resp.text()
        partial = functools.partial(
            feedparser.parse, io.BytesIO(feed_text.encode('utf-8')), response_headers={'Content-Location': url}
        )
        feed_info = await self.bot.loop.run_in_executor(None, partial)
        ttl = None
        if 'ttl' in feed_info.feed:
            ttl = int(feed_info.feed.ttl)
        for entry in feed_info.entries:
            try:
                await ctx.db.execute(
                    """INSERT INTO rss_entries (entry, feed)
                                     VALUES ($1, $2)
                                     ON CONFLICT (entry, feed) DO NOTHING;
                                     """,
                    entry.id,
                    url,
                )
            except AttributeError:
                await ctx.send('Error processing feed: Feed entry missing ID')
                return
        await ctx.db.execute(
            """INSERT INTO rss_feeds (channel_id, feed, last_checked, ttl)
                             VALUES ($1, $2, NOW(), $3);
                             """,
            ctx.channel.id,
            url,
            ttl,
        )
        await ctx.send(f'The feed, {url}, has been added to this text channel.')
        self.new_feed.set()

    @rss.command(name='remove', aliases=['delete'])
    @checks.is_mod()
    async def rss_remove(self, ctx: Context, url: str):
        """Remove a feed from a channel."""
        deleted = await ctx.db.fetchval(
            """
            DELETE FROM rss_feeds
            WHERE channel_id = $1 AND feed = $2
            RETURNING *
            """,
            ctx.channel.id,
            url,
        )
        if not deleted:
            return await ctx.send("This channel isn't following that feed")
        await ctx.send(f'The feed, {url}, has been removed from this channel')

    @rss.command(aliases=['feed'])
    async def feeds(self, ctx):
        """Show feeds being followed in this channel"""
        records = await ctx.bot.pool.fetch('SELECT feed FROM rss_feeds WHERE channel_id = $1', ctx.channel.id)
        await ctx.embed_reply(
            '\n'.join(record['feed'] for record in records), title='RSS feeds being followed in this channel'
        )

    # R/PT60S
    @tasks.loop(seconds=60)
    async def check_feeds(self):
        records = await self.bot.pool.fetch(
            """
            SELECT DISTINCT ON (feed) feed, last_checked, ttl
            FROM rss_feeds
            ORDER BY feed, last_checked
            """
        )
        if not records:
            self.new_feed.clear()
            await self.new_feed.wait()
        for record in records:
            feed = record['feed']
            if record['ttl'] and datetime.datetime.now(datetime.timezone.utc) < record['last_checked'] + datetime.timedelta(
                minutes=record['ttl']
            ):
                continue
            try:
                async with self.bot.session.get(feed) as resp:
                    feed_text = await resp.text()
                feed_info: feedparser.FeedParserDict = await self.bot.loop.run_in_executor(
                    None,
                    functools.partial(
                        feedparser.parse, io.BytesIO(feed_text.encode('UTF-8')), response_headers={'Content-Location': feed}
                    ),
                )
                # Still necessary to run in executor?
                # lots of type ignores here because feedparser is a mess
                ttl = None
                if 'ttl' in feed_info.feed:
                    ttl = int(feed_info.feed.ttl)  # type: ignore
                await self.bot.pool.execute(
                    """
                    UPDATE rss_feeds
                    SET last_checked = NOW(), 
                        ttl = $1
                    WHERE feed = $2
                    """,
                    ttl,
                    feed,
                )
                for entry in feed_info.entries:
                    if 'id' not in entry:
                        continue
                    inserted: asyncpg.Record = await self.bot.pool.fetchrow(
                        """
                        INSERT INTO rss_entries (entry, feed)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                        RETURNING *
                        """,
                        entry.id,
                        feed,
                    )
                    if not inserted:
                        continue
                    # Get timestamp
                    ## if 'published_parsed' in entry:
                    ##  timestamp = datetime.datetime.fromtimestamp(time.mktime(entry.published_parsed))
                    ### inaccurate
                    timestamp = None
                    try:
                        if 'published' in entry and entry.published:
                            timestamp = dateutil.parser.parse(entry.published, tzinfos=self.tzinfos)  # type: ignore
                        elif 'updated' in entry:  # and entry.updated necessary?; check updated first?
                            timestamp = dateutil.parser.parse(entry.updated, tzinfos=self.tzinfos)  # type: ignore
                    except ValueError:
                        pass
                    # Get and set description, title, url + set timestamp
                    if not (description := entry.get('summary')) and 'content' in entry:
                        description = entry['content'][0].get('value')
                    if description:
                        description = BeautifulSoup(description, 'lxml').get_text(separator='\n')
                        description = re.sub(r'\n\s*\n', '\n', description)
                        if len(description) > 4096:
                            space_index = description.rfind(' ', 0, 4093)
                            description = description[:space_index] + '...'
                    if title := entry.get('title'):
                        title = textwrap.shorten(entry.get('title'), width=256, placeholder='...')  # type: ignore
                        title = html.unescape(title)
                    embed = discord.Embed(
                        title=title,
                        url=entry.link,
                        description=description,
                        timestamp=timestamp,
                        colour=0xFA9B39,
                    )
                    # Get and set thumbnail url
                    media_image: feedparser.FeedParserDict | None
                    image_link: feedparser.FeedParserDict | None
                    media_content: feedparser.FeedParserDict | None

                    thumbnail_url = (
                        (media_thumbnail := entry.get('media_thumbnail'))
                        and media_thumbnail[0].get('url')
                        or (
                            (media_content := entry.get('media_content'))  # type: ignore
                            and (media_image := discord.utils.find(lambda c: 'image' in c.get('medium', ''), media_content))
                            and media_image.get('url')
                        )
                        or (
                            (links := entry.get('links'))
                            and (image_link := discord.utils.find(lambda l: 'image' in l.get('type', ''), links))
                            and image_link.get('href')
                        )
                        or (
                            (content := entry.get('content'))
                            and (content_value := content[0].get('value'))
                            and (content_img := getattr(BeautifulSoup(content_value, 'lxml'), 'img'))  # type: ignore
                            and content_img.get('src')
                        )
                        or (
                            (media_content := entry.get('media_content'))  # type: ignore
                            and (media_content := discord.utils.find(lambda c: 'url' in c, media_content))
                            and media_content['url']
                        )
                        or (
                            (description := entry.get('description'))
                            and (description_img := getattr(BeautifulSoup(description, 'lxml'), 'img'))  # type: ignore
                            and description_img.get('src')
                        )
                    )
                    if thumbnail_url:
                        if not urllib.parse.urlparse(thumbnail_url).netloc:  # type: ignore
                            thumbnail_url = feed_info.feed.link + thumbnail_url  # type: ignore
                        embed.set_thumbnail(url=thumbnail_url)
                    # Get and set footer icon url
                    footer_icon_url = (
                        feed_info.feed.get('icon')  # type: ignore
                        or feed_info.feed.get('logo')  # type: ignore
                        or (feed_image := feed_info.feed.get('image'))  # type: ignore
                        and feed_image.get('href')
                        or (parsed_image := BeautifulSoup(feed_text, 'lxml').image)
                        and next(iter(parsed_image.attrs.values()), None)
                        or None
                    )
                    embed.set_footer(text=feed_info.feed.get('title', feed), icon_url=footer_icon_url)  # type: ignore
                    # Send embed(s)
                    channel_records = await self.bot.pool.fetch('SELECT channel_id FROM rss_feeds WHERE feed = $1', feed)
                    for record in channel_records:
                        if text_channel := self.bot.get_channel(record['channel_id']):
                            assert isinstance(text_channel, MessageableGuildChannel)
                            try:
                                await text_channel.send(embed=embed)
                            except discord.Forbidden:
                                pass
                            except discord.HTTPException as e:
                                if e.status == 400 and e.code == 50035:
                                    if (
                                        'In embed.url: Not a well formed URL.' in e.text
                                        or 'In embeds.0.url: Not a well formed URL.' in e.text  # still necessary?
                                        or (
                                            (
                                                'In embed.url: Scheme' in e.text
                                                or 'In embeds.0.url: Scheme' in e.text  # still necessary?
                                            )
                                            and "is not supported. Scheme must be one of ('http', 'https')." in e.text
                                        )
                                    ):
                                        embed.url = None
                                    if (
                                        'In embed.thumbnail.url: Not a well formed URL.' in e.text
                                        or 'In embeds.0.thumbnail.url: Not a well formed URL.' in e.text  # still necessary?
                                        or (
                                            (
                                                'In embed.thumbnail.url: Scheme' in e.text
                                                or 'In embeds.0.thumbnail.url: Scheme' in e.text  # still necessary?
                                            )
                                            and "is not supported. Scheme must be one of ('http', 'https')." in e.text
                                        )
                                    ):
                                        embed.set_thumbnail(url='')
                                    if 'In embed.footer.icon_url: Not a well formed URL.' in e.text or (
                                        'In embed.footer.icon_url: Scheme' in e.text
                                        and "is not supported. Scheme must be one of ('http', 'https')." in e.text
                                    ):
                                        embed.set_footer(text=feed_info.feed.title)  # type: ignore
                                    await text_channel.send(embed=embed)
                                else:
                                    raise
                        # TODO: Remove text channel data if now non-existent
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                aiohttp.TooManyRedirects,
                asyncio.TimeoutError,
                UnicodeDecodeError,
            ) as e:
                await self.bot.pool.execute(
                    """
                    INSERT INTO rss_errors (feed, type, message)
                    VALUES ($1, $2, $3)
                    """,
                    feed,
                    type(e).__name__,
                    str(e),
                )
                # Print error?
                await asyncio.sleep(10)
                # TODO: Add variable for sleep time
                # TODO: Remove persistently erroring feed or exponentially backoff?
            except discord.DiscordServerError as e:
                log.error('RSS Task Discord Server Error: %r', e, exc_info=e)
                await asyncio.sleep(60)
            except Exception as e:
                log.error('Uncaught RSS Task exception\n', exc_info=(type(e), e, e.__traceback__))
                await asyncio.sleep(60)

    @check_feeds.before_loop
    async def before_check_feeds(self):
        await self.bot.wait_until_ready()


async def setup(bot: Ayaka):
    await bot.add_cog(Feeds(bot))
