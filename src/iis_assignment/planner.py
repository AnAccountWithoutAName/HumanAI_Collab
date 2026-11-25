"""Chooses chef 2's goals: context builder, rule planner, Jev planner, and the guard around them."""

import json
import threading
import time
import urllib.request

from .keys import ROOT, api_key
from .skills import DISPENSERS, MOVES, ahead, goals, options, plan, reachable, route, shared_counters
from .talk import Talk, low

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.14"
MAX_CALLS = 150  # per game, so a bug cannot drain the credits
ASK_BELOW = 0.2  # Jev confidence under which chef 2 asks instead of acting
ASK_TICKS = 15  # then it goes ahead with its best option
WAIT_TICKS = 8
VETO_TICKS = 40
BLOCKED_TICKS = 12
RETRY_TICKS = 20  # pause after a failed call
OBJECT_BELOW, OBJECT_ABOVE = 0.1, 0.5  # object if the request scores under one and the alternative over the other
RULES_GAP = 20  # the same idea for rule scores
MODES = ("instructions", "rules", "jev")


# --- Context builder ---------------------------------------------------------------------------


def _held(game, chef):
    obj = game.state.players[chef].held_object
    return obj.name if obj else None


def _count(n, noun):
    return f"{n} more {noun}{'' if n == 1 else 'es' if noun == 'tomato' else 's'}"


def possible(game, inside):
    """Recipes a pot with these ingredients can still become, best paid first."""
    names = [n for n, r in game.recipes.items() if all(inside.count(i) <= r["ingredients"].count(i) for i in set(inside))]
    return sorted(names, key=lambda n: -worth(game, n))


def worth(game, recipe):
    points = game.recipes[recipe]["points"] if recipe else 0
    return points * 2 if game.rush and game.rush[0] == recipe else points


def advances(game, goal, recipe):
    """Whether an add goal keeps its pot on the way to this recipe."""
    soup = game.state.objects.get(goal.pot)
    inside = (list(soup.ingredients) if soup else []) + [goal.item]
    need = game.recipes[recipe]["ingredients"]
    return all(inside.count(i) <= need.count(i) for i in inside)


def toward(game, goal):
    """The recipe an add goal is working towards: the best paid one the pot could still become."""
    if not goal or goal.kind != "add":
        return None
    soup = game.state.objects.get(goal.pot)
    recipes = possible(game, (list(soup.ingredients) if soup else []) + [goal.item])
    return recipes[0] if recipes else None


def rest_ticks(game, chef, goal, recipe):
    """Ticks for this chef to fetch what the pot still lacks after `goal`, or None if out of reach."""
    soup = game.state.objects.get(goal.pot)
    need, total = list(game.recipes[recipe]["ingredients"]), 0
    for i in (list(soup.ingredients) if soup else []) + [goal.item]:
        need.remove(i)
    start = route(game, game.state.players[chef].position, (0, -1), {goal.pot})
    for item in need:
        out = start and route(game, start.pos, start.facing, set(game.mdp.terrain_pos_dict[DISPENSERS[item]]))
        start = out and route(game, out.pos, out.facing, {goal.pot})
        if not start:
            return None
        total += len(out.actions) + len(start.actions) + 2
    return total


def can_use(game, chef, tile):
    touch = {ahead(p, d) for p in reachable(game, chef) for d in MOVES}
    return bool(touch & set(game.mdp.terrain_pos_dict[tile]))


