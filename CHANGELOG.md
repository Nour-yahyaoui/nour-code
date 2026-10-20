# Changelog

All notable changes to **nour-code** are documented here. The project follows
semantic versioning: `MAJOR.MINOR.PATCH`.

## [Unreleased] — cold starts are visible, stopping actually stops

### Added

* **`status`/`waiting` heartbeats.** While the model has not produced a token
  (a cold turn loads ~6 GB of qwen3:8b first), `POST /chat` now emits a
  `status` event every `NOUR_CODE_WAIT_HEARTBEAT_SECONDS` (5 s) carrying
  `waited_ms` and `awaiting_first_token`. Before this, the stream was silent
  for the whole load and the UI looked frozen.
* **`NOUR_CODE_FIRST_TOKEN_TIMEOUT_SECONDS`** (default 300). If the first token
  never arrives, the turn ends with an actionable `error` event ("the model is
  most likely still loading — preload it with `POST /ollama/warmup`") instead
  of hanging until the 600 s request timeout.
* The console shows the wait with a live timer and, after ~8 s, the hint
  *loading the model into memory — the first call can take a minute*.

### Fixed

* **Stopping a turn now stops the model.** Cancelling a stream (Esc, tab
  closed, client disconnect) left the model/tool task running to completion
  because it lived in its own `task`; `_pump()` now cancels it when the
  consumer goes away. A cold generation could previously keep a CPU busy for
  minutes after the user had already hit stop.

## [Unreleased] — terminal chat, landing page

### Added

* **Terminal chat** — `python -m nour_code --chat`, `python -m nour_code.agent`
  or `python agent.py` from a checkout. Streams tokens, prints tool calls with
  their results, supports `/tools`, `/reset`, `/exit`; `--think` shows the
  model's reasoning and `--usage` the token counts. Running `agent.py` directly
  used to die with `ImportError: attempted relative import with no known parent
  package` ([#3](https://github.com/Nour-yahyaoui/nour-code/issues/3)); the
  module now re-runs itself inside the package instead of failing.
* **Landing page** in `landing/` — React 19 + TypeScript + Vite, no external
  requests. Live status panel fed by `GET /health` (pulled vs resident), a
  model warm-up button wired to `POST /ollama/warmup`, filterable endpoint
  reference, and the state handling from the UX skill: timeout + abort + stale
  guard, double-submit guard, slow indicator, delayed spinner, debounced filter,
  offline banner, per-section error boundaries, skeletons with the real height.

## [Unreleased] — model residency visibility

### Added

* **`python -m nour_code --doctor`** — a one-shot self-check: Python version,
  installed dependencies, whether the API port is free (or already served by
  nour-code), Ollama reachability, whether the model is pulled, whether it is
  **resident**, and whether the context Ollama allocated matches
  `NOUR_CODE_NUM_CTX`. Exits non-zero on failure, so it works in scripts.
* **`start.cmd`** — Windows launcher: creates `.venv`, installs the pinned
  requirements, runs the doctor and serves on `:8000`; `start.cmd --doctor`
  runs only the checks. A blank `curl` on Windows now has an explanation.
* **`model_running`** and **`model_expires_at`** in `GET /health`
  (`running_models` lists every resident model). `model_loaded` already meant
  *pulled*; the new field answers *in memory right now* via Ollama's
  `GET /api/ps`, so an idle model no longer looks like a missing one.
* **`POST /ollama/warmup`** — preload the model (and optionally keep it
  resident with `keep_alive=30m` / `-1`) through the native `/api/generate`
  endpoint, which honours `keep_alive`; `/v1/chat/completions` ignores it.
* **`POST /ollama/unload`** — free the memory immediately (`keep_alive: 0`).
* **`NOUR_CODE_KEEP_ALIVE`** — default `keep_alive` for warm-ups.
* The console status chip now distinguishes *resident*, *idle*
  (pulled but unloaded), *not pulled* and *offline*.

## [1.2.0] — agent upgrades

The theme of 1.2.0 is *control and visibility*: you can watch the model reason,
approve everything it wants to change, and see the files it produced.

### Added — real-time chat & reasoning

* **Thinking mode.** Qwen3's reasoning is parsed from the stream
  (`reasoning`, `reasoning_content` or `thinking` deltas) and emitted as
  `thinking` SSE events. The console shows it in a collapsible *thinking* block
  above every answer, and `POST /chat/sync` returns it in `ChatResponse.thinking`.
* **`usage` events** after every model call (`prompt_tokens`, `completion_tokens`,
  `total_tokens`, `iteration`), so the UI can show a live token counter.
* **`file_change` events** (`created` / `modified` / `deleted`) whenever a tool
  really changes a file, computed by checking the target *before* the call runs.
* **Workspace browser**: `GET /files?path=.&recursive=false&limit=300` lists the
  working directory inside the allowed roots; the console has a **files** tab
  that highlights anything touched in the last 10 minutes.

### Added — human-in-the-loop approvals

* A turn can ask for confirmation before acting: `"approval": "mutating"` parks
  every `write_file` / `apply_patch` / `delete_file` / `run_shell` call (and any
  skill tool that writes) until a human decides. `"all"` asks for everything.
* `tool_proposal` SSE events describe the pending call with its arguments, risk
  label (`low` / `medium` / `high`) and the timeout.
* `GET /sessions/{id}/approvals` lists pending proposals, and
  `POST /sessions/{id}/approve` resolves them (`approve`, `reject`,
  `approve_all`, `reject_all` — the `*_all` forms also flip the gate's
  auto-approve flag for the rest of the turn).
* Rejections are turned into `DENIED:` tool results so the model proposes
  something else instead of retrying the same call.

### Added — per-request controls

`POST /chat` and `POST /chat/sync` accept:

| Field | Meaning |
|---|---|
| `model` | run this turn on a different model |
| `temperature`, `seed` | sampling overrides |
| `enable_thinking` | turn Qwen3 thinking mode on/off per turn |
| `tools` | restrict the turn to an allow-list of tool names |
| `verify` | run the project's checks after the turn |
| `plan` | reserved for plan mode (see below) |
| `system_extra` | extra instructions appended to the system prompt |
| `approval` | `off` / `mutating` / `all` for this turn |

### Added — verification pass

* `detect_verify_command()` picks `python -m pytest -q`, `npm test --if-present`,
  `cargo test`, `go test ./...` or `make test` from the project's manifest files.
* With `verify: true` (or `NOUR_CODE_VERIFY_DEFAULT=true`) the command runs
  through the same permission-checked shell tool and its exit code and output are
  emitted as a `verification` event; failures become warnings.

### Added — loop and budget guards

* **Loop detection**: the same call with the same arguments
  `NOUR_CODE_LOOP_DETECTION_THRESHOLD` times in a row (default 3) ends the turn
  with `finish_reason="loop_detected"` instead of burning every iteration.
* **Time budget**: `NOUR_CODE_MAX_TURN_SECONDS` (default 900) →
  `finish_reason="time_budget"`.
* **Token budget**: `NOUR_CODE_MAX_TURN_TOKENS` (default 0 = unlimited) →
  `finish_reason="token_budget"`.

### Added — sampling controls

`NOUR_CODE_TOP_P`, `NOUR_CODE_TOP_K`, `NOUR_CODE_REPEAT_PENALTY`,
`NOUR_CODE_PRESENCE_PENALTY`, `NOUR_CODE_FREQUENCY_PENALTY`, `NOUR_CODE_STOP`
(comma separated) and `NOUR_CODE_ENABLE_THINKING` are forwarded to Ollama in
`options`, and can be overridden per turn through `ModelOptions`.

### Changed

* `ChatResponse` gained `thinking` and `verifications`.
* `/chat/sync` never waits for approvals (`approval` is forced to `off`): a
  blocking request cannot wait for a human.
* Audit writes are flushed before a turn response is returned, so `GET /audit`
  right after a call always shows it.
* Agent turns now summarise their token usage in the header of the console.

### Fixed

* JS/TS body insertion is now precise (statements used to land above the
  function when the body started on the next line) and the injected Python
  logger keeps PEP 8 spacing.
* `add_input_validation` composes with `add_error_handling` (a bare `raise` no
  longer counts as validation).
* `Metrics` uses a private Prometheus registry, so several instances can coexist.
* Production trigger detection ignores whitespace and line breaks.

### Reserved for the next minor

`plan: true` is accepted but not implemented yet — plan mode (the model writes
an ordered plan, the console ticks steps off as they complete) is the headline
feature of 1.3.0.

## [1.1.0]

* Production-ready mode: automatic hardening, placeholder remediation rounds and
  the readiness checklist.
* Skills: hot reload, `security_skill` and `production_skill`.
* Browser console: resizable panes, live event stream, timeline and inspector.

## [1.0.0]

* First release: agentic loop over an OpenAI-compatible Ollama endpoint,
  seven permission-checked tools, sessions, audit trail, JSON logs and metrics.
