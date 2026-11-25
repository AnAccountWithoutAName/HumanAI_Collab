"""Game session: wraps the vendored engine with layouts, rush orders and UI-friendly state."""

import random
from collections import Counter

from overcooked_ai_py.mdp.actions import Action, Direction
from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld, Recipe

from .skills import Runner, options

# X counter, O onions, T tomatoes, D dishes, P pot, S serving hatch, 1/2 chefs
LAYOUTS = {
    "open": [
        "XXPXXXPXX",
        "O       T",
        "X 1   2 X",
        "D   X   D",
        "X       X",
        "XXXXSXXXX",
    ],
    # Ingredients on the left, pots and serving on the right: pass over the middle counter.
    "split": [
        "XXXXXXPXX",
        "O   X   P",
        "T 1 X 2 S",
        "X   X   D",
        "XXXXXXXXX",
    ],
}
ACTIONS = {
    "up": Direction.NORTH,
    "down": Direction.SOUTH,
    "left": Direction.WEST,
    "right": Direction.EAST,
    "interact": Action.INTERACT,
    "stay": Action.STAY,
}
HORIZON = 300
RUSH_TICKS = 90
RECIPES = {
    "onion soup": {"ingredients": ["onion", "onion", "onion"], "points": 20, "cook": 20},
    "tomato soup": {"ingredients": ["tomato", "tomato", "tomato"], "points": 20, "cook": 20},
    "mixed soup": {"ingredients": ["onion", "onion", "tomato"], "points": 30, "cook": 20},
}  # a rush order pays double


def recipe_name(ingredients):
    """Recipe made by exactly these ingredients, or None."""
    return next((n for n, r in RECIPES.items() if sorted(r["ingredients"]) == sorted(ingredients)), None)


class Game:
    def __init__(self, layout="open", seed=0):
        self.layout, self.seed = layout, seed
        self.recipes, self.horizon = RECIPES, HORIZON
        self.mdp = OvercookedGridworld.from_grid(
            LAYOUTS[layout],
            {
                "start_all_orders": [{"ingredients": r["ingredients"]} for r in RECIPES.values()],
                "recipe_times": [r["cook"] for r in RECIPES.values()],
                "old_dynamics": True,  # pots start cooking by themselves at 3 ingredients
            },
        )
        self.pots = sorted(self.mdp.get_pot_locations())
        self.state = self.mdp.get_standard_start_state()
        self.score, self.log = 0, []
        self.last = ["stay", "stay"]
        self.rush = None  # (recipe name, deadline tick)
        self.rush_schedule = self._make_schedule(random.Random(seed))
        self.partner = Runner(self, 1)

    @staticmethod
    def _make_schedule(rng):
        """{start tick: recipe name}; the seed makes every run reproducible."""
        schedule, t = {}, rng.randint(30, 45)
        while t + RUSH_TICKS <= HORIZON:
            schedule[t] = rng.choice(list(RECIPES))
            t += RUSH_TICKS + rng.randint(5, 15)
        return schedule

    @property
    def done(self):
        return self.state.timestep >= HORIZON

    def step(self, actions):
        """Advance one tick. `actions` holds one ACTIONS key per chef."""
        if self.done:
            return
        before, self.last = self.state, list(actions)
        joint = [ACTIONS[a] for a in actions]
        self.state, info = self.mdp.get_state_transition(before, joint)
        for i, served in enumerate(info["event_infos"]["soup_delivery"]):
            if served:
                self._served(i, before.players[i].held_object)
        finished = self.partner.after_step(before)
        if finished:
            self._say(f"Chef 2 finished: {finished}")
        self._update_rush()

    def _served(self, chef, soup):
        name = recipe_name(soup.ingredients)
        reward = RECIPES[name]["points"] if name else 0
        if self.rush and name == self.rush[0]:
            reward *= 2
            self.rush = None
            self._say("Rush order completed")
        self.score += reward
        self._say(f"Chef {chef + 1} served {name or 'a soup nobody ordered'} (+{reward})")

    def fits(self, in_pot, ingredient):
        """Whether the pot can still become a recipe after adding this ingredient."""
        wanted = Counter(list(in_pot) + [ingredient])
        return any(wanted <= Counter(r["ingredients"]) for r in RECIPES.values())

    def _update_rush(self):
        now = self.state.timestep
        if self.rush and now >= self.rush[1]:
            self.rush = None
            self._say("Rush order expired")
        if now in self.rush_schedule:
            self.rush = (self.rush_schedule[now], now + RUSH_TICKS)
            self._say(f"RUSH ORDER: {self.rush[0]}, double points")

    def _say(self, text):
        self.log.append({"tick": self.state.timestep, "text": text})

    def hint(self, i):
        """What pressing interact would do right now, so players need not memorise rules."""
        player, state = self.state.players[i], self.state
        pos = Action.move_in_direction(player.position, player.orientation)
        tile = self.mdp.get_terrain_type_at_pos(pos)
        held = player.held_object.name if player.held_object else None
        there = state.objects.get(pos)
        if tile in "OTD":
            return None if held else f"take {dict(O='onion', T='tomato', D='dish')[tile]}"
        if tile == "X":
            if held and not there:
                return f"put {held} down"
            return f"pick up {there.name}" if there and not held else None
        if tile == "S":
            return "serve soup" if held == "soup" else None
        if tile == "P":
            if there and there.is_ready:
                return "collect soup" if held == "dish" else None
            if there and there.is_cooking:
                return None
            if held in Recipe.ALL_INGREDIENTS:
                inside = there.ingredients if there else []
                warning = "" if self.fits(inside, held) else ", but that makes no recipe!"
                return f"add {held} ({len(inside)}/3 in pot){warning}"
        return None

    @staticmethod
    def _object(obj):
        if obj is None:
            return None
        out = {"name": obj.name, "pos": obj.position}
        if obj.name == "soup":
            out["ingredients"] = obj.ingredients
            out["ready"] = obj.is_ready
            out["left"] = obj.cook_time_remaining if obj.is_cooking else None
            out["cook_time"] = obj.cook_time if obj.is_cooking else None
        return out

    def snapshot(self):
        """JSON-serialisable view used by the browser (and, later, by the planner)."""
        return {
            "layout": self.layout,
            "seed": self.seed,
            "grid": ["".join(row) for row in self.mdp.terrain_mtx],
            "tick": self.state.timestep,
            "horizon": HORIZON,
            "score": self.score,
            "done": self.done,
            "rush": self.rush
            and {"recipe": self.rush[0], "left": self.rush[1] - self.state.timestep},
            "chefs": [
                {"pos": p.position, "facing": p.orientation, "held": self._object(p.held_object), "hint": self.hint(i)}
                for i, p in enumerate(self.state.players)
            ],
            "objects": [self._object(o) for o in self.state.objects.values()],
            "recipes": RECIPES,
            "pots": self.pots,
            "partner": {
                "goal": self.partner.name,
                "label": self.partner.goal.label if self.partner.goal else None,
                "status": self.partner.status,
                "path": self.partner.path,
                "options": options(self, 1),
            },
            "log": self.log[-8:],
        }