def facts(game, chef, goal, ticks):
    """Consequences of a goal, computed from the game. Shown to Jev and to the player."""
    out, left = [], game.horizon - game.state.timestep
    pot = game.state.objects.get(goal.pot) if goal.pot else None
    other = _held(game, 1 - chef)
    if ticks > left:
        out.append("cannot finish before the shift ends")
    if goal.kind == "add":
        inside = (list(pot.ingredients) if pot else []) + [goal.item]
        if len(inside) == 3:
            recipe = possible(game, inside)[0]
            out.append(f"completes {recipe}, worth {worth(game, recipe)}, which then starts cooking")
        else:
            for recipe in possible(game, inside):
                need = game.recipes[recipe]["ingredients"]
                more = [_count(need.count(i) - inside.count(i), i) for i in sorted(set(need)) if need.count(i) > inside.count(i)]
                rest = rest_ticks(game, chef, goal, recipe)
                time = f", about {ticks + rest} ticks in all to fill the pot alone" if rest is not None else ""
                tag = "the rush order, " if game.rush and game.rush[0] == recipe else ""
                out.append(f"{recipe} ({tag}{worth(game, recipe)} points) would need {' and '.join(more)}{time}")
        if game.rush and game.rush[0] not in possible(game, inside):
            out.append("rules out the rush order in this pot")
        if other == goal.item:
            out.append(f"the other chef is already carrying {goal.item}")
    elif goal.kind == "collect":
        recipe = possible(game, list(pot.ingredients))
        name = recipe[0] if recipe else "unlisted soup"
        out.append(f"{name}, worth {worth(game, recipe[0]) if recipe else 0} once served")
        if pot.is_ready:
            out.append("it is ready")
        elif pot.cook_time_remaining > ticks - 2:
            out.append(f"still cooking for {pot.cook_time_remaining} ticks, so it means waiting")
        if other == "dish":
            out.append("the other chef is already carrying a dish")
    elif goal.kind == "serve":
        held = game.state.players[chef].held_object
        soup = held if held and held.name == "soup" else next((o for o in game.state.objects.values() if o.name == "soup" and o.position not in game.pots), None)
        recipe = possible(game, list(soup.ingredients)) if soup else []
        out.append(f"scores {worth(game, recipe[0]) if recipe else 0} now")
    elif goal.kind == "pass":
        if not can_use(game, 1 - chef, "P"):
            out.append("the other chef has no pot to use it with")
        waiting = sum(1 for p in shared_counters(game) if p in game.state.objects and game.state.objects[p].name == goal.item)
        out.append(f"{waiting} already waiting on the shared counter")
    elif goal.kind == "put_down":
        out.append("only frees the hands")
    return "; ".join(out)


def menu(game, chef, vetoed=()):
    """Goals that are possible now: {name: {label, ticks, facts}}. Always includes wait."""
    out = {}
    for o in options(game, chef):
        if not o["reason"] and o["name"] not in vetoed:
            out[o["name"]] = {"label": o["label"], "ticks": o["ticks"], "facts": facts(game, chef, goals(game)[o["name"]], o["ticks"])}
    out["wait"] = {"label": "Wait", "ticks": WAIT_TICKS, "facts": "do nothing for a moment; scores nothing by itself"}
    return out


def tags(name, goal):
    """What a goal involves, for matching against the player's rules."""
    return {goal.kind, goal.item, name[-4:] if goal.pot else None}


def supports_rush(game, chef, goal):
    if not game.rush:
        return False
    wanted = game.recipes[game.rush[0]]["ingredients"]
    if goal.kind == "add":
        soup = game.state.objects.get(goal.pot)
        inside = (list(soup.ingredients) if soup else []) + [goal.item]
        return all(inside.count(i) <= wanted.count(i) for i in inside)
    if goal.kind == "collect":
        return sorted(game.state.objects[goal.pot].ingredients) == sorted(wanted) if goal.pot in game.state.objects else False
    held = game.state.players[chef].held_object
    return goal.kind == "serve" and bool(held) and held.name == "soup" and sorted(held.ingredients) == sorted(wanted)


def costs_of(game, asked, prefer):
    """Concrete, checkable drawbacks of doing `asked` rather than `prefer`. Empty means none can be named."""
    every, out = goals(game), []
    ticks = {n: plan(game, 1, every[n], force=True)[1] or 0 for n in (asked, prefer)}
    said = facts(game, 1, every[asked], ticks[asked])
    for phrase in ("cannot finish before the shift ends", "rules out the rush order in this pot", "the other chef has no pot to use it with"):
        if phrase in said:
            out.append(phrase)
    if supports_rush(game, 1, every[prefer]) and not supports_rush(game, 1, every[asked]):
        out.append(f"the rush {game.rush[0]} has {game.rush[1] - game.state.timestep} ticks left and this does not help it")
    done = facts(game, 1, every[prefer], ticks[prefer])
    if done.startswith("completes") and not said.startswith("completes"):
        out.append(f"{low(every[prefer].label)} {done.split(',')[0]} instead")
    if every[prefer].kind in ("serve", "collect") and every[asked].kind not in ("serve", "collect"):
        if every[prefer].kind == "serve" or "it is ready" in done:
            out.append("a finished soup would be left waiting")
    return out


