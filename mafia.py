"""Discord commands and timer lifecycle; game rules live in mafia_game.py."""

import asyncio
import logging
import math
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from mafia_cards import (
    duration_text,
    game_over_card,
    morning_card,
    night_card,
    send_card,
    vote_result_card,
    voting_card,
)
from mafia_game import MAX_PLAYERS, MIN_PLAYERS, GameRuleError, GameState, Phase, Role
from mafia_lifecycle import DepartureNotice, LifecyclePolicy, LifecycleService
from mafia_restart import notify_interrupted

NIGHT_SECONDS = 120
DISCUSSION_SECONDS = 120
VOTING_SECONDS = 60
MESSAGE_RETRY_SECONDS = 5
logger = logging.getLogger(__name__)

PHASE_LABELS = {
    Phase.LOBBY: "Waiting for players",
    Phase.ASSIGNING: "Sending roles",
    Phase.NIGHT: "Night",
    Phase.RESOLVING: "Announcing morning",
    Phase.DISCUSSION: "Discussion",
    Phase.FINISHED: "Game over",
    Phase.CANCELLED: "Cancelled",
    Phase.OPENING_VOTE: "Opening voting",
    Phase.VOTING: "Voting",
    Phase.COUNTING: "Counting votes",
    Phase.NEXT_NIGHT: "Waiting for night",
    Phase.OPENING_NIGHT: "Starting night",
    Phase.DEPARTING: "Updating players",
}

STABLE_PHASES = {Phase.NIGHT, Phase.DISCUSSION, Phase.VOTING, Phase.NEXT_NIGHT}


class NightPromptUnavailable(Exception):
    """A private night prompt cannot be delivered; stop without exposing its recipient."""


@dataclass
class Lobby:
    """One game's rules plus the Discord objects and background task it needs."""

    game: GameState
    channel: discord.abc.Messageable
    members: dict[int, discord.Member]
    night_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    discussion_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    votes_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    game_task: asyncio.Task | None = field(default=None, repr=False)
    startup_task: asyncio.Task | None = field(default=None, repr=False)
    last_prompted_night: int = 1
    guild: discord.Guild | None = None
    lobby_deadline: float = math.inf
    last_activity: float = 0
    finished_at: float | None = None
    pending_forfeits: set[int] = field(default_factory=set)
    lifecycle_changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    departure_notice: DepartureNotice | None = field(default=None, repr=False)
    journal_id: str | None = None


