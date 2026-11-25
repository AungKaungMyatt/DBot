"""Startup and utility commands never require a real Discord connection."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord.ext import commands

import bot


class StartupTests(unittest.TestCase):
    def test_missing_blank_or_placeholder_token_fails_before_login(self):
        for value in (None, "", "   ", "paste_your_bot_token_here"):
            environment = {} if value is None else {"DISCORD_TOKEN": value}
            with self.subTest(value=value), patch.dict(os.environ, environment, clear=True):
                with patch.object(bot, "load_dotenv"), patch.object(bot.bot, "run") as run:
                    with self.assertRaisesRegex(SystemExit, "Missing DISCORD_TOKEN"):
                        bot.main()
                    run.assert_not_called()

    def test_valid_environment_token_is_trimmed_before_login(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "  unit-test-token  "}, clear=True):
            with patch.object(bot, "load_dotenv"), patch.object(bot.bot, "run") as run:
                bot.main()
                run.assert_called_once_with("unit-test-token")


class UtilityCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_hello_greets_the_sender(self):
        ctx = SimpleNamespace(author=SimpleNamespace(display_name="Learner"), send=AsyncMock())
        await bot.hello.callback(ctx)
        self.assertIn("Learner", ctx.send.await_args.args[0])

    async def test_dice_default_custom_and_invalid_ranges(self):
        ctx = SimpleNamespace(send=AsyncMock())
        for sides in (6, 20, 100):
            await bot.roll.callback(ctx, sides)
            result = int(ctx.send.await_args.args[0].split()[2])
            self.assertTrue(1 <= result <= sides)
        for sides in (1, 101, -1):
            await bot.roll.callback(ctx, sides)
            self.assertIn("between 2 and 100", ctx.send.await_args.args[0])

    async def test_dice_error_explains_bad_input_and_preserves_unexpected_errors(self):
        ctx = SimpleNamespace(send=AsyncMock())
        await bot.roll_error(ctx, commands.BadArgument())
        self.assertIn("whole number", ctx.send.await_args.args[0])
        with self.assertRaisesRegex(RuntimeError, "Simulated failure"):
            await bot.roll_error(ctx, RuntimeError("Simulated failure"))


if __name__ == "__main__":
    unittest.main()
