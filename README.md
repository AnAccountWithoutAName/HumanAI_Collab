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

The AI teammate needs an [OpenRouter](https://openrouter.ai) key with credit, in a file called `.env`:
`OPENROUTER_API_KEY=...`. Without one, chef 2 uses the rules.

| | Move | Use |
|---|---|---|
| Chef 1 (blue) | W A S D | Space |
| Chef 2 (orange) | Arrow keys | Enter |

`Q` waits one tick. By default, time moves only when you do; **Live clock** makes it real-time.

## Rules

Fill a pot with three ingredients, wait 20 ticks, collect the soup with a dish and serve it. A game
lasts 300 ticks. A rush order doubles one recipe for 90 ticks, and the seed fixes which and when. The
*Split* kitchen forces chefs to pass items over a counter.

| Recipe | Ingredients (any order) | Points |
|---|---|---|
| Onion soup | 3 onions | 20 |
| Tomato soup | 3 tomatoes | 20 |
| Mixed soup | 2 onions, 1 tomato | 30 |

Anything else scores 0.

## How chef 2 acts

A planner never chooses moves. It chooses a named goal such as `add_onion_pot1` from a menu that gives
each goal's estimated cost, or the reason it is impossible. The goal layer then finds a path around the
other chef and carries the goal out. 

| Chef 2 decides by | Meaning |
|---|---|
| Jev (AI) | The Jev decision model gives each possible goal a probability |
| Rules | Hand-written scores over the same goals; also the fallback if Jev fails |
| Only what you ask | Follows direct instructions and nothing else |

It decides again only when something happens: it is free, stuck or blocked, a rush starts or ends, a soup is
ready, or you change a pot or veto a goal. Your direct instruction is not replaced by the AI's own choice. If Jev's
confidence is low while chef 2 is free, it asks you. Jev gives no reasons, so the panel shows facts computed from
the game instead. Every decision is written to `logs/`.

## Status

| Part | State |
|---|---|
| Game, recipe book, rush orders, human control of both chefs | Implemented |
| Goals, pathfinding, yielding to the other chef, manual goal panel | Implemented |
| Rule and Jev planners, ranked goals with probabilities, veto, asking when unsure, decision log | Implemented |
| Typed messages, standing orders, help requests, conflict prompt, Haiku wording | Planned |
| Evaluation against baselines | Planned |

## Code

- `src/iis_assignment/game.py`: layouts, rush orders, scoring, state view
- `src/iis_assignment/skills.py`: goals, pathfinding and the runner that carries out a goal
- `src/iis_assignment/planner.py`: state given to planners, rules, Jev, triggers and checks on every answer
- `src/iis_assignment/server.py`: local web server
- `src/iis_assignment/static/index.html`: interface
- `src/overcooked_ai_py/`: third-party engine (see below)

## Development record

- **2026-11-25, environment.** Moved from a bomb-defusal scenario to a cooperative kitchen, to reuse a
  validated human-AI coordination environment and concentrate on planning. A full Overcooked-AI install
  needs Python 3.10, OpenCV, pygame and SciPy, so only its game logic is vendored (numpy is the sole
  dependency). Added rush orders because the original objectives do not change during play. Chose
  time-moves-when-you-do by default so that a slow planner will not penalise the player.
- **2026-11-25, goals and navigation.** Trying the game showed it lacked a recipe book and that nothing
  could steer an AI chef. Added a recipe book, made mixed soup a real recipe worth more, and added the
  goal layer. A random two-chef test exposed two deadlocks: chefs mirroring each other's detours, and each
  waiting for the tile the other occupied. Fixed with independent hesitation and by letting a waiting chef
  move closer. An idle chef now also steps aside when walked into.
- **2026-11-25, choosing goals.** Added the rule and Jev planners. Chose Jev over Haiku for decisions after timing
  one call each: about 7 s for Haiku through the command line, 0.2 to 0.5 s for Jev. First trial, 5 seeds per
  kitchen with a scripted rule-based chef 1 (mean score): open, instruction-only with no instructions given 172,
  rules 184, Jev 226; split, 0, 154 and 182. Jev beat the rules in 9 of 10 games. This is a small trial with no
  people, seeds only change the rush orders, Jev's reply time was not counted, and the rules are simple.

## Reused material

`src/overcooked_ai_py/` is an unmodified subset of
[Overcooked-AI](https://github.com/HumanCompatibleAI/overcooked_ai) (MIT licence, commit `739950a`),
except that the Gymnasium registration was removed from `__init__.py`. It accompanies Carroll et al. (2019),
*On the Utility of Learning about Humans for Human-AI Coordination*. The layouts, interface and artwork are original.
Code was written with assistance from Claude Code.