class Mafia(commands.Cog):
    def __init__(
        self,
        night_seconds=NIGHT_SECONDS,
        discussion_seconds=DISCUSSION_SECONDS,
        voting_seconds=VOTING_SECONDS,
        lifecycle_policy: LifecyclePolicy | None = None,
        restart_journal=None,
        bot=None,
    ):
        if not all(
            math.isfinite(value) and value > 0
            for value in (night_seconds, discussion_seconds, voting_seconds)
        ):
            raise ValueError("Phase durations must be finite and greater than zero.")
        self.lobbies: dict[int, Lobby] = {}
        self.night_seconds = night_seconds
        self.discussion_seconds = discussion_seconds
        self.voting_seconds = voting_seconds
        self.closing = False
        self.worker_tasks: set[asyncio.Task] = set()
        self.lifecycle = LifecycleService(self, lifecycle_policy or LifecyclePolicy())
        self.restart_journal = restart_journal
        self.bot = bot
        self.recovery_task = None
        self.recovery_ready = asyncio.Event()
        if restart_journal is None:
            self.recovery_ready.set()

    def now(self):
        return asyncio.get_running_loop().time()

    def touch(self, lobby):
        lobby.last_activity = self.now()
        if self.restart_journal and lobby.journal_id:
            self.restart_journal.update_host(lobby.journal_id, lobby.game.host_id)

    async def cog_load(self):
        self.lifecycle.start()
        if self.restart_journal is not None:
            records = self.restart_journal.records()
            self.recovery_task = asyncio.create_task(self.recover_interrupted(records))

    async def recover_interrupted(self, records):
        await self.bot.wait_until_ready()
        try:
            await notify_interrupted(
                self.bot, self.restart_journal, records, self.lifecycle.policy.request_seconds
            )
        except Exception:
            logger.exception("Restart notification failed; undelivered records remain on disk.")
        finally:
            self.recovery_ready.set()

    async def cog_unload(self):
        """Release all games and cancel their workers when the bot shuts down."""
        self.closing = True
        lobbies = list(self.lobbies.values())
        self.lobbies.clear()
        tasks = set(self.worker_tasks)
        if self.recovery_task is not None:
            tasks.add(self.recovery_task)
        if self.lifecycle.task is not None:
            self.lifecycle.task.cancel()
            tasks.add(self.lifecycle.task)
        for lobby in lobbies:
            lobby.game.phase = Phase.CANCELLED
            tasks.update(task for task in (lobby.game_task, lobby.startup_task) if task is not None)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def discard_lobby(self, channel_id: int, lobby: Lobby):
        """Identity checks prevent an old task from deleting a replacement game."""
        if self.lobbies.get(channel_id) is not lobby:
            return
        del self.lobbies[channel_id]
        if self.restart_journal and lobby.journal_id:
            self.restart_journal.remove(lobby.journal_id)
        lobby.game.phase = Phase.CANCELLED
        tasks = {
            task
            for task in (lobby.game_task, lobby.startup_task)
            if task is not None and task is not asyncio.current_task()
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def abort_game(self, channel_id: int, lobby: Lobby, reason: str):
        if self.lobbies.get(channel_id) is not lobby:
            return
        await self.discard_lobby(channel_id, lobby)
        try:
            await asyncio.wait_for(
                lobby.members[lobby.game.host_id].send(reason),
                timeout=self.lifecycle.policy.request_seconds,
            )
        except (discord.HTTPException, TimeoutError):
            logger.warning("Could not notify the host of stopped game in channel %s.", channel_id)

    async def close_game(self, channel_id, lobby, title, reason):
        if self.lobbies.get(channel_id) is not lobby:
            return
        await self.discard_lobby(channel_id, lobby)
        if channel_id in self.lobbies:
            return
        try:
            card = discord.Embed(title=title, description=reason, color=0x75808A)
            await self.send_phase_card(lobby.channel, card)
        except (discord.HTTPException, TimeoutError):
            logger.warning("Could not announce closed game in channel %s.", channel_id)

    async def send_phase_card(self, destination, card):
        return await asyncio.wait_for(
            send_card(destination, card), timeout=self.lifecycle.policy.request_seconds
        )

    def require_participating(self, lobby, player_id):
        if player_id in lobby.pending_forfeits or player_id in lobby.game.forfeited:
            raise GameRuleError("You have left this game. Wait for the departure announcement.")

    def active_game(self, player_id: int) -> Lobby | None:
        return next(
            (
                lobby
                for lobby in self.lobbies.values()
                if lobby.game.phase not in (Phase.FINISHED, Phase.CANCELLED)
                and player_id in lobby.game.players
                and player_id not in lobby.game.forfeited
            ),
            None,
        )

    def require_available(self, player_id: int):
        if self.closing:
            raise GameRuleError("The bot is shutting down. Please try again after it restarts.")
        if self.active_game(player_id) is not None:
            raise GameRuleError("You are already in a game. Leave its lobby or wait until it ends.")

    def get_lobby(self, ctx) -> Lobby:
        lobby = self.lobbies.get(ctx.channel.id)
        if lobby is None:
            raise GameRuleError("No lobby here yet. Use `!mafia create`.")
        if lobby.game.phase == Phase.LOBBY and self.now() >= lobby.lobby_deadline:
            del self.lobbies[ctx.channel.id]
            lobby.game.phase = Phase.CANCELLED
            raise GameRuleError("This lobby expired. Use `!mafia create` to start a new one.")
        return lobby

    def player_list(self, lobby: Lobby) -> str:
        game = lobby.game
        return "\n".join(
            f"{number}. {lobby.members[player].mention}"
            + (" (host)" if player == game.host_id else "")
            + (" (eliminated)" if game.roles and player not in game.alive else "")
            for number, player in enumerate(game.players, start=1)
        )

    def target_commands(self, lobby: Lobby, mafia_id: int) -> str:
        return "\n".join(
            f"{number}. {discord.utils.escape_markdown(lobby.members[player].display_name)} — "
            f"`!mafia kill {number}`"
            for number, player in enumerate(lobby.game.players, start=1)
            if player in lobby.game.alive and player != mafia_id
        )

    def role_message(self, ctx, lobby: Lobby, player_id: int) -> str:
        role = lobby.game.roles[player_id]
        if role == Role.MAFIA:
            instructions = (
                "**Choose a target**\n"
                "When Night 1 begins, copy one command below and send it here:\n"
                f"{self.target_commands(lobby, player_id)}\n\n"
                "**Your choice is final.** Morning begins automatically after you choose."
            )
        else:
            instructions = (
                "Find the Mafia during the day.\nAt night, stay quiet and wait for morning.\n\n"
                f"**Players**\n{self.player_list(lobby)}"
            )
        return (
            f"Your secret role is **{role}**. Keep it secret!\n"
            f"{ctx.guild.name} • {ctx.channel.mention}\n\n{instructions}\n\n"
            "Wait for Night 1 in the server channel. "
            "If the game is cancelled or the bot restarts, ignore this DM."
        )

    @commands.group(invoke_without_command=True)
    async def mafia(self, ctx):
        """Play Mafia with 3-6 friends until Citizens or Mafia win."""
        await ctx.send(
            "**Mafia**\n"
            "`!mafia create` - host a lobby (you join automatically)\n"
            "`!mafia join` / `!mafia leave` - join or leave\n"
            "`!mafia lobby` - show players\n"
            "`!mafia check` - check this channel and your DMs\n"
            "`!mafia start` - host sends roles and begins Night 1\n"
            "`!mafia kill <player-number>` - Mafia's final choice, in DMs only\n"
            "`!mafia ready` - skip discussion once a majority is ready\n"
            "`!mafia vote <player-number>` / `!mafia vote skip` - cast a final vote\n"
            "`!mafia cancel` - host clears the lobby\n"
            "`!mafia transfer <player-number>` - hand over hosting\n"
            "`!mafia forfeit` - leave an active game\n"
            "`!mafia forcecancel` - emergency stop (Manage Server permission)\n"
            "Rounds repeat automatically until a team wins.\n"
            "The Mafia's DM includes commands to copy for each target."
        )

    @mafia.command()
    @commands.guild_only()
    async def create(self, ctx):
        """Create a lobby in this server channel and join as host."""
        if not self.recovery_ready.is_set():
            raise GameRuleError("Restart checks are in progress. Try creating a lobby shortly.")
        existing = self.lobbies.get(ctx.channel.id)
        if (
            existing
            and existing.game.phase == Phase.LOBBY
            and self.now() >= existing.lobby_deadline
        ):
            await self.discard_lobby(ctx.channel.id, existing)
        if ctx.channel.id in self.lobbies:
            raise GameRuleError("This channel already has a lobby. Use `!mafia lobby`.")
        self.require_available(ctx.author.id)
        session = (
            self.restart_journal.add(ctx.channel.id, ctx.author.id)
            if self.restart_journal is not None
            else None
        )
        now = self.now()
        self.lobbies[ctx.channel.id] = Lobby(
            game=GameState(ctx.author.id),
            channel=ctx.channel,
            members={ctx.author.id: ctx.author},
            guild=ctx.guild,
            lobby_deadline=now + self.lifecycle.policy.lobby_seconds,
            last_activity=now,
            journal_id=session,
        )
        await ctx.send(
            f"**Mafia lobby open**\nHost: {ctx.author.mention} • 1/{MAX_PLAYERS} players\n"
            "Join: `!mafia join`\n"
            f"Host: use `!mafia start` with {MIN_PLAYERS}–{MAX_PLAYERS} players.\n"
            "Make sure I can send you DMs."
            f" The lobby expires after {duration_text(self.lifecycle.policy.lobby_seconds)} if it isn't started."
        )

    @mafia.command()
    @commands.guild_only()
    async def join(self, ctx):
        """Join the channel's open lobby."""
        lobby = self.get_lobby(ctx)
        self.require_available(ctx.author.id)
        lobby.game.join(ctx.author.id)
        lobby.members[ctx.author.id] = ctx.author
        self.touch(lobby)
        await ctx.send(
            f"{ctx.author.mention} joined! Players: {len(lobby.game.players)}/{MAX_PLAYERS}."
        )

    @mafia.command()
    @commands.guild_only()
    async def leave(self, ctx):
        """Leave an open lobby; hosting passes to the next player automatically."""
        lobby = self.get_lobby(ctx)
        lobby.game.require_open()
        if ctx.author.id not in lobby.game.players:
            raise GameRuleError("You are not in this lobby.")
        await self.remove_waiting_players(ctx.channel.id, lobby, {ctx.author.id})

    async def remove_waiting_players(self, channel_id, lobby, players):
        names = [
            lobby.members[player].mention for player in lobby.game.players if player in players
        ]
        for player in list(lobby.game.players):
            if player in players:
                lobby.game.leave(player)
                del lobby.members[player]
        if not lobby.game.players:
            await self.close_game(
                channel_id,
                lobby,
                "Lobby closed",
                "The last player left. Use `!mafia create` to start fresh.",
            )
            return
        self.touch(lobby)
        await lobby.channel.send(
            f"{', '.join(names)} left the lobby. Host: {lobby.members[lobby.game.host_id].mention}. "
            f"Players: {len(lobby.game.players)}/{MAX_PLAYERS}."
        )

    @mafia.command(name="lobby")
    @commands.guild_only()
    async def show_lobby(self, ctx):
        """Show players and phase, keeping roles secret."""
        lobby = self.get_lobby(ctx)
        phase = PHASE_LABELS[lobby.game.phase]
        if lobby.game.night_number:
            phase += f" • Round {lobby.game.night_number}"
        progress = ""
        if lobby.game.phase == Phase.DISCUSSION:
            progress = (
                f"\nReady: {len(lobby.game.ready_players)}/{lobby.game.majority} needed to skip."
            )
        elif lobby.game.phase == Phase.VOTING:
            progress = f"\nVotes: {len(lobby.game.votes)}/{len(lobby.game.alive)}."
        await ctx.send(
            f"**Mafia — {phase}**\n"
            f"Players: {len(lobby.game.players)}/{MAX_PLAYERS}\n{self.player_list(lobby)}{progress}"
        )

    @mafia.command()
    @commands.guild_only()
    async def start(self, ctx):
        """Host only: send secret roles and begin Night 1."""
        lobby = self.get_lobby(ctx)
        lobby.game.require_host(ctx.author.id)
        # Synchronous rules run before any network await can let another command run.
        lobby.game.begin_assignment()
        self.touch(lobby)
        completed = False
        lobby.startup_task = asyncio.current_task()
        try:
            for player_id, member in lobby.members.items():
                if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                    return
                await asyncio.wait_for(
                    member.send(self.role_message(ctx, lobby, player_id)),
                    timeout=self.lifecycle.policy.request_seconds,
                )
            if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                return
            if lobby.pending_forfeits:
                await self.close_game(
                    ctx.channel.id,
                    lobby,
                    "Game cancelled",
                    "A player left before the game started. Create a fresh lobby.",
                )
                return
            await self.send_phase_card(ctx, night_card(1, self.night_seconds))
            if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                return
            lobby.game.begin_night(asyncio.get_running_loop().time(), self.night_seconds)
            lobby.game_task = asyncio.create_task(self.run_game(ctx.channel.id, lobby))
            self.worker_tasks.add(lobby.game_task)
            lobby.game_task.add_done_callback(self.worker_tasks.discard)
            completed = True
        except (discord.HTTPException, TimeoutError):
            try:
                await asyncio.wait_for(
                    ctx.send(
                        "**Game cancelled** — I couldn't deliver all the messages.\n"
                        "Ignore any roles I just sent. Allow DMs from this bot, "
                        "then use `!mafia create` to try again."
                    ),
                    timeout=self.lifecycle.policy.request_seconds,
                )
            except (discord.HTTPException, TimeoutError):
                logger.warning(
                    "Could not announce failed role delivery in channel %s.", ctx.channel.id
                )
        finally:
            lobby.startup_task = None
            if not completed:
                await self.discard_lobby(ctx.channel.id, lobby)

    @mafia.command(ignore_extra=False)
    @commands.dm_only()
    async def kill(self, ctx, player_number: int):
        """DM only: !mafia kill <player-number>. Your choice is final."""
        lobby = self.active_game(ctx.author.id)
        if lobby is None:
            raise GameRuleError("You are not in an active game. Join a lobby in your server first.")
        self.require_participating(lobby, ctx.author.id)
        target_id = lobby.game.lock_target(
            ctx.author.id, player_number, asyncio.get_running_loop().time()
        )
        lobby.night_ready.set()
        self.touch(lobby)
        await self.confirm_action(
            ctx,
            f"Choice locked: **{discord.utils.escape_markdown(lobby.members[target_id].display_name)}**.\n"
            "Morning will begin automatically.",
        )

    @mafia.command(ignore_extra=False)
    @commands.guild_only()
    async def ready(self, ctx):
        """Living players: mark ready to skip the rest of discussion by majority."""
        lobby = self.get_lobby(ctx)
        self.require_participating(lobby, ctx.author.id)
        if lobby.game.mark_ready(ctx.author.id, asyncio.get_running_loop().time()):
            lobby.discussion_ready.set()
        self.touch(lobby)
        await self.confirm_action(
            ctx,
            f"{ctx.author.mention} is ready to vote. "
            f"{len(lobby.game.ready_players)}/{lobby.game.majority} ready to skip discussion.",
        )

    @mafia.command(ignore_extra=False)
    @commands.guild_only()
    async def vote(self, ctx, selection: str):
        """Cast a final vote: !mafia vote <player-number>, or !mafia vote skip."""
        lobby = self.get_lobby(ctx)
        self.require_participating(lobby, ctx.author.id)
        if selection.lower() == "skip":
            number = None
        else:
            try:
                number = int(selection)
            except ValueError:
                raise GameRuleError("Use `!mafia vote 3` or `!mafia vote skip`.") from None
        if lobby.game.cast_vote(ctx.author.id, number, asyncio.get_running_loop().time()):
            lobby.votes_ready.set()
        self.touch(lobby)
        await self.confirm_action(
            ctx,
            f"{ctx.author.mention}'s vote is locked. "
            f"{len(lobby.game.votes)}/{len(lobby.game.alive)} players have voted.",
        )

    async def confirm_action(self, ctx, message: str):
        try:
            await ctx.send(message)
        except discord.HTTPException:
            logger.warning("Could not confirm a saved game action.")

    async def wait_for_phase(self, event: asyncio.Event, deadline: float, interrupt: asyncio.Event):
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        tasks = [asyncio.create_task(event.wait()), asyncio.create_task(interrupt.wait())]
        try:
            await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            return not interrupt.is_set()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_game(self, channel_id: int, lobby: Lobby):
        """One worker owns every timed phase, so rounds cannot overlap."""
        failures = 0
        try:
            while self.lobbies.get(channel_id) is lobby and lobby.game.phase != Phase.FINISHED:
                try:
                    game = lobby.game
                    if game.phase == Phase.DEPARTING:
                        await self.announce_departures(channel_id, lobby)
                        failures = 0
                        continue
                    if lobby.pending_forfeits and game.phase in STABLE_PHASES:
                        self.begin_departures(lobby)
                        continue
                    lobby.lifecycle_changed.clear()
                    if game.phase == Phase.NIGHT:
                        if not await self.wait_for_phase(
                            lobby.night_ready, game.night_deadline, lobby.lifecycle_changed
                        ):
                            continue
                        await self.resolve_night(channel_id, lobby)
                    elif game.phase == Phase.DISCUSSION:
                        if not await self.wait_for_phase(
                            lobby.discussion_ready,
                            game.discussion_deadline,
                            lobby.lifecycle_changed,
                        ):
                            continue
                        await self.open_voting(lobby)
                    elif game.phase == Phase.VOTING:
                        if not await self.wait_for_phase(
                            lobby.votes_ready, game.voting_deadline, lobby.lifecycle_changed
                        ):
                            continue
                        await self.resolve_votes(lobby)
                    elif game.phase == Phase.NEXT_NIGHT:
                        await self.open_night(lobby)
                    else:
                        raise RuntimeError(f"Unexpected worker phase: {game.phase}")
                    failures = 0
                except NightPromptUnavailable:
                    await self.close_game(
                        channel_id,
                        lobby,
                        "Game stopped",
                        "A private night prompt could not be delivered. Check DM settings before creating a new lobby.",
                    )
                    return
                except (discord.Forbidden, discord.NotFound):
                    logger.warning("Game channel %s is unavailable; stopping the game.", channel_id)
                    await self.abort_game(
                        channel_id,
                        lobby,
                        "Your Mafia game was cancelled because I can't post in its channel. "
                        "Check the channel and my permissions, then create a new lobby.",
                    )
                except (discord.HTTPException, TimeoutError) as error:
                    failures += 1
                    status = getattr(error, "status", 0)
                    if 400 <= status < 500 and status != 429:
                        logger.error(
                            "Discord rejected a game message in channel %s (HTTP %s).",
                            channel_id,
                            status,
                        )
                        await self.abort_game(
                            channel_id,
                            lobby,
                            "Your Mafia game stopped because Discord rejected an announcement. "
                            "Check the bot's terminal, then create a new lobby.",
                        )
                        return
                    if failures >= self.lifecycle.policy.delivery_attempts:
                        await self.close_game(
                            channel_id,
                            lobby,
                            "Game stopped — delivery failed",
                            "Discord messages kept failing. Please create a new lobby when the connection recovers.",
                        )
                        return
                    logger.warning(
                        "Game message delivery failed in channel %s; retrying.", channel_id
                    )
                    await asyncio.sleep(MESSAGE_RETRY_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Game worker failed in channel %s.", channel_id)
            await self.abort_game(
                channel_id,
                lobby,
                "Your Mafia game stopped because of an unexpected error. "
                "Check the bot's terminal, then create a new lobby.",
            )
        finally:
            if self.lobbies.get(channel_id) is lobby and lobby.game.phase == Phase.FINISHED:
                lobby.finished_at = self.now()
                if self.restart_journal and lobby.journal_id:
                    self.restart_journal.remove(lobby.journal_id)

    def begin_departures(self, lobby):
        game = lobby.game
        players = tuple(
            player
            for player in game.players
            if player in lobby.pending_forfeits and player not in game.forfeited
        )
        lobby.pending_forfeits.clear()
        lobby.lifecycle_changed.clear()
        if not players:
            return
        previous, old_host = game.phase, game.host_id
        outcome = game.forfeit_players(set(players))
        self.touch(lobby)
        lobby.departure_notice = DepartureNotice(previous, outcome, players, old_host)
        game.phase = Phase.DEPARTING

    async def announce_departures(self, channel_id, lobby):
        notice = lobby.departure_notice
        if notice is None:
            raise RuntimeError("Missing departure result")
        game = lobby.game
        names = ", ".join(lobby.members[player].mention for player in notice.players)
        result = f"{names} left the game."
        if notice.outcome.winner:
            card = game_over_card(game, notice.outcome, result)
        else:
            card = discord.Embed(
                title="Player departure" if game.alive else "Game closed",
                description=result,
                color=0x75808A,
            )
            if game.alive:
                card.add_field(name="Alive", value=f"{len(game.alive)} players", inline=False)
                if notice.old_host != game.host_id:
                    card.add_field(
                        name="New host", value=lobby.members[game.host_id].mention, inline=False
                    )
                card.set_footer(
                    text="Numbers stay the same. Votes for departed players are cleared."
                )
            else:
                card.add_field(
                    name="Start fresh", value="Everyone left. Use `!mafia create`.", inline=False
                )
        await self.send_phase_card(lobby.channel, card)
        if self.lobbies.get(channel_id) is not lobby:
            return
        lobby.departure_notice = None
        if not game.alive:
            await self.discard_lobby(channel_id, lobby)
        elif notice.outcome.winner:
            game.phase = Phase.FINISHED
        else:
            game.phase = notice.previous_phase
            for event, ready in (
                (lobby.night_ready, game.night_target is not None),
                (lobby.discussion_ready, len(game.ready_players) >= game.majority),
                (lobby.votes_ready, len(game.votes) == len(game.alive)),
            ):
                event.clear()
                if ready:
                    event.set()

    async def resolve_night(self, channel_id: int, lobby: Lobby):
        if self.lobbies.get(channel_id) is not lobby or lobby.game.phase != Phase.NIGHT:
            return
        outcome = lobby.game.prepare_morning()
        try:
            await self.send_phase_card(
                lobby.channel,
                morning_card(lobby.game, lobby.members, outcome, self.discussion_seconds),
            )
        except BaseException:
            # Restore the state on failure, including task cancellation, then re-raise.
            lobby.game.retry_morning()
            raise
        lobby.discussion_ready.clear()
        lobby.game.complete_morning(
            outcome, asyncio.get_running_loop().time(), self.discussion_seconds
        )

    async def open_voting(self, lobby: Lobby):
        game = lobby.game
        game.prepare_voting()
        try:
            await self.send_phase_card(
                lobby.channel, voting_card(game, lobby.members, self.voting_seconds)
            )
        except BaseException:
            if game.phase == Phase.OPENING_VOTE:
                game.phase = Phase.DISCUSSION
            raise
        lobby.votes_ready.clear()
        game.begin_voting(asyncio.get_running_loop().time(), self.voting_seconds)

    async def resolve_votes(self, lobby: Lobby):
        game = lobby.game
        outcome = game.prepare_vote_result()
        try:
            await self.send_phase_card(
                lobby.channel, vote_result_card(game, lobby.members, outcome)
            )
        except BaseException:
            if game.phase == Phase.COUNTING:
                game.phase = Phase.VOTING
            raise
        game.complete_vote_result(outcome)

    async def open_night(self, lobby: Lobby):
        game = lobby.game
        game.prepare_next_night()
        number = game.night_number + 1
        try:
            if lobby.last_prompted_night < number:
                mafia_id = next(player for player in game.alive if game.roles[player] == Role.MAFIA)
                try:
                    await asyncio.wait_for(
                        lobby.members[mafia_id].send(
                            f"🌙 **Night {number}** • {lobby.channel.mention}\n"
                            f"{self.target_commands(lobby, mafia_id)}\n"
                            "Your choice is final. Wait for the night announcement, then send a command here."
                        ),
                        timeout=self.lifecycle.policy.request_seconds,
                    )
                except (discord.Forbidden, discord.NotFound) as error:
                    raise NightPromptUnavailable from error
                lobby.last_prompted_night = number
            await self.send_phase_card(lobby.channel, night_card(number, self.night_seconds))
        except BaseException:
            if game.phase == Phase.OPENING_NIGHT:
                game.phase = Phase.NEXT_NIGHT
            raise
        lobby.night_ready.clear()
        game.begin_night(asyncio.get_running_loop().time(), self.night_seconds)

    @mafia.command()
    @commands.guild_only()
    async def cancel(self, ctx):
        """Host only: close this lobby and invalidate its roles."""
        lobby = self.get_lobby(ctx)
        lobby.game.require_host(ctx.author.id)
        await self.discard_lobby(ctx.channel.id, lobby)
        await ctx.send("Lobby cancelled. Discard its role DMs. Use `!mafia create` to start fresh.")

    @mafia.command(name="check")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.member)
    async def check_setup(self, ctx):
        """Check this channel and your own DMs without creating a game."""
        permissions = ctx.channel.permissions_for(ctx.guild.me)
        lines = ["**Mafia setup check**"]
        for label, allowed in (
            ("View channel", permissions.view_channel),
            ("Send messages", permissions.send_messages),
            ("Embed cards", permissions.embed_links),
        ):
            lines.append(f"{label}: {'available' if allowed else 'missing'}")
        try:
            await asyncio.wait_for(
                ctx.author.send(
                    "DBot DM check passed. No game was created and no role was assigned."
                ),
                self.lifecycle.policy.request_seconds,
            )
            lines.append("Your DMs: delivered successfully")
        except (discord.HTTPException, TimeoutError):
            lines.append("Your DMs: could not deliver; check privacy settings or try again later")
        lines.append(
            "This checks only your account and this channel. Other players must check too."
        )
        report = "\n".join(lines)
        try:
            await asyncio.wait_for(ctx.send(report), self.lifecycle.policy.request_seconds)
        except (discord.HTTPException, TimeoutError):
            try:
                await asyncio.wait_for(
                    ctx.author.send(report), self.lifecycle.policy.request_seconds
                )
            except (discord.HTTPException, TimeoutError):
                logger.warning(
                    "Setup check could not report results in channel %s.", ctx.channel.id
                )

    @mafia.command(ignore_extra=False)
    @commands.guild_only()
    async def transfer(self, ctx, player_number: int):
        """Host: hand hosting to another participant from !mafia lobby."""
        lobby = self.get_lobby(ctx)
        self.require_participating(lobby, ctx.author.id)
        if lobby.game.phase != Phase.LOBBY and lobby.game.phase not in STABLE_PHASES:
            raise GameRuleError("Wait until the current announcement finishes before transferring.")
        players = lobby.game.players
        if (
            1 <= player_number <= len(players)
            and players[player_number - 1] in lobby.pending_forfeits
        ):
            raise GameRuleError("That player is leaving. Choose another host.")
        target = lobby.game.transfer_host(ctx.author.id, player_number)
        self.touch(lobby)
        await ctx.send(f"{lobby.members[target].mention} is now the host.")

    @mafia.command(ignore_extra=False)
    @commands.guild_only()
    async def forfeit(self, ctx):
        """Leave an active match permanently; the game checks for a winner."""
        lobby = self.get_lobby(ctx)
        if lobby.game.phase == Phase.LOBBY:
            raise GameRuleError("Use `!mafia leave` to leave a waiting lobby.")
        if ctx.author.id not in lobby.game.players:
            raise GameRuleError("You are not in this game.")
        self.require_participating(lobby, ctx.author.id)
        if lobby.game.phase not in STABLE_PHASES:
            raise GameRuleError("Wait until the current announcement finishes before forfeiting.")
        lobby.pending_forfeits.add(ctx.author.id)
        lobby.lifecycle_changed.set()
        self.touch(lobby)
        await self.confirm_action(
            ctx, "Your forfeit is recorded. Your departure will be announced."
        )

    @mafia.command(ignore_extra=False)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def forcecancel(self, ctx):
        """Server moderators with Manage Server: stop a stranded game."""
        lobby = self.get_lobby(ctx)
        await self.discard_lobby(ctx.channel.id, lobby)
        await ctx.send("Game stopped by a server moderator. Use `!mafia create` to start fresh.")

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use that Mafia command in the server channel.")
        elif isinstance(error, commands.PrivateMessageOnly):
            await ctx.send("Use `!mafia kill` only in a private DM to this bot.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "Only a server moderator with Manage Server can use `!mafia forcecancel`."
            )
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"Please wait {math.ceil(error.retry_after)} seconds before checking again."
            )
        elif isinstance(error, commands.UserInputError):
            if ctx.command and ctx.command.qualified_name == "mafia kill":
                await ctx.send(
                    "Copy a target command from your role DM, such as `!mafia kill 3`. "
                    "Use only the player number. Your choice is final."
                )
            elif ctx.command and ctx.command.qualified_name == "mafia vote":
                await ctx.send("Use `!mafia vote 3` or `!mafia vote skip` in the game channel.")
            elif ctx.command and ctx.command.qualified_name == "mafia transfer":
                await ctx.send("Use `!mafia transfer 2` with a player number from `!mafia lobby`.")
            else:
                await ctx.send("Check the command format with `!help mafia`.")
        elif isinstance(error, GameRuleError):
            await ctx.send(str(error))
        elif isinstance(error, discord.HTTPException):
            await ctx.send(
                "Discord could not deliver a message. Check `!mafia lobby` and try again."
            )
        else:
            raise error
