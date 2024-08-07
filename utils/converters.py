"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import datetime
import re
import zoneinfo
from typing import Any, Literal, Sequence, Type, TypedDict

import discord
import yarl
from discord.ext import commands
from typing_extensions import NotRequired, Self

from utils.context import Context, GuildContext


class DucklingNormalised(TypedDict):
    unit: Literal['second']
    value: int


class DucklingResponseValue(TypedDict):
    normalized: DucklingNormalised
    type: Literal['value']
    unit: str
    value: NotRequired[str]
    minute: NotRequired[int]
    hour: NotRequired[int]
    second: NotRequired[int]
    day: NotRequired[int]
    week: NotRequired[int]
    hour: NotRequired[int]


class DucklingResponse(TypedDict):
    body: str
    dim: Literal['duration', 'time']
    end: int
    start: int
    latent: bool
    value: DucklingResponseValue


class MemeDict(dict):
    def __getitem__(self, k: Sequence[Any]) -> Any:
        for key in self:
            if k in key:
                return super().__getitem__(key)
        raise KeyError(k)


class RedditMediaURL:
    VALID_PATH = re.compile(r'/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/.+)?')

    def __init__(self, url: yarl.URL) -> None:
        self.url = url
        self.filename = url.parts[1] + '.mp4'

    @classmethod
    async def convert(cls: Type[Self], ctx: Context, argument: str) -> Self:
        try:
            url = yarl.URL(argument)
        except Exception:
            raise commands.BadArgument('Not a valid URL.')

        headers = {'User-Agent': 'Discord:Ayaka:v1.0 (by @5ht2)'}
        await ctx.typing()
        if url.host == 'v.redd.it':
            # have to do a request to fetch the 'main' URL.
            async with ctx.session.get(url, headers=headers) as resp:
                url = resp.url

        is_valid_path = url.host and url.host.endswith('.reddit.com') and cls.VALID_PATH.match(url.path)
        if not is_valid_path:
            raise commands.BadArgument('Not a reddit URL.')

        # Now we go the long way
        async with ctx.session.get(url / '.json', headers=headers) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f'Reddit API failed with {resp.status}.')

            data = await resp.json()
            try:
                submission = data[0]['data']['children'][0]['data']
            except (KeyError, TypeError, IndexError):
                raise commands.BadArgument('Could not fetch submission.')

            try:
                media = submission['media']['reddit_video']
            except (KeyError, TypeError):
                try:
                    # maybe it's a cross post
                    crosspost = submission['crosspost_parent_list'][0]
                    media = crosspost['media']['reddit_video']
                except (KeyError, TypeError, IndexError):
                    raise commands.BadArgument('Could not fetch media information.')

            try:
                fallback_url = yarl.URL(media['fallback_url'])
            except KeyError:
                raise commands.BadArgument('Could not fetch fallback URL.')

            return cls(fallback_url)


class DatetimeConverter(commands.Converter[datetime.datetime]):
    @staticmethod
    async def get_timezone(ctx: Context) -> zoneinfo.ZoneInfo | None:
        if ctx.guild is None:
            tz = zoneinfo.ZoneInfo('UTC')
        else:
            row: str | None = await ctx.db.fetchval(
                'SELECT tz FROM tz_store WHERE user_id = $1 and $2 = ANY(guild_ids);', ctx.author.id, ctx.guild.id
            )
            if row:
                tz = zoneinfo.ZoneInfo(row)
            else:
                tz = zoneinfo.ZoneInfo('UTC')
        return tz

    @classmethod
    async def parse(
        cls,
        argument: str,
        /,
        *,
        ctx: Context,
        timezone: datetime.tzinfo | None = datetime.timezone.utc,
        now: datetime.datetime | None = None,
    ) -> list[tuple[datetime.datetime, int, int]]:
        now = now or datetime.datetime.now(datetime.timezone.utc)

        times = []

        async with ctx.bot.session.post(
            'http://127.0.0.1:7731/parse',
            data={'locale': 'en_GB', 'text': argument, 'dims': '["time", "duration"]', 'tz': str(timezone)},
        ) as resp:
            data: list[DucklingResponse] = await resp.json()

            for time in data:
                if time['dim'] == 'time' and 'value' in time['value']:
                    times.append((datetime.datetime.fromisoformat(time['value']['value']), time['start'], time['end']))  # type: ignore
                elif time['dim'] == 'duration':
                    times.append(
                        (
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=time['value']['normalized']['value']),
                            time['start'],
                            time['end'],
                        )
                    )
        return times

    @classmethod
    async def convert(cls, ctx: GuildContext, argument: str) -> datetime.datetime:
        timezone = await cls.get_timezone(ctx)
        now = ctx.message.created_at.astimezone(tz=timezone)

        parsed_times = await cls.parse(argument, ctx=ctx, timezone=timezone, now=now)

        if len(parsed_times) == 0:
            raise commands.BadArgument('Could not parse time.')
        elif len(parsed_times) > 1:
            ...  # TODO: raise on too many?

        return parsed_times[0][0]


class WhenAndWhatConverter_(commands.Converter[tuple[datetime.datetime, str]]):
    @classmethod
    async def convert(cls, ctx: GuildContext, argument: str) -> tuple[datetime.datetime, str]:
        timezone = await DatetimeConverter.get_timezone(ctx)
        now = ctx.message.created_at.astimezone(tz=timezone)

        # Strip some commmon stuff
        for prefix in ('me to ', 'me in ', 'me at ', 'me that '):
            if argument.startswith(prefix):
                argument = argument[len(prefix) :]
                break

        for suffix in ('from now',):
            if argument.endswith(suffix):
                argument = argument[: -len(suffix)]

        argument = argument.strip()

        # Determine the date argument
        parsed_times = await DatetimeConverter.parse(argument, ctx=ctx, timezone=timezone, now=now)

        if len(parsed_times) == 0:
            raise commands.BadArgument('Invalid time provided. Try e.g. "tomorrow" or "3 days".')
        elif len(parsed_times) > 1:
            ...  # TODO: raise on too many?

        when, begin, end = parsed_times[0]

        if begin != 0 and end != len(argument):
            raise commands.BadArgument(
                'Time is either in an inappropriate location, which '
                'must be either at the end or beginning of your input, '
                'or I just flat out did not understand what you meant. Sorry.'
            )

        if begin == 0:
            what = argument[end + 1 :].lstrip(' ,.!:;')
        else:
            what = argument[:begin].strip()

        for prefix in ('to ',):
            if what.startswith(prefix):
                what = what[len(prefix) :]

        return when, what


class MessageOrContent(commands.Converter[discord.Message | str]):
    async def convert(self, ctx: Context, argument: str) -> discord.Message | str:
        try:
            msg = await commands.MessageConverter().convert(ctx, argument)
        except commands.BadArgument:
            return argument
        return msg


class MessageOrCleanContent(commands.Converter[discord.Message | commands.clean_content]):
    async def convert(self, ctx: Context, argument: str) -> discord.Message | str:
        try:
            msg = await commands.MessageConverter().convert(ctx, argument)
        except commands.BadArgument:
            return await commands.clean_content().convert(ctx, argument)
        return msg


# This is because Discord is stupid with Slash Commands and doesn't actually have integer types.
# So to accept snowflake inputs you need a string and then convert it into an integer.
class Snowflake:
    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> int:
        try:
            return int(argument)
        except ValueError:
            param = ctx.current_parameter
            if param:
                raise commands.BadArgument(f'{param.name} argument expected a Discord ID, not {argument!r}')
            raise commands.BadArgument(f'expected a Discord ID not {argument!r}')
