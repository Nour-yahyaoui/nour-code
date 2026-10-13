"""System prompt construction, stack hints and production-ready detection.

The prompt is rebuilt for every turn so it always reflects the *current*
directory listing, loaded skills and permission scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Stack hints
# ---------------------------------------------------------------------------
STACK_HINTS: dict[str, str] = {
    "nextjs": """### Next.js (App Router)
- Routes live in `app/**/{page,layout,route,loading,error}.tsx`; `app/api/**/route.ts` for handlers.
- Components are **Server Components by default**. Add `"use client"` only when you need state,
  effects, browser APIs or event handlers — and keep that file as small as possible.
- Data fetching: `async` Server Components + `fetch(url, { cache, next: { revalidate, tags } })`;
  mutate with Server Actions (`"use server"`) or `route.ts` handlers, then `revalidatePath`/`revalidateTag`.
- Env vars: only `NEXT_PUBLIC_*` are exposed to the browser. Everything else stays server-side and
  must be validated in a single `env.ts` module (zod) instead of scattered `process.env` reads.
- Prefer `next/image`, `next/font`, and route-level `loading.tsx`/`error.tsx` for UX.
- Do not use `getServerSideProps`/`getStaticProps`/`pages/` unless the project really is Pages Router.""",
    "node": """### Node.js / TypeScript
- ESM-first; declare `"type": "module"` when the project is ESM and keep import specifiers explicit.
- Never block the event loop: no sync fs calls in request paths, use streams for large payloads.
- Validate all external input at the boundary (zod/valibot) before it reaches business logic.
- Handle `unhandledRejection` and `uncaughtException` at the entrypoint, log, then exit non-zero.
- Keep secrets out of source: read from `process.env` through one validated config module.""",
    "actix": """### Actix Web (Rust)
- Handlers are `async fn(...) -> Result<impl Responder, Error>`; extractors (`web::Json<T>`,
  `web::Path<T>`, `web::Data<T>`) do the parsing — never hand-parse the request.
- Shared state is `web::Data<AppState>`; clone the `Data` handle, not the state.
- Middleware is registered with `.wrap(...)` (e.g. `Logger`, `Compress`, `Cors::permissive()` only in dev).
- Errors: implement `ResponseError` for your domain error type so they map to proper HTTP codes.
- Postgres with `sqlx`: compile-time checked `sqlx::query!` macros, `PgPool` in `web::Data`,
  `pool.begin()` for transactions, never build SQL by string concatenation.""",
    "postgresql": """### PostgreSQL
- Schema changes go through **migrations** (`sqlx migrate`, `alembic`, `prisma migrate`) — never
  hand-edit a live schema, and never edit an already-applied migration.
- Index every foreign key and every column used in `WHERE`/`ORDER BY`/`JOIN`. Prefer composite
  indexes that match the query's column order; check plans with `EXPLAIN ANALYZE`.
- Connection pooling: one pool per process (pgbouncer or the driver pool), tuned `max_connections`,
  explicit statement timeouts, and no connection opened per request.
- Use transactions for multi-statement writes, `SELECT ... FOR UPDATE` for read-modify-write,
  and always set `NOT NULL`/defaults/`CHECK` constraints instead of trusting the app layer.
