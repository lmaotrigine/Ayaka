"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import textwrap
from typing import TYPE_CHECKING, Any, Sequence

import asyncpg
import discord
from discord.ext import commands

from utils import db, formats, time
from utils.context import Context, GuildContext
from utils.converters import WhenAndWhatConverter


if TYPE_CHECKING:
    from typing_extensions import Self

    from bot import Ayaka


class Reminders(db.Table):
    id = db.PrimaryKeyColumn()

    expires = db.Column(db.Datetime(timezone=True), index=True)
    created = db.Column(db.Datetime(timezone=True), default="now() at time zone 'utc'")
    event = db.Column(db.String)
    extra = db.Column(db.JSON, default="'{}'::jsonb")


class DucklingConverter(commands.Converter[datetime.datetime]):
    async def get_tz(self, ctx: GuildContext) -> str | None:

        row = await ctx.bot.pool.fetchval(
            'SELECT tz FROM tz_store WHERE user_id = $1 and $2 = ANY(guild_ids);', ctx.author.id, ctx.guild.id
        )
        return row

    async def convert(self, ctx: GuildContext, argument: str) -> datetime.datetime:
        params = {'locale': 'en_GB', 'text': argument, 'dims': str(['time'])}
        tz = await self.get_tz(ctx)
        if tz is not None:
            params['tz'] = tz

        async with ctx.bot.session.post('http://127.0.0.1:7731/parse', data=params) as response:
            data = await response.json()

        return datetime.datetime.fromisoformat(data[0]['value']['values'][0]['value'])


class Timer:
    __slots__ = ('args', 'kwargs', 'event', 'id', 'created_at', 'expires')

    def __init__(self, *, record: asyncpg.Record) -> None:
        self.id: int = record['id']

        extra = record['extra']
        self.args: Sequence[Any] = extra.get('args', [])
        self.kwargs: dict[str, Any] = extra.get('kwargs', {})
        self.event: str = record['event']
        self.created_at: datetime.datetime = record['created']
        self.expires: datetime.datetime = record['expires']

    @classmethod
    def temporary(
        cls: type[Self],
        *,
        expires: datetime.datetime,
        created: datetime.datetime,
        event: str,
        args: Sequence[Any],
        kwargs: dict[str, Any],
    ) -> Self:
        pseudo = {
            'id': None,
            'extra': {'args': args, 'kwargs': kwargs},
            'event': event,
            'created': created,
            'expires': expires,
        }
        return cls(record=pseudo)

    def __eq__(self, other: Timer) -> bool:
        try:
            return self.id == other.id
        except AttributeError:
            return False

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def human_delta(self) -> str:
        return time.format_relative(self.created_at)

    @property
    def author_id(self) -> int | None:
        if self.args:
            return int(self.args[0])
        return None

    def __repr__(self) -> str:
        return f'<Timer created={self.created_at} expires={self.expires} event={self.event}>'


