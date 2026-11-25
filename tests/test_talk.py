"""Conversation tests. No network: Haiku and Jev are replaced by stand-ins."""

import unittest

from iis_assignment.game import Game
from iis_assignment import planner
from iis_assignment.planner import Teammate, context, menu
from iis_assignment.skills import goals
from iis_assignment.talk import TOPICS, grounded, understand_schema
from test_planner import FakeJev, play, quiet

ACT = {"type": "chat", "goal": "none", "recipe": "none", "rule": "none", "topics": [], "until": "cancelled", "candidates": []}


class FakeHaiku:
    """Returns the queued act for messages; echoes the draft (or `say`) for wording."""

    def __init__(self, *acts, say=None, fail=False):
        self.acts, self.say, self.fail = [{**ACT, **a} for a in acts], say, fail

    def __call__(self, system, user, schema):
        if self.fail:
            raise OSError("down")
        meta = {"cost": 0.1}
        if "say" in schema["properties"]:
            import json
            return {"say": self.say or json.loads(user)["draft"]}, meta
        return self.acts.pop(0), meta


class Scored(FakeJev):
    """Gives exact probabilities; everything else gets 0."""

    def __init__(self, help="none", **scores):
        super().__init__()
        self.scores, self.help, self.asks = scores, help, []

    def __call__(self, state, choices, help_choices=None):
        self.asks.append(help_choices)
        answer = super().__call__(state, choices)
        answer["ranking"] = sorted(((n, self.scores.get(n, 0)) for n in choices), key=lambda kv: -kv[1])
        answer["help"] = self.help
        return answer


def setup(layout="open", jev=None, haiku=None, mode="jev", rush=None):
    game = quiet(layout)
    if rush:
        game.open_order(rush, 90)
    return game, Teammate(game, mode, ask=jev or Scored(), talk_ask=haiku or FakeHaiku())


def last(mate):
    return mate.talk.chat[-1]["text"]