- Long-running migrations: add nullable columns first, backfill, then add constraints.""",
}

STACK_MARKERS: dict[str, tuple[str, ...]] = {
    # stack -> substrings searched for in marker files (package.json / Cargo.toml)
    "nextjs": ('"next"', "'next'"),
    "node": ('"name"',),
    "actix": ("actix-web", "actix_web"),
}


@dataclass
class StackDetection:
    """Result of sniffing the working directory for a known stack."""

    stacks: list[str]
    evidence: dict[str, list[str]]

    def as_dict(self) -> dict[str, Any]:
        return {"stacks": self.stacks, "evidence": self.evidence}


def detect_stack(root: Path, files: Sequence[str] | None = None) -> StackDetection:
    """Cheap, best-effort stack detection based on marker files."""
    names = set(files or [])
    evidence: dict[str, list[str]] = {}
    found: list[str] = []

    def _note(stack: str, why: str) -> None:
        if stack not in found:
            found.append(stack)
        evidence.setdefault(stack, []).append(why)

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            text = package_json.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            text = ""
        if any(marker in text for marker in STACK_MARKERS["nextjs"]):
            _note("nextjs", "package.json depends on next")
        if (root / "next.config.js").is_file() or (root / "next.config.mjs").is_file():
            _note("nextjs", "next.config.* present")
        elif "next.config" in " ".join(names):
            _note("nextjs", "next.config present")
        _note("node", "package.json present")

    cargo = root / "Cargo.toml"
    if cargo.is_file():
        try:
            text = cargo.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            text = ""
        if any(marker in text for marker in STACK_MARKERS["actix"]):
            _note("actix", "Cargo.toml depends on actix-web")
        if "sqlx" in text:
            _note("postgresql", "Cargo.toml depends on sqlx")

    for marker, why in (
        ("migrations", "migrations/ directory"),
        ("alembic.ini", "alembic.ini present"),
        ("docker-compose.yml", "docker-compose declares postgres"),
        ("docker-compose.yaml", "docker-compose declares postgres"),
        ("prisma/schema.prisma", "prisma schema"),
    ):
        candidate = root / marker
        if candidate.exists():
            if marker.startswith("docker-compose") and candidate.is_file():
                try:
                    body = candidate.read_text(encoding="utf-8", errors="replace")[:200_000]
                except OSError:
                    body = ""
                if "postgres" in body:
                    _note("postgresql", why)
                continue
            _note("postgresql" if "prisma" not in marker else "postgresql", why)

    if any(name.endswith((".sql",)) for name in names):
        _note("postgresql", "*.sql files in root")

    if not found:
        _note("node", "no stack markers found; assuming generic project")
        found.remove("node")
        return StackDetection(stacks=[], evidence=evidence)
    return StackDetection(stacks=found, evidence=evidence)


# ---------------------------------------------------------------------------
# Production-ready mode
# ---------------------------------------------------------------------------
PRODUCTION_TRIGGERS: tuple[str, ...] = (
    r"production[\s_-]?ready",
    r"production[\s_-]?grade",
    r"make it production",
    r"prepare (?:it |this )?for production",
    r"ready for prod(?:uction)?",
    r"ship (?:it )?to production",
    r"harden (?:this|it|the)",
    r"deploy[\s_-]?ready",
    r"enterprise[\s_-]?ready",
)

PRODUCTION_REGEX = re.compile("|".join(PRODUCTION_TRIGGERS), re.IGNORECASE)

PLACEHOLDER_REGEX = re.compile(
    r"(?i)\b(TODO|FIXME|XXX|HACK|PLACEHOLDER|Lorem ipsum|your code here|coming soon|not implemented)\b"
    r"|NotImplementedError|pass\s+#\s*implement"
)


def detect_production_mode(message: str) -> bool:
    """True when the user is asking for production-grade output."""
    return bool(PRODUCTION_REGEX.search(message or ""))


PRODUCTION_INSTRUCTION = """## PRODUCTION-READY MODE (active for this turn)

The user asked for production-grade code. Every file you touch or create MUST ship with:

1. **Logging** — structured, levelled, with correlation ids where relevant. No `print()` in Python,
   no `console.log` in Node (use the project logger).
2. **Error handling** — catch specific exceptions, never swallow them, log with context, and return a
   meaningful error to the caller. Python: `except SomeError as exc:` + `raise`/`return` an error
   result. Rust: map errors with `?` and a domain error enum. Node: try/catch around every await.
3. **Input validation** — validate at every external boundary (HTTP body/query, CLI args, env vars,
   DB rows) before use. Reject with a 4xx, never a 500.