def context(game, chef, why_now, recent):
    """The game state a planner sees, as JSON-ready data."""
    pots = {}
    for n, pos in enumerate(game.pots, 1):
        soup = game.state.objects.get(pos)
        status = "empty" if not soup else "ready" if soup.is_ready else f"cooking, {soup.cook_time_remaining} ticks left" if soup.is_cooking else "filling"
        pots[f"pot{n}"] = {"has": list(soup.ingredients) if soup else [], "status": status}
    return {
        "role": f"You choose the next goal for chef {chef + 1}, teammate of a human chef in a cooperative kitchen. Maximise the team's score before the shift ends.",
        "rules": "A pot needs 3 ingredients matching a recipe, cooks 20 ticks, then is collected with a dish and served.",
        "ticks_left": game.horizon - game.state.timestep,
        "score": game.score,
        "rush_order": game.rush and {"recipe": game.rush[0], "pays": "double", "ticks_left": game.rush[1] - game.state.timestep},
        "recipes": {n: {"needs": r["ingredients"], "points": r["points"]} for n, r in game.recipes.items()},
        "pots": pots,
        "you": {"holding": _held(game, chef), "current_goal": game.partner.name if chef == 1 else None},
        "human": {"holding": _held(game, 1 - chef), "recently": recent},
        "items_on_counters": [o.name for p, o in game.state.objects.items() if p not in game.pots],
        "why_deciding_now": why_now,
    }


# --- Planners ----------------------------------------------------------------------------------


def rules(game, chef, choices):
    """Hand-written baseline using the same menu. Returns [(name, score)] best first."""
    scored, left = {}, game.horizon - game.state.timestep
    for name, c in choices.items():
        if name == "wait":
            scored[name] = 5
            continue
        goal, ticks = goals(game)[name], c["ticks"]
        pot = game.state.objects.get(goal.pot) if goal.pot else None
        other = _held(game, 1 - chef)
        if ticks > left:
            score = 0
        elif goal.kind == "serve":
            score = 100
        elif goal.kind == "collect":
            score = (80 if pot.cook_time_remaining <= ticks else 25) - (60 if other == "dish" else 0)
        elif goal.kind == "add":
            inside = (list(pot.ingredients) if pot else []) + [goal.item]
            score = 20 + 10 * len(inside) + worth(game, possible(game, inside)[0]) / 2 - ticks
            score -= 15 if other == goal.item and len(inside) == 3 else 0
        elif goal.kind == "pass":
            useful = can_use(game, 1 - chef, "P") and not can_use(game, chef, "P")
            waiting = [game.state.objects[p].name for p in shared_counters(game) if p in game.state.objects]
            wanted = goal.item != "dish" or not can_use(game, 1 - chef, "D")
            score = 45 - 15 * len(waiting) if useful and wanted and len(waiting) < 2 else 0
            if useful and wanted and game.rush and goal.item in game.recipes[game.rush[0]]["ingredients"]:
                score += 5
        else:
            score = 1
        scored[name] = score
    return sorted(scored.items(), key=lambda kv: -kv[1])


def ask_jev(state, choices, help_choices=None):
    """One decision request. Returns {ranking, confidence, model, tokens, cost, seconds}; raises on failure."""
    body = {
        "model": JEV_MODEL,
        "state": state,
        "questions": {
            "goal": {
                "type": "choice",
                "instructions": "Which goal should this chef do next?",
                "criteria": {n: f"{c['label']}, about {c['ticks']} ticks. {c['facts']}" for n, c in choices.items()},
            }
        },
    }
    if help_choices:
        body["questions"]["help"] = {
            "type": "choice",
            "instructions": "Which request to the human would most help the team now? Chef 2 cannot fetch these itself.",
            "criteria": {"none": "Nothing: chef 2 already has what it needs or enough other work.", **help_choices},
        }
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    start = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(JEV_URL, json.dumps(body).encode(), headers), timeout=8) as reply:
        data = json.load(reply)
    answer, usage = data["answers"]["goal"], data.get("usage", {})
    return {
        "ranking": sorted(answer["probabilities"].items(), key=lambda kv: -kv[1]),
        "confidence": answer["confidence"],
        "help": data["answers"].get("help", {}).get("choice", "none"),
        "model": data["model"],
        "tokens": usage.get("input_tokens"),
        "cost": usage.get("cost", 0),
        "seconds": round(time.perf_counter() - start, 3),
    }


