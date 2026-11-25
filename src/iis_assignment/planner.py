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


def due(game, recipe):
    """Ticks left on an order chef 2 may work towards, or None."""
    return None if recipe in game.skipped else game.due(recipe)


def worth(game, recipe):
    points = game.recipes[recipe]["points"] if recipe else 0
    return points * 2 if due(game, recipe) is not None else points


def advances(game, goal, recipe):
    """Whether an add goal keeps its pot on the way to this recipe."""
    soup = game.state.objects.get(goal.pot)
    inside = (list(soup.ingredients) if soup else []) + [goal.item]
    need = game.recipes[recipe]["ingredients"]
    return all(inside.count(i) <= need.count(i) for i in inside)


def value(game, chef, goal, recipe):
    """What this recipe would bring if the goal leads to it, not counting an order that would be missed."""
    left, needed = due(game, recipe), finish_ticks(game, chef, goal, recipe)
    late = left is not None and needed is not None and needed > left
    return game.recipes[recipe]["points"] if late else worth(game, recipe)


def toward(game, goal):
    """The recipe an add goal is working towards: the most valuable one the pot could still become."""
    if not goal or goal.kind != "add":
        return None
    soup = game.state.objects.get(goal.pot)
    recipes = possible(game, (list(soup.ingredients) if soup else []) + [goal.item])
    return max(recipes, key=lambda n: value(game, 1, goal, n)) if recipes else None


def brew(game, pos, facing, pot, inside, recipe, cook=None):
    """(ticks to fill the pot, ticks until served) for a chef working alone; None where something is out of reach."""
    need, fill, here = list(game.recipes[recipe]["ingredients"]), 0, (pos, facing)
    for i in inside:
        need.remove(i)
    tiles = game.mdp.terrain_pos_dict

    def trip(start, *stops):
        ticks = 0
        for stop in stops:
            leg = route(game, *start, stop)
            if leg is None:
                return None, None
            ticks, start = ticks + len(leg.actions) + 1, (leg.pos, leg.facing)
        return ticks, start

    for item in need:
        ticks, here = trip(here, set(tiles[DISPENSERS[item]]), {pot})
        if ticks is None:
            return None, None
        fill += ticks
    plate, at_pot = trip(here, set(tiles["D"]), {pot})
    out, _ = trip(at_pot, set(tiles["S"])) if plate is not None else (None, None)
    if out is None:
        return fill, None
    return fill, fill + max(game.recipes[recipe]["cook"] if cook is None else cook, plate) + out


def after_add(game, chef, goal, recipe):
    """brew() from the moment this add goal is done."""
    soup = game.state.objects.get(goal.pot)
    inside = (list(soup.ingredients) if soup else []) + [goal.item]
    stand = route(game, game.state.players[chef].position, (0, -1), {goal.pot})
    return brew(game, stand.pos, stand.facing, goal.pot, inside, recipe) if stand else (None, None)


def finish_ticks(game, chef, goal, recipe):
    """Estimated ticks until this soup is served if the chef starts with this goal and works alone, or None."""
    reason, ticks = plan(game, chef, goal, force=True)
    if reason:
        return None
    if goal.kind == "add":
        rest = after_add(game, chef, goal, recipe)[1]
        return None if rest is None else ticks + rest
    if goal.kind == "collect":
        stand = route(game, game.state.players[chef].position, (0, -1), {goal.pot})
        out = stand and route(game, stand.pos, stand.facing, set(game.mdp.terrain_pos_dict["S"]))
        return ticks + len(out.actions) + 1 if out else None
    return ticks if goal.kind == "serve" else None


def time_to_serve(game, chef, recipe):
    """Quickest estimate for this chef alone to serve this recipe from now, using any pot, or None."""
    best, every = None, goals(game)
    for goal in every.values():
        if recipe in made_by(game, chef, goal):
            ticks = finish_ticks(game, chef, goal, recipe)
            best = ticks if best is None or ticks is not None and ticks < best else best
    return best


def advances_open(game, goal, recipe):
    soup = game.state.objects.get(goal.pot)
    return not (soup and soup.is_full) and advances(game, goal, recipe)