4. **Configuration** — no hard-coded hosts, ports, secrets or paths. Read them from the environment
   through one validated config module, with sensible defaults and clear failure on missing values.
5. **Health & observability** — expose `/health` (liveness) and `/ready`/`/healthz` (readiness,
   checks dependencies) plus `/metrics` when the stack supports it.
6. **No placeholders** — `TODO`, `FIXME`, `NotImplementedError`, `// your code here` and friends are
   REJECTED. If something cannot be implemented, say so explicitly instead of leaving a stub.
7. **Tests** — unit tests for the new logic and at least one happy-path integration test.
8. **Docs** — update the README/docstring for anything that changes behaviour or configuration.

After finishing, the server re-checks the files you touched and appends a checklist to your answer.
Anything you leave unfinished will be visible in that checklist."""


CHECKLIST_ITEMS: tuple[tuple[str, str], ...] = (
    ("logging", "Structured logging present (no print/console.log in production paths)"),
    ("error_handling", "Errors handled explicitly and logged with context"),
    ("input_validation", "External inputs validated at the boundary"),
    ("config", "Configuration read from the environment, nothing hard-coded"),
    ("health", "Health/liveness endpoint exposed"),
    ("tests", "Tests added or updated"),
    ("no_placeholders", "No TODO / FIXME / placeholder stubs"),
    ("dockerfile", "Dockerfile present"),
    ("ci", "CI pipeline present"),
)


def build_production_checklist(status: dict[str, tuple[bool, str]]) -> list[dict[str, Any]]:
    """Render the checklist shown at the end of a production-ready answer."""
    rows: list[dict[str, Any]] = []
    for key, label in CHECKLIST_ITEMS:
        ok, detail = status.get(key, (False, "not verified"))
        rows.append(
            {
                "item": key,
                "label": label,
                "status": "ok" if ok else "missing",
                "symbol": "[x]" if ok else "[ ]",
                "detail": detail,
            }
        )
    return rows


def render_checklist_markdown(rows: Sequence[dict[str, Any]]) -> str:
    lines = ["", "---", "### Production-readiness checklist"]
    lines.extend(f"- {row['symbol']} {row['label']} — {row['detail']}" for row in rows)
    missing = [r for r in rows if r["status"] != "ok"]
    if missing:
        lines.append("")
        lines.append(
            f"**{len(missing)} item(s) still open.** Resolve them before shipping."
        )
    else:
        lines.append("")
        lines.append("**All checks passed.**")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
BASE_RULES = """## Operating rules
- You have real tools. Use them: inspect before you edit, and verify after you edit.
- All paths must resolve inside an allowed root. Relative paths resolve against the working
  directory. Never guess a path you have not listed or searched for.
- Prefer `apply_patch` for editing existing files (unified diff, correct `@@` hunk headers) and
  `write_file` only for new files or full rewrites.
- Run the project's real commands (`npm test`, `cargo test`, `pytest`, `npm run build`) to verify
  changes instead of claiming they work.
- Tool output is truncated when very large; re-run with narrower arguments if you need more.
- Report denials verbatim (they start with `DENIED:`) and choose a different approach rather than
  retrying the same call.