# --- Teammate: triggers, guard, log ------------------------------------------------------------


class Teammate:
    """Decides when to plan, checks every answer, and hands goals to the runner."""

    def __init__(self, game, mode, lock=None, log_path=None, ask=ask_jev, talk_ask=None):
        self.game, self.mode, self.ask, self.log_path = game, mode, ask, log_path
        self.lock = lock  # None: call the planner inline (tests, evaluation)
        self.runner = game.partner
        self.owner = None  # who set the current goal: "human" or "ai"
        self.decision = None  # last decision, for display
        self.asking = None  # {"until": tick, "choices": [...]} while waiting for the player
        self.vetoes = {}  # goal name: tick it expires
        self.calls, self.cost = 0, 0.0
        self.note = None
        self._pending = 0  # id of the call in flight, 0 if none
        self._ids = 0
        self._wait_until = self._retry_at = self._blocked = 0
        self._force = None
        self._recent = []
        self._seen = self._signature()
        self.orders = []  # the player's lasting rules
        self._order_ids = 0
        self._judging = 0  # id of the request being judged
        self.offline = not (talk_ask or (ask is ask_jev and api_key()))  # no Haiku
        self.talk = Talk(self, talk_ask) if talk_ask else Talk(self)

    # Player controls

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("unknown mode")
        self.mode, self._pending, self.asking, self.decision, self.note = mode, 0, None, None, None
        self._wait_until = 0
        if mode == "instructions" and self.owner == "ai":
            self.runner.set(None)
        else:
            self._force = "you changed how chef 2 decides"
        self.tick()

    def instruct(self, name, force=False):
        """Carry out the player's goal until done, stuck or stopped. None stops chef 2."""
        self.runner.set(name, force)
        self.asking, self._pending, self._judging = None, 0, 0
        self.talk.card = None
        self.owner = "human" if name else None
        self.vetoes.pop(name, None)
        self._wait_until = 0 if name else self.game.state.timestep + WAIT_TICKS

    def replan(self, why):
        self._force, self._wait_until, self.asking = why, 0, None
        self.tick()

    def background(self, work, done):
        """Run `work` off the lock, then done(result, error) on it. Inline when there is no lock."""

        def run():
            try:
                return work(), None
            except Exception as error:  # network, HTTP, malformed reply
                return None, f"{type(error).__name__}: {error}"

        if self.lock is None:
            return done(*run())

        def thread():
            result = run()
            with self.lock:
                done(*result)

        threading.Thread(target=thread, daemon=True).start()

    # Requests from the player: accept, refuse, warn or object

    def propose(self, name):
        game, now = self.game, self.game.state.timestep
        goal = goals(game).get(name)
        if goal is None:
            raise ValueError("unknown goal")
        if game.done:
            return
        self.talk.card, self._judging = None, 0
        reason, ticks = plan(game, 1, goal, force=True)
        if reason:
            return self.talk.refuse(name, reason)
        if self.mode == "instructions":  # the baseline obeys
            return self._accept(name)
        costs = []
        if plan(game, 1, goal)[0] == "makes no recipe":
            costs.append("it would spoil the pot, because those ingredients make no recipe")
        if ticks > game.horizon - now:
            costs.append("it cannot finish before the shift ends")
        if costs:
            return self.talk.warn(name, costs)
        choices = menu(game, 1)
        if len(choices) < 3:  # nothing to prefer instead
            return self._accept(name)
        if self.mode == "rules" or self.calls >= MAX_CALLS or now < self._retry_at:
            return self._judge(name, rules(game, 1, choices), RULES_GAP)
        self.calls += 1
        self._ids += 1
        self._judging = ticket = self._ids
        state = context(game, 1, "chef 2 is free", self._recent)  # not told of the request, which would sway the scores

        def done(answer, error):
            self.cost += (answer or {}).get("cost") or 0
            if ticket != self._judging:
                return  # the player has moved on
            self._judging = 0
            if error:
                return self._accept(name)  # no judgement available: the player's word stands
            self._judge(name, answer["ranking"], None, answer)

        self.background(lambda: self.ask(state, choices), done)

    def _judge(self, name, ranking, gap, answer=None):
        game, value = self.game, dict(ranking)
        reason = plan(game, 1, goals(game)[name])[0]
        if reason:
            return self.talk.refuse(name, reason)
        usable = [n for n, _ in ranking if n not in (name, "wait") and not self.blocked(n) and not plan(game, 1, goals(game)[n])[0]]
        prefer = usable[0] if usable else None
        asked = value.get(name, 0)
        strong = prefer and (value[prefer] - asked >= gap if gap else asked < OBJECT_BELOW and value[prefer] > OBJECT_ABOVE)
        costs = costs_of(game, name, prefer) if strong else []
        self._log({"event": "judged", "asked": name, "prefer": prefer, "ranking": ranking, "costs": costs, "objects": bool(costs),
                   "cost": (answer or {}).get("cost"), "seconds": (answer or {}).get("seconds")})
        if not costs:
            return self._accept(name)
        scores = None if gap else {"asked": f"{round(asked * 100)}%", "suggestion": f"{round(value[prefer] * 100)}%"}
        self.talk.object(name, prefer, costs, scores)

    def _accept(self, name, force=False):
        rule = self.blocked(name)
        label = goals(self.game)[name].label
        self.instruct(name, force)
        self.talk.say(f"On it: {label[0].lower()}{label[1:]}." + (f" Just this once, despite your rule ({rule['text']})." if rule else ""))

    def answer(self, reply):
        """The player settles an objection, warning or conflict."""
        card = self.talk.card
        if not card:
            return
        if card["kind"] == "clarify":
            self.talk.card = None if reply == "withdraw" else card
            return
        self.talk.card = None
        if reply not in ("insist", "idea", "withdraw", "cancel"):
            raise ValueError("unknown answer")
        if reply == "insist" and card["kind"] == "veto":
            self.veto(card["goal"], force=True)
        elif reply == "insist" and card["kind"] == "make":
            self.make(card["recipe"], force=True)
        elif reply == "insist":
            self._accept(card["goal"], force=card["kind"] == "warn")
        elif card["kind"] == "veto":
            self.talk.say("OK, carrying on with it.")
        elif card["kind"] == "make" and reply == "idea":
            self.make(card["prefer"], force=True)
        elif reply == "cancel" and card["kind"] == "conflict":
            self.cancel_orders(card["order"])
        elif reply == "idea" and card["kind"] == "object":
            if not plan(self.game, 1, goals(self.game)[card["prefer"]])[0]:
                self._accept(card["prefer"])
            else:
                self.talk.say("That is no longer possible, so I'll leave it.")
        elif reply == "withdraw":
            self.talk.say("OK, I'll keep to your rule." if card["kind"] == "conflict" else "OK, I won't.")
        else:
            raise ValueError("unknown answer")

    # Lasting rules

    def add_order(self, rule, topics, until):
        no_rush = until == "rush_end" and not self.game.rush
        if no_rush:
            until = "cancelled"
        names = {"onion": "onions", "tomato": "tomatoes", "pot1": "pot 1", "pot2": "pot 2", "collect": "collecting", "serve": "serving"}
        what = " and ".join(names.get(t, t) for t in topics)
        text = (f"leave {what} alone" if rule == "avoid" else f"only do {what}") + (" until the rush ends" if until == "rush_end" else "")
        self._order_ids += 1
        self.orders.append({"id": self._order_ids, "rule": rule, "topics": topics, "until": until, "text": text})
        self.talk.say(f"Understood: {text}." + (" There is no rush now, so it lasts until you undo it." if no_rush else " You can undo it below."))
        self._log({"event": "order", **self.orders[-1]})
        if self.owner == "ai" and self.runner.name and self.blocked(self.runner.name):
            self.runner.set(None)
        self.replan("you gave a lasting rule")

    def make(self, recipe, force=False):
        """The player wants a particular soup next."""
        game = self.game
        if recipe == "rush_order":
            recipe = game.rush and game.rush[0]
        if recipe not in game.recipes:
            return self.talk.say("There is no rush order right now. Name a soup and I'll work on it.")
        cooking = [p for p in game.pots if p in game.state.objects and game.state.objects[p].is_full]
        ready_for_rush = game.rush and any(sorted(game.state.objects[p].ingredients) == sorted(game.recipes[game.rush[0]]["ingredients"]) for p in cooking)
        if self.mode != "instructions" and not force and game.rush and game.rush[0] != recipe and not ready_for_rush:
            return self.talk.object_make(recipe, game.rush[0], [
                f"the rush {game.rush[0]} pays {worth(game, game.rush[0])} and has {game.rush[1] - game.state.timestep} ticks left",
                f"{recipe} pays {worth(game, recipe)}"])
        if force:  # the player has just weighed this up: do not raise the same conflict again
            self.talk._told.update({("conflict", n): game.state.timestep + 90 for n in goals(game)})
        self.orders = [o for o in self.orders if o["rule"] != "make"]
        self._order_ids += 1
        self.orders.append({"id": self._order_ids, "rule": "make", "topics": [recipe], "until": "made", "text": f"make {recipe}", "since": len(game.served)})
        self.vetoes = {n: t for n, t in self.vetoes.items() if goals(game)[n].kind != "add" or not advances(game, goals(game)[n], recipe)}
        needs = game.recipes[recipe]["ingredients"]
        counts = " and ".join(_count(needs.count(i), i).replace("more ", "") for i in sorted(set(needs)))
        self.talk.say(f"OK, {recipe} next: {counts}." + ("" if any(goals(game)[n].kind == "add" and not self.blocked(n) and not plan(game, 1, goals(game)[n])[0] for n in goals(game)) else " I can't add to a pot for it yet, so I'll start when I can."))
        self._log({"event": "order", **self.orders[-1]})
        if self.owner != "human" and self.runner.name and self.blocked(self.runner.name):
            self.runner.set(None)
        self.owner = None if self.owner == "human" and not self.runner.goal else self.owner
        self.replan("you asked for a particular soup")

    def free(self):
        """The player hands the choice back: vetoes and rules go."""
        had = bool(self.vetoes or self.orders)
        self.vetoes, self.orders, self.talk.card = {}, [], None
        self.talk.say("OK, I'll choose." + (" Your vetoes and rules are cleared." if had else ""))
        if self.owner == "human":
            self.runner.set(None)
            self.owner = None
        self.replan("you left the choice to chef 2")

    def cancel_orders(self, order_id=None):
        if isinstance(order_id, str):
            self.vetoes.pop(order_id.removeprefix("veto:"), None)
            return self.replan("you lifted a veto")
        if order_id is None:
            self.vetoes = {}
        self.orders = [o for o in self.orders if order_id is not None and o["id"] != order_id]
        self.talk.say("Rule removed." if order_id is not None else "All your rules are removed.")
        self.replan("you removed a rule")

    def blocked(self, name):
        """The player's rule that forbids this goal, if any."""
        if name == "wait" or name == "put_down":
            return None
        goal = goals(self.game)[name]
        if name in self.vetoes:
            return {"id": f"veto:{name}", "text": f"you vetoed {low(goal.label)}"}
        has = tags(name, goal)
        for o in self.orders:
            if o["rule"] == "make":
                # Finished soups may always be cleared away, so only adding is restricted.
                if goal.kind == "add" and not advances(self.game, goal, o["topics"][0]):
                    return o
            elif bool(has & set(o["topics"])) == (o["rule"] == "avoid"):
                return o
        return None

    def veto(self, name, force=False):
        if name not in goals(self.game):
            raise ValueError("unknown goal")
        costs = [] if force or self.mode == "instructions" or self.game.done else self._veto_costs(name)
        if costs:
            return self.talk.object_veto(name, costs)
        self.vetoes[name] = self.game.state.timestep + VETO_TICKS
        if self.runner.name == name:
            self.runner.set(None)
        self.asking, self._pending, self._wait_until = None, 0, 0
        self.talk.say(f"OK, I'll leave this for now: {low(goals(self.game)[name].label)}.")
        self._force = f"you vetoed {goals(self.game)[name].label.lower()}"
        self.tick()

    def _veto_costs(self, name):
        """What the veto would cost, judged by the last decision and the game as it is now."""
        game, d = self.game, self.decision
        ranking = {o["name"]: o["value"] for o in (d["ranking"] if d else [])}
        if plan(game, 1, goals(game)[name])[0]:
            return []
        left = [n for n in menu(game, 1) if n not in (name, "wait", "put_down") and not self.blocked(n)]
        if not left:
            return ["it leaves me nothing useful to do"]
        if name not in ranking:
            return []
        best = max(left, key=lambda n: ranking.get(n, 0))
        strong = ranking[name] > OBJECT_ABOVE if d["source"] == "jev" else ranking[name] - ranking.get(best, 0) >= RULES_GAP
        costs = costs_of(game, best, name) if strong else []
        ticks = plan(game, 1, goals(game)[name])[1]
        if costs and self.runner.name == name and ticks <= 4:
            costs.append(f"I am about {ticks} ticks from finishing it")
        return costs

    # Every tick

    def act(self):
        return self.runner.act()

    def tick(self):
        """Call after each game step: decide whether something happened that needs a new plan."""
        game, now = self.game, self.game.state.timestep
        self.talk.tick()
        if game.done:
            return
        made = [o for o in self.orders if o["rule"] == "make" and o["topics"][0] in game.served[o["since"]:]]
        if made:
            self.orders = [o for o in self.orders if o not in made]
            self.talk.say(f"That's the {made[0]['topics'][0]} served. Back to choosing for myself.")
        if not game.rush:
            self.orders = [o for o in self.orders if o["until"] != "rush_end"]
        if self.owner == "human" and self.runner.goal and self.runner.status.startswith("Stuck"):
            self.talk.say(f"I can't finish what you asked any more: {self.runner.status[7:]}.")
            if self.mode == "instructions":
                self.runner.set(None)
        if self.mode == "instructions":
            return
        self.vetoes = {n: t for n, t in self.vetoes.items() if t > now}
        if self.runner.goal is None:
            self.owner = None
        self._blocked = self._blocked + 1 if self.runner.status.startswith("Waiting for the other chef") else 0
        why = self._why(now)
        if why and not self._pending:
            self._decide(why)

    def _signature(self):
        pots = []
        for pos in self.game.pots:
            soup = self.game.state.objects.get(pos)
            pots.append((len(soup.ingredients), soup.is_ready) if soup else (0, False))
        return self.game.rush, tuple(pots)

    def _why(self, now):
        forced, self._force = self._force, None if not self._pending else self._force
        if forced:
            return forced
        rush, pots = self._signature()
        old_rush, old_pots = self._seen
        stuck = self.runner.goal and self.runner.status.startswith("Stuck")
        if self.owner == "human":
            return "your instruction became impossible" if stuck else None
        if self.asking:
            if now >= self.asking["until"]:
                name, self.asking = self.asking["choices"][0], None
                self._start(name)
                return None
        elif stuck:
            return "the goal became impossible"
        elif self._blocked > BLOCKED_TICKS:
            self._blocked = 0
            return "the way is blocked"
        if rush != old_rush:
            return "a rush order started" if rush else "the rush order ended"
        if pots != old_pots:
            if any(new[1] and not old[1] for new, old in zip(pots, old_pots)):
                return "a soup is ready"
            if self.game.last[0] == "interact":
                return "you changed a pot"
        if self.runner.goal is None and not self.asking and now >= self._wait_until:
            return "chef 2 is free"
        return None

    def _decide(self, why):
        game = self.game
        choices = menu(game, 1)
        self._seen = self._signature()
        self._recent = [e["text"] for e in game.log if e["text"].startswith("Chef 1")][-3:]
        usable = self.mode == "jev" and len(choices) > 1 and self.calls < MAX_CALLS and game.state.timestep >= self._retry_at
        if not usable:
            if self.mode == "jev" and self.calls >= MAX_CALLS:
                self.note = "Call limit reached, using rules"
            return self._apply(why, choices, {"ranking": rules(game, 1, choices)}, "rules")
        state = context(game, 1, why, self._recent)
        state["human_cannot_help_with"] = [i for i, t in self.talk.declined.items() if t > game.state.timestep]
        asks = self.talk.help_menu()
        self.calls += 1
        self._ids += 1
        self._pending = ticket = self._ids
        call = (lambda: self.ask(state, choices, asks)) if asks else (lambda: self.ask(state, choices))
        self.background(call, lambda answer, error: self._receive(ticket, why, choices, answer, error))

    def _receive(self, ticket, why, choices, answer, error):
        if answer:
            self.cost += answer["cost"] or 0
        if ticket != self._pending:
            return  # overtaken by the player or a new mode
        self._pending = 0
        if error:
            self.note = "Jev unavailable, using rules"
            self._retry_at = self.game.state.timestep + RETRY_TICKS
            fresh = menu(self.game, 1)
            return self._apply(why, fresh, {"ranking": rules(self.game, 1, fresh), "error": error}, "rules")
        self.note = None
        self._apply(why, choices, answer, "jev")

    def _apply(self, why, choices, answer, source):
        """Guard: the game moved on while the planner was thinking, so check before acting."""
        game, now = self.game, self.game.state.timestep
        if game.done:
            return
        valid = [
            (n, p) for n, p in answer["ranking"]
            if n == "wait" or (n in choices and plan(game, 1, goals(game)[n])[0] is None)
        ]
        everything, valid = valid, [(n, p) for n, p in valid if not self.blocked(n)]
        confidence = answer.get("confidence")
        # Unsure, and nothing under way: ask rather than guess.
        unsure = confidence is not None and confidence < ASK_BELOW and len(valid) > 1 and self.runner.goal is None
        chosen = None if unsure or not valid else valid[0][0]
        self.asking = {"until": now + ASK_TICKS, "choices": [n for n, _ in valid[:2]]} if unsure else None
        self.decision = {
            "tick": now, "why": why, "source": source, "chosen": chosen, "confidence": confidence, "seconds": answer.get("seconds"),
            "ranking": [{"name": n, "value": round(p, 3), **choices.get(n, {})} for n, p in valid],
        }
        self._log({**self.decision, "asked": unsure, "offered": choices, "jev_ranking": answer["ranking"],
                   **{k: answer.get(k) for k in ("model", "tokens", "cost", "error")}})
        if chosen:
            self._start(chosen)
        self._speak_up(everything, valid, answer, source)

    def _speak_up(self, everything, valid, answer, source):
        """Raise a costly rule with the player, or ask for help."""
        if everything and valid and everything[0][0] != valid[0][0] and everything[0][0] != "wait":
            (best, top), (allowed, value) = everything[0], valid[0]
            strong = top > OBJECT_ABOVE if source == "jev" else top - value >= RULES_GAP
            if allowed != "wait":
                costs = costs_of(self.game, allowed, best)
            elif supports_rush(self.game, 1, goals(self.game)[best]):
                costs = [f"the rush {self.game.rush[0]} has {self.game.rush[1] - self.game.state.timestep} ticks left and nothing I may do helps it"]
            else:
                costs = []  # idle by the player's choice is not worth an interruption
            if strong and costs:
                return self.talk.conflict(best, allowed, self.blocked(best), costs)
        asks = self.talk.help_menu()
        name = answer.get("help", "none")
        if source == "rules" and asks and (not valid or valid[0][0] in ("wait", "pass_dish")):
            wanted = self.game.recipes[self.game.rush[0]]["ingredients"] if self.game.rush else []
            # Rush ingredients first, then whatever fills the fullest pot.
            name = min(asks, key=lambda n: (n[5:] not in wanted, "has 2" not in asks[n], "has 1" not in asks[n]))
        if name in asks:
            self.talk.ask_for(name)

    def _start(self, name):
        if name == "wait":
            self.runner.set(None)
            self.runner.status = "Waiting: your vetoes and rules leave little to do" if self.vetoes or self.orders else "Nothing useful to do, waiting"
            self._wait_until = self.game.state.timestep + WAIT_TICKS
            self.owner = None
        else:
            if self.runner.name != name:
                self.runner.set(name)
            self.owner = "ai"

    def _log(self, entry):
        if self.log_path:
            entry = {"layout": self.game.layout, "seed": self.game.seed, "mode": self.mode, "score": self.game.score, **entry}
            self.log_path.parent.mkdir(exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def view_toward(self):
        goal = self.runner.goal
        asked = [o["topics"][0] for o in self.orders if o["rule"] == "make"]
        if asked and goal and goal.kind == "add" and advances(self.game, goal, asked[0]):
            return asked[0]
        return toward(self.game, goal)

    def view(self):
        now = self.game.state.timestep
        return {
            "toward": self.view_toward(), "mode": self.mode, "thinking": bool(self._pending or self._judging), "orders": self.orders, "talk": self.talk.view(), "owner": self.owner, "decision": self.decision, "note": self.note,
            "asking": self.asking and {"left": self.asking["until"] - now, "choices": self.asking["choices"]},
            "vetoes": {n: t - now for n, t in self.vetoes.items() if t > now},
            "calls": self.calls, "max_calls": MAX_CALLS, "cost": self.cost,
        }