def made_by(game, chef, goal):
    """Recipes this goal brings closer."""
    if goal.kind == "add":
        return [n for n in game.recipes if advances_open(game, goal, n)]
    if goal.kind == "collect":
        soup = game.state.objects.get(goal.pot)
    elif goal.kind == "serve":
        held = game.state.players[chef].held_object
        soup = held if held and held.name == "soup" else next((o for p, o in game.state.objects.items() if o.name == "soup" and p not in game.pots), None)
    else:
        return []
    return [n for n, r in game.recipes.items() if soup and sorted(r["ingredients"]) == sorted(soup.ingredients)]


def open_orders(game):
    """Orders chef 2 may work towards, most urgent first."""
    return [o for o in game.active if o["recipe"] not in game.skipped]


def order_text(game, recipe):
    return f"the {recipe} order pays {worth(game, recipe)} and has {due(game, recipe)} ticks left"


def orders_helped(game, chef, goal):
    """Open orders this goal brings closer."""
    made = made_by(game, chef, goal)
    return [o["recipe"] for o in open_orders(game) if o["recipe"] in made]


def can_use(game, chef, tile):
    touch = {ahead(p, d) for p in reachable(game, chef) for d in MOVES}
    return bool(touch & set(game.mdp.terrain_pos_dict[tile]))


def order_note(game, chef, goal, recipe):
    """How a goal stands against the open order for a recipe, if there is one."""
    left = due(game, recipe)
    if left is None:
        return ""
    needed = finish_ticks(game, chef, goal, recipe)
    if needed is None:
        return f", an order with {left} ticks left"
    return f", an order with {left} ticks left, " + (f"too late alone as serving takes about {needed}" if needed > left else f"in time alone as serving takes about {needed}")


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
            out.append(f"completes {recipe}, worth {worth(game, recipe)}, which then starts cooking" + order_note(game, chef, goal, recipe))
        else:
            for recipe in possible(game, inside):
                need = game.recipes[recipe]["ingredients"]
                more = [_count(need.count(i) - inside.count(i), i) for i in sorted(set(need)) if need.count(i) > inside.count(i)]
                rest = after_add(game, chef, goal, recipe)[0]
                time = f", about {ticks + rest} ticks in all to fill the pot alone" if rest is not None else ""
                out.append(f"{recipe} ({worth(game, recipe)} points{order_note(game, chef, goal, recipe)}) would need {' and '.join(more)}{time}")
        for o in open_orders(game):
            if o["recipe"] in possible(game, inside[:-1]) and o["recipe"] not in possible(game, inside):
                out.append(f"rules out the {o['recipe']} order in this pot")
        if other == goal.item:
            out.append(f"the other chef is already carrying {goal.item}")
    elif goal.kind == "collect":
        recipe = possible(game, list(pot.ingredients))
        name = recipe[0] if recipe else "unlisted soup"
        out.append(f"{name}, worth {worth(game, recipe[0]) if recipe else 0} once served" + (order_note(game, chef, goal, name) if recipe else ""))
        if pot.is_ready:
            out.append("it is ready")
        elif pot.cook_time_remaining > ticks - 2:
            out.append(f"still cooking for {pot.cook_time_remaining} ticks, so it means waiting")
        if other == "dish":
            out.append("the other chef is already carrying a dish")
    elif goal.kind == "serve":
        recipe = made_by(game, chef, goal)
        out.append(f"scores {worth(game, recipe[0]) if recipe else 0} now" + (order_note(game, chef, goal, recipe[0]) if recipe else ""))
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