class Reminder(commands.Cog):
    """Reminders to do something."""

    def __init__(self, bot: Ayaka) -> None:
        self.bot = bot
        self._have_data = asyncio.Event()
        self._current_timer: Timer | None = None
        self._task = bot.loop.create_task(self.dispatch_timers())

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{ALARM CLOCK}')

    def cog_unload(self) -> None:
        self._task.cancel()

    async def cog_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))
        if isinstance(error, commands.TooManyArguments):
            await ctx.send(f'You called the {ctx.command.name} command with too many arguments.')

    async def get_active_timer(self, *, connection: asyncpg.Connection | None = None, days: int = 7) -> Timer | None:
        query = 'SELECT * FROM reminders WHERE expires < (CURRENT_DATE + $1::interval) ORDER BY expires LIMIT 1;'
        con = connection or self.bot.pool

        record = await con.fetchrow(query, datetime.timedelta(days=days))
        return Timer(record=record) if record else None

    async def wait_for_active_timers(self, *, connection: asyncpg.Connection | None = None, days: int = 7) -> Timer:
        async with db.MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            timer = await self.get_active_timer(connection=con, days=days)
            if timer is not None:
                self._have_data.set()
                return timer

            self._have_data.clear()
            self._current_timer = None
            await self._have_data.wait()
            timer = await self.get_active_timer(connection=con, days=days)
            assert timer is not None
            return timer

    async def call_timer(self, timer: Timer) -> None:
        # delete the timer
        query = 'DELETE FROM reminders WHERE id=$1;'
        await self.bot.pool.execute(query, timer.id)

        # dispatch the event
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self) -> None:
        try:
            while not self.bot.is_closed():
                # can only asyncio.sleep for up to ~48 days reliably
                # so we're gonna cap it off at 40 days
                # see: http://bugs.python.org/issue20493
                timer = self._current_timer = await self.wait_for_active_timers(days=40)
                now = datetime.datetime.now(datetime.timezone.utc)

                if timer.expires >= now:
                    to_sleep = (timer.expires - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.call_timer(timer)
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    async def short_timer_optimisation(self, seconds: float, timer: Timer) -> None:
        await asyncio.sleep(seconds)
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def create_timer(self, when: datetime.datetime, event: str, *args: Any, **kwargs: Any) -> Timer:
        r"""Creates a timer.
        Parameters
        -----------
        when: datetime.datetime
            When the timer should fire.
        event: str
            The name of the event to trigger.
            Will transform to 'on_{event}_timer_complete'.
        \*args
            Arguments to pass to the event
        \*\*kwargs
            Keyword arguments to pass to the event
        connection: asyncpg.Connection
            Special keyword-only argument to use a specific connection
            for the DB request.
        created: datetime.datetime
            Special keyword-only argument to use as the creation time.
            Should make the timedeltas a bit more consistent.
        Note
        ------
        Arguments and keyword arguments must be JSON serialisable.
        Returns
        --------
        :class:`Timer`
        """
        try:
            connection: asyncpg.Connection | asyncpg.Pool = kwargs.pop('connection')
        except KeyError:
            connection = self.bot.pool

        try:
            now: datetime.datetime = kwargs.pop('created')
        except KeyError:
            now = datetime.datetime.now(datetime.timezone.utc)

        when = when.astimezone(datetime.timezone.utc)
        now = now.astimezone(datetime.timezone.utc)

        timer = Timer.temporary(event=event, args=args, kwargs=kwargs, expires=when, created=now)
        delta = (when - now).total_seconds()
        if delta <= 60:
            # a shortcut for small timers
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        query = """INSERT INTO reminders (event, extra, expires, created)
                   VALUES ($1, $2::jsonb, $3, $4)
                   RETURNING id;
                """

        row: asyncpg.Record = await connection.fetchrow(query, event, {'args': args, 'kwargs': kwargs}, when, now)
        timer.id = row[0]

        # only set the data check if it can be waited on
        if delta <= (86400 * 40):  # 40 days
            self._have_data.set()

        # check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    @commands.group(aliases=['timer', 'remind'], usage='<when>', invoke_without_command=True)
    async def reminder(
        self,
        ctx: Context,
        *,
        when: tuple[datetime.datetime, str] = commands.param(converter=WhenAndWhatConverter),
    ):
        """Reminds you of something after a certain amount of time.
        The input can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:
        - 'next thursday at 3pm do something funny'
        - 'do the dishes tomorrow'
        - 'in 3 days do the thing'
        - '2d unmute someone'
        Times are in UTC.
        """
        parsed_when, parsed_what = when

        timer = await self.create_timer(
            parsed_when,
            'reminder',
            ctx.author.id,
            ctx.channel.id,
            parsed_what or '...',
            connection=ctx.db,
            created=ctx.message.created_at,
            message_id=ctx.message.id,
        )
        human = discord.utils.format_dt(timer.expires, style='F')
        await ctx.reply(
            f'Alright, at {human}: {parsed_what}',
            mention_author=False,
        )

    @reminder.command(name='list', ignore_extra=False)
    async def reminder_list(self, ctx: Context):
        """Shows the 10 latest currently running reminders."""
        query = """SELECT id, expires, extra #>> '{args,2}'
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires
                   LIMIT 10;
                """

        records = await ctx.db.fetch(query, str(ctx.author.id))

        if len(records) == 0:
            return await ctx.send('No currently running reminders.')

        e = discord.Embed(colour=discord.Colour.blurple(), title='Reminders')

        if len(records) == 10:
            e.set_footer(text='Only showing up to 10 reminders.')
        else:
            e.set_footer(text=f'{len(records)} {formats.plural(len(records)):reminder}.')

        for _id, expires, message in records:
            shorten = textwrap.shorten(message, width=512)
            e.add_field(
                name=f'{_id}: {time.format_relative(expires)}',
                value=shorten,
                inline=False,
            )

        await ctx.send(embed=e)

    @reminder.command(name='delete', aliases=['remove', 'cancel'], ignore_extra=False)
    async def reminder_delete(self, ctx: Context, *, id: int):
        """Deletes a reminder by its ID.
        To get a reminder ID, use the reminder list command.
        You must own the reminder to delete it, obviously.
        """

        query = """DELETE FROM reminders
                   WHERE id=$1
                   AND event = 'reminder'
                   AND extra #>> '{args,0}' = $2;
                """

        status = await ctx.db.execute(query, id, str(ctx.author.id))
        if status == 'DELETE 0':
            return await ctx.send('Could not delete any reminders with that ID.')

        # if the current timer is being deleted
        if self._current_timer and self._current_timer.id == id:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        await ctx.send('Successfully deleted reminder.')

    @reminder.command(name='clear', ignore_extra=False)
    async def reminder_clear(self, ctx: Context):
        """Clears all reminders you have set."""

        # For UX purposes this has to be two queries.

        query = """SELECT COUNT(*)
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1;
                """

        author_id = str(ctx.author.id)
        total = await ctx.db.fetchrow(query, author_id)
        assert total is not None  # will always be an int
        total = total[0]
        if total == 0:
            return await ctx.send('You do not have any reminders to delete.')

        confirm = await ctx.prompt(f'Are you sure you want to delete {formats.plural(total):reminder}?')
        if not confirm:
            return await ctx.send('Aborting')

        query = """DELETE FROM reminders WHERE event = 'reminder' AND extra #>> '{args,0}' = $1;"""
        await ctx.db.execute(query, author_id)

        # check if the current timer is being cleared and cancel it if so
        if self._current_timer and self._current_timer.author_id == ctx.author.id:
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())
        await ctx.send(f'Successfully deleted {formats.plural(total):reminder}.')

    @commands.Cog.listener()
    async def on_reminder_timer_complete(self, timer: Timer) -> None:
        author_id, channel_id, message = timer.args

        try:
            channel = self.bot.get_channel(channel_id) or (await self.bot.fetch_channel(channel_id))
        except discord.HTTPException:
            return

        guild_id = channel.guild.id if isinstance(channel, (discord.TextChannel, discord.Thread)) else '@me'
        message_id = timer.kwargs.get('message_id')
        msg = f'<@{author_id}>, {timer.human_delta}: {message}'
        view = discord.utils.MISSING

        if message_id:
            url = f'https://discord.com/channels/{guild_id}/{channel.id}/{message_id}'
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='Go to original message', url=url))

        try:
            await channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True), view=view)  # type: ignore # can't make this a non-messageable lol
        except discord.HTTPException:
            return


async def setup(bot):
    await bot.add_cog(Reminder(bot))
