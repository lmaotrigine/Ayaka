"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from bs4 import BeautifulSoup
from discord.ext import commands


if TYPE_CHECKING:
    from bot import Ayaka


class Anime(commands.Cog):
    """Anilist stuff idk."""

    def __init__(self, bot: Ayaka):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='anilist', id=961878585419890728)

    @commands.group(aliases=['anilist'], case_insensitive=True, invoke_without_command=True)
    async def anime(self, ctx, *, search: str):
        """Search AniList"""
        url = 'https://graphql.anilist.co'
        query = """
        query ($search: String) {
            Media (search: $search, type: ANIME) {
                title { romaji english native }
                format status description
                startDate { year month day }
                endDate { year month day }
                season seasonYear episodes duration
                source (version: 2)
                hashtag
                coverImage { extraLarge }
                bannerImage genres synonyms averageScore meanScore popularity favourites
                tags { name rank isMediaSpoiler }
                relations { edges {
                    node {
                        title { romaji english native }
                        type
                        status
                    }
                    relationType
                } }
                studios { edges {
                    node { name siteUrl }
                    isMain
                } }
                isAdult
                nextAiringEpisode { airingAt timeUntilAiring episode }
                rankings { rank type year season allTime context }
                siteUrl
            }
        }
        """
        # Use?:
        # relations
        # nextAiringEpisode
        # rankings
        # Other commands?:
        # airingSchedule
        # characters
        # recommendations
        # reviews
        # staff
        # stats
        # streamingEpisodes
        # trailer
        # trending
        # trends
        data = {'query': query, 'variables': {'search': search}}
        async with ctx.bot.session.post(url, json=data) as resp:
            data = await resp.json()
        if not (media := data['data']['Media']) and 'errors' in data:
            return await ctx.send(f':no_entry: Error: {data["errors"][0]["message"]}')
        # Title
        english_title = media['title']['english']
        native_title = media['title']['native']
        romaji_title = media['title']['romaji']
        title = english_title or native_title
        if native_title != title:
            title += f' ({native_title})'
        if romaji_title != english_title and len(title) + len(romaji_title) < 256:
            title += f' ({romaji_title})'
        # Description
        description = ''
        if media['description']:
            description = BeautifulSoup(media['description'], 'lxml').text
        # Format + Episodes
        fields: list[tuple[str, str] | tuple[str, str, bool]] = [
            (
                'Format',
                ' '.join(word if word in ('TV', 'OVA', 'ONA') else word.capitalize() for word in media['format'].split('_')),
            ),
            ('Episodes', media['episodes']),
        ]
        non_inline_fields = []
        # Episode Duration
        if duration := media['duration']:
            fields.append(('Episode Duration', f'{duration} minutes'))
        # Status
        fields.append(('Status', ' '.join(word.capitalize() for word in media['status'].split('_'))))
        # Start + End Date
        for date_type in ('start', 'end'):
            if year := media[date_type + 'Date']['year']:
                date = str(year)
                if month := media[date_type + 'Date']['month']:
                    date += f'-{month:0>2}'
                    if day := media[date_type + 'Date']['day']:
                        date += f'-{day:0>2}'
                fields.append((date_type.capitalize() + ' Date', date))
        # Season
        if media['season']:  # and media['seasonYear'] ?
            fields.append(('Season', f'{media["season"].capitalize()} {media["seasonYear"]}'))
        # Average Score
        if average_score := media['averageScore']:
            fields.append(('Average Score', f'{average_score}%'))
        # Mean Score
        if mean_score := media['meanScore']:
            fields.append(('Mean Score', f'{mean_score}%'))
        # Popularity + Favorites
        fields.extend((('Popularity', media['popularity']), ('Favorites', media['favourites'])))
        # Main Studio + Producers
        main_studio = None
        producers = []
        for studio in media['studios']['edges']:
            if studio['isMain']:
                main_studio = studio['node']
            else:
                producers.append(studio['node'])
        if main_studio:
            fields.append(('Studio', f'[{main_studio["name"]}]({main_studio["siteUrl"]})'))
        if producers:
            fields.append(
                (
                    'Producers',
                    ', '.join(f'[{producer["name"]}]({producer["siteUrl"]})' for producer in producers),
                    len(producers) <= 2,
                )
            )
        # Source
        if source := media['source']:
            fields.append(('Source', ' '.join(word.capitalize() for word in source.split('_'))))
        # Hashtag
        if hashtag := media['hashtag']:
            fields.append(('Hashtag', hashtag))
        # Genres
        if len(media['genres']) <= 2:
            fields.append(('Genres', ', '.join(media['genres'])))
        else:
            non_inline_fields.append(('Genres', ', '.join(media['genres']), False))
        # Synonyms
        if synonyms := media['synonyms']:
            fields.append(('Synonyms', ', '.join(synonyms)))
        # Adult
        fields.append(('Adult', media['isAdult']))
        # Tags
        tags = []
        for tag in media['tags']:
            if tag['isMediaSpoiler']:
                tags.append(f'||{tag["name"]}|| ({tag["rank"]}%)')
            else:
                tags.append(f'{tag["name"]} ({tag["rank"]}%)')
        if 0 < len(tags) <= 2:
            fields.append(('Tags', ', '.join(tags)))
        elif tags:
            non_inline_fields.append(('Tags', ', '.join(tags), False))
        embed = discord.Embed(description=description, title=title, url=media['siteUrl'])
        for field in fields:
            embed.add_field(name=field[0], value=field[1], inline=True)
        for field in non_inline_fields:
            embed.add_field(name=field[0], value=field[1], inline=field[2])
        nsfw_ok = not media['isAdult'] or ctx.channel.is_nsfw()
        if nsfw_ok:
            embed.set_thumbnail(url=media['coverImage']['extraLarge'])
            embed.set_image(url=media['bannerImage'])
        await ctx.send(embed=embed)

    @anime.command(name='links', aliases=['link'])
    async def anime_links(self, ctx, *, search: str):
        """Links for anime"""
        url = 'https://graphql.anilist.co'
        query = """
        query ($search: String) {
            Media (search: $search, type: ANIME) {
                title { romaji english native}
                coverImage { extraLarge }
                bannerImage
                externalLinks { url site }
                siteUrl
                isAdult
            }
        }
        """
        data = {'query': query, 'variables': {'search': search}}
        async with ctx.bot.session.post(url, json=data) as resp:
            data = await resp.json()
        if not (media := data['data']['Media']) and 'errors' in data:
            return await ctx.send(f':no_entry: Error: {data["errors"][0]["message"]}')
        english_title = media['title']['english']
        native_title = media['title']['native']
        romaji_title = media['title']['romaji']
        title = english_title or native_title
        if native_title != title:
            title += f' ({native_title})'
        if romaji_title != english_title and len(title) + len(romaji_title) < 256:
            title += f' ({romaji_title})'
        desc = '\n'.join(f'[{link["site"]}]({link["url"]})' for link in media['externalLinks'])
        embed = discord.Embed(description=desc, title=title, url=media['siteUrl'])
        nsfw_ok = not media['isAdult'] or ctx.channel.is_nsfw()
        if nsfw_ok:
            embed.set_thumbnail(url=media['coverImage']['extraLarge'])
            embed.set_image(url=media['bannerImage'])
        await ctx.send(embed=embed)


async def setup(bot: Ayaka):
    await bot.add_cog(Anime(bot))
