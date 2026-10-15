# Changelog

All notable changes to **nour-code** are documented here. The project follows
semantic versioning: `MAJOR.MINOR.PATCH`.

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

* **CI is green again.** The workflow ran the bare `pytest` console script, so
  the repository root was not on `sys.path` and collection died with
  `ModuleNotFoundError: No module named 'nour_code'` (pytest exit code 4) on
  Python 3.11/3.12/3.13. The repo root is now importable from `pytest.ini`
  (`pythonpath = .`, mirrored in `pyproject.toml`) and CI calls
  `python -m pytest`, matching `make test`.
* Docs: the `/health` example reports the real version (`1.2.0`) and the test
  count is up to date; `.env.example` now lists the 1.2.0 agent controls
  (turn budgets, loop detection, approvals, verification, plan mode).
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
