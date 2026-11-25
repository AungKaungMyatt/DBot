"""Discord commands and timer lifecycle; game rules live in mafia_game.py."""

import asyncio
import logging
import math
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from mafia_cards import morning_card, night_card, send_card, vote_result_card, voting_card
from mafia_game import MAX_PLAYERS, MIN_PLAYERS, GameRuleError, GameState, Phase, Role

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
}


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
    last_prompted_night: int = 1


class Mafia(commands.Cog):
    def __init__(
        self,
        night_seconds=NIGHT_SECONDS,
        discussion_seconds=DISCUSSION_SECONDS,
        voting_seconds=VOTING_SECONDS,
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

    async def cog_unload(self):
        """Release all games and cancel their workers when the bot shuts down."""
        self.closing = True
        lobbies = list(self.lobbies.values())
        self.lobbies.clear()
        tasks = []
        for lobby in lobbies:
            lobby.game.phase = Phase.CANCELLED
            if lobby.game_task is not None:
                lobby.game_task.cancel()
                tasks.append(lobby.game_task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def discard_lobby(self, channel_id: int, lobby: Lobby):
        """Identity checks prevent an old task from deleting a replacement game."""
        if self.lobbies.get(channel_id) is not lobby:
            return
        del self.lobbies[channel_id]
        lobby.game.phase = Phase.CANCELLED
        task = lobby.game_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def abort_game(self, channel_id: int, lobby: Lobby, reason: str):
        await self.discard_lobby(channel_id, lobby)
        try:
            await lobby.members[lobby.game.host_id].send(reason)
        except discord.HTTPException:
            logger.warning("Could not notify the host of stopped game in channel %s.", channel_id)

    def active_game(self, player_id: int) -> Lobby | None:
        return next(
            (
                lobby
                for lobby in self.lobbies.values()
                if lobby.game.phase not in (Phase.FINISHED, Phase.CANCELLED)
                and player_id in lobby.game.players
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
            "`!mafia start` - host sends roles and begins Night 1\n"
            "`!mafia kill <player-number>` - Mafia's final choice, in DMs only\n"
            "`!mafia ready` - skip discussion once a majority is ready\n"
            "`!mafia vote <player-number>` / `!mafia vote skip` - cast a final vote\n"
            "`!mafia cancel` - host clears the lobby\n"
            "Rounds repeat automatically until a team wins.\n"
            "The Mafia's DM includes commands to copy for each target."
        )

    @mafia.command()
    @commands.guild_only()
    async def create(self, ctx):
        """Create a lobby in this server channel and join as host."""
        if ctx.channel.id in self.lobbies:
            raise GameRuleError("This channel already has a lobby. Use `!mafia lobby`.")
        self.require_available(ctx.author.id)
        self.lobbies[ctx.channel.id] = Lobby(
            game=GameState(ctx.author.id), channel=ctx.channel, members={ctx.author.id: ctx.author}
        )
        await ctx.send(
            f"**Mafia lobby open**\nHost: {ctx.author.mention} • 1/{MAX_PLAYERS} players\n"
            "Join: `!mafia join`\n"
            f"Host: use `!mafia start` with {MIN_PLAYERS}–{MAX_PLAYERS} players.\n"
            "Make sure I can send you DMs."
        )

    @mafia.command()
    @commands.guild_only()
    async def join(self, ctx):
        """Join the channel's open lobby."""
        lobby = self.get_lobby(ctx)
        self.require_available(ctx.author.id)
        lobby.game.join(ctx.author.id)
        lobby.members[ctx.author.id] = ctx.author
        await ctx.send(
            f"{ctx.author.mention} joined! Players: {len(lobby.game.players)}/{MAX_PLAYERS}."
        )

    @mafia.command()
    @commands.guild_only()
    async def leave(self, ctx):
        """Leave an open lobby. The host must cancel instead."""
        lobby = self.get_lobby(ctx)
        lobby.game.leave(ctx.author.id)
        del lobby.members[ctx.author.id]
        await ctx.send(
            f"{ctx.author.mention} left. Players: {len(lobby.game.players)}/{MAX_PLAYERS}."
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
        completed = False
        try:
            for player_id, member in lobby.members.items():
                if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                    return
                await member.send(self.role_message(ctx, lobby, player_id))
            if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                return
            await send_card(ctx, night_card(1, self.night_seconds))
            if self.closing or self.lobbies.get(ctx.channel.id) is not lobby:
                return
            lobby.game.begin_night(asyncio.get_running_loop().time(), self.night_seconds)
            lobby.game_task = asyncio.create_task(self.run_game(ctx.channel.id, lobby))
            completed = True
        except discord.HTTPException:
            await ctx.send(
                "**Game cancelled** — I couldn't deliver all the messages.\n"
                "Ignore any roles I just sent. Allow DMs from this bot, "
                "then use `!mafia create` to try again."
            )
        finally:
            if not completed:
                await self.discard_lobby(ctx.channel.id, lobby)

    @mafia.command(ignore_extra=False)
    @commands.dm_only()
    async def kill(self, ctx, player_number: int):
        """DM only: !mafia kill <player-number>. Your choice is final."""
        lobby = self.active_game(ctx.author.id)
        if lobby is None:
            raise GameRuleError("You are not in an active game. Join a lobby in your server first.")
        target_id = lobby.game.lock_target(
            ctx.author.id, player_number, asyncio.get_running_loop().time()
        )
        lobby.night_ready.set()
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
        if lobby.game.mark_ready(ctx.author.id, asyncio.get_running_loop().time()):
            lobby.discussion_ready.set()
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
        if selection.lower() == "skip":
            number = None
        else:
            try:
                number = int(selection)
            except ValueError:
                raise GameRuleError("Use `!mafia vote 3` or `!mafia vote skip`.") from None
        if lobby.game.cast_vote(ctx.author.id, number, asyncio.get_running_loop().time()):
            lobby.votes_ready.set()
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

    async def wait_for_phase(self, event: asyncio.Event, deadline: float):
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        try:
            await asyncio.wait_for(event.wait(), timeout=remaining)
        except TimeoutError:
            pass

    async def run_game(self, channel_id: int, lobby: Lobby):
        """One worker owns every timed phase, so rounds cannot overlap."""
        try:
            while self.lobbies.get(channel_id) is lobby and lobby.game.phase != Phase.FINISHED:
                try:
                    game = lobby.game
                    if game.phase == Phase.NIGHT:
                        await self.wait_for_phase(lobby.night_ready, game.night_deadline)
                        await self.resolve_night(channel_id, lobby)
                    elif game.phase == Phase.DISCUSSION:
                        await self.wait_for_phase(lobby.discussion_ready, game.discussion_deadline)
                        await self.open_voting(lobby)
                    elif game.phase == Phase.VOTING:
                        await self.wait_for_phase(lobby.votes_ready, game.voting_deadline)
                        await self.resolve_votes(lobby)
                    elif game.phase == Phase.NEXT_NIGHT:
                        await self.open_night(lobby)
                    else:
                        raise RuntimeError(f"Unexpected worker phase: {game.phase}")
                except (discord.Forbidden, discord.NotFound):
                    logger.warning("Game channel %s is unavailable; stopping the game.", channel_id)
                    await self.abort_game(
                        channel_id,
                        lobby,
                        "Your Mafia game was cancelled because I can't post in its channel. "
                        "Check the channel and my permissions, then create a new lobby.",
                    )
                except discord.HTTPException as error:
                    if 400 <= error.status < 500 and error.status != 429:
                        logger.error(
                            "Discord rejected a game message in channel %s (HTTP %s).",
                            channel_id,
                            error.status,
                        )
                        await self.abort_game(
                            channel_id,
                            lobby,
                            "Your Mafia game stopped because Discord rejected an announcement. "
                            "Check the bot's terminal, then create a new lobby.",
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

    async def resolve_night(self, channel_id: int, lobby: Lobby):
        if self.lobbies.get(channel_id) is not lobby or lobby.game.phase != Phase.NIGHT:
            return
        outcome = lobby.game.prepare_morning()
        try:
            await send_card(
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
            await send_card(lobby.channel, voting_card(game, lobby.members, self.voting_seconds))
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
            await send_card(lobby.channel, vote_result_card(game, lobby.members, outcome))
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
                lobby.last_prompted_night = number
                mafia_id = next(player for player in game.alive if game.roles[player] == Role.MAFIA)
                try:
                    await lobby.members[mafia_id].send(
                        f"🌙 **Night {number}** • {lobby.channel.mention}\n"
                        f"{self.target_commands(lobby, mafia_id)}\n"
                        "Your choice is final. Wait for the night announcement, then send a command here."
                    )
                except discord.HTTPException:
                    logger.warning("Could not deliver a new night prompt by DM.")
            await send_card(lobby.channel, night_card(number, self.night_seconds))
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
        if lobby.game.phase in (
            Phase.ASSIGNING,
            Phase.RESOLVING,
            Phase.OPENING_VOTE,
            Phase.COUNTING,
            Phase.OPENING_NIGHT,
        ):
            raise GameRuleError("Please wait until message delivery finishes, then cancel.")
        await self.discard_lobby(ctx.channel.id, lobby)
        await ctx.send("Lobby cancelled. Discard its role DMs. Use `!mafia create` to start fresh.")

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use that Mafia command in the server channel.")
        elif isinstance(error, commands.PrivateMessageOnly):
            await ctx.send("Use `!mafia kill` only in a private DM to this bot.")
        elif isinstance(error, commands.UserInputError):
            if ctx.command and ctx.command.qualified_name == "mafia kill":
                await ctx.send(
                    "Copy a target command from your role DM, such as `!mafia kill 3`. "
                    "Use only the player number. Your choice is final."
                )
            elif ctx.command and ctx.command.qualified_name == "mafia vote":
                await ctx.send("Use `!mafia vote 3` or `!mafia vote skip` in the game channel.")
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
