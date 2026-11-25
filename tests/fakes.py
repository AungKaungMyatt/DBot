"""Reusable fake Discord players, channels, and asynchronous test helpers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from mafia import Mafia


def message_text(call):
    """Read either a plain reply or a card without coupling game tests to layout."""
    parts = [str(call.args[0])] if call.args else []
    card = call.kwargs.get("embed")
    if card is not None:
        parts.extend([card.title or "", card.description or ""])
        for field in card.fields:
            parts.extend([field.name, field.value])
        parts.append(card.footer.text or "")
    return "\n".join(parts)


class DiscordGameFixture:
    def setUp(self):
        self.cog = Mafia()
        self.members = {
            i: SimpleNamespace(
                id=i, display_name=f"Player {i}", mention=f"<@{i}>", send=AsyncMock()
            )
            for i in range(1, 15)
        }
        self.channels = {}

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def context(self, player=1, channel=100, private=False):
        if channel not in self.channels:
            self.channels[channel] = SimpleNamespace(
                id=channel, mention=f"<#{channel}>", send=AsyncMock()
            )
        return SimpleNamespace(
            author=self.members[player],
            channel=self.channels[channel],
            guild=None if private else SimpleNamespace(name="Test server"),
            message=SimpleNamespace(id=900 + channel),
            send=AsyncMock() if private else self.channels[channel].send,
        )

    async def invoke(self, command, player=1, channel=100):
        ctx = self.context(player, channel)
        await getattr(self.cog, command).callback(self.cog, ctx)
        return ctx

    async def ready_lobby(self, count=3, channel=100, host=1):
        await self.invoke("create", host, channel)
        for player in range(host + 1, host + count):
            await self.invoke("join", player, channel)

    async def night_game(self, count=5, channel=100, host=1, mafia_player=None):
        await self.ready_lobby(count, channel, host)
        with patch(
            "mafia_game.random.choice", return_value=host if mafia_player is None else mafia_player
        ):
            await self.invoke("start", host, channel)
        self.channels[channel].send.reset_mock()
        return self.cog.lobbies[channel]

    async def choose(self, target=2, player=1, send=None):
        ctx = self.context(player, channel=999, private=True)
        if send is not None:
            ctx.send = send
        await self.cog.kill.callback(self.cog, ctx, target)
        return ctx

    async def finish(self, lobby):
        async def transitioned():
            while lobby.game.phase not in ("discussion", "finished", "cancelled"):
                if lobby.game_task.done():
                    raise AssertionError("Game worker stopped before the phase completed")
                await asyncio.sleep(0)

        await asyncio.wait_for(transitioned(), timeout=3)

    def delivery_error(self):
        return discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Try again")

    async def wait_phase(self, lobby, phase, night=None):
        async def reached():
            while lobby.game.phase != phase or (
                night is not None and lobby.game.night_number != night
            ):
                if lobby.game_task.done():
                    raise AssertionError(f"Worker stopped at {lobby.game.phase}, expected {phase}")
                await asyncio.sleep(0)

        await asyncio.wait_for(reached(), timeout=5)

    async def skip_to_voting(self, lobby):
        for player in sorted(lobby.game.alive)[: lobby.game.majority]:
            await self.invoke("ready", player)
        await self.wait_phase(lobby, "voting")

    async def cast(self, player, selection):
        ctx = self.context(player)
        await self.cog.vote.callback(self.cog, ctx, str(selection))
        return ctx
