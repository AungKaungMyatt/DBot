"""Test the game rules with plain Python objects and a pretend clock."""

import unittest
from itertools import product
from unittest.mock import patch

from mafia_game import GameRuleError, GameState, Phase, Role


class GameStateTests(unittest.TestCase):
    def game_at_night(self, count=5):
        game = GameState(host_id=10)
        for player_id in range(11, 10 + count):
            game.join(player_id)
        with patch("mafia_game.random.choice", return_value=10):
            game.begin_assignment()
        game.begin_night(now=100, duration=120)
        return game

    def test_host_joins_and_player_numbers_follow_join_order(self):
        game = GameState(host_id=10)
        game.join(20)
        game.join(30)
        game.leave(20)
        game.join(40)
        self.assertEqual(game.players, [10, 30, 40])
        with self.assertRaises(GameRuleError):
            game.leave(10)
        with self.assertRaises(GameRuleError):
            game.require_host(30)

    def test_assignment_requires_enough_players_and_freezes_membership(self):
        game = GameState(host_id=10)
        with self.assertRaises(GameRuleError):
            game.begin_assignment()
        game = self.game_at_night()
        self.assertEqual(list(game.roles.values()).count(Role.MAFIA), 1)
        self.assertEqual(list(game.roles.values()).count(Role.CITIZEN), 4)
        for action in (lambda: game.join(99), lambda: game.leave(11), game.begin_assignment):
            with self.assertRaises(GameRuleError):
                action()

    def test_number_maps_to_player_id_and_choice_is_final(self):
        game = self.game_at_night()
        self.assertEqual(game.lock_target(10, player_number=3, now=101), 12)
        with self.assertRaises(GameRuleError):
            game.lock_target(10, player_number=4, now=102)
        self.assertEqual(game.night_target, 12)

    def test_deadline_boundary_is_exclusive(self):
        game = self.game_at_night()
        for now in (220, 221):
            with self.assertRaises(GameRuleError):
                game.lock_target(10, 2, now=now)
        self.assertIsNone(game.night_target)

    def test_only_living_mafia_can_act(self):
        game = self.game_at_night()
        for actor in (11, 99):
            with self.assertRaises(GameRuleError):
                game.lock_target(actor, 3, now=101)
        game.alive.remove(10)
        with self.assertRaises(GameRuleError):
            game.lock_target(10, 3, now=101)

    def test_invalid_self_and_dead_targets_do_not_lock_choice(self):
        game = self.game_at_night()
        game.alive.remove(12)
        for number in (0, -1, 6, 1, 3):
            with self.assertRaises(GameRuleError):
                game.lock_target(10, number, now=101)
        self.assertIsNone(game.night_target)

    def test_morning_can_retry_before_committing_and_keeps_numbers_stable(self):
        game = self.game_at_night()
        game.lock_target(10, 3, now=101)
        outcome = game.prepare_morning()
        self.assertEqual(game.phase, Phase.RESOLVING)
        self.assertIn(12, game.alive)
        self.assertNotIn(12, outcome.survivors)
        game.retry_morning()
        self.assertEqual(game.phase, Phase.NIGHT)
        self.assertEqual(game.prepare_morning(), outcome)
        game.complete_morning(outcome, now=101, duration=120)
        self.assertEqual(game.phase, Phase.DISCUSSION)
        self.assertNotIn(12, game.alive)
        self.assertEqual(game.players, [10, 11, 12, 13, 14])
        with self.assertRaises(GameRuleError):
            game.complete_morning(outcome, now=101, duration=120)

    def test_no_target_preserves_everyone_and_parity_wins_for_mafia(self):
        game = self.game_at_night(count=3)
        outcome = game.prepare_morning()
        self.assertIsNone(outcome.victim_id)
        self.assertEqual(outcome.survivors, {10, 11, 12})
        self.assertFalse(outcome.mafia_wins)
        game.retry_morning()
        game.lock_target(10, 2, now=101)
        outcome = game.prepare_morning()
        self.assertTrue(outcome.mafia_wins)
        game.complete_morning(outcome, now=101, duration=120)
        self.assertEqual(game.phase, Phase.FINISHED)

    def game_in_discussion(self, count=5):
        game = self.game_at_night(count)
        game.complete_morning(game.prepare_morning(), now=101, duration=120)
        return game

    def game_in_voting(self, count=5):
        game = self.game_in_discussion(count)
        game.prepare_voting()
        game.begin_voting(now=200, duration=60)
        return game

    def test_majority_ready_skips_discussion_but_duplicates_and_dead_players_do_not_count(self):
        game = self.game_in_discussion()
        self.assertEqual(game.majority, 3)
        self.assertFalse(game.mark_ready(10, now=102))
        with self.assertRaises(GameRuleError):
            game.mark_ready(10, now=103)
        with self.assertRaises(GameRuleError):
            game.mark_ready(99, now=103)
        self.assertFalse(game.mark_ready(11, now=104))
        self.assertTrue(game.mark_ready(12, now=105))
        self.assertEqual(game.ready_players, {10, 11, 12})

    def test_ready_requires_discussion_and_unexpired_deadline(self):
        game = self.game_at_night()
        with self.assertRaises(GameRuleError):
            game.mark_ready(10, now=101)
        game = self.game_in_discussion()
        with self.assertRaises(GameRuleError):
            game.mark_ready(10, now=221)
        game.alive.remove(11)
        with self.assertRaises(GameRuleError):
            game.mark_ready(11, now=102)

    def test_votes_are_final_and_all_living_votes_end_voting_early(self):
        game = self.game_in_voting(count=3)
        self.assertFalse(game.cast_vote(10, 2, now=201))
        with self.assertRaises(GameRuleError):
            game.cast_vote(10, None, now=202)
        self.assertFalse(game.cast_vote(11, None, now=202))
        self.assertTrue(game.cast_vote(12, 1, now=203))
        self.assertEqual(game.votes, {10: 11, 11: None, 12: 10})

    def test_invalid_dead_self_and_late_votes_do_not_count(self):
        game = self.game_in_voting()
        game.alive.remove(12)
        for actor, target, now in (
            (99, 2, 201),
            (12, 2, 201),
            (10, 1, 201),
            (10, 3, 201),
            (10, 0, 201),
            (10, 6, 201),
            (10, 2, 260),
        ):
            with self.assertRaises(GameRuleError):
                game.cast_vote(actor, target, now=now)
        self.assertFalse(game.votes)

    def test_votes_outside_voting_are_rejected(self):
        game = self.game_in_discussion()
        with self.assertRaises(GameRuleError):
            game.cast_vote(10, 2, now=102)

    def test_tie_and_skips_produce_no_elimination(self):
        game = self.game_in_voting()
        for actor, target in ((10, 2), (11, 1), (12, 1), (13, 2), (14, None)):
            game.cast_vote(actor, target, now=201)
        outcome = game.prepare_vote_result()
        self.assertIsNone(outcome.victim_id)
        self.assertEqual(outcome.survivors, {10, 11, 12, 13, 14})

    def test_majority_is_of_all_living_players_not_only_submitted_votes(self):
        game = self.game_in_voting()
        game.cast_vote(11, 1, now=201)
        game.cast_vote(12, 1, now=201)
        outcome = game.prepare_vote_result()
        self.assertIsNone(outcome.victim_id)

    def test_voting_out_mafia_wins_for_citizens(self):
        game = self.game_in_voting()
        for actor in (11, 12, 13):
            game.cast_vote(actor, 1, now=201)
        outcome = game.prepare_vote_result()
        self.assertEqual(outcome.victim_id, 10)
        self.assertEqual(outcome.winner, Role.CITIZEN)
        game.complete_vote_result(outcome)
        self.assertEqual(game.phase, Phase.FINISHED)

    def test_wrong_day_elimination_can_give_mafia_parity(self):
        game = self.game_in_voting(count=3)
        game.cast_vote(10, 3, now=201)
        game.cast_vote(11, 3, now=201)
        outcome = game.prepare_vote_result()
        self.assertEqual(outcome.winner, Role.MAFIA)
        game.complete_vote_result(outcome)
        self.assertEqual(game.phase, Phase.FINISHED)

    def test_next_night_preserves_roles_and_numbers_but_clears_daily_actions(self):
        game = self.game_in_voting()
        original_roles = dict(game.roles)
        game.ready_players = {10, 11, 12}
        for actor in (10, 11, 12):
            game.cast_vote(actor, 5, now=201)
        outcome = game.prepare_vote_result()
        game.complete_vote_result(outcome)
        self.assertEqual(game.phase, Phase.NEXT_NIGHT)
        game.prepare_next_night()
        game.begin_night(now=300, duration=120)
        self.assertEqual(game.night_number, 2)
        self.assertEqual(game.night_deadline, 420)
        self.assertEqual(game.roles, original_roles)
        self.assertEqual(game.players, [10, 11, 12, 13, 14])
        self.assertNotIn(14, game.alive)
        self.assertFalse(game.ready_players)
        self.assertFalse(game.votes)
        with self.assertRaises(GameRuleError):
            game.lock_target(10, 5, now=301)

    def test_every_three_and_four_player_ballot_combination(self):
        """Compare all 2,692 valid ballots, including missing votes, to the rules."""
        absent = object()
        checked = 0
        for count in (3, 4):
            players = list(range(10, 10 + count))
            options = [
                [absent, None] + [number for number in range(1, count + 1) if number != index + 1]
                for index in range(count)
            ]
            for mafia_id in players:
                for ballots in product(*options):
                    game = GameState(players[0])
                    game.players = list(players)
                    game.alive = set(players)
                    game.roles = {
                        player: Role.MAFIA if player == mafia_id else Role.CITIZEN
                        for player in players
                    }
                    game.phase = Phase.VOTING
                    game.voting_deadline = 100
                    expected_counts = dict.fromkeys(players, 0)
                    for player, number in zip(players, ballots, strict=True):
                        if number is absent:
                            continue
                        game.cast_vote(player, number, now=1)
                        if number is not None:
                            expected_counts[players[number - 1]] += 1
                    expected_victim = next(
                        (player for player, votes in expected_counts.items() if votes * 2 > count),
                        None,
                    )
                    expected_survivors = set(players) - {expected_victim}
                    expected_winner = (
                        Role.CITIZEN
                        if mafia_id not in expected_survivors
                        else Role.MAFIA
                        if len(expected_survivors) <= 2
                        else None
                    )
                    outcome = game.prepare_vote_result()
                    self.assertEqual(outcome.victim_id, expected_victim)
                    self.assertEqual(outcome.survivors, expected_survivors)
                    self.assertEqual(outcome.winner, expected_winner)
                    game.complete_vote_result(outcome)
                    self.assertEqual(game.alive, expected_survivors)
                    self.assertEqual(game.players, players)
                    checked += 1
        self.assertEqual(checked, 2692)


if __name__ == "__main__":
    unittest.main()
