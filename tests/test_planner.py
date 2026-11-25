"""Planner, trigger and guard tests. No network: Jev is replaced by a stand-in."""

import json
import unittest
from unittest import mock

from iis_assignment import planner
from iis_assignment.game import Game
from iis_assignment.planner import Teammate, context, menu, rules
from iis_assignment.skills import Runner


def quiet(layout="open"):
    game = Game(layout)
    game.rush_schedule = {}
    return game


class FakeJev:
    """Prefers the given goals in order, else the first offered."""

    def __init__(self, *prefer, confidence=0.9, fail=False):
        self.prefer, self.confidence, self.fail, self.seen = prefer, confidence, fail, []

    def __call__(self, state, choices):
        self.seen.append((state, choices))
        if self.fail:
            raise OSError("no network")
        order = [n for n in self.prefer if n in choices] + [n for n in choices if n not in self.prefer]
        return {"ranking": [(n, 1 / (i + 2)) for i, n in enumerate(order)], "confidence": self.confidence,
                "model": "fake", "tokens": 1, "cost": 0.5, "seconds": 0.1}


def play(mate, ticks, human="stay"):
    for _ in range(ticks):
        mate.game.step([human, mate.act()])
        mate.tick()


def duo(game, mate, limit=10**6):
    """Play a whole game with a rule-based stand-in for the human."""
    first = Runner(game, 0)
    while not game.done:
        if first.goal is None or first.status.startswith("Stuck"):
            name = rules(game, 0, menu(game, 0))[0][0]
            first.set(None if name == "wait" else name)
        before = game.state
        game.step([first.act(), mate.act()])
        first.after_step(before)
        mate.tick()
    return game.score


class ContextTest(unittest.TestCase):
    def test_context_is_json_and_menu_only_offers_possible_goals(self):
        game = quiet("split")
        json.dumps(context(game, 1, "start", []))
        self.assertEqual(set(menu(game, 1)), {"pass_dish", "wait"})
        self.assertIn("no pot to use it with", menu(game, 1)["pass_dish"]["facts"])

    def test_facts_describe_consequences(self):
        game = quiet()
        mate = Teammate(game, "instructions")
        for name in ("add_onion_pot1", "add_onion_pot1"):
            mate.instruct(name)
            while game.partner.goal:
                play(mate, 1)
        game.rush = ("tomato soup", 99)
        facts = {n: c["facts"] for n, c in menu(game, 1).items()}
        self.assertIn("completes mixed soup, worth 30", facts["add_tomato_pot1"])
        self.assertIn("rules out the rush order", facts["add_tomato_pot1"])
        self.assertIn("tomato soup (40 points) would need 2 more tomatoes", facts["add_tomato_pot2"])


