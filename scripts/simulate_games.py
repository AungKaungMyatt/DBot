"""Run complete games with simulated players and a clock that never sleeps."""

import argparse
import random
from collections import Counter

from mafia_game import MAX_PEACEFUL_ROUNDS, GameState, Phase, Role


def simulate_match(rng: random.Random, count: int, passive: bool = False) -> str:
    game = GameState(1)
    for player in range(2, count + 1):
        game.join(player)
    game.begin_assignment(rng)
    original_roles, original_players = dict(game.roles), list(game.players)
    previous_alive = set(game.alive)
    now = 1000.0
    for _ in range(MAX_PEACEFUL_ROUNDS * count):
        game.begin_night(now, duration=120)
        mafia = next(player for player in game.alive if game.roles[player] == Role.MAFIA)
        if not passive and rng.random() < 0.8:
            targets = [
                number
                for number, player in enumerate(game.players, 1)
                if player in game.alive and player != mafia
            ]
            game.lock_target(mafia, rng.choice(targets), now + 1)
        now += 120
        outcome = game.prepare_morning()
        game.complete_morning(outcome, now, duration=120)
        assert game.alive <= previous_alive
        previous_alive = set(game.alive)
        if game.phase != Phase.FINISHED:
            if not passive:
                for player in sorted(game.alive):
                    game.mark_ready(player, now + 1)
            now += 120
            game.prepare_voting()
            game.begin_voting(now, duration=60)
            for player in sorted(game.alive):
                choices = [None] + [
                    number
                    for number, target in enumerate(game.players, 1)
                    if target in game.alive and target != player
                ]
                game.cast_vote(player, None if passive else rng.choice(choices), now + 1)
            outcome = game.prepare_vote_result()
            game.complete_vote_result(outcome)
            now += 60
        assert game.roles == original_roles
        assert game.players == original_players
        assert game.alive <= previous_alive
        previous_alive = set(game.alive)
        if game.phase == Phase.FINISHED:
            return (
                "Draw" if outcome.draw else "Mafia" if outcome.winner == Role.MAFIA else "Citizens"
            )
        game.prepare_next_night()
    raise AssertionError("A simulated game exceeded the maximum possible quiet-round bound")


def simulate_games(games: int = 500, seed: int = 7) -> dict[str, int]:
    if not 1 <= games <= 10000:
        raise ValueError("Choose between 1 and 10000 simulated games.")
    rng = random.Random(seed)
    results = Counter({"Mafia": 0, "Citizens": 0, "Draw": 0})
    for number in range(games):
        results[simulate_match(rng, rng.randint(3, 6), passive=number % 10 == 0)] += 1
    return dict(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        results = simulate_games(args.games, args.seed)
    except ValueError as error:
        parser.error(str(error))
    print(f"Offline simulation: {args.games} games, seed {args.seed}")
    for result, total in results.items():
        print(f"  {result}: {total}")
    print(
        "PASS: every game ended; roles and player numbers stayed fixed; eliminated players never returned."
    )
    print(
        "Every tenth game is deliberately passive to check draw handling. No Discord token was used."
    )


if __name__ == "__main__":
    main()