- Be concrete and complete: real code, real file paths, no placeholders."""


def render_file_listing(entries: Sequence[dict[str, Any]], limit: int = 60) -> str:
    """Compact shallow listing of the working directory."""
    if not entries:
        return "(empty directory)"
    shown = list(entries)[:limit]
    lines: list[str] = []
    for entry in shown:
        kind = entry.get("type", "file")
        suffix = "/" if kind == "dir" else ""
        size = entry.get("size")
        size_text = f"  {size}B" if isinstance(size, int) and kind == "file" else ""
        lines.append(f"- {entry.get('name', '?')}{suffix}{size_text}")
    if len(entries) > limit:
        lines.append(f"- ... and {len(entries) - limit} more")
    return "\n".join(lines)


def build_system_prompt(
    *,
    cwd: str,
    files: Sequence[dict[str, Any]] | None = None,
    skills: Sequence[dict[str, Any]] | None = None,
    permissions: dict[str, Any] | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
    production_mode: bool = False,
    extra_context: str | None = None,
) -> str:
    """Assemble the system prompt for one turn."""
    parts: list[str] = [
        f"You are **nour-code**, a local AI coding agent running against a Qwen3 8B model on Ollama.",
        "",
        f"Working directory: `{cwd}`",
        "",
        "## Files in the working directory (shallow)",
        render_file_listing(files or []),
    ]

    stack = detect_stack(Path(cwd), [str(entry.get("name", "")) for entry in (files or [])])
    if stack.stacks:
        parts.append("")
        parts.append("## Stack hints (detected: " + ", ".join(stack.stacks) + ")")
        for name in stack.stacks:
            hint = STACK_HINTS.get(name)
            if hint:
                parts.append(hint)
    else:
        parts.append("")
        parts.append("## Stack hints")
        parts.append(
            "No framework markers detected. If the project uses Next.js, Node, Actix Web or "
            "PostgreSQL, follow their standard conventions (ask or inspect before inventing)."
        )
        parts.append(STACK_HINTS["node"])

    parts.append("")
    parts.append("## Permission scope")
    if permissions:
        roots = permissions.get("allowed_roots") or []
        parts.append("- Allowed roots: " + (", ".join(f"`{r}`" for r in roots) or "(none)"))
        allowed = permissions.get("shell_allowed_commands") or []
        parts.append("- Shell allow-list: " + (", ".join(f"`{c}`" for c in allowed) or "(none)"))
        denied = permissions.get("shell_denied_patterns") or []
        if denied:
            parts.append("- Shell deny-list: " + ", ".join(f"`{p}`" for p in denied))
        parts.append(
            f"- Outbound network: {'allowed' if permissions.get('network_allow_outbound') else 'blocked'}"
        )
        disabled = sorted(k for k, v in (permissions.get("tools") or {}).items() if v is False)
        if disabled:
            parts.append("- Disabled tools: " + ", ".join(f"`{t}`" for t in disabled))
        for warning in permissions.get("warnings") or []:
            parts.append(f"- Scope note: {warning}")
    else:
        parts.append("- (scope unavailable)")

    if tools:
        parts.append("")
        parts.append("## Available tools")
        for tool in tools:
            name = tool.get("name", "?")
            description = (tool.get("description") or "").splitlines()[0] if tool.get("description") else ""
            source = f" [skill: {tool['skill']}]" if tool.get("skill") else ""
            parts.append(f"- `{name}`{source} — {description}")

    parts.append("")
    parts.append("## Loaded skills")
    if skills:
        for skill in skills:
            name = skill.get("name", "?")
            description = skill.get("description", "")
            triggers = skill.get("triggers") or []
            trigger_text = f" (triggers: {'; '.join(triggers)})" if triggers else ""
            parts.append(f"- **{name}** — {description}{trigger_text}")
    else:
        parts.append("- (no skills loaded)")

    parts.append("")
    parts.append(BASE_RULES)

    if production_mode:
        parts.append("")
        parts.append(PRODUCTION_INSTRUCTION)

    if extra_context:
        parts.append("")
        parts.append("## Additional context for this turn")
        parts.append(extra_context)

    return "\n".join(parts)


def skill_trigger_hint(skills: Iterable[dict[str, Any]]) -> str:
    """One-paragraph reminder of when to reach for a skill."""
    rows = [
        f"- use `{name}` when: {'; '.join(skill.get('triggers') or [])}"
        for skill in skills
        if (name := skill.get("name")) and skill.get("triggers")
    ]
    if not rows:
        return ""
    return "## Skill triggers\n" + "\n".join(rows)
