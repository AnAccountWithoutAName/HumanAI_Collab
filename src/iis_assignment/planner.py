"""Chooses chef 2's goals: context builder, rule planner, Jev planner, and the guard around them."""

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from .skills import MOVES, ahead, goals, options, plan, reachable, shared_counters

ROOT = Path(__file__).resolve().parents[2]
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.14"
MAX_CALLS = 150  # per game, so a bug cannot drain the credits
ASK_BELOW = 0.2  # Jev confidence under which chef 2 asks instead of acting
ASK_TICKS = 15  # then it goes ahead with its best option
WAIT_TICKS = 8
VETO_TICKS = 40
BLOCKED_TICKS = 12
RETRY_TICKS = 20  # pause after a failed call
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
                out.append(f"{recipe} ({worth(game, recipe)} points) would need {' and '.join(more)}")
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


def api_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                return value.strip() or None
    except OSError:
        pass
    return None


def ask_jev(state, choices):
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
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    start = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(JEV_URL, json.dumps(body).encode(), headers), timeout=8) as reply:
        data = json.load(reply)
    answer, usage = data["answers"]["goal"], data.get("usage", {})
    return {
        "ranking": sorted(answer["probabilities"].items(), key=lambda kv: -kv[1]),
        "confidence": answer["confidence"],
        "model": data["model"],
        "tokens": usage.get("input_tokens"),
        "cost": usage.get("cost", 0),
        "seconds": round(time.perf_counter() - start, 3),
    }


# --- Teammate: triggers, guard, log ------------------------------------------------------------


class Teammate:
    """Decides when to plan, checks every answer, and hands goals to the runner."""

    def __init__(self, game, mode, lock=None, log_path=None, ask=ask_jev):
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

    def instruct(self, name):
        """The player picks a goal directly; it is kept until done, stuck or stopped. None stops chef 2."""
        self.runner.set(name)
        self.asking, self._pending = None, 0
        self.owner = "human" if name else None
        self.vetoes.pop(name, None)
        self._wait_until = 0 if name else self.game.state.timestep + WAIT_TICKS

    def veto(self, name):
        if name not in goals(self.game):
            raise ValueError("unknown goal")
        self.vetoes[name] = self.game.state.timestep + VETO_TICKS
        if self.runner.name == name:
            self.runner.set(None)
        self.asking, self._pending, self._wait_until = None, 0, 0
        self._force = f"you vetoed {goals(self.game)[name].label.lower()}"
        self.tick()

    # Every tick

    def act(self):
        return self.runner.act()

    def tick(self):
        """Call after each game step: decide whether something happened that needs a new plan."""
        game, now = self.game, self.game.state.timestep
        if game.done or self.mode == "instructions":
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
        choices = menu(game, 1, self.vetoes)
        self._seen = self._signature()
        self._recent = [e["text"] for e in game.log if e["text"].startswith("Chef 1")][-3:]
        usable = self.mode == "jev" and len(choices) > 1 and self.calls < MAX_CALLS and game.state.timestep >= self._retry_at
        if not usable:
            if self.mode == "jev" and self.calls >= MAX_CALLS:
                self.note = "Call limit reached, using rules"
            return self._apply(why, choices, {"ranking": rules(game, 1, choices)}, "rules")
        state = context(game, 1, why, self._recent)
        self.calls += 1
        self._ids += 1
        self._pending = ticket = self._ids

        def call():
            try:
                return self.ask(state, choices), None
            except Exception as error:  # network, HTTP, malformed reply: all mean "use the rules"
                return None, f"{type(error).__name__}: {error}"

        if self.lock is None:
            return self._receive(ticket, why, choices, *call())

        def work():
            result = call()
            with self.lock:
                self._receive(ticket, why, choices, *result)

        threading.Thread(target=work, daemon=True).start()

    def _receive(self, ticket, why, choices, answer, error):
        if answer:
            self.cost += answer["cost"] or 0
        if ticket != self._pending:
            return  # overtaken by the player or a new mode
        self._pending = 0
        if error:
            self.note = "Jev unavailable, using rules"
            self._retry_at = self.game.state.timestep + RETRY_TICKS
            fresh = menu(self.game, 1, self.vetoes)
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
            if n == "wait" or (n in choices and n not in self.vetoes and plan(game, 1, goals(game)[n])[0] is None)
        ]
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

    def _start(self, name):
        if name == "wait":
            self.runner.set(None)
            self.runner.status = "Nothing useful to do, waiting"
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

    def view(self):
        now = self.game.state.timestep
        return {
            "mode": self.mode, "thinking": bool(self._pending), "owner": self.owner, "decision": self.decision, "note": self.note,
            "asking": self.asking and {"left": self.asking["until"] - now, "choices": self.asking["choices"]},
            "vetoes": {n: t - now for n, t in self.vetoes.items() if t > now},
            "calls": self.calls, "max_calls": MAX_CALLS, "cost": self.cost,
        }
