"""Restart notification uses durable IDs, never secret role snapshots."""

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from mafia import Mafia
from mafia_game import GameRuleError
from mafia_restart import RestartJournal, notify_interrupted
from tests.fakes import DiscordGameFixture, message_text


class JournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "local" / "restart.sqlite3"
        self.journal = RestartJournal(self.path)
        self.bot = SimpleNamespace(fetch_channel=AsyncMock(), fetch_user=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot.fetch_channel.return_value = self.channel

    def unavailable(self):
        return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Unavailable")

    async def test_records_survive_reopen_and_contain_only_session_channel_host(self):
        session = self.journal.add(100, 1)
        self.journal.update_host(session, 2)
        reopened = RestartJournal(self.path)
        self.assertEqual(reopened.records(), [(session, 100, 2)])
        with closing(sqlite3.connect(self.path)) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
        self.assertEqual(columns, ["id", "channel_id", "host_id"])
        reopened.remove(session)
        self.assertEqual(self.journal.records(), [])

    async def test_channel_notification_removes_only_old_record(self):
        old = self.journal.add(100, 1)
        records = self.journal.records()
        new = self.journal.add(100, 2)
        await notify_interrupted(self.bot, self.journal, records)
        self.assertEqual(self.journal.records(), [(new, 100, 2)])
        self.channel.send.assert_awaited_once()
        self.assertIn("previous", self.channel.send.await_args.args[0].lower())
        self.bot.fetch_user.assert_not_awaited()
        self.assertNotEqual(old, new)

    async def test_channel_denial_falls_back_to_host(self):
        self.journal.add(100, 1)
        self.channel.send.side_effect = self.unavailable()
        host = SimpleNamespace(send=AsyncMock())
        self.bot.fetch_user.return_value = host
        await notify_interrupted(self.bot, self.journal, self.journal.records())
        host.send.assert_awaited_once()
        self.assertFalse(self.journal.records())

    async def test_unreachable_channel_and_host_remain_for_next_boot(self):
        self.journal.add(100, 1)
        self.bot.fetch_channel.side_effect = self.unavailable()
        self.bot.fetch_user.side_effect = self.unavailable()
        with self.assertLogs("mafia_restart", level="WARNING"):
            await notify_interrupted(self.bot, self.journal, self.journal.records())
        self.assertEqual(len(self.journal.records()), 1)

    async def test_timeout_keeps_notice_and_cancellation_does_not_acknowledge_it(self):
        self.journal.add(100, 1)

        async def blocked(*args):
            await asyncio.Event().wait()

        self.bot.fetch_channel.side_effect = blocked
        self.bot.fetch_user.side_effect = self.unavailable()
        with self.assertLogs("mafia_restart", level="WARNING"):
            await notify_interrupted(
                self.bot, self.journal, self.journal.records(), request_seconds=0.01
            )
        self.assertEqual(len(self.journal.records()), 1)
        self.bot.fetch_channel.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await notify_interrupted(self.bot, self.journal, self.journal.records())
        self.assertEqual(len(self.journal.records()), 1)


class RestartIntegrationTests(DiscordGameFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.journal = RestartJournal(Path(self.folder.name) / "restart.sqlite3")
        self.cog = Mafia(restart_journal=self.journal)
        self.cog.recovery_ready.set()

    async def test_cancel_removes_record_but_shutdown_preserves_interrupted_game(self):
        await self.ready_lobby()
        await self.cog.transfer.callback(self.cog, self.context(), 2)
        self.assertEqual(self.journal.records()[0][2], 2)
        await self.invoke("cancel", 2)
        self.assertFalse(self.journal.records())
        await self.invoke("create")
        await self.cog.cog_unload()
        self.assertEqual(len(self.journal.records()), 1)

    async def test_finished_game_is_not_reported_as_interrupted(self):
        lobby = await self.night_game(count=3)
        await self.choose(2)
        await self.wait_phase(lobby, "finished")
        await lobby.game_task
        self.assertFalse(self.journal.records())

    async def test_recovery_waits_for_connection_and_blocks_new_lobbies_until_done(self):
        self.journal.add(50, 1)
        connected = asyncio.Event()
        self.cog.bot = SimpleNamespace(
            wait_until_ready=connected.wait,
            fetch_channel=AsyncMock(return_value=SimpleNamespace(send=AsyncMock())),
        )
        self.cog.recovery_ready.clear()
        await self.cog.cog_load()
        with self.assertRaisesRegex(GameRuleError, "Restart checks"):
            await self.invoke("create")
        connected.set()
        await asyncio.wait_for(self.cog.recovery_task, 5)
        self.assertTrue(self.cog.recovery_ready.is_set())
        self.assertFalse(self.journal.records())
        await self.invoke("create")

    async def test_setup_check_tests_only_callers_dm_without_creating_lobby(self):
        ctx = self.context()
        ctx.guild.me = self.members[14]
        ctx.channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=False
        )
        await self.cog.check_setup.callback(self.cog, ctx)
        self.members[1].send.assert_awaited_once()
        self.members[2].send.assert_not_awaited()
        self.assertIn("Embed cards: missing", message_text(ctx.send.await_args))
        self.assertIn("delivered successfully", message_text(ctx.send.await_args))
        self.assertFalse(self.cog.lobbies)
        self.assertFalse(self.journal.records())

    async def test_setup_check_framework_limits_repeated_dms_and_rejects_private_use(self):
        # No real journal or Discord connection is needed for framework routing.
        self.cog = Mafia()
        self.members[1].bot = False
        ctx_source = self.context()
        ctx_source.guild.id = 50
        ctx_source.guild.me = self.members[14]
        ctx_source.channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True
        )
        async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as client:
            client._connection.user = SimpleNamespace(id=99999)
            await client.add_cog(self.cog)
            message = SimpleNamespace(
                content="!mafia check",
                author=self.members[1],
                guild=ctx_source.guild,
                channel=ctx_source.channel,
                _state=client._connection,
                attachments=[],
                edited_at=None,
                created_at=datetime.now(timezone.utc),
            )
            for _ in range(2):
                ctx = await client.get_context(message)
                ctx.send = AsyncMock()
                await client.invoke(ctx)
            self.members[1].send.assert_awaited_once()
            self.assertIn("wait", message_text(ctx.send.await_args))
            message.guild = None
            ctx = await client.get_context(message)
            ctx.send = AsyncMock()
            await client.invoke(ctx)
            self.assertIn("server channel", message_text(ctx.send.await_args))

    async def test_setup_check_reports_blocked_dm_and_falls_back_when_channel_denied(self):
        ctx = self.context()
        ctx.guild.me = self.members[14]
        ctx.channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, send_messages=False, embed_links=True
        )
        denied = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Blocked")
        self.members[1].send.side_effect = [denied, None]
        ctx.send.side_effect = denied
        await self.cog.check_setup.callback(self.cog, ctx)
        self.assertIn("could not deliver", self.members[1].send.await_args.args[0])
        self.assertIn("Send messages: missing", self.members[1].send.await_args.args[0])
