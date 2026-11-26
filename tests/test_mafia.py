"""Check lobby rules, private commands, and timers without connecting to Discord."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from mafia import Mafia
from mafia_game import GameRuleError
from tests.fakes import DiscordGameFixture, message_text


class MafiaTests(DiscordGameFixture, unittest.IsolatedAsyncioTestCase):
    async def test_create_joins_host_and_rejects_duplicate(self):
        await self.invoke("create")
        self.assertEqual(list(self.cog.lobbies[100].game.players), [1])
        with self.assertRaises(GameRuleError):
            await self.invoke("create", 2)

    async def test_join_requires_lobby_and_prevents_duplicates_and_overflow(self):
        with self.assertRaises(GameRuleError):
            await self.invoke("join", 2)
        await self.invoke("create")
        with self.assertRaises(GameRuleError):
            await self.invoke("join")
        for player in range(2, 7):
            await self.invoke("join", player)
        with self.assertRaises(GameRuleError):
            await self.invoke("join", 7)

    async def test_leave_transfers_host_to_next_waiting_player(self):
        await self.ready_lobby()
        await self.invoke("leave", 2)
        self.assertNotIn(2, self.cog.lobbies[100].game.players)
        with self.assertRaises(GameRuleError):
            await self.invoke("leave", 2)
        await self.invoke("leave", 1)
        self.assertEqual(self.cog.lobbies[100].game.host_id, 3)

    async def test_start_requires_host_and_three_players(self):
        await self.ready_lobby(count=2)
        for player in (1, 2):
            with self.assertRaises(GameRuleError):
                await self.invoke("start", player)
        self.assertEqual(self.cog.lobbies[100].game.phase, "lobby")
        self.members[1].send.assert_not_awaited()

    async def test_one_game_per_player_includes_open_lobbies(self):
        await self.ready_lobby()
        await self.invoke("create", 6, 200)
        with self.assertRaises(GameRuleError):
            await self.invoke("join", 2, 200)
        with self.assertRaises(GameRuleError):
            await self.invoke("create", 2, 300)
        await self.invoke("leave", 2)
        await self.invoke("join", 2, 200)
        await self.invoke("cancel")
        await self.invoke("create", 1, 300)

    async def test_simultaneous_joins_only_allow_one_game(self):
        await self.invoke("create")
        await self.invoke("create", 6, 200)
        results = await asyncio.gather(
            self.invoke("join", 2), self.invoke("join", 2, 200), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(result, GameRuleError) for result in results), 1)
        self.assertEqual(sum(2 in lobby.game.players for lobby in self.cog.lobbies.values()), 1)

    async def test_private_roles_and_short_target_commands(self):
        lobby = await self.night_game(count=6)
        self.assertEqual(lobby.game.phase, "night")
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4, 5, 6})
        self.assertEqual(list(lobby.game.roles.values()).count("Mafia"), 1)
        self.assertEqual(list(lobby.game.roles.values()).count("Citizen"), 5)
        for player, role in lobby.game.roles.items():
            self.members[player].send.assert_awaited_once()
            text = message_text(self.members[player].send.await_args)
            self.assertIn(f"Your secret role is **{role}**", text)
            self.assertNotIn("Game ID", text)
        mafia_dm = message_text(self.members[1].send.await_args)
        self.assertIn("Player 2 — `!mafia kill 2`", mafia_dm)
        self.assertNotIn("`!mafia kill 1`", mafia_dm)
        self.assertIn("Your choice is final", mafia_dm)
        status = await self.invoke("show_lobby")
        self.assertNotIn("Citizen", message_text(status.send.await_args))
        self.assertNotIn("target", message_text(status.send.await_args))

    async def test_failed_role_delivery_clears_memberships_and_starts_no_timer(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        self.members[2].send.side_effect = self.delivery_error()
        ctx = await self.invoke("start")
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIsNone(lobby.game_task)
        self.assertIn("cancelled", message_text(ctx.send.await_args))
        self.members[3].send.assert_not_awaited()
        await self.invoke("create", 1, 200)

    async def test_failed_start_announcement_also_clears_lobby(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        ctx = self.context()
        ctx.send.side_effect = self.delivery_error()
        with self.assertLogs("mafia", level="WARNING"):
            await self.cog.start.callback(self.cog, ctx)
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIsNone(lobby.game_task)

    async def test_no_timer_or_membership_changes_until_night_announcement_finishes(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        sending = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            sending.set()
            await release.wait()

        self.channels[100].send.side_effect = delayed_send
        task = asyncio.create_task(self.invoke("start"))
        try:
            await asyncio.wait_for(sending.wait(), timeout=3)
            self.assertEqual(lobby.game.phase, "assigning")
            self.assertIsNone(lobby.game.night_deadline)
            self.assertIsNone(lobby.game_task)
            for command, player in (("join", 4), ("leave", 2), ("start", 1)):
                with self.assertRaises(GameRuleError):
                    await self.invoke(command, player)
        finally:
            release.set()
            await task
        self.assertEqual(lobby.game.phase, "night")
        self.assertIsNotNone(lobby.game_task)
        self.assertGreater(lobby.game.night_deadline, asyncio.get_running_loop().time())

    async def test_choice_is_final_and_automatically_announces_morning_once(self):
        lobby = await self.night_game()
        ctx = await self.choose(3)
        self.assertIn("Choice locked", message_text(ctx.send.await_args))
        with self.assertRaises(GameRuleError):
            await self.choose(4)
        await self.finish(lobby)
        self.assertEqual(lobby.game.alive, {1, 2, 4, 5})
        self.assertEqual(lobby.game.phase, "discussion")
        self.assertIsNone(lobby.game.night_target)
        self.channels[100].send.assert_awaited_once()
        text = message_text(self.channels[100].send.await_args)
        self.assertIn("<@3> was eliminated", text)
        self.assertNotIn("<@1>", text)
        status = await self.invoke("show_lobby")
        self.assertIn("3. <@3> (eliminated)", message_text(status.send.await_args))

    async def test_timeout_without_choice_kills_nobody(self):
        self.cog.night_seconds = 0.01
        lobby = await self.night_game(count=3)
        await self.finish(lobby)
        self.assertEqual(lobby.game.alive, {1, 2, 3})
        self.assertEqual(lobby.game.phase, "discussion")
        self.assertIn("Nobody was eliminated", message_text(self.channels[100].send.await_args))
        self.channels[100].send.assert_awaited_once()

    async def test_choice_after_deadline_is_rejected_even_if_timer_has_not_run(self):
        lobby = await self.night_game()
        lobby.game.night_deadline = asyncio.get_running_loop().time() - 1
        with self.assertRaises(GameRuleError):
            await self.choose()
        await self.finish(lobby)
        self.assertEqual(len(lobby.game.alive), 5)
        self.channels[100].send.assert_awaited_once()

    async def test_choice_and_deadline_together_resolve_once(self):
        lobby = await self.night_game()
        await self.choose(3)
        lobby.game.night_deadline = asyncio.get_running_loop().time() - 1
        await self.finish(lobby)
        self.assertNotIn(3, lobby.game.alive)
        self.channels[100].send.assert_awaited_once()

    async def test_citizens_outsiders_and_dead_mafia_cannot_select(self):
        lobby = await self.night_game()
        for player in (2, 8):
            with self.assertRaises(GameRuleError):
                await self.choose(player=player)
        lobby.game.alive.remove(1)
        with self.assertRaises(GameRuleError):
            await self.choose()
        self.assertIsNone(lobby.game.night_target)

    async def test_invalid_self_and_eliminated_targets_are_rejected(self):
        lobby = await self.night_game()
        lobby.game.alive.remove(3)
        for target in (-1, 0, 6, 1, 3):
            with self.assertRaises(GameRuleError):
                await self.choose(target)
        self.assertIsNone(lobby.game.night_target)

    async def test_channels_with_different_players_resolve_independently(self):
        first = await self.night_game()
        second = await self.night_game(count=3, channel=200, host=6)
        await self.choose(2, player=6)
        await self.finish(second)
        self.assertEqual(first.game.phase, "night")
        self.assertEqual(len(first.game.alive), 5)
        self.assertNotIn(7, second.game.alive)
        self.channels[100].send.assert_not_awaited()

    async def test_three_players_end_in_mafia_victory_after_kill(self):
        lobby = await self.night_game(count=3)
        await self.choose()
        await self.finish(lobby)
        self.assertEqual(lobby.game.phase, "finished")
        self.assertEqual(lobby.game.alive, {1, 3})
        self.assertIn("Mafia wins", message_text(self.channels[100].send.await_args))

    async def test_finished_game_releases_members_and_dm_routes_to_the_new_game(self):
        old = await self.night_game(count=3)
        await self.choose()
        await self.finish(old)
        new = await self.night_game(count=4, channel=200)
        await self.choose(3)
        await self.finish(new)
        self.assertEqual(old.game.alive, {1, 3})
        self.assertEqual(new.game.alive, {1, 2, 4})

    async def test_only_host_can_cancel_and_cancel_stops_timer(self):
        lobby = await self.night_game()
        with self.assertRaises(GameRuleError):
            await self.invoke("cancel", 2)
        await self.invoke("cancel")
        self.assertTrue(lobby.game_task.cancelled())
        self.assertNotIn(100, self.cog.lobbies)
        with self.assertRaises(GameRuleError):
            await self.choose()
        await self.invoke("create")
        replacement = self.cog.lobbies[100]
        self.channels[100].send.reset_mock()
        await self.cog.resolve_night(100, lobby)
        await self.cog.discard_lobby(100, lobby)
        self.assertIs(self.cog.lobbies[100], replacement)
        self.assertEqual(replacement.game.phase, "lobby")
        self.channels[100].send.assert_not_awaited()

    async def test_cog_unload_cancels_pending_timers(self):
        lobby = await self.night_game()
        await self.cog.cog_unload()
        self.assertTrue(lobby.game_task.cancelled())
        self.assertFalse(self.cog.lobbies)
        self.channels[100].send.assert_not_awaited()

    async def test_failed_private_confirmation_still_resolves_the_final_choice(self):
        lobby = await self.night_game()
        with self.assertLogs("mafia", level="WARNING"):
            await self.choose(3, send=AsyncMock(side_effect=self.delivery_error()))
        await self.finish(lobby)
        self.assertNotIn(3, lobby.game.alive)
        self.channels[100].send.assert_awaited_once()

    async def test_failed_morning_delivery_retries_without_double_elimination(self):
        lobby = await self.night_game()
        self.channels[100].send.side_effect = [self.delivery_error(), None]
        with patch("mafia.MESSAGE_RETRY_SECONDS", 0), self.assertLogs("mafia", level="WARNING"):
            await self.choose(3)
            await self.finish(lobby)
        self.assertEqual(lobby.game.alive, {1, 2, 4, 5})
        self.assertEqual(lobby.game.phase, "discussion")
        self.assertEqual(self.channels[100].send.await_count, 2)

    async def test_overlapping_choices_cancel_and_resolution_cannot_change_morning(self):
        lobby = await self.night_game()
        sending = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            sending.set()
            await release.wait()

        self.channels[100].send.side_effect = delayed_send
        await self.choose(3)
        try:
            await asyncio.wait_for(sending.wait(), timeout=3)
            self.assertEqual(lobby.game.phase, "resolving")
            with self.assertRaises(GameRuleError):
                await self.choose(4)
            await self.cog.resolve_night(100, lobby)
            self.channels[100].send.assert_awaited_once()
        finally:
            release.set()
            await self.finish(lobby)
        self.assertNotIn(3, lobby.game.alive)
        self.assertIn(4, lobby.game.alive)

    async def test_dm_and_server_checks_apply_to_the_correct_commands(self):
        dm_ctx = self.context(private=True)
        public_ctx = self.context()
        self.assertFalse(self.cog.mafia.checks)
        for check in self.cog.kill.checks:
            self.assertTrue(await discord.utils.maybe_coroutine(check, dm_ctx))
            with self.assertRaises(commands.PrivateMessageOnly):
                await discord.utils.maybe_coroutine(check, public_ctx)
        for name in ("create", "join", "leave", "show_lobby", "start", "cancel", "ready", "vote"):
            for check in getattr(self.cog, name).checks:
                with self.assertRaises(commands.NoPrivateMessage):
                    await discord.utils.maybe_coroutine(check, dm_ctx)

    async def test_framework_accepts_short_private_command_and_rejects_bad_or_old_syntax(self):
        lobby = await self.night_game()
        async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as client:
            client._connection.user = SimpleNamespace(id=99999)
            await client.add_cog(self.cog)

            async def send_command(content, guild):
                message = SimpleNamespace(
                    content=content,
                    author=self.members[1],
                    guild=guild,
                    channel=SimpleNamespace(id=999),
                    _state=client._connection,
                    attachments=[],
                )
                ctx = await client.get_context(message)
                ctx.send = AsyncMock()
                await client.invoke(ctx)
                return ctx

            for text in ("!mafia kill", "!mafia kill banana", "!mafia kill 1000 2"):
                ctx = await send_command(text, guild=None)
                self.assertIn("!mafia kill 3", message_text(ctx.send.await_args))
                self.assertIsNone(lobby.game.night_target)
            ctx = await send_command("!mafia kill 3", guild=SimpleNamespace(id=50))
            self.assertIn("private DM", message_text(ctx.send.await_args))
            self.assertIsNone(lobby.game.night_target)
            ctx = await send_command("!mafia kill 3", guild=None)
            self.assertIn("Choice locked", message_text(ctx.send.await_args))
            await self.finish(lobby)
            self.assertNotIn(3, lobby.game.alive)

    async def test_game_commands_registered_and_manual_morning_removed(self):
        from bot import DBot

        async with DBot(command_prefix="!", intents=discord.Intents.default()) as client:
            # Registration must not open the operator's real restart database.
            with patch("bot.RestartJournal", return_value=None):
                await client.setup_hook()
            for name in (
                "create",
                "join",
                "leave",
                "lobby",
                "start",
                "cancel",
                "kill",
                "ready",
                "vote",
                "check",
                "transfer",
                "forfeit",
                "forcecancel",
            ):
                self.assertIsNotNone(client.get_command(f"mafia {name}"))
            self.assertIsNone(client.get_command("mafia morning"))

    async def assert_permanent_channel_failure(self, error):
        lobby = await self.night_game()
        self.channels[100].send.side_effect = error
        with self.assertLogs("mafia", level="WARNING"):
            await self.choose(3)
            await self.finish(lobby)
        expected_sends = 2 if isinstance(error, discord.Forbidden) else 1
        self.assertEqual(self.channels[100].send.await_count, expected_sends)
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIsNone(self.cog.active_game(1))
        self.assertIn("cancelled", message_text(self.members[1].send.await_args))

    async def test_lost_channel_permissions_stop_game_instead_of_retrying_forever(self):
        await self.assert_permanent_channel_failure(
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send")
        )

    async def test_deleted_channel_stops_game_and_notifies_host(self):
        await self.assert_permanent_channel_failure(
            discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown channel")
        )

    async def test_other_permanent_discord_errors_stop_instead_of_retrying(self):
        lobby = await self.night_game()
        self.channels[100].send.side_effect = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"), "Invalid message"
        )
        with self.assertLogs("mafia", level="ERROR"):
            await self.choose(3)
            await self.finish(lobby)
        self.channels[100].send.assert_awaited_once()
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIn("Discord rejected", message_text(self.members[1].send.await_args))

    def test_phase_durations_must_be_positive_and_finite(self):
        for name in ("night_seconds", "discussion_seconds", "voting_seconds"):
            for value in (0, -1, float("inf"), float("-inf"), float("nan")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    Mafia(**{name: value})

    async def test_nonhost_mafia_can_act_but_only_the_host_can_cancel_even_if_dead(self):
        lobby = await self.night_game(mafia_player=3)
        with self.assertRaises(GameRuleError):
            await self.choose(2, player=1)
        await self.choose(1, player=3)
        await self.finish(lobby)
        self.assertNotIn(1, lobby.game.alive)
        with self.assertRaises(GameRuleError):
            await self.invoke("ready", 1)
        with self.assertRaises(GameRuleError):
            await self.invoke("cancel", 3)
        await self.invoke("cancel", 1)
        self.assertFalse(self.cog.lobbies)

    async def test_unexpected_timer_error_releases_players_and_notifies_host(self):
        lobby = await self.night_game()
        self.cog.resolve_night = AsyncMock(side_effect=RuntimeError("Simulated failure"))
        with self.assertLogs("mafia", level="ERROR"):
            await self.choose(3)
            await self.finish(lobby)
        self.assertNotIn(100, self.cog.lobbies)
        self.assertIn("unexpected error", message_text(self.members[1].send.await_args))

    async def test_shutdown_during_role_delivery_does_not_start_an_orphan_timer(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        sending = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            sending.set()
            await release.wait()

        self.members[1].send.side_effect = delayed_send
        task = asyncio.create_task(self.invoke("start"))
        try:
            await asyncio.wait_for(sending.wait(), timeout=3)
            await self.cog.cog_unload()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(lobby.game_task)
        self.assertFalse(self.cog.lobbies)
        self.members[2].send.assert_not_awaited()
        self.members[3].send.assert_not_awaited()

    async def test_shutdown_during_night_announcement_does_not_start_a_timer(self):
        await self.ready_lobby()
        lobby = self.cog.lobbies[100]
        sending = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            sending.set()
            await release.wait()

        self.channels[100].send.side_effect = delayed_send
        task = asyncio.create_task(self.invoke("start"))
        try:
            await asyncio.wait_for(sending.wait(), timeout=3)
            await self.cog.cog_unload()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(lobby.game_task)
        self.assertFalse(self.cog.lobbies)

    async def test_two_round_game_keeps_roles_and_finishes_with_citizen_victory(self):
        lobby = await self.night_game(count=6)
        original_roles = dict(lobby.game.roles)
        original_worker = lobby.game_task
        await self.choose(6)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        for player in sorted(lobby.game.alive):
            await self.cast(player, "skip")
        await self.wait_phase(lobby, "night", night=2)
        self.assertIs(lobby.game_task, original_worker)
        self.assertEqual(lobby.game.roles, original_roles)
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4, 5})
        self.assertFalse(lobby.game.votes)
        mafia_prompt = message_text(self.members[1].send.await_args)
        self.assertIn("Night 2", mafia_prompt)
        self.assertNotIn("!mafia kill 6", mafia_prompt)
        await self.choose(5)
        await self.finish(lobby)
        await asyncio.sleep(0)
        self.assertEqual(lobby.game.phase, "discussion")
        self.assertFalse(lobby.game.ready_players)
        await self.skip_to_voting(lobby)
        await self.cast(1, "skip")
        for player in (2, 3, 4):
            await self.cast(player, 1)
        await self.wait_phase(lobby, "finished")
        await asyncio.wait_for(lobby.game_task, timeout=5)
        self.assertNotIn(1, lobby.game.alive)
        self.assertIn("Citizens win", message_text(self.channels[100].send.await_args))

    async def test_discussion_can_end_early_only_with_majority_of_living_players(self):
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        for actor in (6, 14):
            with self.assertRaises(GameRuleError):
                await self.invoke("ready", actor)
        await self.invoke("ready", 1)
        with self.assertRaises(GameRuleError):
            await self.invoke("ready", 1)
        await self.invoke("ready", 2)
        await asyncio.sleep(0)
        self.assertEqual(lobby.game.phase, "discussion")
        await self.invoke("ready", 3)
        await self.wait_phase(lobby, "voting")
        self.assertEqual(len(lobby.game.ready_players), 3)

    async def test_discussion_timeout_opens_voting_without_ready_commands(self):
        self.cog.discussion_seconds = 0.05
        lobby = await self.night_game()
        await self.choose(5)
        await self.wait_phase(lobby, "voting")
        self.assertFalse(lobby.game.ready_players)
        self.assertIn("Voting", message_text(self.channels[100].send.await_args))

    async def test_vote_timeout_without_majority_begins_next_night_with_no_day_elimination(self):
        self.cog.voting_seconds = 0.2
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        # Observe the persistent next phase, rather than racing a very short vote window.
        for player in sorted(lobby.game.alive)[: lobby.game.majority]:
            await self.invoke("ready", player)
        await self.wait_phase(lobby, "night", night=2)
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4, 5})
        results = [message_text(call) for call in self.channels[100].send.await_args_list]
        self.assertTrue(any("No player received a majority" in text for text in results))

    async def test_three_player_wrong_day_vote_wins_for_mafia(self):
        lobby = await self.night_game(count=3)
        lobby.night_ready.set()
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        await self.cast(1, 3)
        await self.cast(2, 3)
        await self.cast(3, "skip")
        await self.wait_phase(lobby, "finished")
        self.assertEqual(lobby.game.alive, {1, 2})
        self.assertIn("Mafia wins", message_text(self.channels[100].send.await_args))

    async def test_ready_and_votes_in_night_are_rejected(self):
        lobby = await self.night_game()
        with self.assertRaises(GameRuleError):
            await self.invoke("ready")
        with self.assertRaises(GameRuleError):
            await self.cast(1, "skip")
        self.assertFalse(lobby.game.ready_players)
        self.assertFalse(lobby.game.votes)

    async def test_cancel_during_voting_stops_all_future_rounds(self):
        lobby = await self.night_game()
        await self.choose(5)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        await self.invoke("cancel")
        self.channels[100].send.reset_mock()
        await asyncio.sleep(0)
        self.assertTrue(lobby.game_task.cancelled())
        self.assertFalse(self.cog.lobbies)
        self.channels[100].send.assert_not_awaited()

    async def test_vote_result_retry_applies_elimination_once_and_prompts_next_night_once(self):
        lobby = await self.night_game(count=6)
        await self.choose(6)
        await self.finish(lobby)
        await self.skip_to_voting(lobby)
        failures = []

        async def flaky_send(text=None, *, embed=None):
            if embed is not None and embed.title == "⚖️ Voting result" and not failures:
                failures.append(embed)
                raise self.delivery_error()

        self.channels[100].send.side_effect = flaky_send
        with patch("mafia.MESSAGE_RETRY_SECONDS", 0), self.assertLogs("mafia", level="WARNING"):
            for actor in (1, 2, 3):
                await self.cast(actor, 5)
            for actor in (4, 5):
                await self.cast(actor, "skip")
            await self.wait_phase(lobby, "night", night=2)
        self.assertEqual(lobby.game.alive, {1, 2, 3, 4})
        self.assertEqual(lobby.game.night_number, 2)
        self.assertEqual(self.members[1].send.await_count, 2)
        self.assertEqual(len(failures), 1)

    async def test_ready_and_vote_commands_are_parsed_and_dm_votes_are_rejected(self):
        lobby = await self.night_game(count=3)
        lobby.night_ready.set()
        await self.finish(lobby)
        async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as client:
            client._connection.user = SimpleNamespace(id=99999)
            await client.add_cog(self.cog)

            async def send_command(text, actor, private=False):
                message = SimpleNamespace(
                    content=text,
                    author=self.members[actor],
                    guild=None if private else SimpleNamespace(id=50),
                    channel=self.channels[100],
                    _state=client._connection,
                    attachments=[],
                )
                ctx = await client.get_context(message)
                ctx.send = AsyncMock()
                await client.invoke(ctx)
                return ctx

            ctx = await send_command("!mafia ready", 1, private=True)
            self.assertIn("server channel", message_text(ctx.send.await_args))
            for actor in (1, 2):
                await send_command("!mafia ready", actor)
            await self.wait_phase(lobby, "voting")
            ctx = await send_command("!mafia vote 1", 2, private=True)
            self.assertIn("server channel", message_text(ctx.send.await_args))
            for text in ("!mafia vote", "!mafia vote banana"):
                ctx = await send_command(text, 2)
                self.assertIn("!mafia vote", message_text(ctx.send.await_args))
            await send_command("!mafia vote skip", 1)
            await send_command("!mafia vote 1", 2)
            await send_command("!mafia vote 1", 3)
            await self.wait_phase(lobby, "finished")
            self.assertIn("Citizens win", message_text(self.channels[100].send.await_args))


if __name__ == "__main__":
    unittest.main()
