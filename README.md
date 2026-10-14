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
6. [API](#6-api)
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

* **<http://localhost:8000>** — tiny browser console (send prompts, watch tool calls stream in)
* **<http://localhost:8000/docs>** — interactive OpenAPI documentation
* **<http://localhost:8000/health>** — Ollama + model status

Check that everything is wired:

```bash
curl -s http://localhost:8000/health | jq
```

```json
{
  "status": "ok",
  "app": "nour-code",
  "version": "1.0.0",
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