class VerdictTest(unittest.TestCase):
    def test_refuses_the_impossible_and_says_what_would_help(self):
        game, mate = setup()
        mate.propose("serve")
        self.assertIsNone(game.partner.goal)
        self.assertIn("I can't serve soup: no soup within reach", last(mate))
        self.assertIsNone(mate.talk.card)  # nothing to insist on

    def test_accepts_a_reasonable_request(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.4, add_tomato_pot1=0.6))
        mate.propose("add_onion_pot1")
        self.assertEqual((game.partner.name, mate.owner), ("add_onion_pot1", "human"))
        self.assertIn("On it", last(mate))

    def test_objects_with_a_cost_and_the_player_can_insist(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup")
        mate.propose("add_onion_pot1")
        self.assertIsNone(game.partner.goal)
        self.assertEqual(mate.talk.card["kind"], "object")
        self.assertIn("rules out the tomato soup order in this pot", last(mate))
        mate.answer("insist")
        self.assertEqual((game.partner.name, mate.owner), ("add_onion_pot1", "human"))
        self.assertIsNone(mate.talk.card)

    def test_judge_is_not_told_what_was_asked(self):
        _, mate = setup()
        mate.propose("add_onion_pot2")
        state = mate.ask.seen[-1][0]
        self.assertNotIn("asked", str(state))
        self.assertNotIn("onion to pot 2", str(state))

    def test_player_can_take_the_suggestion(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup")
        mate.propose("add_onion_pot1")
        mate.answer("idea")
        self.assertEqual(game.partner.name, "add_tomato_pot1")

    def test_no_objection_without_a_nameable_cost(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.0, add_tomato_pot1=1.0))
        mate.propose("add_onion_pot1")
        self.assertEqual(game.partner.name, "add_onion_pot1")

    def test_spoiling_a_pot_is_done_only_after_a_warning(self):
        game, mate = setup()
        for _ in range(2):
            mate.instruct("add_tomato_pot1")
            while game.partner.goal:
                game.step(["stay", mate.act()])
        mate.propose("add_onion_pot1")
        self.assertEqual(mate.talk.card["kind"], "warn")
        self.assertIsNone(game.partner.goal)
        mate.answer("insist")
        while mate.owner == "human" and game.partner.goal:
            mate.talk.tick()
            game.step(["stay", mate.act()])
        self.assertEqual(sorted(game.state.objects[game.pots[0]].ingredients), ["onion", "tomato", "tomato"])
        menu(game, 1)  # a spoiled pot must not break the menu
        mate.mode = "rules"
        play(mate, 100)

    def test_typed_yes_never_confirms_a_harmful_job(self):
        game, mate = setup(haiku=FakeHaiku({"type": "go_ahead"}, {"type": "withdraw"}))
        for _ in range(2):
            mate.instruct("add_tomato_pot1")
            while game.partner.goal:
                game.step(["stay", mate.act()])
        mate.propose("add_onion_pot1")
        mate.talk.hear("no wait, don't")  # misread as yes
        self.assertIsNone(game.partner.goal)
        self.assertEqual(mate.talk.card["kind"], "warn")
        mate.talk.hear("drop it")
        self.assertIsNone(mate.talk.card)
        self.assertIsNone(game.partner.goal)

    def test_objection_can_be_withdrawn(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup")
        mate.propose("add_onion_pot1")
        mate.answer("withdraw")
        self.assertIsNone(game.partner.goal)
        self.assertIsNone(mate.talk.card)

    def test_baseline_obeys_without_objecting(self):
        game, mate = setup(mode="instructions", rush="tomato soup")
        mate.propose("add_onion_pot1")
        self.assertEqual(game.partner.name, "add_onion_pot1")
        self.assertEqual(mate.calls, 0)

    def test_rules_can_object_too(self):
        game, mate = setup(mode="rules", rush="tomato soup")
        for _ in range(2):
            mate.instruct("add_tomato_pot1")
            while game.partner.goal:
                game.step(["stay", mate.act()])
        mate.propose("add_onion_pot2")
        self.assertEqual(mate.talk.card["prefer"], "add_tomato_pot1")

    def test_if_the_judge_fails_the_player_is_obeyed(self):
        game, mate = setup(jev=FakeJev(fail=True))
        mate.propose("add_onion_pot1")
        self.assertEqual(game.partner.name, "add_onion_pot1")

    def test_card_disappears_when_it_no_longer_applies(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup")
        mate.propose("add_onion_pot1")
        game.state.timestep = 300
        mate.tick()
        self.assertIsNone(mate.talk.card)
        mate.answer("insist")  # harmless

    def test_stuck_instruction_is_reported_once(self):
        game, mate = setup()
        mate.instruct("collect_pot1")
        play(mate, 10)
        self.assertEqual(sum("can't finish" in c["text"] for c in mate.talk.chat), 1)

    def test_unknown_goal_and_answer(self):
        _, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup")
        with self.assertRaises(ValueError):
            mate.propose("fly")
        mate.propose("add_onion_pot1")
        with self.assertRaises(ValueError):
            mate.answer("maybe")


class MessageTest(unittest.TestCase):
    def test_typed_goal(self):
        game, mate = setup(haiku=FakeHaiku({"type": "goal", "goal": "add_onion_pot2"}), jev=Scored(add_onion_pot2=1))
        mate.talk.hear("onion in the right pot pls")
        self.assertEqual(game.partner.name, "add_onion_pot2")

    def test_unclear_offers_choices(self):
        _, mate = setup(haiku=FakeHaiku({"type": "unclear", "candidates": ["add_onion_pot1", "add_onion_pot2"]}))
        mate.talk.hear("onion")
        self.assertEqual([c["name"] for c in mate.talk.view()["card"]["choices"]], ["add_onion_pot1", "add_onion_pot2"])

    def test_clarification_can_be_dismissed(self):
        _, mate = setup(haiku=FakeHaiku({"type": "unclear", "candidates": ["add_onion_pot1", "add_onion_pot2"]}))
        mate.talk.hear("onion")
        mate.answer("withdraw")
        self.assertIsNone(mate.talk.card)

    def test_haiku_down_or_odd_replies_are_harmless(self):
        for haiku in (FakeHaiku(fail=True), FakeHaiku({"type": "goal", "goal": "fly"}), FakeHaiku({"type": "order", "rule": "avoid"}),
                      FakeHaiku({"type": "go_ahead"}), FakeHaiku({"type": "help_ok"}), FakeHaiku({"type": "hack"})):
            game, mate = setup(haiku=haiku)
            mate.talk.hear("hello")
            self.assertEqual(mate.talk.chat[-1]["who"], "chef2")
            self.assertIsNone(game.partner.goal)

    def test_empty_and_long_messages(self):
        _, mate = setup(haiku=FakeHaiku({}))
        with self.assertRaises(ValueError):
            mate.talk.hear("   ")
        mate.talk.hear("x" * 5000)
        self.assertEqual(len(mate.talk.chat[0]["text"]), 300)

    def test_insist_and_stop_by_message(self):
        game, mate = setup(jev=Scored(add_onion_pot1=0.03, add_tomato_pot1=0.9), rush="tomato soup", haiku=FakeHaiku({"type": "go_ahead"}, {"type": "stop"}))
        mate.propose("add_onion_pot1")
        mate.talk.hear("just do it")
        self.assertEqual(game.partner.name, "add_onion_pot1")
        mate.talk.hear("stop")
        self.assertIsNone(game.partner.goal)

    def test_schema_matches_the_kitchen(self):
        schema = understand_schema(Game("split"))
        self.assertIn("pass_dish", schema["properties"]["goal"]["enum"])
        self.assertTrue(set(TOPICS))


class WhyTest(unittest.TestCase):
    def test_explains_current_job_from_the_decision_record(self):
        _, mate = setup(jev=Scored(add_tomato_pot1=0.8, add_onion_pot1=0.2))
        mate.tick()
        mate.talk.why()
        self.assertIn("80%", last(mate))
        self.assertIn("chef 2 is free", last(mate))

    def test_explains_why_not(self):
        _, mate = setup(jev=Scored(add_tomato_pot1=0.8, add_onion_pot1=0.2))
        mate.tick()
        mate.talk.why("add_onion_pot1")
        self.assertIn("20% against 80%", last(mate))
        mate.talk.why("serve")
        self.assertIn("not possible", last(mate))

    def test_human_goal_is_attributed_to_the_human(self):
        _, mate = setup()
        mate.instruct("add_onion_pot1")
        mate.talk.why()
        self.assertIn("You asked", last(mate))

    def test_no_job(self):
        _, mate = setup()
        mate.talk.why()
        self.assertIn("no job", last(mate))

    def test_wording_beyond_the_brief_is_replaced(self):
        for say in ("The rush ends in 12 ticks!", "We already have a mixed soup.", "", "word " * 80):
            _, mate = setup(haiku=FakeHaiku(say=say))
            mate.propose("serve")
            if say:
                self.assertEqual(mate.talk.chat[-1]["via"], "template", say)
            self.assertIn("I can't serve soup", last(mate))

    def test_grounded(self):
        brief = {"costs": ["rush tomato soup has 41 ticks left"], "n": 3}
        self.assertTrue(grounded("The rush tomato soup has 41 ticks left.", brief))
        self.assertTrue(grounded("It needs three more.", brief))
        self.assertFalse(grounded("It needs two more.", brief))
        self.assertFalse(grounded("Use an onion.", brief))
        answer = {"verdict": "answer", "rush": "tomato soup"}
        self.assertFalse(grounded("I was weighing a tomato soup.", answer))
        self.assertFalse(grounded("I'll do tomato soup.", answer))
        self.assertTrue(grounded("The order is tomato soup.", answer))
        why = {"verdict": "explain the current job", "facts": ["the soup is ready"]}
        self.assertFalse(grounded("I'm doing it because the soup is ready.", why))
        self.assertTrue(grounded("What I had to go on: the soup is ready.", why))


class RuleTest(unittest.TestCase):
    def test_rule_is_enforced_whatever_the_planner_says(self):
        game, mate = setup(jev=Scored(add_tomato_pot1=0.9, add_onion_pot1=0.1), haiku=FakeHaiku({"type": "order", "rule": "avoid", "topics": ["tomato"]}))
        mate.talk.hear("leave the tomatoes to me")
        self.assertIn("Understood: leave tomatoes alone", mate.talk.chat[1]["text"])
        self.assertEqual(game.partner.name, "add_onion_pot1")
        mate.cancel_orders(mate.orders[0]["id"])
        self.assertEqual(game.partner.name, "add_tomato_pot1")

    def test_only_rule(self):
        _, mate = setup()
        mate.add_order("only", ["collect", "serve"], "cancelled")
        self.assertTrue(mate.blocked("add_onion_pot1"))
        self.assertIsNone(mate.blocked("collect_pot1"))
        self.assertIsNone(mate.blocked("put_down"))

    def test_rule_ends_with_the_rush(self):
        game, mate = setup(rush="tomato soup")
        mate.add_order("avoid", ["pot1"], "orders_end")
        self.assertEqual(len(mate.orders), 1)
        game.active[0]["status"] = "expired"
        mate.tick()
        self.assertEqual(mate.orders, [])

    def test_costly_rule_raises_one_conflict_and_is_kept_until_answered(self):
        game, mate = setup(jev=Scored(add_tomato_pot1=0.9, add_onion_pot1=0.1), rush="tomato soup")
        mate.add_order("avoid", ["tomato"], "cancelled")
        self.assertEqual(mate.talk.card["kind"], "conflict")
        self.assertEqual(game.partner.name, "add_onion_pot1")
        mate.answer("withdraw")
        self.assertEqual(game.partner.name, "add_onion_pot1")
        mate.replan("again")
        self.assertIsNone(mate.talk.card)

    def test_idle_by_choice_is_not_a_conflict(self):
        _, mate = setup(jev=Scored(add_tomato_pot1=0.9))
        mate.add_order("only", ["serve"], "cancelled")
        self.assertIsNone(mate.talk.card)

    def test_rule_until_rush_without_a_rush_says_so(self):
        _, mate = setup()
        mate.add_order("avoid", ["pot1"], "orders_end")
        self.assertIn("no orders now", mate.talk.chat[0]["text"])

    def test_conflict_exception_is_for_one_job(self):
        game, mate = setup(jev=Scored(add_tomato_pot1=0.9, add_onion_pot1=0.1), rush="tomato soup")
        mate.add_order("avoid", ["tomato"], "cancelled")
        mate.answer("insist")
        self.assertEqual(game.partner.name, "add_tomato_pot1")
        self.assertIn("Just this once", last(mate))
        self.assertEqual(len(mate.orders), 1)

    def test_conflict_can_cancel_the_rule(self):
        game, mate = setup(jev=Scored(add_tomato_pot1=0.9, add_onion_pot1=0.1), rush="tomato soup")
        mate.add_order("avoid", ["tomato"], "cancelled")
        mate.answer("cancel")
        self.assertEqual(mate.orders, [])
        self.assertEqual(game.partner.name, "add_tomato_pot1")


class SoupTest(unittest.TestCase):
    def test_make_restricts_adding_until_served(self):
        game, mate = setup(haiku=FakeHaiku({"type": "make", "recipe": "tomato soup"}), jev=Scored(add_onion_pot1=0.9, add_tomato_pot2=0.1))
        mate.talk.hear("but we need tomato soup")
        self.assertIn("3 tomatoes", mate.talk.chat[-1]["text"])
        self.assertEqual(game.partner.name, "add_tomato_pot2")
        game.served.append("tomato soup")
        mate.tick()
        self.assertEqual(mate.orders, [])

    def test_make_against_the_rush_is_questioned(self):
        game, mate = setup(rush="tomato soup")
        mate.make("onion soup")
        self.assertEqual((mate.talk.card["kind"], mate.orders), ("make", []))
        json_safe(mate)
        mate.answer("idea")
        self.assertEqual(mate.orders[0]["topics"], ["tomato soup"])
        mate.make("onion soup")
        mate.answer("insist")
        self.assertEqual(mate.orders[0]["topics"], ["onion soup"])
        self.assertIsNone(mate.talk.card)

    def test_make_the_order(self):
        _, mate = setup(rush="mixed soup")
        mate.make("an_order")
        self.assertEqual(mate.orders[0]["topics"], ["mixed soup"])
        self.assertIsNone(mate.blocked("add_onion_pot1"))
        _, mate = setup()
        mate.make("an_order")
        self.assertIn("no open order", last(mate))

    def test_make_the_order_asks_which_if_several(self):
        game, mate = setup(rush="mixed soup")
        game.open_order("tomato soup", 80)
        mate.make("an_order")
        self.assertEqual(mate.talk.card["kind"], "which_order")
        self.assertEqual(mate.orders, [])
        json_safe(mate)
        mate.make("tomato soup")
        self.assertEqual(mate.orders[0]["topics"], ["tomato soup"])
        self.assertIsNone(mate.talk.card)

    def test_make_an_order_that_cannot_be_met_is_questioned(self):
        game, mate = setup()
        game.open_order("onion soup", 12)
        game.open_order("tomato soup", 85)
        mate.make("onion soup")
        self.assertEqual((mate.talk.card["kind"], mate.talk.card["prefer"]), ("make", "tomato soup"))
        self.assertIn("12 ticks left but needs about", last(mate))

    def test_make_one_of_two_feasible_orders_is_accepted(self):
        game, mate = setup(rush="mixed soup")
        game.open_order("tomato soup", 85)
        mate.make("tomato soup")
        self.assertIsNone(mate.talk.card)
        self.assertEqual(mate.orders[0]["topics"], ["tomato soup"])

    def test_skip_leaves_an_order_until_it_closes(self):
        game, mate = setup(haiku=FakeHaiku({"type": "skip", "recipe": "mixed soup"}), rush="mixed soup")
        self.assertEqual(planner.worth(game, "mixed soup"), 60)
        mate.talk.hear("I'll do the mixed order")
        self.assertEqual(planner.worth(game, "mixed soup"), 30)
        self.assertEqual(mate.view()["skipped"], ["mixed soup"])
        self.assertEqual(context(game, 1, "", [])["orders"], [])
        game._close_order(game.active[0], "completed", 0)
        mate.tick()
        self.assertEqual((mate.orders, game.skipped), ([], set()))

    def test_skip_can_be_undone_and_make_overrides_it(self):
        game, mate = setup(rush="mixed soup")
        mate.skip("mixed soup")
        mate.cancel_orders(mate.orders[0]["id"])
        self.assertEqual(game.skipped, set())
        mate.skip("mixed soup")
        mate.make("mixed soup")
        self.assertEqual((game.skipped, [o["rule"] for o in mate.orders]), (set(), ["make"]))
        mate.skip("tomato soup")
        self.assertIn("no open order for tomato soup", last(mate))

    def test_skip_holds_back_jobs_that_only_serve_that_order(self):
        game, mate = setup(rush="tomato soup")
        mate.instruct("add_tomato_pot1")
        while game.partner.goal:
            game.step(["stay", mate.act()])
        mate.skip("tomato soup")
        self.assertTrue(mate.blocked("add_tomato_pot1"))
        self.assertIsNone(mate.blocked("add_onion_pot1"))
        self.assertIsNone(mate.blocked("add_tomato_pot2"))

    def test_free_clears_rules(self):
        _, mate = setup(haiku=FakeHaiku({"type": "free"}))
        mate.add_order("avoid", ["tomato"], "cancelled")
        mate.talk.hear("do something")
        self.assertEqual(mate.orders, [])

    def test_why_mentions_rules_and_recipe(self):
        _, mate = setup(jev=Scored(add_onion_pot1=0.9))
        mate.add_order("avoid", ["tomato"], "cancelled")
        mate.talk.why()
        self.assertIn("your rules: leave tomatoes alone", last(mate))
        self.assertIn("towards mixed soup", last(mate))

    def test_question_falls_back_to_a_status(self):
        _, mate = setup(haiku=FakeHaiku({"type": "question"}), rush="mixed soup")
        mate.talk.hear("what is the order?")
        self.assertIn("mixed soup", last(mate))


def json_safe(mate):
    import json
    json.dumps(mate.view())
    mate.talk._question()
    mate.talk.tick()


class HelpTest(unittest.TestCase):
    def test_asks_when_the_planner_says_so_and_thanks(self):
        game, mate = setup("split", jev=Scored(help="pass_onion", wait=1))
        mate.tick()
        self.assertEqual(mate.talk.request["item"], "onion")
        for action in ("left", "left", "up", "left", "interact", "right", "right", "right", "interact"):
            game.step([action, mate.act()])
            mate.tick()
        self.assertIsNone(mate.talk.request)
        self.assertIn("thanks for the onion", last(mate))

    def test_cannot_help_is_respected(self):
        game, mate = setup("split", jev=Scored(help="pass_onion", wait=1))
        mate.tick()
        mate.talk.answer_request(False)
        play(mate, 50)
        self.assertEqual(sum("Could you pass" in c["text"] for c in mate.talk.chat), 1)
        self.assertIn("onion", mate.ask.seen[-1][0]["human_cannot_help_with"])

    def test_one_request_at_a_time_and_withdrawn(self):
        _, mate = setup("split", jev=Scored(help="pass_onion", wait=1))
        mate.tick()
        mate.talk.answer_request(True)
        play(mate, 39)
        self.assertIsNotNone(mate.talk.request)
        play(mate, 2)
        self.assertIsNone(mate.talk.request)
        self.assertEqual(sum("Could you pass" in c["text"] for c in mate.talk.chat), 1)

    def test_never_asks_in_the_open_kitchen(self):
        _, mate = setup("open", jev=Scored(help="pass_onion", wait=1))
        mate.tick()
        self.assertIsNone(mate.talk.request)

    def test_rules_ask_for_the_rush_ingredient(self):
        _, mate = setup("split", mode="rules", rush="tomato soup")
        mate.tick()
        self.assertEqual(mate.talk.request["item"], "tomato")

    def test_view_is_json(self):
        import json
        _, mate = setup("split", jev=Scored(help="pass_onion", wait=1))
        mate.tick()
        json.dumps(mate.view())
        self.assertIn("serve", goals(mate.game))


if __name__ == "__main__":
    unittest.main()
