"""Check card privacy, stable rosters, and permission fallback."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from mafia_cards import card_text, duration_text, morning_card, night_card, send_card, voting_card
from mafia_game import GameState


class CardTests(unittest.IsolatedAsyncioTestCase):
    def game(self, count=5):
        game = GameState(1)
        for player in range(2, count + 1):
            game.join(player)
        with patch("mafia_game.random.choice", return_value=1):
            game.begin_assignment()
        game.begin_night(now=0, duration=120)
        members = {player: SimpleNamespace(mention=f"<@{player}>") for player in game.players}
        return game, members

    def test_normal_morning_keeps_mafia_identity_private(self):
        game, members = self.game()
        game.lock_target(1, 3, now=1)
        outcome = game.prepare_morning()
        card = morning_card(game, members, outcome, 120)
        text = card_text(card)
        self.assertIn("<@3> was eliminated", text)
        self.assertNotIn("<@1>", text)
        self.assertIn("!mafia ready", text)
        self.assertNotIn("120 seconds", text)
        self.assertLess(len(text), 2000)
        self.assertLess(len(card), 6000)

    def test_winning_morning_emphasizes_winner_without_discussion_instructions(self):
        game, members = self.game(count=3)
        game.lock_target(1, 3, now=1)
        card = morning_card(game, members, game.prepare_morning(), 120)
        self.assertIn("Mafia wins", card.title)
        text = card_text(card)
        self.assertIn("<@3> was eliminated", text)
        self.assertIn("1 Mafia • 1 Citizen", text)
        self.assertNotIn("!mafia ready", text)
        self.assertNotIn("Discussion begins", text)

    def test_voting_roster_omits_dead_players_without_changing_numbers(self):
        game, members = self.game()
        game.alive.remove(2)
        card = voting_card(game, members, 60)
        roster = next(field.value for field in card.fields if field.name == "Living players")
        self.assertIn("3. <@3>", roster)
        self.assertNotIn("<@2>", roster)
        self.assertIn("1 minute", card.footer.text)
        self.assertTrue(all(len(field.value) <= 1024 for field in card.fields))

    def test_durations_use_readable_units(self):
        for seconds, expected in (
            (120, "2 minutes"),
            (60, "1 minute"),
            (30, "30 seconds"),
            (1, "1 second"),
        ):
            self.assertEqual(duration_text(seconds), expected)

    async def test_cards_fall_back_to_spaced_text_without_embed_permission(self):
        channel = SimpleNamespace(
            guild=SimpleNamespace(me=object()),
            send=AsyncMock(),
            permissions_for=lambda member: SimpleNamespace(embed_links=False),
        )
        card = night_card(1, 120)
        await send_card(channel, card)
        text = channel.send.await_args.args[0]
        self.assertIn("**🌙 Night 1**\n\n", text)
        self.assertIn("2 minutes", text)
        self.assertNotIn("embed", channel.send.await_args.kwargs)
        context = SimpleNamespace(channel=channel, send=AsyncMock())
        await send_card(context, card)
        self.assertEqual(context.send.await_args.args[0], text)

    async def test_embed_permission_sends_one_card_without_duplicate_plain_message(self):
        channel = SimpleNamespace(
            guild=SimpleNamespace(me=object()),
            send=AsyncMock(),
            permissions_for=lambda member: SimpleNamespace(embed_links=True),
        )
        card = night_card(2, 120)
        await send_card(channel, card)
        channel.send.assert_awaited_once_with(embed=card)

    async def test_permission_change_during_send_retries_as_plain_text(self):
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No embeds")
        channel = SimpleNamespace(
            guild=SimpleNamespace(me=object()),
            send=AsyncMock(side_effect=[forbidden, None]),
            permissions_for=lambda member: SimpleNamespace(embed_links=True),
        )
        await send_card(channel, night_card(1, 120))
        self.assertEqual(channel.send.await_count, 2)
        self.assertIn("2 minutes", channel.send.await_args.args[0])
        self.assertFalse(channel.send.await_args.kwargs)

    async def test_uncached_bot_membership_uses_text_without_guessing_permissions(self):
        channel = SimpleNamespace(guild=SimpleNamespace(me=None), send=AsyncMock())
        await send_card(channel, night_card(1, 120))
        self.assertIn("Night 1", channel.send.await_args.args[0])
        self.assertFalse(channel.send.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
