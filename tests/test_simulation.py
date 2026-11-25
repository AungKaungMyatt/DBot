"""The offline simulation is deterministic and never needs a Discord token."""

import io
import random
import unittest
from contextlib import redirect_stderr, redirect_stdout

from scripts.simulate_games import main, simulate_games, simulate_match


class SimulationTests(unittest.TestCase):
    def test_seeded_games_end_reproducibly(self):
        first = simulate_games(50, seed=7)
        self.assertEqual(sum(first.values()), 50)
        self.assertEqual(first, simulate_games(50, seed=7))
        self.assertGreaterEqual(first["Draw"], 5)

    def test_passive_players_draw_instead_of_looping_forever(self):
        for count in range(3, 7):
            self.assertEqual(simulate_match(random.Random(3), count, passive=True), "Draw")

    def test_cli_reports_success_and_rejects_bad_game_counts(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["--games", "10", "--seed", "2"])
        self.assertIn("PASS", output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--games", "0"])


if __name__ == "__main__":
    unittest.main()