def costs_of(game, asked, prefer):
    """Concrete, checkable drawbacks of doing `asked` rather than `prefer`. Empty means none can be named."""
    every, out = goals(game), []
    ticks = {n: plan(game, 1, every[n], force=True)[1] or 0 for n in (asked, prefer)}
    said = facts(game, 1, every[asked], ticks[asked])
    done = facts(game, 1, every[prefer], ticks[prefer])
    for part in said.split("; "):
        if part.startswith(("cannot finish before", "rules out the", "the other chef has no pot")):
            out.append(part)

    def in_time(name):
        """Orders a goal helps that can still be met."""
        ok = {}
        for recipe in orders_helped(game, 1, every[name]):
            needed = finish_ticks(game, 1, every[name], recipe)
            if needed is None or needed <= due(game, recipe):
                ok[recipe] = needed
        return ok

    mine, better = in_time(asked), in_time(prefer)
    for recipe in better:
        if recipe in mine:
            continue
        if not mine:
            out.append(f"{order_text(game, recipe)}, and this does not help it")
        elif all(worth(game, recipe) > worth(game, m) or due(game, recipe) < due(game, m) for m in mine):
            out.append(f"{order_text(game, recipe)}, while {order_text(game, next(iter(mine)))}")
    for recipe in orders_helped(game, 1, every[asked]):
        if recipe not in mine and better and recipe not in better:
            out.append(f"the {recipe} order has {due(game, recipe)} ticks left but serving it would take about {finish_ticks(game, 1, every[asked], recipe)} alone")
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
        "rules": "An ordered soup pays double if served before its order runs out; a missed order just loses the bonus. A pot needs 3 ingredients matching a recipe, cooks 20 ticks, then is collected with a dish and served.",
        "ticks_left": game.horizon - game.state.timestep,
        "score": game.score,
        "orders": [
            {"recipe": o["recipe"], "pays": worth(game, o["recipe"]), "ticks_left": due(game, o["recipe"]),
             "chef_alone_needs_about": time_to_serve(game, chef, o["recipe"])}
            for o in open_orders(game)
        ],
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
            values = {n: value(game, chef, goal, n) for n in possible(game, inside)}
            best = max(values, key=values.get)
            score = 20 + 10 * len(inside) + values[best] / 2 - ticks
            if values[best] > game.recipes[best]["points"]:  # an order that can be met: sooner deadlines first
                score += max(0, 10 - due(game, best) / 10)
            score -= 15 if other == goal.item and len(inside) == 3 else 0
        elif goal.kind == "pass":
            useful = can_use(game, 1 - chef, "P") and not can_use(game, chef, "P")
            waiting = [game.state.objects[p].name for p in shared_counters(game) if p in game.state.objects]
            wanted = goal.item != "dish" or not can_use(game, 1 - chef, "D")
            score = 45 - 15 * len(waiting) if useful and wanted and len(waiting) < 2 else 0
            if useful and wanted and any(goal.item in game.recipes[o["recipe"]]["ingredients"] for o in open_orders(game)):
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
        if card["kind"] in ("clarify", "which_order"):
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
        watch = [o["id"] for o in self.game.active] if until == "orders_end" else []
        no_orders = until == "orders_end" and not watch
        if no_orders:
            until = "cancelled"
        names = {"onion": "onions", "tomato": "tomatoes", "pot1": "pot 1", "pot2": "pot 2", "collect": "collecting", "serve": "serving"}
        what = " and ".join(names.get(t, t) for t in topics)
        text = (f"leave {what} alone" if rule == "avoid" else f"only do {what}") + (" until the current orders close" if until == "orders_end" else "")
        self._order_ids += 1
        self.orders.append({"id": self._order_ids, "rule": rule, "topics": topics, "until": until, "watch": watch, "text": text})
        self.talk.say(f"Understood: {text}." + (" There are no orders now, so it lasts until you undo it." if no_orders else " You can undo it below."))
        self._log({"event": "order", **self.orders[-1]})
        if self.owner == "ai" and self.runner.name and self.blocked(self.runner.name):
            self.runner.set(None)
        self.replan("you gave a lasting rule")

    def make(self, recipe, force=False):
        """The player wants a particular soup next."""
        game = self.game
        self.talk.card = None
        if recipe == "an_order":
            names = [o["recipe"] for o in game.active]
            if len(names) > 1:
                return self.talk.which_order(names)
            recipe = names[0] if names else None
        if recipe not in game.recipes:
            return self.talk.say("There is no open order right now. Name a soup and I'll work on it.")
        self.unskip(recipe)
        if self.mode != "instructions" and not force:
            better, costs = self._make_costs(recipe)
            if costs:
                return self.talk.object_make(recipe, better, costs)
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

    def _make_costs(self, recipe):
        """(better order, costs) if working on this soup would cost an order that can still be met."""
        game = self.game

        def slack(name):
            needed = time_to_serve(game, 1, name)
            return None if needed is None else due(game, name) - needed

        # An order already cooking or on its way out is not endangered.
        cooking = {n for g in goals(game).values() if g.kind in ("collect", "serve") for n in made_by(game, 1, g)}
        others = [o["recipe"] for o in open_orders(game) if o["recipe"] != recipe and o["recipe"] not in cooking]
        others = [n for n in others if slack(n) is None or slack(n) >= 0]
        if not others:
            return None, []
        best = max(others, key=lambda n: (worth(game, n), -due(game, n)))
        if due(game, recipe) is None:
            return best, [order_text(game, best), f"{recipe} is not ordered and pays {worth(game, recipe)}"]
        if recipe not in cooking and slack(recipe) is not None and slack(recipe) < 0:
            return best, [f"the {recipe} order has {due(game, recipe)} ticks left but needs about {time_to_serve(game, 1, recipe)} alone", order_text(game, best)]
        return None, []

    def skip(self, recipe):
        """The player wants chef 2 to leave one order alone."""
        game = self.game
        if recipe == "an_order":
            names = [o["recipe"] for o in game.active]
            if len(names) != 1:
                return self.talk.say("Which order should I leave? Name the soup." if names else "There is no open order right now.")
            recipe = names[0]
        if game.due(recipe) is None:
            return self.talk.say(f"There is no open order for {recipe}.")
        self.orders = [o for o in self.orders if not (o["rule"] in ("make", "skip") and o["topics"] == [recipe])]
        game.skipped.add(recipe)
        self._order_ids += 1
        watch = [o["id"] for o in game.active if o["recipe"] == recipe]
        self.orders.append({"id": self._order_ids, "rule": "skip", "topics": [recipe], "until": "orders_end", "watch": watch, "text": f"leave the {recipe} order alone"})
        self.talk.say(f"OK, I'll leave the {recipe} order to you.")
        self._log({"event": "order", **self.orders[-1]})
        if self.owner == "ai" and self.runner.name and self.blocked(self.runner.name):
            self.runner.set(None)
        self.replan("you told chef 2 to leave an order")

    def unskip(self, recipe=None):
        for name in list(self.game.skipped):
            if recipe in (None, name):
                self.game.skipped.discard(name)
                self.orders = [o for o in self.orders if not (o["rule"] == "skip" and o["topics"] == [name])]

    def free(self):
        """The player hands the choice back: vetoes and rules go."""
        had = bool(self.vetoes or self.orders)
        self.unskip()
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
        for o in self.orders:
            if o["rule"] == "skip" and order_id in (None, o["id"]):
                self.game.skipped.discard(o["topics"][0])
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
            elif o["rule"] == "skip":
                # Only jobs that could lead to nothing else are held back.
                if goal.kind == "add" and advances_open(self.game, goal, o["topics"][0]) and made_by(self.game, 1, goal) == o["topics"]:
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
        still = {o["id"] for o in game.active}
        self.orders = [o for o in self.orders if o["until"] != "orders_end" or still & set(o["watch"])]
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
        return frozenset(o["id"] for o in self.game.active), tuple(pots)

    def _why(self, now):
        forced, self._force = self._force, None if not self._pending else self._force
        if forced:
            return forced
        open_now, pots = self._signature()
        open_before, old_pots = self._seen
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
        if open_now - open_before:
            return "a new order arrived"
        if open_before - open_now:
            return "an order was completed or ran out"
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
            elif orders_helped(self.game, 1, goals(self.game)[best]):
                costs = [f"{order_text(self.game, orders_helped(self.game, 1, goals(self.game)[best])[0])}, and nothing I may do helps it"]
            else:
                costs = []  # idle by the player's choice is not worth an interruption
            if strong and costs:
                return self.talk.conflict(best, allowed, self.blocked(best), costs)
        asks = self.talk.help_menu()
        name = answer.get("help", "none")
        if source == "rules" and asks and (not valid or valid[0][0] in ("wait", "pass_dish")):
            wanted = [i for o in open_orders(self.game) for i in self.game.recipes[o["recipe"]]["ingredients"]]
            # Ordered ingredients first, then whatever fills the fullest pot.
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
            "toward": self.view_toward(), "mode": self.mode, "thinking": bool(self._pending or self._judging), "orders": self.orders, "talk": self.talk.view(), "skipped": sorted(self.game.skipped), "owner": self.owner, "decision": self.decision, "note": self.note,
            "asking": self.asking and {"left": self.asking["until"] - now, "choices": self.asking["choices"]},
            "vetoes": {n: t - now for n, t in self.vetoes.items() if t > now},
            "calls": self.calls, "max_calls": MAX_CALLS, "cost": self.cost,
        }
