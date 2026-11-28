# The two-role demo

nour-code is a coding agent: it writes code, asks permission, runs the tests,
reads the failures and fixes them. This directory runs that whole loop for real,
with no model download and no human at the keyboard.

Two things are played:

| role | who | what it is |
|---|---|---|
| the local model | `landing/scripts/fake-ollama/daemon.py` | an OpenAI-compatible stand-in for Ollama, scripted through the `shop` scenario |
| the user | `demo/run_conversation.py` | drives the real `POST /chat` SSE stream and resolves proposals through the real approval endpoint |

Everything between them is the actual product: the same agent, the same
permission gate, the same tools, the same loop detector.

## Run it

```bash
# 1. the "model"
. demo/env.sh
python3 landing/scripts/fake-ollama/daemon.py --port 11934 --scenario shop \
  --fixture demo/workspaces/shop

# 2. nour-code itself, pointed at that model
. demo/env.sh
.venv/bin/python -m nour_code --port 8001

# 3. the user
.venv/bin/python demo/run_conversation.py
```

`demo/env.sh` puts the venv first on `PATH` (otherwise the agent cannot find
`pytest`, and it will — correctly — refuse to edit code it cannot test) and
points nour-code at the fake model and the demo's permissions file.

Output: `demo/transcript.md` (readable) and `demo/events.jsonl` (every SSE frame).

## What happens

Three acts. Each one is driven by what the tools actually returned, never by
searching the whole conversation for keywords — the first failure stays in an
append-only transcript forever, and a model that keeps re-reading it repeats
itself until the loop detector fires.

1. **Build.** Write `shop/app.py` and its tests, run them: 2 failed. Read the
   failure, propose the one-line fix. The user **rejects it** with a reason
   ("show me the diff"), so nothing is written; the agent reads the file, shows
   the changed line, and re-proposes. Approved, re-run: 3 pass.
2. **2050.** One self-contained page — no CDN, no web fonts, no build step —
   plus tests that assert the promises it makes: honest numbers, a keyboard
   path, motion opt-out. 8 pass.
3. **The bug it shipped.** The handler looks for the page next to `app.py`; the
   page is at the workspace root. `test_ui.py` passed anyway because it only
   read the file off disk. The user reports the 404, the agent writes a test
   that starts the server and fetches `/`, watches it fail, and fixes the path.
   9 pass.

Act 3 is not decoration: it is a bug that was really there. The first version of
this demo served `/` and returned `FileNotFoundError` while claiming the page was
"served at `/`" — its own tests never asked.

## Why the fake model is a state machine

The scenario keeps a stage and decides from the **tail** — the tool results after
the last assistant message — plus the names of the calls that produced them. That
is roughly what a real model does when it is working well: it looks at what just
happened, not at its entire past.

It also parses `"exit_code"` out of the `run_shell` payload rather than looking
for words in the output. `pytest -q` through a pipe prints only the progress dots
(`... [100%]`) — the words "9 passed" are simply not there. That is a real trap,
and it is why verification in the product now uses `-v`.

## The fixture

`demo/workspaces/shop` holds the **good** code. The scenario derives its three
states from it in order:

| state | sum | page path |
|---|---|---|
| `broken` | reads `i["unit_price"]` (a field nothing sets) | next to `app.py` |
| `fixed` | reads `i["price"]` | next to `app.py` |
| `served` | reads `i["price"]` | at the workspace root |

If the fixture is not the good version, the scenario refuses to start rather
than quietly acting out a story with no bug in it.

## 2050

The page is `demo/workspaces/shop/ui-2050.html`, served at `/` by the stdlib
server. `ui-2050.snapshot.html` is the same page with one real `/cart` response
inlined, for viewing without starting anything.
