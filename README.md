# Kitchen Teammate

A small two-player cooperative kitchen for exploring how an AI teammate plans and reprioritises
subgoals alongside a human. Rush orders change what matters mid-game.

## Run

Needs [uv](https://docs.astral.sh/uv/) and a web browser.

```
uv run iis-assignment            # opens http://127.0.0.1:8000
uv run iis-assignment 8123       # use another port
uv run python -m unittest discover tests
```

| | Move | Use |
|---|---|---|
| Chef 1 (blue) | W A S D | Space |
| Chef 2 (orange) | Arrow keys | Enter |

`Q` waits one tick. By default, time moves only when you do; **Live clock** makes it real-time.

## Rules

Put three of the same ingredient in a pot, wait 20 ticks, collect the soup with a dish and serve it
(20 points). A rush order doubles one soup type for 90 ticks. Mixed soups score 0. A game lasts 300
ticks, and the seed fixes its rush orders. The *Split* kitchen forces chefs to pass items over a counter.

## Status

| Part | State |
|---|---|
| Game, rush orders, browser interface, human control of both chefs | Implemented |
| AI teammate (chef 2 idles unless a human moves it) | Planned |
| Goal panel, instructions, override and conflict prompt | Planned |
| Evaluation against baselines | Planned |

## Code

- `src/iis_assignment/game.py`: layouts, rush orders, scoring, state view
- `src/iis_assignment/server.py`: local web server; `partner_action` is where the teammate plugs in
- `src/iis_assignment/static/index.html`: interface
- `src/overcooked_ai_py/`: third-party engine (see below)

## Development record

- **2026-11-25, environment.** Moved from a bomb-defusal scenario to a cooperative kitchen, to reuse a
  validated human-AI coordination environment and concentrate on planning. A full Overcooked-AI install
  needs Python 3.10, OpenCV, pygame and SciPy, so only its game logic is vendored (numpy is the sole
  dependency). Added rush orders because the original objectives do not change during play. Chose
  time-moves-when-you-do by default so that a slow planner will not penalise the player.

## Reused material

`src/overcooked_ai_py/` is an unmodified subset of
[Overcooked-AI](https://github.com/HumanCompatibleAI/overcooked_ai) (MIT licence, commit `739950a`),
except that the Gymnasium registration was removed from `__init__.py`. It accompanies Carroll et al. (2019),
*On the Utility of Learning about Humans for Human-AI Coordination*. The layouts, interface and artwork are original.
Code was written with assistance from Claude Code.
