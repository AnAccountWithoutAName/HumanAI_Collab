"""Goal layer: named goals a planner can choose, and the pathfinding that carries them out.

A planner (person, rules or LLM) never picks moves. It picks a goal name from `options()`;
`Runner` turns that goal into one action per tick.
"""

import random
from collections import deque, namedtuple

from overcooked_ai_py.mdp.actions import Direction

MOVES = {Direction.NORTH: "up", Direction.SOUTH: "down", Direction.WEST: "left", Direction.EAST: "right"}
DISPENSERS = {"onion": "O", "tomato": "T", "dish": "D"}

Goal = namedtuple("Goal", "label kind item pot", defaults=[None, None])
Route = namedtuple("Route", "actions tiles pos facing target")


def ahead(pos, direction):
    return pos[0] + direction[0], pos[1] + direction[1]


def route(game, pos, facing, targets, blocked=()):
    """Breadth-first search to a tile next to, and facing, the closest of `targets`."""
    floor = set(game.mdp.terrain_pos_dict[" "]) - set(blocked)
    queue, seen = deque([(pos, facing, (), ())]), {pos}
    while queue:
        p, f, actions, tiles = queue.popleft()
        for d in sorted(MOVES, key=lambda d: d != f):
            if ahead(p, d) in targets:
                turn = () if d == f else (MOVES[d],)
                return Route(actions + turn, tiles, p, d, ahead(p, d))
        for d in MOVES:
            n = ahead(p, d)
            if n in floor and n not in seen:
                seen.add(n)
                queue.append((n, d, actions + (MOVES[d],), tiles + (n,)))
    return None


def reachable(game, chef):
    """All floor tiles this chef can ever walk to."""
    floor, start = set(game.mdp.terrain_pos_dict[" "]), game.state.players[chef].position
    seen, todo = {start}, [start]
    while todo:
        p = todo.pop()
        for d in MOVES:
            n = ahead(p, d)
            if n in floor and n not in seen:
                seen.add(n)
                todo.append(n)
    return seen


def counters(game):
    return set(game.mdp.terrain_pos_dict["X"])


def shared_counters(game):
    """Counters both chefs can touch."""
    touch = [{ahead(p, d) for p in reachable(game, i) for d in MOVES} for i in (0, 1)]
    return counters(game) & touch[0] & touch[1]


def divided(game):
    return not reachable(game, 0) & reachable(game, 1)


def goals(game):
    """Every goal in this kitchen: {name: Goal}."""
    out = {}
    for n, pot in enumerate(game.pots, 1):
        for item in ("onion", "tomato"):
            out[f"add_{item}_pot{n}"] = Goal(f"Add {item} to pot {n}", "add", item, pot)
        out[f"collect_pot{n}"] = Goal(f"Collect from pot {n}", "collect", "dish", pot)
    out["serve"] = Goal("Serve soup", "serve", "soup")
    if divided(game):
        for item in DISPENSERS:
            out[f"pass_{item}"] = Goal(f"Pass {item} over", "pass", item)
    out["put_down"] = Goal("Put down item", "put_down")
    return out


def _held(game, chef):
    obj = game.state.players[chef].held_object
    return obj.name if obj else None


def _sources(game, goal):
    found = set(game.mdp.terrain_pos_dict[DISPENSERS[goal.item]]) if goal.item in DISPENSERS else set()
    if goal.kind != "pass":  # never take back what was just passed over
        found |= {p for p in counters(game) if p in game.state.objects and game.state.objects[p].name == goal.item}
    return found


def _destinations(game, goal):
    if goal.pot:
        return {goal.pot}
    if goal.kind == "serve":
        return set(game.mdp.terrain_pos_dict["S"])
    free = shared_counters(game) if goal.kind == "pass" else counters(game)
    return free - set(game.state.objects)


def _legs(game, chef, goal):
    """Target sets to visit, in order, from the chef's current situation."""
    held, legs = _held(game, chef), []
    if goal.kind != "put_down" and held != goal.item:
        if held:
            legs.append(counters(game) - set(game.state.objects))  # free the hands first
        legs.append(_sources(game, goal))
    legs.append(_destinations(game, goal))
    return legs


