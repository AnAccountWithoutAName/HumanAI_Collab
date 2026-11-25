"""Goal layer tests."""

import itertools
import random
import unittest

from iis_assignment.game import LAYOUTS, RECIPES, Game
from iis_assignment.skills import Runner, goals, options


def run(game, name, limit=80):
    """Give chef 2 a goal and play until it finishes; returns ticks used."""
    game.partner.set(name)
    for t in range(1, limit + 1):
        game.step(["stay", game.partner.act()])
        if game.partner.goal is None:
            return t
    raise AssertionError(f"{name}: {game.partner.status}")


def quiet(layout):
    game = Game(layout)
    game.rush_schedule = {}
    return game


def reasons(game):
    return {o["name"]: o["reason"] for o in options(game, 1)}


class SkillsTest(unittest.TestCase):
    def test_chef2_cooks_every_recipe(self):
        for name, recipe in RECIPES.items():
            game = quiet("open")
            for ingredient in recipe["ingredients"]:
                run(game, f"add_{ingredient}_pot1")
            run(game, "collect_pot1")
            run(game, "serve")
            self.assertEqual(game.score, recipe["points"], name)

    def test_tick_estimate_is_exact_when_unobstructed(self):
        game = quiet("open")
        game.step(["down", "stay"])
        for name in ("add_onion_pot2", "add_onion_pot2", "add_tomato_pot2", "collect_pot2", "serve"):
            estimate = next(o["ticks"] for o in options(game, 1) if o["name"] == name)
            self.assertEqual(run(game, name), estimate, name)

    def test_frees_hands_when_holding_the_wrong_item(self):
        game = quiet("open")
        game.partner.set("add_onion_pot1")
        while not game.state.players[1].held_object:
            game.step(["stay", game.partner.act()])
        run(game, "add_tomato_pot1")
        self.assertEqual(game.state.objects[game.pots[0]].ingredients, ["tomato"])

    def test_infeasible_goals_say_why(self):
        why = reasons(quiet("split"))
        self.assertEqual(why["add_onion_pot1"], "no onion within reach")
        self.assertEqual(why["collect_pot1"], "not cooking")
        self.assertEqual(why["serve"], "no soup within reach")
        self.assertEqual(why["put_down"], "hands are empty")

    def test_refuses_to_spoil_a_pot(self):
        game = quiet("open")
        run(game, "add_tomato_pot1")
        run(game, "add_tomato_pot1")
        why = reasons(game)
        self.assertEqual(why["add_onion_pot1"], "makes no recipe")
        self.assertIsNone(why["add_tomato_pot1"])

    def test_waits_when_pot_is_blocked_then_continues(self):
        game = quiet("open")
        game.step(["up", "stay"])  # chef 1 parks under pot 1
        game.partner.set("add_onion_pot1")
        for _ in range(40):
            game.step(["stay", game.partner.act()])
        self.assertEqual(game.partner.status, "Waiting for the other chef to move")
        game.step(["down", game.partner.act()])
        self.assertLess(run(game, "add_onion_pot1"), 20)

    def test_idle_chef_steps_aside_when_walked_into(self):
        game = quiet("open")
        for _ in range(3):
            game.step(["right", game.partner.act()])  # chef 1 walks up beside chef 2 at (6, 2)
        for _ in range(5):
            game.step(["stay", game.partner.act()])  # standing there is not a request
        self.assertEqual(game.state.players[1].position, (6, 2))
        for _ in range(2):
            game.step(["right", game.partner.act()])
        self.assertNotEqual(game.state.players[1].position, (6, 2))

    def test_split_kitchen_needs_both_chefs(self):
        game = quiet("split")
        first = Runner(game, 0)
        for _ in range(3):
            first.set("pass_onion")
            while first.goal:
                before = game.state
                game.step([first.act(), "stay"])
                first.after_step(before)
            run(game, "add_onion_pot1")
        run(game, "collect_pot1")
        run(game, "serve")
        self.assertEqual(game.score, 20)

    def test_serving_an_unlisted_mix_scores_zero(self):
        game = quiet("open")
        run(game, "add_tomato_pot1")
        run(game, "add_tomato_pot1")
        game.fits = lambda *_: True  # bypass the safeguard
        run(game, "add_onion_pot1")
        run(game, "collect_pot1")
        run(game, "serve")
        self.assertEqual(game.score, 0)

    def test_random_goals_never_crash_or_freeze(self):
        for layout, seed in itertools.product(LAYOUTS, range(25)):
            game, rng = Game(layout, seed), random.Random(seed)
            first, finished, idle = Runner(game, 0), 0, 0
            while not game.done:
                for runner in (first, game.partner):
                    if runner.goal is None or rng.random() < 0.02:
                        ok = [o["name"] for o in options(game, runner.chef) if not o["reason"]]
                        runner.set(rng.choice(ok) if ok else None)
                before = game.state
                game.step([first.act(), game.partner.act()])
                done = [first.after_step(before), any(e["tick"] == before.timestep + 1 and "finished" in e["text"] for e in game.log)]
                finished += sum(map(bool, done))
                # In the split kitchen a chef may rightly wait for a soup; in the open one nothing should stall.
                idle = 0 if any(done) else idle + 1
                if layout == "open":
                    self.assertLess(idle, 80, (layout, seed, first.status, game.partner.status))
            self.assertGreater(finished, 10 if layout == "open" else 3, (layout, seed))

    def test_unknown_goal_is_rejected(self):
        with self.assertRaises(KeyError):
            Game().partner.set("fly")
        self.assertNotIn("pass_onion", goals(Game("open")))


if __name__ == "__main__":
    unittest.main()
