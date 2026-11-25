# Kitchen Teammate

A small two-player cooperative kitchen for exploring how an AI teammate plans and reprioritises
subgoals alongside a human. A queue of orders with deadlines changes what matters mid-game.

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
lasts 300 ticks. Up to three orders are open at once, each for a different soup with its own deadline (60 to 90
ticks). An ordered soup pays double; a missed order only loses the bonus. The seed fixes every order. The
*Split* kitchen forces chefs to pass items over a counter.

| Recipe | Ingredients (any order) | Points |
|---|---|---|
| Onion soup | 3 onions | 20 |
| Tomato soup | 3 tomatoes | 20 |
| Mixed soup | 2 onions, 1 tomato | 30 |

Anything else scores 0.

![System diagram](docs/system-diagram.svg)

## How chef 2 acts

A planner never chooses moves. It chooses a named goal such as `add_onion_pot1` from a menu that gives
each goal's estimated cost, or the reason it is impossible. The goal layer then finds a path around the
other chef and carries the goal out. 

| Chef 2 decides by | Meaning |
|---|---|
| Jev (AI) | The Jev decision model gives each possible goal a probability |
| Rules | Hand-written scores over the same goals; also the fallback if Jev fails |
| Only what you ask | Follows direct instructions and nothing else |

It decides again only when something happens: it is free, stuck or blocked, an order arrives or closes, a soup is
ready, you change a pot, or you change a rule. Every decision and message is written to `logs/`.

## Talking with chef 2

Press `C` to type, or use the buttons. Who does what:

| Part | Role |
|---|---|
| Haiku | Turns your message into a structured act, and words some replies. Never decides. |
| Code | Works out what is possible and harmful, and enforces your rules |
| Jev (or the rules) | Scores your request against the alternatives |
| You | Have the last word on everything that is possible |

| Your request is | Chef 2 |
|---|---|
| Impossible | Refuses, says why and what would help |
| Harmful (spoils a pot, cannot finish in time) | Warns, and needs a button press to go ahead |
| A soup, with a clear cost (such as another order) | Objects; you choose |
| Much worse than another option, with a cost the code can name | Objects and suggests; you choose |
| Anything else | Does it |

A lasting rule ("leave the tomatoes to me") is enforced by code until undone. If it costs an order or a finished
soup, chef 2 keeps to it and asks about an exception. Chef 2 can also ask you to pass something, and respects "can't".

Jev gives no reasons, so explanations list the recorded facts and scores rather than a cause. Haiku sees only a
brief; a reply with a number or ingredient not in the brief, or a causal claim in an explanation, is replaced by a
template. This check is simple and cannot catch every bad wording. Without a key, templates and buttons are used.

## Status

| Part | State |
|---|---|
| Game, recipe book, order queue, human control of both chefs | Implemented |
| Goals, pathfinding, yielding to the other chef, manual goal panel | Implemented |
| Rule and Jev planners, ranked goals with probabilities, asking when unsure, decision log | Implemented |
| Typed messages, refusals and objections, lasting rules, help requests, explanations | Implemented |
| Informal trial against baselines, with a scripted chef 1 | Simulated |
| Speech input, learned player preferences, user study | Planned |

## Code

- `src/iis_assignment/game.py`: layouts, order queue, scoring, state view
- `src/iis_assignment/skills.py`: goals, pathfinding and the runner that carries out a goal
- `src/iis_assignment/planner.py`: state given to planners, rules, Jev, triggers and checks on every answer
- `src/iis_assignment/talk.py`: messages, verdict wording, explanations and help requests
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
- **2026-11-25, orders panel.** Rush orders moved out of the event list, where they were hard to find.
- **2026-11-25, conversation.** Chef 2 can now refuse, warn, object, explain and ask for help. Haiku is called through
  OpenRouter (about 1 s a call) after about 7 s through the command line. Trying real messages changed the design:
  "no wait, don't" was read as "do it anyway" and would have spoiled a pot, so harmful jobs now need a button press
  and saying no is its own act; a conflict message blamed the wrong job, so its brief now names both; and Haiku wrote
  "because" where Jev gave no reason, so causal explanations are rejected. Telling Jev what the player had asked for
  raised its score for that job in 4 of 4 checks (once from 2% to 11%, hiding an objection), so it now judges blind.
- **2026-11-25, after playing.** Playing showed that chef 2 obeyed vetoes silently (four vetoes left it idle into a
  rush), that chat knew single jobs but not soups or orders, and that it opened with onions almost every time. Vetoes
  now get the same scrutiny as requests and are raised when they block a rush; chat understands "make tomato soup"
  and "do the order" and can answer questions; the panel shows which soup a job goes towards. The onion habit was not
  caused by option order (shuffling barely changed it); adding each soup's total time to the facts fixed it. Mean over
  the same 5 seeds: open 226 to 254, split 182 to 188. Free answers invented reasons, so claims about the past,
  promises and causes are now rejected there too.
- **2026-11-25, telling instead of vetoing.** Veto buttons were removed from the interface and the panel for telling
  chef 2 what to do moved beside the game: a veto takes control, while a request chef 2 can answer is coordination. Lasting rules cover "leave that to me".
- **2026-11-25, order queue.** One order at a time meant that reprioritising was only "switch to the rush". Up to three
  orders now overlap, and the facts say which orders each job helps and whether one can still be met. Chef 2 compares
  orders when it objects, asks which one "the order" means, and can be told to leave one to the player. Orders are
  fixed by the seed whatever the players do, so planners can be compared on the same orders. First trial (5 seeds per
  kitchen, scripted rule-based chef 1, mean score and orders met of 30): open, rules 260 (21), Jev 272 (21); split, rules
  216 (17), Jev 234 (20). The gap is smaller than before the queue, and Jev is not repeatable: one seed gave 200 and then
  300 with the same settings. Scores from before the queue are not comparable.
- **2026-11-25, tidying.** Veto code removed. Option cards show one line; the full facts appear on hover and go to Jev.
  Chef 2 no longer asks "Not sure" between two ways to the same soup, or between working and waiting.

- **Submission.** Added the system diagram. Storyboard captures in `docs/storyboards/` use the live system with scripted input.

## Reused material

`src/overcooked_ai_py/` is an unmodified subset of
[Overcooked-AI](https://github.com/HumanCompatibleAI/overcooked_ai) (MIT licence, commit `739950a`),
except that the Gymnasium registration was removed from `__init__.py`. It accompanies Carroll et al. (2019),
*On the Utility of Learning about Humans for Human-AI Coordination*. The layouts, interface and artwork are original.
Code was written with assistance from Claude Code.
