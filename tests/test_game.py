"""Run with: uv run python -m unittest discover tests"""

import unittest

from iis_assignment.game import HORIZON, RUSH_TICKS, Game

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
        g.rush_schedule = {}
        cook_onion_soup(g)
        self.assertEqual(g.hint(0), "serve soup")
        play(g, "e")
        self.assertEqual(g.score, 20)

    def test_rush_pays_double_once_then_clears(self):
        g = Game("open")
        g.rush_schedule = {5: "onion soup"}
        cook_onion_soup(g)
        play(g, "e")
        self.assertEqual(g.score, 40)
        self.assertIsNone(g.rush)
        self.assertEqual([(o["status"], o["chef"]) for o in g.orders], [("completed", 0)])
        self.assertFalse(any("ush" in e["text"] for e in g.log))

    def test_rush_expires(self):
        g = Game("open")
        g.rush_schedule = {5: "tomato soup"}
        play(g, "." * 5)
        self.assertEqual(g.snapshot()["rush"], {"recipe": "tomato soup", "left": RUSH_TICKS})
        play(g, "." * RUSH_TICKS)
        self.assertIsNone(g.rush)
        self.assertEqual(g.snapshot()["orders"][0]["status"], "expired")

    def test_same_seed_same_rushes(self):
        self.assertEqual(Game(seed=3).rush_schedule, Game(seed=3).rush_schedule)
        for seed in range(50):
            self.assertGreaterEqual(len(Game(seed=seed).rush_schedule), 2)

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