def plan(game, chef, goal):
    """(reason it cannot be done or None, estimated ticks). Ignores the other chef's position."""
    pot = game.state.objects.get(goal.pot) if goal.pot else None
    held = _held(game, chef)
    if goal.kind == "add":
        if pot and pot.is_full:
            return "pot is full", None
        if not game.fits(pot.ingredients if pot else [], goal.item):
            return "makes no recipe", None
    if goal.kind == "collect" and not (pot and pot.is_full):
        return "not cooking", None
    if goal.kind == "put_down" and not held:
        return "hands are empty", None
    player, ticks = game.state.players[chef], 0
    pos, facing = player.position, player.orientation
    for targets in _legs(game, chef, goal):
        leg = route(game, pos, facing, targets)
        if leg is None:
            return (f"no {goal.item} within reach" if goal.item else "no free counter"), None
        ticks += len(leg.actions) + 1
        pos, facing = leg.pos, leg.facing
    if goal.kind == "collect" and not pot.is_ready:
        ticks = max(ticks, pot.cook_time_remaining + 1)
    return None, ticks


def options(game, chef):
    """The menu a planner chooses from, with feasibility and cost."""
    out = []
    for name, goal in goals(game).items():
        reason, ticks = plan(game, chef, goal)
        out.append({"name": name, "label": goal.label, "ticks": ticks, "reason": reason})
    return out


class Runner:
    """Carries out one goal at a time for one chef."""

    def __init__(self, game, chef):
        self.game, self.chef = game, chef
        self.rng = random.Random(f"{game.seed}-{chef}")  # chefs must not hesitate in step
        self.name = self.goal = None
        self.status, self.path = "No goal", ()
        self._tried = self._before = self._partner = None

    def set(self, name):
        """Choose a goal by name, or None to stop."""
        self.goal = goals(self.game)[name] if name else None
        self.name, self.path, self._tried = name, (), None
        self.status = "Starting" if name else "No goal"

    def act(self):
        """One action towards the goal."""
        player = self.game.state.players[self.chef]
        partner = self.game.state.players[1 - self.chef].position
        bumped = self._tried == "move" and self._before == player.position
        partner_moving = self._partner not in (None, partner)
        self._tried, self._before, self._partner, self.path = None, player.position, partner, ()
        if not self.goal:
            return self._make_way(player, partner_moving)
        reason, _ = plan(self.game, self.chef, self.goal)
        if reason:
            self.status = f"Stuck: {reason}"
            return self._make_way(player, partner_moving)
        legs = _legs(self.game, self.chef, self.goal)
        way = route(self.game, player.position, player.orientation, legs[0], {partner})
        direct = route(self.game, player.position, player.orientation, legs[0])
        if way is None:
            # The partner is in the only place we can use: get close, then wait beside them.
            self.status = "Waiting for the other chef to move"
            if direct.tiles[0] == partner:
                # Facing them signals the request; an occasional sidestep breaks mutual waiting.
                return self._sidestep(player) if self.rng.random() < 0.2 else direct.actions[0]
            way = direct
        else:
            self.status = "On the way"
        self.path = way.tiles
        if way.actions:
            # Two moving chefs can mirror each other's detours for ever unless one hesitates.
            detour = partner_moving and len(way.actions) > len(direct.actions)
            if way.tiles and (bumped or detour) and self.rng.random() < 0.4:
                return "stay"
            self._tried = "move" if way.tiles else None  # a turn on the spot cannot bump
            return way.actions[0]
        pot = self.game.state.objects.get(self.goal.pot) if self.goal.pot else None
        if self.goal.kind == "collect" and len(legs) == 1 and not pot.is_ready:
            self.status = "Waiting for the pot"
            return "stay"
        self.status = "Using"
        if len(legs) == 1:
            self._tried = "finish"
        return "interact"

    def _sidestep(self, player):
        floor = set(self.game.mdp.terrain_pos_dict[" "]) - set(self.game.state.player_positions)
        moves = [name for d, name in MOVES.items() if ahead(player.position, d) in floor]
        return self.rng.choice(moves) if moves else "stay"

    def _make_way(self, player, partner_moving):
        """With nothing to do, step aside for a partner who is walking into us."""
        partner = self.game.state.players[1 - self.chef]
        pushed = self.game.last[1 - self.chef] in MOVES.values() and not partner_moving
        facing_us = ahead(partner.position, partner.orientation) == player.position
        return self._sidestep(player) if pushed and facing_us else "stay"

    def after_step(self, previous):
        """Finish the goal if its last interaction worked. `previous` is the state before the step."""
        was, now = previous.players[self.chef].held_object, self.game.state.players[self.chef].held_object
        if self._tried == "finish" and (was and was.name) != (now and now.name):
            label = self.goal.label
            self.set(None)
            self.status = f"Done: {label}"
            return label
        return None
