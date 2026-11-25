"""Run with: uv run python -m unittest discover tests"""

import unittest

from iis_assignment.game import HORIZON, RECIPES, Game

KEYS = {"u": "up", "d": "down", "l": "left", "r": "right", "e": "interact", ".": "stay"}


def play(game, keys, chef=0):
    for k in keys:
        actions = ["stay", "stay"]
        actions[chef] = KEYS[k]
        game.step(actions)


def cook_onion_soup(game):
    play(game, "ulle" + "rue" + "lle" + "rue" + "lle" + "rue")  # three onions into the left pot
    play(game, "lddle" + "." * 15 + "uurue" + "ddd" + "rrd")  # dish, collect, walk to the hatch


class GameTest(unittest.TestCase):
    def test_human_can_cook_and_serve(self):
        g = Game("open")
        g.order_schedule = {}
        cook_onion_soup(g)
        self.assertEqual(g.hint(0), "serve soup")
        play(g, "e")
        self.assertEqual(g.score, 20)

    def test_rush_pays_double_once_then_clears(self):
        g = Game("open")
        g.order_schedule = {5: ("onion soup", 90)}
        cook_onion_soup(g)
        play(g, "e")
        self.assertEqual(g.score, 40)
        self.assertEqual(g.active, [])
        self.assertEqual([(o["status"], o["chef"]) for o in g.orders], [("completed", 0)])
        self.assertFalse(any("ush" in e["text"] for e in g.log))

    def test_rush_expires(self):
        g = Game("open")
        g.order_schedule = {5: ("tomato soup", 90)}
        play(g, "." * 5)
        self.assertEqual(g.snapshot()["due"], {"tomato soup": 90})
        play(g, "." * 90)
        self.assertEqual(g.active, [])
        self.assertEqual(g.snapshot()["orders"][0]["status"], "expired")

    def test_same_seed_same_rushes(self):
        self.assertEqual(Game(seed=3).order_schedule, Game(seed=3).order_schedule)
        for seed in range(200):
            schedule = Game(seed=seed).order_schedule
            self.assertGreaterEqual(len(schedule), 5)
            for t in range(HORIZON):
                open_now = [recipe for start, (recipe, n) in schedule.items() if start <= t < start + n]
                self.assertLessEqual(len(open_now), 3)
                self.assertEqual(len(open_now), len(set(open_now)), "open orders never share a recipe")
            self.assertTrue(all(start + n <= HORIZON for start, (_, n) in schedule.items()))

    def test_orders_usually_overlap(self):
        overlaps = 0
        for seed in range(50):
            s = Game(seed=seed).order_schedule
            overlaps += sum(1 for t in range(HORIZON) if sum(a <= t < a + n for a, (_, n) in s.items()) >= 2)
        self.assertGreater(overlaps / 50, 120)

    def test_queue_fills_and_expires_independently(self):
        g = Game("open")
        g.order_schedule = {3: ("tomato soup", 20), 5: ("onion soup", 90), 7: ("mixed soup", 90)}
        play(g, "." * 10)
        self.assertEqual([o["recipe"] for o in g.active], ["tomato soup", "onion soup", "mixed soup"])
        cook_onion_soup(g)
        play(g, "e")
        self.assertEqual(g.score, 40)
        self.assertEqual({o["recipe"]: o["status"] for o in g.orders}, {"tomato soup": "expired", "onion soup": "completed", "mixed soup": "active"})
        self.assertEqual(set(RECIPES), set(o["recipe"] for o in g.orders))

    def test_split_kitchen_handover(self):
        g = Game("split")
        play(g, "llerrre")  # chef 1: tomato onto the shared counter
        self.assertIsNone(g.state.players[0].held_object)
        play(g, "lle", chef=1)
        self.assertEqual(g.state.players[1].held_object.name, "tomato")

    def test_game_ends(self):
        g = Game()
        play(g, "." * (HORIZON + 5))
        self.assertTrue(g.done)
        self.assertEqual(g.state.timestep, HORIZON)


if __name__ == "__main__":
    unittest.main()
