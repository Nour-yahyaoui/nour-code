# nour-code — Local AI Coding Agent

A production-grade **local** AI coding agent server. `nour-code` puts a Qwen3 8B
model running on [Ollama](https://ollama.com) behind a FastAPI service and gives
it *controlled* access to your filesystem, your shell and an extensible set of
Python **skills**. Nothing leaves your machine except the traffic you explicitly
allow.

```
┌────────────┐   SSE    ┌──────────────────┐  OpenAI-compatible  ┌──────────────┐
│  client    │◀────────▶│  nour-code       │◀──────────────────▶│  Ollama      │
│ curl / UI  │  /chat   │  (FastAPI)       │   /v1/chat/...     │  qwen3:8b    │
└────────────┘          └────────┬─────────┘                    └──────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
  ┌───────────┐          ┌──────────────┐          ┌──────────────┐
  │ tools     │          │ skills       │          │ sessions,    │
  │ fs/shell/ │          │ security_    │          │ audit, logs, │
  │ patch     │          │ production_  │          │ metrics      │
  └─────┬─────┘          └──────┬───────┘          └──────────────┘
        ▼                       ▼
  ┌────────────────────────────────────────┐
  │ permissions.yaml  (allowed roots,      │
  │ shell allow/deny, per-tool switches)   │
  └────────────────────────────────────────┘
```

**Highlights**

* **Agentic loop** — message → model → tool call → tool result → answer, up to
  `NOUR_CODE_MAX_ITERATIONS` (default 15) iterations, streamed token by token
  over SSE or returned in one shot.
* **Seven built-in tools** — `read_file`, `write_file`, `list_directory`,
  `search_files`, `run_shell`, `apply_patch`, `delete_file` — all Pydantic
  validated, permission checked, audited and output-capped.
* **Hot-reloadable skills** — drop a `.py` file into the skills directory
  (or `POST /skills/reload`) and its tools appear in the model's tool list.
  A broken skill is logged and skipped; it can never take the server down.
* **Production-ready mode** — say *"make it production ready"* and the agent
  injects logging, error handling, input validation, a Dockerfile and a CI
  pipeline, then refuses to hand back TODO stubs and appends a checklist.
* **Project-aware** — the system prompt carries your working directory, a shallow
  file listing, the loaded skills, the active permission scope and stack hints
  for Node.js / Next.js / Actix Web / PostgreSQL.
* **Auditable** — JSON rotating logs, a JSONL audit trail of every tool call and
  optional Prometheus metrics.

---

## Table of contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Run](#3-run)
4. [Configuration](#4-configuration)
5. [Permissions](#5-permissions)
6. [API](#6-api) — [approvals](#61-human-in-the-loop-approvals) · [per-request controls](#62-per-request-controls) · [guards](#63-guard-rails)
7. [Streaming events (SSE)](#7-streaming-events-sse)
8. [Built-in tools](#8-built-in-tools)
9. [Skills](#9-skills)
10. [Production-ready mode](#10-production-ready-mode)
11. [Sessions](#11-sessions)
12. [Observability](#12-observability)
13. [Docker](#13-docker)
14. [Security model](#14-security-model)
15. [Development & tests](#15-development--tests)
16. [Troubleshooting](#16-troubleshooting)
17. [Assumptions & limitations](#17-assumptions--limitations)

---

## 1. Requirements

| Component  | Version                        | Notes |
|------------|--------------------------------|-------|
| Python     | **3.11+**                      | 3.12 works too |
| Ollama     | recent (0.4+ recommended)      | `ollama serve` running locally |
| Model      | **qwen3:8b** (default)         | `ollama pull qwen3:8b` |
| OS         | Linux, macOS, Windows          | Windows paths fully supported |
| Optional   | `git`, `patch`, `npm`, `docker`| used by `apply_patch` and the skills |

Pull the model once:

```bash
ollama pull qwen3:8b
ollama serve          # listens on http://localhost:11434
```

## 2. Install

```bash
git clone https://github.com/Nour-yahyaoui/nour-code.git
cd nour-code

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt    # pinned versions
# optional, for the test suite / linting:
pip install -r requirements-dev.txt
```

Or install as a package (adds the `nour-code` console script):

```bash
pip install -e ".[dev]"
```

## 3. Run

```bash
# recommended
python -m uvicorn nour_code.main:app --host 0.0.0.0 --port 8000

# or the bundled CLI (same thing, with --print-config / --reload / --workers)
python -m nour_code --port 8000
nour-code --port 8000              # if installed with pip

# development, auto-reload on file changes
python -m uvicorn nour_code.main:app --reload --port 8000
```

Then open:

* **<http://localhost:8000>** — browser console (send prompts, watch the agent work live)
* **<http://localhost:8000/docs>** — interactive OpenAPI documentation
* **<http://localhost:8000/health>** — Ollama + model status

### Browser console

`GET /` serves a single-page client (`nour_code/static/console.html`) that talks to
the same JSON/SSE API:

* **Two resizable panes** — drag the vertical gutter to rebalance *agent* vs
  *console*, and the horizontal one to grow the event inspector. Both positions
  are remembered in `localStorage`.
* **Live transcript** — tokens stream in as they arrive; every tool call becomes
  a card showing its arguments, then its status, duration and full output.
* **Console pane** — every SSE frame (`token`, `tool_call`, `tool_result`,
  `skill_loaded`, `error`, `done`) with a timestamp and a one-line summary;
  click a row to inspect the raw payload. Filter with free text, `type:tool_call`
  or `tool:read_file`, and switch to the **timeline** (tool calls in order) or
  **skills & tools** tab.
* **Live counters, stop and export** — `N events · K tools · Ss`, `Esc`/`stop`
  aborts the stream (the server cancels the model call), and **export** downloads
  the turn as JSONL.
* Shortcuts: `Ctrl`/`⌘ + Enter` streams the message, `Esc` stops it.

Check that everything is wired:

```bash
curl -s http://localhost:8000/health | jq
```

```json
{
  "status": "ok",
  "app": "nour-code",
  "version": "1.2.0",
  "uptime_seconds": 12.4,
  "ollama_reachable": true,
  "ollama_url": "http://localhost:11434",
  "model": "qwen3:8b",
  "model_loaded": true,
  "skills_loaded": 2,
  "active_sessions": 0,
  "tools": 15
}
```

## 4. Configuration

Every setting is an environment variable prefixed with `NOUR_CODE_`. A `.env`
file in the working directory is read automatically (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `NOUR_CODE_MODEL` | `qwen3:8b` | Ollama model name |
| `NOUR_CODE_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama base URL |
| `NOUR_CODE_OLLAMA_API_KEY` | `ollama` | Bearer token (Ollama ignores it) |
| `NOUR_CODE_TEMPERATURE` | `0.1` | Low temperature = deterministic code |
| `NOUR_CODE_NUM_CTX` | `16384` | Context window passed to Ollama |
| `NOUR_CODE_NUM_PREDICT` | unset | Cap on generated tokens |
| `NOUR_CODE_MAX_TURN_SECONDS` | `900` | Wall-clock budget for one turn (`time_budget`) |
| `NOUR_CODE_MAX_TURN_TOKENS` | `0` | Token budget for one turn, `0` = unlimited (`token_budget`) |
| `NOUR_CODE_LOOP_DETECTION_THRESHOLD` | `3` | Identical calls in a row that count as a stuck loop |
| `NOUR_CODE_APPROVAL_MODE` | `off` | `off` / `mutating` / `all` default for every request |
| `NOUR_CODE_APPROVAL_TIMEOUT_SECONDS` | `300` | How long a proposal waits for a human |
| `NOUR_CODE_VERIFY_DEFAULT` | `false` | Run the project checks after every turn |
| `NOUR_CODE_VERIFY_TIMEOUT_SECONDS` | `180` | Timeout for that command |
| `NOUR_CODE_PLAN_MODE` | `auto` | Reserved for plan mode (1.3.0) |
| `NOUR_CODE_MAX_CONCURRENT_TURNS` | `4` | Parallel agent turns allowed |
| `NOUR_CODE_TOP_P` | – | Nucleus sampling, forwarded to Ollama |
| `NOUR_CODE_TOP_K` | – | Top-k sampling |
| `NOUR_CODE_REPEAT_PENALTY` | – | Repetition penalty |
| `NOUR_CODE_PRESENCE_PENALTY` | – | Presence penalty |
| `NOUR_CODE_FREQUENCY_PENALTY` | – | Frequency penalty |
| `NOUR_CODE_STOP` | – | Comma separated stop sequences |
| `NOUR_CODE_ENABLE_THINKING` | – | Qwen3 thinking mode on/off |
| `NOUR_CODE_MAX_ITERATIONS` | `15` | Tool-call iterations per turn |
| `NOUR_CODE_MAX_TOOL_OUTPUT_CHARS` | `100000` | Hard cap on any tool result |
| `NOUR_CODE_MAX_TOOL_HISTORY_CHARS` | `20000` | Cap per tool message kept in history |
| `NOUR_CODE_MAX_HISTORY_MESSAGES` | `200` | History trimming threshold |
| `NOUR_CODE_PARALLEL_TOOL_CALLS` | `false` | Let the model emit several calls at once |
| `NOUR_CODE_HOST` / `NOUR_CODE_PORT` | `0.0.0.0` / `8000` | Bind address and port |
| `NOUR_CODE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `NOUR_CODE_CORS_ORIGINS` | `*` | Comma separated allow-list |
| `NOUR_CODE_ALLOWED_ROOTS` | unset | Overrides `permissions.yaml` roots (`:` / `;` separated) |
| `NOUR_CODE_WORKING_DIR` | cwd | Default session working directory |
| `NOUR_CODE_PERMISSIONS_FILE` | `./permissions.yaml` | Permission scope file |
| `NOUR_CODE_SKILLS_DIRS` | `nour_code/skills`, `./skills` | Skill search path |
| `NOUR_CODE_SESSION_TTL_SECONDS` | `7200` | Idle sessions expire after 2 h |
| `NOUR_CODE_SESSIONS_FILE` | `./nour-code-sessions.json` | Session persistence |
| `NOUR_CODE_LOG_DIR` | `./logs` | Where `nour-code.log` and the audit file live |
| `NOUR_CODE_METRICS_ENABLED` | `true` | Serve `GET /metrics` |
| `NOUR_CODE_AUDIT_ENABLED` | `true` | Write `logs/nour-code-audit.jsonl` |
| `NOUR_CODE_PRODUCTION_AUTO_HARDEN` | `true` | Auto-run the production skill in production mode |
| `NOUR_CODE_PRODUCTION_MAX_REMEDIATION_ROUNDS` | `2` | Extra attempts to remove placeholders |
| `NOUR_CODE_SKILL_TOOL_TIMEOUT_SECONDS` | `180` | Timeout per tool (and per skill dispatch) |

Print the resolved configuration:

```bash
python -m nour_code --print-config
```

## 5. Permissions

`permissions.yaml` is the single source of truth. It is loaded at startup and
consulted before **every** filesystem, shell and patch operation.

```yaml
version: 1
allowed_roots:
  - C:/Users/HP/projects      # Windows roots from the spec …
  - C:/Users/HP/sandbox
  # - /home/me/projects       # … add POSIX roots for Linux/macOS
filesystem:
  max_read_bytes: 1000000
  max_write_bytes: 2000000
  denied_globs: ["**/.ssh/id_rsa", "**/*.pem", "**/.env"]
  ignore_dirs: [.git, node_modules, __pycache__, .venv, dist, build, target, .next]
shell:
  allowed_commands: [npm, npx, node, cargo, git, python, pip, docker]
  denied_patterns: ["rm -rf", "format", "del /f /s /q C:"]
  default_timeout_seconds: 60
  max_timeout_seconds: 600
network:
  allow_outbound: true
tools:
  delete_file: false      # destructive: opt-in
  run_shell: true
  read_file: true
  write_file: true
  list_directory: true
  search_files: true
  apply_patch: true
```

How it behaves:

* **Roots that do not exist on this machine are skipped** (with a warning in the
  log). The two Windows roots above are therefore harmless on Linux/macOS; if
  that leaves no usable root, nour-code falls back to the process working
  directory plus `<cwd>/sandbox`. Override with
  `NOUR_CODE_ALLOWED_ROOTS=/srv/app:/home/me/code`.
* A path is usable only if it **resolves** (symlinks followed) to somewhere
  inside an allowed root. `../` escapes, absolute paths outside the roots,
  Windows drive letters on POSIX hosts (and vice versa) and symlinks that point
  outside a root are all rejected with a `DENIED: …` message the model can read.
* `run_shell` validates the command **before** spawning: the executable must be
  in `shell.allowed_commands`, the whole command line must not match any
  `shell.denied_patterns` regex (case-insensitive), the `cwd` must be inside an
  allowed root, and the timeout is clamped to `max_timeout_seconds`.
* `network.allow_outbound: false` makes network-facing commands (`npm`, `pip`,
  `docker`, `curl`, …) fail fast instead of hanging.
* Any tool can be switched off individually under `tools:`.

> **Gotcha:** the literal deny pattern `format` (required by the spec) also
> matches `git log --format=…`. Swap it for `(^|\s)(format|mkfs)(\s|$)` in
> `permissions.yaml` if you need `--format` back — the file documents this
> inline.

## 6. API

All endpoints return JSON except `/chat`, which is an SSE stream, and `/metrics`,
which is Prometheus text.

### `POST /chat` — stream a turn (SSE)

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "list the python files and summarise the entrypoint"}'
```

```bash
# resume a session, force production mode, cap the iterations
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "make it production ready", "session_id": "sess_ab12cd34ef56",
       "production_ready": true, "max_iterations": 8}'
```

Body:

| Field | Type | Description |
|---|---|---|
| `message` | string (required) | The user prompt |
| `session_id` | string | Resume an existing session; a new one is created when omitted |
| `working_dir` | string | Override the working directory for this turn (must be inside an allowed root) |
| `max_iterations` | int | Override `NOUR_CODE_MAX_ITERATIONS` |
| `production_ready` | bool | Force production mode on/off instead of auto-detecting |
| `metadata` | object | Free-form metadata stored on the session |

### `POST /chat/sync` — same turn, single response

```bash
curl -s -X POST http://localhost:8000/chat/sync \
  -H 'Content-Type: application/json' \
  -d '{"message": "what files are in this folder?"}' | jq
```

```json
{
  "session_id": "sess_9f2c1a7b3d44",
  "content": "There are 12 files …",
  "iterations": 2,
  "finished": true,
  "finish_reason": "stop",
  "tool_calls": [
    {"id": "call_0", "tool": "list_directory", "arguments": {"path": "."},
     "status": "ok", "result": "…", "error": null, "duration_ms": 4.1,
     "iteration": 1, "source": "builtin", "skill": null}
  ],
  "warnings": [],
  "production_ready": false,
  "production_checklist": [],
  "usage": {"prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908,
            "calls": 2, "total_latency_ms": 1830.5},
  "duration_ms": 1902.7
}
```

### `GET /skills` — list loaded skills

```bash
curl -s http://localhost:8000/skills | jq
```

### `POST /skills/reload` — hot reload

```bash
curl -s -X POST http://localhost:8000/skills/reload | jq
# {"ok": true, "loaded": ["security_skill (security_scan, check_secrets, check_dependency_cves)"],
#  "skipped": [], "errors": [], "duration_ms": 12.4}
```

### `GET /sessions` / `DELETE /sessions/{id}`

```bash
curl -s http://localhost:8000/sessions | jq
curl -s -X DELETE http://localhost:8000/sessions/sess_9f2c1a7b3d44 | jq
```

### `GET /health`

```bash
curl -s http://localhost:8000/health | jq '{status, ollama_reachable, model_loaded, tools}'
```

### `GET /audit` — tail the audit log

```bash
curl -s 'http://localhost:8000/audit?limit=20' | jq '.entries[-3:]'
```

```json
[{"ts": "2026-10-14T09:12:44Z", "ts_epoch": 1760434364.12, "session_id": "sess_9f2c1a7b3d44",
  "tool": "write_file", "args_hash": "4f3c9a1de8b2c7aa", "result_status": "ok",
  "duration_ms": 3.42, "iteration": 1, "source": "builtin", "skill": null}]
```

### `GET /metrics`

```bash
curl -s http://localhost:8000/metrics | grep nour_code | head
```

### `GET /files`

Read-only listing of the workspace (resolved inside the allowed roots):

```bash
curl -s "http://localhost:8000/files?path=.&recursive=true&limit=300" | jq
```

```json
{"root": "/home/user/project", "path": "/home/user/project", "count": 2, "truncated": false,
 "entries": [{"name": "app.py", "path": "/home/user/project/app.py", "type": "file",
              "size": 812, "modified": 1760434364.12}]}
```

### `POST /sessions/{id}/approve`

With `"approval": "mutating"` on `/chat`, every call that changes the machine is
parked until you decide (see [section 6.1](#61-human-in-the-loop-approvals)).

```bash
# what is waiting?
curl -s http://localhost:8000/sessions/sess_9f2c1a7b3d44/approvals | jq

# approve one, or everything still to come
curl -s -X POST http://localhost:8000/sessions/sess_9f2c1a7b3d44/approve \
  -H 'Content-Type: application/json' -d '{"proposal_id": "prop_ab12cd34", "decision": "approve"}'

curl -s -X POST http://localhost:8000/sessions/sess_9f2c1a7b3d44/approve \
  -H 'Content-Type: application/json' -d '{"decision": "approve_all"}'
```

Decisions: `approve`, `reject`, `approve_all`, `reject_all` (the `*_all` forms
also set the auto-approve flag for the rest of the turn). A proposal that is not
answered within `NOUR_CODE_APPROVAL_TIMEOUT_SECONDS` (default 300) is rejected
and the model is told to propose something else.

### Errors

Errors are always JSON: `{"error": "...", "code": "...", "detail": "..."}` with
`403` (permission denied), `404` (unknown session), `422` (request validation),
`502` (Ollama error), `503` (Ollama unreachable / metrics disabled) or `500`.

## 7. Streaming events (SSE)

`/chat` emits named events; the `data:` line is a JSON object:

| Event | Payload | When |
|---|---|---|
| `thinking` | `{text, iteration}` | The model's reasoning (Qwen3 thinking mode), streamed as it is produced |
| `token` | `{text, iteration}` | Every chunk the model produces, forwarded immediately |
| `tool_call` | `{id, tool, arguments, iteration, skill}` | Before a tool runs |
| `tool_result` | `{id, tool, status, result, error, duration_ms, iteration, skill, rejected?}` | After it finishes |
| `tool_proposal` | `{id, tool, arguments, iteration, skill, risk, timeout_seconds}` | A mutating call is waiting for your approval |
| `skill_loaded` | `{name, description, triggers, tools, source, version}` | On the first turn of a session, and after a reload |
| `usage` | `{iteration, prompt_tokens, completion_tokens, total_tokens, elapsed_ms}` | After every model call |
| `verification` | `{command, exit_code, stdout, stderr, status, output}` | Result of the project checks (`verify: true`) |
| `file_change` | `{tool, path, action, iteration, call_id}` | A file was created, modified or deleted |
| `error` | `{message, code, fatal}` | Model/agent failure; the stream still ends with `done` |
| `done` | `{response: {...ChatResponse...}}` | Always last |

`status` is `ok`, `error` or `denied`. Keep-alive comments (`: keepalive`) are
sent every 15 s so proxies do not drop long tool calls.

```bash
curl -N -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "read README.md and count the sections"}' | grep -E '^event:'
```

Browser example:

```js
const response = await fetch('/chat', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({message: 'explain this repo'}),
});
const reader = response.body.getReader();
// parse "event: <type>\ndata: <json>\n\n" frames …
```

### 6.1 Human-in-the-loop approvals

Ask for a plan you can veto. Send `"approval": "mutating"` (or `"all"`) with
`POST /chat` and the agent will:

1. emit a `tool_proposal` event instead of running the call,
2. show an **approve / reject / approve all** card in the console,
3. wait up to `NOUR_CODE_APPROVAL_TIMEOUT_SECONDS` for
   `POST /sessions/{id}/approve`,
4. run it on approval — or turn the rejection into a `DENIED:` tool result so the
   model proposes a different approach.

Mutating means: `write_file`, `apply_patch`, `delete_file`, `run_shell` and any
skill tool that rewrites files (`security_skill`'s scanners are explicitly
read-only). Set `NOUR_CODE_APPROVAL_MODE=mutating` to make it the default for
every request.

### 6.2 Per-request controls

```bash
curl -s -X POST http://localhost:8000/chat/sync -H 'Content-Type: application/json' -d '{
  "message": "add a health endpoint",
  "model": "qwen3:14b",
  "temperature": 0.2,
  "enable_thinking": true,
  "tools": ["read_file", "write_file", "apply_patch"],
  "verify": true,
  "system_extra": "Answer with file paths and a short diff summary.",
  "approval": "mutating"
}' | jq '{iterations, thinking, finish_reason, verifications}'
```

`tools` filters the tool list sent to the model for that turn, `verify` runs the
project's own checks afterwards (`python -m pytest -q`, `npm test --if-present`,
`cargo test`, `go test ./...` or `make test`, detected from the manifests), and
`system_extra` is appended to the system prompt.

### 6.3 Guard rails

| Guard | Setting | Result |
|---|---|---|
| Iterations | `NOUR_CODE_MAX_ITERATIONS` (15) | `finish_reason="max_iterations"` + warning |
| Stuck loop | `NOUR_CODE_LOOP_DETECTION_THRESHOLD` (3) | `finish_reason="loop_detected"` |
| Wall clock | `NOUR_CODE_MAX_TURN_SECONDS` (900) | `finish_reason="time_budget"` |
| Tokens | `NOUR_CODE_MAX_TURN_TOKENS` (0 = off) | `finish_reason="token_budget"` |
| Approvals | `NOUR_CODE_APPROVAL_TIMEOUT_SECONDS` (300) | proposal rejected |

## 8. Built-in tools

| Tool | Arguments | Notes |
|---|---|---|
| `read_file` | `path`, `max_chars=100000`, `encoding="utf-8"` | Refuses binary files; size cap from `filesystem.max_read_bytes` |
| `write_file` | `path`, `content`, `create_parents=true`, `append=false` | Atomic write (temp file + `os.replace`); parent dirs created |
| `list_directory` | `path="."`, `recursive=false`, `max_entries=500` | Skips `ignore_dirs`; returns name/type/size |
| `search_files` | `root`, `pattern`, `content_regex?`, `max_results=200` | Glob search, optional regex over file contents |
| `run_shell` | `command`, `cwd?`, `timeout_seconds=60`, `env?` | Allow-listed executables, deny-list regexes, process-group kill on timeout |
| `apply_patch` | `path`, `diff`, `create_backup=false`, `dry_run=true?` | `git apply` → `patch` → built-in unified-diff engine |
| `delete_file` | `path`, `recursive=false` | **Disabled by default** (`tools.delete_file: false`) |

Tool results are capped at `NOUR_CODE_MAX_TOOL_OUTPUT_CHARS` (100 KB by default)
with a `...[nour-code: output truncated, N chars removed]...` marker, so a
runaway `cat` can never blow up the context window.

`run_shell` returns `{command, cwd, executable, exit_code, timed_out, stdout,
stderr, truncated}`. Non-zero exits keep the payload and report
`ERROR: command exited with N` plus the tail of stdout/stderr, so skills that
parse command output (`npm audit --json`) still work.

## 9. Skills

A skill is a single `.py` file that declares metadata plus a dispatcher. Drop it
into a skills directory and its tools join the model's tool list.

```python
SKILL_NAME = "security_review"
SKILL_DESCRIPTION = "..."
SKILL_TRIGGERS = ["when user asks for a security review", "before deploy"]
SKILL_TOOLS = [ ...list of tool schemas... ]

async def handle(tool_name: str, args: dict, ctx: dict) -> dict:
    ...
```

* **Discovery** — every `*.py` file in `NOUR_CODE_SKILLS_DIRS`
  (default: `nour_code/skills/` then `./skills/`) except `__init__.py` and
  anything starting with `_`.
* **Hot reload** — `POST /skills/reload` re-imports everything; skill tools are
  unregistered and re-registered, and clients get fresh `skill_loaded` events.
* **Failure isolation** — a skill that raises on import, lacks `SKILL_NAME`, has
  a non-async `handle`, or claims a tool name already taken is **logged and
  skipped**; the server keeps serving.
* **Context** — `ctx` carries `tools` (facade over the built-ins),
  `permissions`, `settings`, `session_id`, `working_dir`, `logger`, `skills`,
  `registry` and `emit`.
* **Return values** — return any JSON-serialisable dict.
  `{"ok": False, "error": "..."}` is surfaced to the model as `ERROR: …`;
  add `"denied": True` for a permission refusal, which becomes `DENIED: …`.

### Bundled skills

**`security_skill`** (read-only)

| Tool | What it does |
|---|---|
| `security_scan(path)` | Walks source files and flags shell injection, `eval`/`pickle`, disabled TLS verification, SQL built by concatenation, XSS sinks, weak hashes, hard-coded credentials, permissive CORS, debug flags, plaintext HTTP, secrets in logs, path traversal, disabled CSRF. |
| `check_secrets(path)` | Detects AWS keys, GitHub/GitLab/Slack/npm/PyPI/Google/Stripe/SendGrid tokens, private key blocks, JWTs, connection strings with inline passwords, hard-coded password literals and high-entropy strings. **Evidence and snippets are redacted** so findings are safe to log. |
| `check_dependency_cves(package_json_path)` | Runs `npm audit --json` in the project and groups advisories by severity with fixable packages and next steps. Reports `npm_unavailable` explicitly when npm/network is missing rather than pretending the project is clean. |

**`production_skill`** (writes files)

| Tool | What it does |
|---|---|
| `add_logging(file_path)` | Ensures a logger exists (`logging.getLogger(__name__)` for Python, pino/winston/structured-console for JS/TS) and every function logs its entry. |
| `add_error_handling(file_path)` | Wraps functions that do I/O, network or parsing in `try/except` (Python) or `try/catch` (JS/TS) that logs with context and re-raises. |
| `add_input_validation(file_path)` | Adds `isinstance`/`TypeError` checks for annotated Python parameters and null-guard clauses for JS/TS parameters. |
| `generate_dockerfile(project_root, runtime)` | Writes a multi-stage, non-root, health-checked `Dockerfile` for Node/Next.js, Python or Rust, plus a `.dockerignore`. |
| `generate_ci_pipeline(project_root, target)` | Writes `.github/workflows/ci.yml` (or `.gitlab-ci.yml`) with dependency caching, lint, tests against a PostgreSQL service, a build step and a container build. |

All three code transformations are **idempotent** (they report
`already_compliant` on a second run) and **safe**: the rewritten file is only
written if it still parses (Python) or its braces still balance (JS/TS).

### Writing your own

```bash
cp SKILL_TEMPLATE.py skills/my_skill.py
$EDITOR skills/my_skill.py
curl -s -X POST http://localhost:8000/skills/reload | jq
curl -s http://localhost:8000/skills | jq '.skills[] | {name, tools}'
```

A minimal skill:

```python
SKILL_NAME = "line_counter"
SKILL_DESCRIPTION = "Count the lines of code in a project, grouped by language."
SKILL_TRIGGERS = ["when the user asks how big the codebase is"]
SKILL_TOOLS = [{
    "type": "function",
    "function": {
        "name": "count_lines",
        "description": "Count lines per file extension under a directory.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False},
    },
}]

async def handle(tool_name, args, ctx):
    if tool_name != "count_lines":
        return {"ok": False, "error": f"unknown tool {tool_name!r}"}
    try:
        root = ctx["permissions"].resolve_path(args["path"], must_exist=True, kind="dir")
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": str(exc)}

    from collections import Counter
    counts = Counter()
    for file in root.rglob("*"):
        if file.is_file() and "node_modules" not in file.parts:
            try:
                counts[file.suffix or "(no extension)"] += sum(1 for _ in file.open(errors="ignore"))
            except OSError:
                continue
    return {"ok": True, "by_extension": dict(counts.most_common(20))}
```

You can also define tool schemas with Pydantic models — pass the class in
`SKILL_TOOLS` and its JSON schema is derived automatically (name from
`tool_name`/class name, description from `tool_description`/docstring).

## 10. Production-ready mode

Triggered automatically when the message matches phrases such as
*"production ready"*, *"make it production"*, *"prepare for production"*,
*"production-grade"*, *"ship to production"*, *"deploy ready"* or *"harden this"*.
It can also be forced per request with `"production_ready": true`.

When active:

1. A `PRODUCTION-READY MODE` instruction block is injected into the system
   prompt for that turn: structured logging, explicit error handling, input
   validation, environment-driven configuration, `/health` + `/metrics`
   endpoints, tests, docs — and **no placeholders**.
2. After the model finishes, the agent automatically runs the production skill's
   `add_logging`, `add_error_handling` and `add_input_validation` on every source
   file the model touched (falling back to the project's top-level sources).
   Mentioning *docker*/*image* also generates a `Dockerfile`; mentioning
   *CI*/*pipeline*/*workflow* also generates the pipeline.
3. The touched files are scanned for `TODO`, `FIXME`, `XXX`, `NotImplementedError`,
   *"your code here"*, *lorem ipsum*, … If any remain, the agent asks the model to
   fix them (up to `NOUR_CODE_PRODUCTION_MAX_REMEDIATION_ROUNDS` extra rounds).
4. A checklist is appended to the answer, computed from the real project state:

```
### Production-readiness checklist
- [x] Structured logging present (no print/console.log in production paths) — found in /app/service.py
- [x] Errors handled explicitly and logged with context — found in /app/service.py
- [ ] Input validated at every boundary — no validation found
- [ ] Health/liveness endpoint exposed — no /health endpoint found
- [x] No TODO / FIXME / placeholder stubs — clean
- [ ] Dockerfile present — no Dockerfile
- [ ] CI pipeline present — no .github/workflows found

**4 item(s) still open.** Resolve them before shipping.
```

Set `NOUR_CODE_PRODUCTION_AUTO_HARDEN=false` to keep the instructions but skip
the automatic tool invocation.

## 11. Sessions

* In-memory dict `session_id -> Session(messages, created_at, last_active,
  working_dir, tool_call_count)`.
* A background sweeper drops sessions idle for more than
  `NOUR_CODE_SESSION_TTL_SECONDS` (2 h by default).
* On shutdown the store is written to `nour-code-sessions.json` (atomic
  replace) and reloaded on startup; corrupt or expired records are ignored.
* Concurrent requests for the same session serialise on a per-session lock, so
  history can never interleave.
* History is trimmed to `NOUR_CODE_MAX_HISTORY_MESSAGES` (system message kept).

```bash
# start a session and reuse it
SID=$(curl -s -X POST http://localhost:8000/chat/sync -H 'Content-Type: application/json' \
  -d '{"message":"remember: the entrypoint is main.py"}' | jq -r .session_id)
curl -s -X POST http://localhost:8000/chat/sync -H 'Content-Type: application/json' \
  -d "{\"message\":\"what is the entrypoint?\",\"session_id\":\"$SID\"}" | jq -r .content
```

## 12. Observability

| Artefact | Path / endpoint | Content |
|---|---|---|
| Application log | `logs/nour-code.log` | JSON lines, rotating 10 MB × 5 (`ts`, `level`, `logger`, `message`, plus structured extras) |
| Audit trail | `logs/nour-code-audit.jsonl` | One object per tool call: `ts`, `session_id`, `tool`, `args_hash`, `result_status`, `duration_ms`, `iteration`, `source`, `skill`, `error` |
| Metrics | `GET /metrics` | `nour_code_requests_total`, `nour_code_model_calls_total`, `nour_code_model_latency_seconds`, `nour_code_model_tokens_total`, `nour_code_tool_calls_total`, `nour_code_tool_latency_seconds`, `nour_code_agent_iterations_total`, `nour_code_sessions_active`, `nour_code_errors_total` |
| Health | `GET /health` | Ollama reachability, version, whether the model is pulled, loaded skills/tools, active sessions |

Arguments are stored as a **SHA-256 fingerprint** (`args_hash`), never verbatim,
so the audit trail can be shipped without leaking file contents. Every model call
also logs `iteration`, `latency_ms`, `prompt_tokens` and `completion_tokens`.

`/metrics` returns `503` when `prometheus-client` is not installed or
`NOUR_CODE_METRICS_ENABLED=false` — the server works fine either way.

```bash
tail -f logs/nour-code.log | jq
tail -1 logs/nour-code-audit.jsonl | jq
```

## 13. Docker

The image talks to Ollama on the **host** via `host.docker.internal`.

```bash
docker build -t nour-code:latest .

docker run --rm -p 8000:8000 \
  --add-host=host.docker.internal:host-gateway \   # required on Linux
  -v "$PWD/permissions.yaml:/app/permissions.yaml:ro" \
  -v "$HOME/projects:/data/projects" \
  -e NOUR_CODE_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  nour-code:latest
```

Or with Compose (see `docker-compose.yml`):

```bash
docker compose up --build
curl -s http://localhost:8000/health | jq
```

Mount the code you want the agent to work on and add that mount path to
`allowed_roots` (e.g. `NOUR_CODE_ALLOWED_ROOTS=/data/projects`).

> On Docker Desktop (macOS/Windows) `host.docker.internal` resolves
> automatically; on Linux you need either `--add-host` (shown above) or
> `extra_hosts: ["host.docker.internal:host-gateway"]`.

## 14. Security model

| Threat | Mitigation |
|---|---|
| Path traversal (`../../etc/passwd`) | Every path is resolved and must land inside an allowed root |
| Symlink escape | Each existing path component is checked; a link that resolves outside the roots is refused |
| Absolute-path / cross-platform escapes | `C:\…` paths are refused on POSIX hosts and vice versa |
| Shell injection | Allow-list of executables + deny-list regexes + no multi-line commands + `cwd` validation + hard timeout with process-group kill |
| Runaway commands | `timeout_seconds` clamped to `shell.max_timeout_seconds`; the whole process group is SIGTERM'd then SIGKILL'd (Windows: `taskkill /F /T`) |
| Context blow-up | Tool output capped (100 KB), per-message history capped (20 KB), file reads capped (`filesystem.max_read_bytes`) |
| Secret leakage | `filesystem.denied_globs` blocks `*.pem`, `.env`, ssh keys; audit logs store argument hashes; the secret scanner redacts evidence |
| Destructive actions | `delete_file` disabled by default; deleting an allowed root is refused; directory deletion requires `recursive=true` |
| Bad skills | Import errors, contract violations, duplicate tool names and runtime exceptions are contained: logged, skipped, never fatal |
| Denied operations | Reported to the model as `DENIED: <reason>` so it can change strategy instead of looping |

The agent is a **powerful tool running with your privileges** — review
`permissions.yaml` before pointing it at anything you care about, prefer a
dedicated sandbox root, and keep `network.allow_outbound: false` for fully
offline work.

## 15. Development & tests

```bash
pip install -r requirements-dev.txt

pytest                       # 325 tests
pytest -k security           # one area
pytest --tb=short -q         # terse failures

ruff check nour_code tests   # lint
ruff format nour_code tests  # format (optional)
```

`make help` lists the same commands (venv bootstrap, lint, test, run, docker
build). CI runs `ruff`, `compileall` and `pytest` on Python 3.11-3.13 for every
push and pull request (`.github/workflows/ci.yml`).

The suite runs entirely offline: the Ollama client is replaced by a scripted
`FakeModel` (see `tests/conftest.py`) that replays tool calls and answers, so the
agent loop, permissions, skills and HTTP layer are exercised without a GPU.

```
nour-code/
├── nour_code/
│   ├── __init__.py          # package metadata
│   ├── __main__.py          # CLI (python -m nour_code)
│   ├── main.py              # FastAPI app, routes, DI container, graceful shutdown
│   ├── agent.py             # agentic loop, SSE events, production-ready mode
│   ├── ollama_client.py     # async httpx wrapper (streaming + blocking)
│   ├── tools/
│   │   ├── __init__.py      # ToolRegistry: validate → authorise → run → audit
│   │   ├── filesystem.py    # read/write/list/search/delete
│   │   ├── shell.py         # allow-listed, timeout-ed shell execution
│   │   └── patch.py         # git apply → patch → built-in diff engine
│   ├── skills/
│   │   ├── __init__.py      # loader, hot reload, dispatch
│   │   ├── security_skill.py
│   │   └── production_skill.py
│   ├── permissions.py       # permissions.yaml + path/shell validation
│   ├── sessions.py          # in-memory store, TTL sweeper, JSON persistence
│   ├── prompts.py           # system prompt, stack hints, checklist
│   ├── models.py            # pydantic v2 schemas
│   ├── observability.py     # JSON logs, audit trail, Prometheus metrics
│   ├── config.py            # NOUR_CODE_* settings
│   └── version_check.py     # Python 3.11 guard
├── static/
│   └── console.html         # browser console (resizable panes + live events)
├── tests/                   # 325 tests, no network required
├── permissions.yaml
├── requirements.txt         # pinned versions
├── requirements-dev.txt
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── SKILL_TEMPLATE.py
└── README.md
```

Add your own skills in `./skills/` (create the directory; it is picked up
automatically) or point `NOUR_CODE_SKILLS_DIRS` somewhere else.

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `health` shows `"ollama_reachable": false` | Start Ollama (`ollama serve`) or fix `NOUR_CODE_OLLAMA_BASE_URL` |
| `health` shows `"model_loaded": false` | `ollama pull qwen3:8b` (or your `NOUR_CODE_MODEL`) |
| `DENIED: path … is outside the allowed roots` | Add the directory to `allowed_roots` or set `NOUR_CODE_ALLOWED_ROOTS` |
| Warnings about Windows roots at startup | Expected on Linux/macOS — those roots do not exist here and are skipped. Add POSIX roots to silence it |
| `git log --format` is refused | The spec's `format` deny pattern; see the note in `permissions.yaml` |
| `tool 'delete_file' is disabled` | Set `tools.delete_file: true` in `permissions.yaml` |
| `apply_patch` fails on every strategy | Check the diff header paths (`--- a/x`, `+++ b/y`) and the `-p` level; run with `dry_run: true` first |
| Stream stalls during a long command | Keep-alives are sent every 15 s; make sure no proxy buffers SSE (`X-Accel-Buffering: no` is set) |
| Model loops without answering | Lower `NOUR_CODE_MAX_ITERATIONS`, or make the prompt more specific; the response then comes back with `finish_reason: "max_iterations"` and a warning |
| `502` from `/chat` | Ollama returned an error (bad model name, OOM with `num_ctx=16384`); check `logs/nour-code.log` |

## 17. Assumptions & limitations

Assumptions made where the brief left room for interpretation:

1. **Ollama's OpenAI-compatible API** (`/v1/chat/completions`) is used for chat,
   with `/api/tags` and `/api/version` for health checks — both are served by a
   stock Ollama daemon. The API key is required by the protocol but ignored by
   Ollama; `NOUR_CODE_OLLAMA_API_KEY` defaults to `ollama`.
2. **Allowed roots** — the Windows roots from the spec are kept in
   `permissions.yaml` for fidelity, but roots that do not exist on the current
   host are skipped at load time and nour-code falls back to the working
   directory plus `<cwd>/sandbox`. This makes the defaults safe and runnable on
   Linux/macOS. Override with `NOUR_CODE_ALLOWED_ROOTS`.
3. **Skills directory** — the bundled skills live in `nour_code/skills/` (as in
   the requested layout) and `./skills/` is scanned too when it exists, so a
   top-level `/skills` drop-in directory works out of the box.
4. **The `format` deny pattern** also blocks `git log --format=…`; the YAML file
   documents an anchored alternative.
5. **Code transformations** (logging / error handling / validation) are
   conservative and idempotent: one-liner function bodies, functions that
   already contain `try`/`raise`, and files that would not parse afterwards are
   left untouched and reported.
6. **Production "refusal"** — the agent does not hide an answer that still
   contains placeholders; it reports them in the checklist and the warnings so
   nothing is silently swallowed.
7. **Parallel tool calls** are off by default (`NOUR_CODE_PARALLEL_TOOL_CALLS=false`)
   because qwen3:8b is more reliable one call at a time; the loop handles
   multiple calls per response either way.
8. **No global mutable state** — everything hangs off a `Container` built during
   startup and injected with FastAPI dependencies; tests build isolated
   containers with a fake model.
9. **`num_ctx=16384` and `temperature=0.1`** are always sent in `options`;
   `num_predict` and `seed` are only sent when configured.

Known limitations:

* Shell commands run through the platform shell, so pipes/redirects work but
  multi-line scripts are rejected by design.
* `apply_patch` needs `git` or `patch` for best results; the built-in engine
  handles standard unified diffs with fuzzy hunk matching but not binary or
  rename-only patches.
* One agent turn is CPU-bound by the model; concurrency is limited by Ollama, not
  by nour-code.
