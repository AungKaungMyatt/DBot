"""The Mafia rules: ordinary Python, with no Discord connection or timers."""

import random
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

MIN_PLAYERS = 3
MAX_PLAYERS = 6


class Phase(StrEnum):
    LOBBY = "lobby"
    ASSIGNING = "assigning"
    NIGHT = "night"
    RESOLVING = "resolving"
    DISCUSSION = "discussion"
    OPENING_VOTE = "opening_vote"
    VOTING = "voting"
    COUNTING = "counting"
    NEXT_NIGHT = "next_night"
    OPENING_NIGHT = "opening_night"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class Role(StrEnum):
    MAFIA = "Mafia"
    CITIZEN = "Citizen"


class GameRuleError(ValueError):
    """An expected rejection, such as trying to target yourself."""


@dataclass(frozen=True)
class EliminationOutcome:
    victim_id: int | None
    survivors: frozenset[int]
    winner: Role | None

    @property
    def mafia_wins(self) -> bool:
        return self.winner == Role.MAFIA


@dataclass
class GameState:
    host_id: int
    players: list[int] = field(init=False)
    phase: Phase = Phase.LOBBY
    roles: dict[int, Role] = field(default_factory=dict, repr=False)
    alive: set[int] = field(default_factory=set)
    night_target: int | None = field(default=None, repr=False)
    night_deadline: float | None = None
    night_number: int = 0
    discussion_deadline: float | None = None
    voting_deadline: float | None = None
    ready_players: set[int] = field(default_factory=set)
    votes: dict[int, int | None] = field(default_factory=dict)

    def __post_init__(self):
        self.players = [self.host_id]

    def require_host(self, player_id: int) -> None:
        if player_id != self.host_id:
            raise GameRuleError("Only the lobby host can do that.")

    def require_open(self) -> None:
        if self.phase != Phase.LOBBY:
            raise GameRuleError("Roles are being sent or already assigned; this lobby is closed.")

    def join(self, player_id: int) -> None:
        self.require_open()
        if player_id in self.players:
            raise GameRuleError("You are already in this lobby.")
        if len(self.players) >= MAX_PLAYERS:
            raise GameRuleError(f"This lobby is full ({MAX_PLAYERS} players).")
        self.players.append(player_id)

    def leave(self, player_id: int) -> None:
        self.require_open()
        if player_id not in self.players:
            raise GameRuleError("You are not in this lobby.")
        if player_id == self.host_id:
            raise GameRuleError("As host, use `!mafia cancel` to close the lobby.")
        self.players.remove(player_id)

    def begin_assignment(self) -> None:
        self.require_open()
        if len(self.players) < MIN_PLAYERS:
            raise GameRuleError(f"You need at least {MIN_PLAYERS} players, including the host.")
        mafia_id = random.choice(self.players)
        self.roles = {
            player: Role.MAFIA if player == mafia_id else Role.CITIZEN for player in self.players
        }
        self.alive = set(self.players)
        self.phase = Phase.ASSIGNING

    def begin_night(self, now: float, duration: float) -> None:
        if self.phase not in (Phase.ASSIGNING, Phase.OPENING_NIGHT):
            raise GameRuleError("The game is not ready for night.")
        self.night_number += 1
        self.night_target = None
        self.ready_players.clear()
        self.votes.clear()
        self.discussion_deadline = None
        self.voting_deadline = None
        self.night_deadline = now + duration
        self.phase = Phase.NIGHT

    def lock_target(self, actor_id: int, player_number: int, now: float) -> int:
        if actor_id not in self.alive or self.roles.get(actor_id) != Role.MAFIA:
            raise GameRuleError("Only the living Mafia player in this game can choose a target.")
        if self.phase != Phase.NIGHT:
            raise GameRuleError("Target selection is available only during the night.")
        if self.night_deadline is None or now >= self.night_deadline:
            raise GameRuleError("Night has ended. Please wait for the morning announcement.")
        if self.night_target is not None:
            raise GameRuleError("Your choice is already locked in. Please wait for morning.")
        if not 1 <= player_number <= len(self.players):
            raise GameRuleError(
                f"Choose a player number from 1 to {len(self.players)} in your role DM."
            )
        target_id = self.players[player_number - 1]
        if target_id == actor_id:
            raise GameRuleError("You cannot target yourself. Choose another living player.")
        if target_id not in self.alive:
            raise GameRuleError("That player is already eliminated. Choose a living player.")
        self.night_target = target_id
        return target_id

    def elimination_outcome(self, victim_id: int | None) -> EliminationOutcome:
        survivors = self.alive - {victim_id} if victim_id is not None else set(self.alive)
        mafia_count = sum(self.roles[player] == Role.MAFIA for player in survivors)
        citizen_count = len(survivors) - mafia_count
        winner = (
            Role.CITIZEN
            if mafia_count == 0
            else Role.MAFIA
            if mafia_count >= citizen_count
            else None
        )
        return EliminationOutcome(victim_id, frozenset(survivors), winner)

    def prepare_morning(self) -> EliminationOutcome:
        """Calculate the result without changing who is alive yet."""
        if self.phase != Phase.NIGHT:
            raise GameRuleError("The game is not waiting for morning.")
        outcome = self.elimination_outcome(self.night_target)
        self.phase = Phase.RESOLVING
        return outcome

    def complete_morning(self, outcome: EliminationOutcome, now: float, duration: float) -> None:
        """Commit after the public announcement was delivered."""
        if self.phase != Phase.RESOLVING:
            raise GameRuleError("No morning announcement is in progress.")
        self.alive = set(outcome.survivors)
        self.night_target = None
        self.phase = Phase.FINISHED if outcome.winner else Phase.DISCUSSION
        self.ready_players.clear()
        self.discussion_deadline = now + duration if not outcome.winner else None

    def retry_morning(self) -> None:
        if self.phase == Phase.RESOLVING:
            self.phase = Phase.NIGHT

    @property
    def majority(self) -> int:
        return len(self.alive) // 2 + 1

    def require_living(self, player_id: int) -> None:
        if player_id not in self.alive:
            raise GameRuleError("Only living players in this game can do that.")

    def mark_ready(self, player_id: int, now: float) -> bool:
        self.require_living(player_id)
        if self.phase != Phase.DISCUSSION:
            raise GameRuleError("Use `!mafia ready` during discussion.")
        if self.discussion_deadline is None or now >= self.discussion_deadline:
            raise GameRuleError("Discussion has ended. Please wait for voting.")
        if player_id in self.ready_players:
            raise GameRuleError("You are already ready for voting.")
        self.ready_players.add(player_id)
        return len(self.ready_players) >= self.majority

    def prepare_voting(self) -> None:
        if self.phase != Phase.DISCUSSION:
            raise GameRuleError("The game is not in discussion.")
        self.phase = Phase.OPENING_VOTE

    def begin_voting(self, now: float, duration: float) -> None:
        if self.phase != Phase.OPENING_VOTE:
            raise GameRuleError("Voting is not ready to begin.")
        self.votes.clear()
        self.voting_deadline = now + duration
        self.phase = Phase.VOTING

    def cast_vote(self, player_id: int, player_number: int | None, now: float) -> bool:
        self.require_living(player_id)
        if self.phase != Phase.VOTING:
            raise GameRuleError("You can vote only during voting.")
        if self.voting_deadline is None or now >= self.voting_deadline:
            raise GameRuleError("Voting has ended. Please wait for the result.")
        if player_id in self.votes:
            raise GameRuleError("Your vote is already final.")
        target_id = None
        if player_number is not None:
            if not 1 <= player_number <= len(self.players):
                raise GameRuleError(
                    "Choose a player number from the roster, or use `!mafia vote skip`."
                )
            target_id = self.players[player_number - 1]
            if target_id == player_id:
                raise GameRuleError("Choose another player, or use `!mafia vote skip`.")
            if target_id not in self.alive:
                raise GameRuleError("That player is already eliminated.")
        self.votes[player_id] = target_id
        return len(self.votes) == len(self.alive)

    def prepare_vote_result(self) -> EliminationOutcome:
        if self.phase != Phase.VOTING:
            raise GameRuleError("The game is not waiting for votes.")
        counts = Counter(target for target in self.votes.values() if target is not None)
        victim_id = next(
            (target for target, count in counts.items() if count >= self.majority), None
        )
        outcome = self.elimination_outcome(victim_id)
        self.phase = Phase.COUNTING
        return outcome

    def complete_vote_result(self, outcome: EliminationOutcome) -> None:
        if self.phase != Phase.COUNTING:
            raise GameRuleError("No vote result is being announced.")
        self.alive = set(outcome.survivors)
        self.phase = Phase.FINISHED if outcome.winner else Phase.NEXT_NIGHT

    def prepare_next_night(self) -> None:
        if self.phase != Phase.NEXT_NIGHT:
            raise GameRuleError("The game is not ready for another night.")
        self.phase = Phase.OPENING_NIGHT
