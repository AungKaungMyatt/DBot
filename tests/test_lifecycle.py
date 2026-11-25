"""Lifecycle failures exercised with simulated Discord players and clocks."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from mafia import Mafia
from mafia_game import GameRuleError
from mafia_lifecycle import LifecyclePolicy
from tests.fakes import DiscordGameFixture, message_text


class LifecycleTests(DiscordGameFixture, unittest.IsolatedAsyncioTestCase):
    async def until(self, condition):
        loop = asyncio.get_running_loop()
        reached = loop.create_future()
        handle = None

        def observe():
            nonlocal handle
            if reached.done():
                return
            try:
                ready = condition()
            except Exception as error:
                reached.set_exception(error)
                return
            if ready:
                reached.set_result(None)
            else:
                handle = loop.call_later(0.001, observe)

        observe()
        try:
            await asyncio.wait_for(reached, timeout=5)
        finally:
            if handle is not None:
                handle.cancel()

    def missing_member(self, code=10007):
        return discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": code, "message": "Missing member"},
        )

    async def test_shutdown_cancels_worker_notifying_host_after_lobby_removal(self):
        lobby = await self.night_game()
        entered = asyncio.Event()

        async def blocked_notice(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.members[1].send.side_effect = blocked_notice
        self.channels[100].send.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Channel deleted"
        )
        await self.choose(2)
        await asyncio.wait_for(entered.wait(), 5)
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIn(lobby.game_task, self.cog.worker_tasks)
        await self.cog.cog_unload()
        self.assertTrue(lobby.game_task.cancelled())
        self.assertFalse(self.cog.worker_tasks)

    async def test_role_delivery_timeout_releases_players_without_starting_worker(self):
        self.cog.lifecycle.policy = LifecyclePolicy(request_seconds=0.02)
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]

        async def blocked_role(*args, **kwargs):
            await asyncio.Event().wait()

        self.members[1].send.side_effect = blocked_role
        await asyncio.wait_for(self.invoke("start"), 5)
        self.assertFalse(self.cog.lobbies)
        self.assertIsNone(lobby.game_task)
        self.assertFalse(self.cog.worker_tasks)
        self.members[2].send.assert_not_awaited()

    async def test_waiting_lobby_expires_and_players_can_join_again(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        await self.cog.lifecycle.check_once(now=lobby.lobby_deadline - 1)
        self.assertIn(100, self.cog.lobbies)
        await self.cog.lifecycle.check_once(now=lobby.lobby_deadline)
        self.assertNotIn(100, self.cog.lobbies)
        await self.invoke("create")

    async def test_expired_lobby_rejects_late_join_and_can_be_recreated_immediately(self):
        await self.invoke("create")
        self.cog.lobbies[100].lobby_deadline = 0
        with self.assertRaisesRegex(GameRuleError, "expired"):
            await self.invoke("join", 2)
        await self.invoke("create")
        old = self.cog.lobbies[100]
        old.lobby_deadline = 0
        await self.invoke("create")
        self.assertIsNot(self.cog.lobbies[100], old)

    async def test_last_waiting_player_leaving_closes_lobby(self):
        await self.invoke("create")
        await self.invoke("leave")
        self.assertFalse(self.cog.lobbies)
        await self.invoke("create", 2)

    async def test_idle_game_is_closed_but_invalid_commands_do_not_extend_it(self):
        lobby = await self.night_game()
        last_action = lobby.last_activity
        with self.assertRaises(GameRuleError):
            await self.invoke("ready")
        self.assertEqual(lobby.last_activity, last_action)
        await self.cog.lifecycle.check_once(
            now=last_action + self.cog.lifecycle.policy.idle_seconds - 1
        )
        self.assertIn(100, self.cog.lobbies)
        await self.cog.lifecycle.check_once(
            now=last_action + self.cog.lifecycle.policy.idle_seconds
        )
        self.assertFalse(self.cog.lobbies)
        self.assertTrue(lobby.game_task.cancelled())

    async def test_fractional_clock_expires_at_exact_idle_deadline(self):
        lobby = await self.night_game()
        lobby.last_activity = 126.1364164
        deadline = lobby.last_activity + self.cog.lifecycle.policy.idle_seconds
        await self.cog.lifecycle.check_once(now=deadline - 0.000001)
        self.assertIn(100, self.cog.lobbies)
        await self.cog.lifecycle.check_once(now=deadline)
        self.assertFalse(self.cog.lobbies)

    async def test_finished_game_is_released_after_retention_time(self):
        lobby = await self.night_game(count=3)
        await self.choose(2)
        await self.wait_phase(lobby, "finished")
        await lobby.game_task
        self.assertIsNotNone(lobby.finished_at)
        await self.cog.lifecycle.check_once(
            now=lobby.finished_at + self.cog.lifecycle.policy.finished_seconds
        )
        self.assertFalse(self.cog.lobbies)

    async def test_host_can_transfer_and_only_new_host_can_cancel(self):
        await self.ready_lobby()
        await self.cog.transfer.callback(self.cog, self.context(), 2)
        self.assertEqual(self.cog.lobbies[100].game.host_id, 2)
        with self.assertRaises(GameRuleError):
            await self.invoke("cancel", 1)
        await self.invoke("start", 2)
        await self.invoke("cancel", 2)
        self.assertFalse(self.cog.lobbies)

    async def test_transfer_rejects_wrong_host_invalid_numbers_and_self(self):
        await self.ready_lobby()
        for actor, number in ((2, 3), (1, 0), (1, 4), (1, 1)):
            with self.assertRaises(GameRuleError):
                await self.cog.transfer.callback(self.cog, self.context(actor), number)
        self.assertEqual(self.cog.lobbies[100].game.host_id, 1)

    async def test_cancel_during_assignment_cannot_start_an_orphan_worker(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()

        self.members[1].send.side_effect = blocked
        task = asyncio.create_task(self.invoke("start"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await self.invoke("cancel")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(lobby.game_task)
        self.assertFalse(self.cog.lobbies)
        self.members[2].send.assert_not_awaited()

    async def test_cancel_during_morning_delivery_does_not_commit_a_kill(self):
        lobby = await self.night_game()
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(content=None, *, embed=None):
            if embed is not None:
                entered.set()
                await release.wait()

        self.channels[100].send.side_effect = blocked
        await self.choose(5)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await self.invoke("cancel")
        finally:
            release.set()
        self.assertTrue(lobby.game_task.cancelled())
        self.assertIn(5, lobby.game.alive)
        self.assertFalse(self.cog.lobbies)

    async def test_moderator_override_checks_guild_permission_through_command_framework(self):
        await self.night_game()
        self.members[14].guild_permissions = discord.Permissions.none()
        async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as client:
            client._connection.user = SimpleNamespace(id=99999)
            await client.add_cog(self.cog)

            async def invoke_override():
                message = SimpleNamespace(
                    content="!mafia forcecancel",
                    author=self.members[14],
                    guild=SimpleNamespace(id=50),
                    channel=self.channels[100],
                    _state=client._connection,
                    attachments=[],
                )
                ctx = await client.get_context(message)
                ctx.send = AsyncMock()
                await client.invoke(ctx)
                return ctx

            ctx = await invoke_override()
            self.assertIn("Manage Server", message_text(ctx.send.await_args))
            self.assertIn(100, self.cog.lobbies)
            self.members[14].guild_permissions.manage_guild = True
            await invoke_override()
            self.assertFalse(self.cog.lobbies)

    async def test_citizen_host_forfeits_transfers_host_and_can_join_another_game(self):
        lobby = await self.night_game(mafia_player=3)
        await self.invoke("forfeit", 1)
        await self.until(lambda: 1 in lobby.game.forfeited and lobby.game.phase == "night")
        self.assertEqual(lobby.game.host_id, 2)
        self.assertEqual(lobby.game.players, [1, 2, 3, 4, 5])
        self.assertNotIn(1, lobby.game.alive)
        await self.invoke("create", 1, 200)
        await self.invoke("cancel", 2)
        self.assertIn(200, self.cog.lobbies)

    async def test_mafia_forfeit_gives_citizens_the_win(self):
        lobby = await self.night_game()
        await self.invoke("forfeit", 1)
        with self.assertRaises(GameRuleError):
            await self.choose(2)
        await self.wait_phase(lobby, "finished")
        self.assertIn("Citizens win", message_text(self.channels[100].send.await_args))
        self.assertIsNone(self.cog.active_game(1))

    async def test_departure_invalidates_night_target_without_a_second_elimination(self):
        lobby = await self.night_game()
        await self.invoke("forfeit", 5)
        await self.choose(5)
        await self.until(lambda: 5 in lobby.game.forfeited and lobby.game.phase == "night")
        self.assertIsNone(lobby.game.night_target)
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4})
        await self.choose(4)
        await self.finish(lobby)
        self.assertEqual(lobby.game.alive, {1, 2, 3})

    async def test_departure_recalculates_ready_majority(self):
        lobby = await self.night_game()
        await self.choose(5)
        await self.finish(lobby)
        await self.invoke("ready", 1)
        await self.invoke("ready", 2)
        await self.invoke("forfeit", 4)
        await self.wait_phase(lobby, "voting")
        self.assertEqual(lobby.game.majority, 2)

    async def test_votes_for_departed_player_are_cleared_and_can_be_recast(self):
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        await self.cast(1, 5)
        await self.cast(2, 5)
        await self.invoke("forfeit", 5)
        await self.until(lambda: 5 in lobby.game.forfeited and lobby.game.phase == "voting")
        self.assertFalse(lobby.game.votes)
        await self.cast(1, "skip")
        await self.cast(2, "skip")
        await self.cast(3, 1)
        await self.cast(4, 1)
        await self.wait_phase(lobby, "night", night=2)
        self.assertIn(1, lobby.game.alive)

    async def test_everyone_leaving_closes_without_a_false_team_win(self):
        lobby = await self.night_game()
        lobby.pending_forfeits.update(lobby.game.players)
        lobby.lifecycle_changed.set()
        await self.until(lambda: 100 not in self.cog.lobbies)
        self.assertNotIn("Citizens win", message_text(self.channels[100].send.await_args))
        self.assertNotIn("Mafia wins", message_text(self.channels[100].send.await_args))

    async def test_confirmed_server_departure_is_forfeit_but_offline_status_is_not(self):
        lobby = await self.night_game()

        async def fetch(player):
            if player == 5:
                raise self.missing_member()
            return SimpleNamespace(id=player, status="offline")

        lobby.guild.fetch_member = AsyncMock(side_effect=fetch)
        await self.cog.lifecycle.check_once()
        await self.until(lambda: 5 in lobby.game.forfeited and lobby.game.phase == "night")
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4})

    async def test_member_lookup_failure_does_not_treat_players_as_departed(self):
        lobby = await self.night_game()
        lobby.guild.fetch_member = AsyncMock(side_effect=self.delivery_error())
        with self.assertLogs("mafia_lifecycle", level="WARNING"):
            await self.cog.lifecycle.check_once()
        self.assertFalse(lobby.pending_forfeits)
        self.assertEqual(len(lobby.game.alive), 5)

    async def test_unknown_guild_and_missing_access_close_without_false_forfeits(self):
        lobby = await self.night_game()
        lobby.guild.fetch_member = AsyncMock(side_effect=self.missing_member(10004))
        await self.cog.lifecycle.check_once()
        self.assertFalse(self.cog.lobbies)
        self.assertFalse(lobby.game.forfeited)

    async def test_stale_membership_result_cannot_remove_replacement_lobby(self):
        lobby = await self.night_game()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(player):
            entered.set()
            await release.wait()
            return self.members[player]

        lobby.guild.fetch_member = AsyncMock(side_effect=fetch)
        task = asyncio.create_task(self.cog.lifecycle.check_once())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await self.invoke("cancel")
            await self.invoke("create")
            replacement = self.cog.lobbies[100]
        finally:
            release.set()
            await task
        self.assertIs(self.cog.lobbies[100], replacement)
        self.assertFalse(replacement.pending_forfeits)

    async def test_background_expiry_monitor_runs_and_stops_with_cog(self):
        self.cog = Mafia(lifecycle_policy=LifecyclePolicy(lobby_seconds=0.02, check_seconds=0.01))
        await self.cog.cog_load()
        await self.invoke("create")
        await self.until(lambda: not self.cog.lobbies)
        self.assertFalse(self.cog.lifecycle.task.done())
        await self.cog.cog_unload()
        self.assertTrue(self.cog.lifecycle.task.cancelled())

    async def test_repeated_message_failures_are_bounded_and_release_players(self):
        lobby = await self.night_game()
        self.channels[100].send.side_effect = self.delivery_error()
        with patch("mafia.MESSAGE_RETRY_SECONDS", 0), self.assertLogs("mafia", level="WARNING"):
            await self.choose(5)
            await self.until(lambda: not self.cog.lobbies)
        self.assertEqual(
            self.channels[100].send.await_count, self.cog.lifecycle.policy.delivery_attempts + 1
        )
        self.assertEqual(lobby.game.phase, "cancelled")

    async def test_transient_night_dm_failure_is_retried_before_marking_it_delivered(self):
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        self.members[1].send.side_effect = [self.delivery_error(), None]
        with patch("mafia.MESSAGE_RETRY_SECONDS", 0), self.assertLogs("mafia", level="WARNING"):
            for actor in sorted(lobby.game.alive):
                await self.cast(actor, "skip")
            await self.wait_phase(lobby, "night", night=2)
        self.assertEqual(lobby.last_prompted_night, 2)
        self.assertEqual(self.members[1].send.await_count, 3)

    async def test_blocked_night_dm_stops_game_without_revealing_mafia(self):
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        self.members[1].send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "DM blocked"
        )
        for actor in sorted(lobby.game.alive):
            await self.cast(actor, "skip")
        await self.until(lambda: not self.cog.lobbies)
        self.assertNotIn("<@1>", message_text(self.channels[100].send.await_args))

    def test_lifecycle_settings_are_validated(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                LifecyclePolicy(lobby_seconds=value)
        for value in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                LifecyclePolicy(delivery_attempts=value)

    async def test_three_peaceful_rounds_end_in_a_public_draw(self):
        lobby = await self.night_game()
        for number in range(1, 4):
            lobby.night_ready.set()
            await self.until(
                lambda number=number: (
                    lobby.game.phase == "discussion" and lobby.game.night_number == number
                )
            )
            await self.skip_to_voting(lobby)
            for player in sorted(lobby.game.alive):
                await self.cast(player, "skip")
            if number < 3:
                await self.wait_phase(lobby, "night", night=number + 1)
        await self.wait_phase(lobby, "finished")
        self.assertIn("Draw", message_text(self.channels[100].send.await_args))
        self.assertNotIn("Citizens win", message_text(self.channels[100].send.await_args))


if __name__ == "__main__":
    unittest.main()