class TeammateTest(unittest.TestCase):
    def test_rules_teammate_scores_alone(self):
        game = quiet()
        mate = Teammate(game, "rules")
        mate.tick()
        play(mate, 300)
        self.assertGreaterEqual(game.score, 80)

    def test_rules_pair_beats_one_chef_and_never_stalls(self):
        for layout in ("open", "split"):
            for seed in range(5):
                game = Game(layout, seed)
                self.assertGreaterEqual(duo(game, Teammate(game, "rules")), 100, (layout, seed))

    def test_jev_choice_is_carried_out_and_calls_stay_low(self):
        jev = FakeJev("serve", "collect_pot1", "add_tomato_pot1")
        game = quiet()
        mate = Teammate(game, "jev", ask=jev)
        mate.tick()
        self.assertEqual(game.partner.name, "add_tomato_pot1")
        play(mate, 120)
        self.assertGreaterEqual(game.score, 20)
        self.assertLess(mate.calls, 30)

    def test_failure_falls_back_to_rules_and_backs_off(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev(fail=True))
        mate.tick()
        self.assertEqual(mate.decision["source"], "rules")
        self.assertIsNotNone(game.partner.goal)
        play(mate, planner.RETRY_TICKS - 1)
        self.assertEqual(mate.calls, 1)

    def test_call_cap(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev())
        mate.calls = planner.MAX_CALLS
        mate.tick()
        self.assertEqual((mate.calls, mate.decision["source"]), (planner.MAX_CALLS, "rules"))

    def test_rush_triggers_replanning_mid_goal(self):
        game, jev = Game(), FakeJev("add_onion_pot1")
        game.rush_schedule = {2: "tomato soup"}
        mate = Teammate(game, "jev", ask=jev)
        mate.tick()
        jev.prefer = ("add_tomato_pot2",)
        play(mate, 2)
        self.assertEqual(jev.seen[-1][0]["why_deciding_now"], "a rush order started")
        self.assertEqual(game.partner.name, "add_tomato_pot2")

    def test_instruction_is_not_overridden(self):
        game = Game()
        game.rush_schedule = {3: "tomato soup"}
        mate = Teammate(game, "jev", ask=FakeJev("add_tomato_pot2"))
        mate.instruct("add_onion_pot1")
        play(mate, 6)
        self.assertEqual((game.partner.name, mate.owner), ("add_onion_pot1", "human"))
        self.assertEqual(mate.calls, 0)

    def test_veto_blocks_then_expires_and_asking_lifts_it(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev("add_onion_pot1"))
        mate.tick()
        mate.veto("add_onion_pot1")
        self.assertNotEqual(game.partner.name, "add_onion_pot1")
        self.assertNotIn("add_onion_pot1", mate.ask.seen[-1][1])
        mate.instruct("add_onion_pot1")
        self.assertEqual(mate.vetoes, {})
        mate.veto("add_onion_pot1")
        play(mate, planner.VETO_TICKS)
        self.assertEqual(mate.vetoes, {})
        with self.assertRaises(ValueError):
            mate.veto("fly")

    def test_unsure_asks_then_goes_ahead(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev("add_onion_pot1", confidence=0.01))
        mate.tick()
        self.assertEqual(mate.view()["asking"]["choices"][0], "add_onion_pot1")
        self.assertIsNone(game.partner.goal)
        play(mate, planner.ASK_TICKS - 1)
        self.assertIsNone(game.partner.goal)
        play(mate, 1)
        self.assertEqual(game.partner.name, "add_onion_pot1")
        self.assertEqual(mate.calls, 1)

    def test_unsure_takes_the_players_answer(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev("add_onion_pot1", confidence=0.01))
        mate.tick()
        mate.instruct("add_tomato_pot2")
        self.assertIsNone(mate.asking)
        self.assertEqual(game.partner.name, "add_tomato_pot2")

    def test_late_answer_is_revalidated(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev())
        choices = menu(game, 1)
        mate._pending = 7
        game.state.players[1].held_object = None
        answer = FakeJev("serve", "add_onion_pot2")(None, {**choices, "serve": choices["wait"]})
        mate._receive(7, "test", {**choices, "serve": choices["wait"]}, answer, None)  # serve is impossible now
        self.assertEqual(game.partner.name, "add_onion_pot2")

    def test_answer_overtaken_by_the_player_is_dropped(self):
        game = quiet()
        mate = Teammate(game, "jev", ask=FakeJev())
        mate._pending = 7
        mate.instruct("add_tomato_pot1")
        mate._receive(7, "test", menu(game, 1), FakeJev("add_onion_pot2")(None, menu(game, 1)), None)
        self.assertEqual(game.partner.name, "add_tomato_pot1")

    def test_stop_and_modes(self):
        game = quiet()
        mate = Teammate(game, "rules")
        mate.tick()
        mate.instruct(None)
        play(mate, 3)
        self.assertIsNone(game.partner.goal)
        mate.set_mode("instructions")
        play(mate, 20)
        self.assertIsNone(game.partner.goal)
        with self.assertRaises(ValueError):
            mate.set_mode("magic")

    def test_background_call_does_not_block(self):
        import threading
        gate, lock, game = threading.Event(), threading.Lock(), quiet()
        fake = FakeJev("add_onion_pot1")

        def slow(state, choices):
            gate.wait(5)
            return fake(state, choices)

        mate = Teammate(game, "jev", lock, ask=slow)
        with lock:
            mate.tick()
            self.assertTrue(mate.view()["thinking"])
            play(mate, 3)
            self.assertEqual(mate.calls, 1)
        gate.set()
        for _ in range(100):
            with lock:
                if not mate.view()["thinking"]:
                    break
            threading.Event().wait(0.02)
        self.assertEqual(game.partner.name, "add_onion_pot1")

    def test_missing_key_is_none(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(planner, "ROOT", planner.Path("nowhere")):
            self.assertIsNone(planner.api_key())


if __name__ == "__main__":
    unittest.main()
