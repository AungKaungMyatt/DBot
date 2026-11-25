"""Expiry and confirmed membership checks, separate from the game rules."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from mafia_cards import duration_text
from mafia_game import EliminationOutcome, Phase

if TYPE_CHECKING:
    from mafia import Mafia

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecyclePolicy:
    lobby_seconds: float = 600
    idle_seconds: float = 900
    finished_seconds: float = 300
    check_seconds: float = 30
    request_seconds: float = 5
    delivery_attempts: int = 5

    def __post_init__(self):
        values = (
            self.lobby_seconds,
            self.idle_seconds,
            self.finished_seconds,
            self.check_seconds,
            self.request_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Lifecycle durations must be finite and greater than zero.")
        if not isinstance(self.delivery_attempts, int) or self.delivery_attempts < 1:
            raise ValueError("Delivery attempts must be a positive integer.")


@dataclass(frozen=True)
class DepartureNotice:
    previous_phase: Phase
    outcome: EliminationOutcome
    players: tuple[int, ...]
    old_host: int


class LifecycleService:
    def __init__(self, owner: Mafia, policy: LifecyclePolicy):
        self.owner = owner
        self.policy = policy
        self.task: asyncio.Task | None = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self):
        try:
            while not self.owner.closing:
                await asyncio.sleep(self.policy.check_seconds)
                try:
                    await self.check_once()
                except Exception:
                    logger.exception("Lifecycle maintenance failed; it will retry next interval.")
        except asyncio.CancelledError:
            raise

    async def check_once(self, now: float | None = None):
        now = self.owner.now() if now is None else now
        for channel_id, lobby in list(self.owner.lobbies.items()):
            if self.owner.lobbies.get(channel_id) is not lobby:
                continue
            phase = lobby.game.phase
            if phase == Phase.LOBBY and now >= lobby.lobby_deadline:
                await self.owner.close_game(
                    channel_id,
                    lobby,
                    "Lobby expired",
                    "The lobby was not started in time. Use `!mafia create` to try again.",
                )
            elif phase == Phase.FINISHED:
                if (
                    lobby.finished_at is not None
                    and now - lobby.finished_at >= self.policy.finished_seconds
                ):
                    await self.owner.discard_lobby(channel_id, lobby)
            elif now - lobby.last_activity >= self.policy.idle_seconds:
                await self.owner.close_game(
                    channel_id,
                    lobby,
                    "Game closed — inactive",
                    f"Nobody has taken a game action for {duration_text(self.policy.idle_seconds)}. Create a new lobby when everyone is ready.",
                )
            else:
                await self.check_members(channel_id, lobby)

    async def check_members(self, channel_id, lobby):
        fetch = getattr(lobby.guild, "fetch_member", None)
        if fetch is None:
            return
        departed = set()
        for player in list(lobby.game.players):
            if player in lobby.game.forfeited:
                continue
            try:
                await asyncio.wait_for(fetch(player), timeout=self.policy.request_seconds)
            except discord.NotFound as error:
                if error.code == 10004:
                    await self.owner.close_game(
                        channel_id, lobby, "Game closed", "I can no longer access this server."
                    )
                    return
                departed.add(player)
            except discord.Forbidden:
                await self.owner.close_game(
                    channel_id,
                    lobby,
                    "Game closed",
                    "I can no longer verify access to this server.",
                )
                return
            except (discord.HTTPException, TimeoutError):
                logger.warning(
                    "Membership lookup failed in channel %s; unverified players were kept.",
                    channel_id,
                )
                break
            if self.owner.lobbies.get(channel_id) is not lobby:
                return
        if self.owner.lobbies.get(channel_id) is not lobby or not departed:
            return
        if lobby.game.phase == Phase.LOBBY:
            await self.owner.remove_waiting_players(channel_id, lobby, departed)
        elif lobby.game.phase not in (Phase.FINISHED, Phase.CANCELLED):
            lobby.pending_forfeits.update(departed)
            lobby.lifecycle_changed.set()
