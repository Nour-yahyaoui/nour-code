"""Production skill — hardens source files and scaffolds deployment artefacts.

Exposed tools
-------------
``add_logging(file_path)``
    Ensures a module-scoped logger exists and every function logs its entry.
``add_error_handling(file_path)``
    Wraps function bodies that perform risky work (I/O, network, parsing) in a
    ``try/except`` (Python) or ``try/catch`` (JS/TS) that logs with context and
    re-raises.
``add_input_validation(file_path)``
    Adds boundary validation for function arguments (``isinstance``/``TypeError``
    in Python, null-guard clauses in JS/TS).
``generate_dockerfile(project_root, runtime)``
    Writes a multi-stage, non-root, health-checked ``Dockerfile`` (Node/Next.js
    or Python).
``generate_ci_pipeline(project_root, target)``
    Writes a GitHub Actions workflow with dependency caching, lint, test, build
    and a Postgres service.

Every transformation is **idempotent** and conservative: if the file already
contains the construct, or if it cannot be transformed safely, the tool reports
that instead of mangling the code.  The original file is never rewritten unless
the new content parses (Python) or braces balance (JS/TS).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

SKILL_NAME = "production_skill"
SKILL_DESCRIPTION = (
    "Prepares code for production: adds logging, error handling and input validation to source "
    "files, and generates a multi-stage Dockerfile plus a GitHub Actions CI pipeline."
)
SKILL_TRIGGERS = [
    "when the user says production ready / make it production / prepare for production",
    "when the user asks to harden a service before deployment",
    "when the user asks for a Dockerfile or container image",
    "when the user asks for a CI pipeline or GitHub Actions workflow",
]

SKILL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_logging",
            "description": (
                "Add production logging to a Python or JS/TS file: ensures a module logger exists "
                "(logging.getLogger / pino-style logger) and every function logs its entry. Idempotent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File to modify."},
                    "level": {"type": "string", "enum": ["debug", "info"], "default": "info"},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_error_handling",
            "description": (
                "Wrap functions that do I/O, network or parsing in try/except (Python) or try/catch "
                "(JS/TS) that logs the failure with context and re-raises. Idempotent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File to modify."},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_input_validation",
            "description": (
                "Add boundary validation to function arguments: isinstance/TypeError checks for "
                "annotated Python parameters, null-guard clauses for JS/TS parameters. Idempotent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File to modify."},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_dockerfile",
            "description": (
                "Write a production Dockerfile: multi-stage build, dependency layer caching, "
                "non-root user, healthcheck, explicit CMD. Detects Node/Next.js or Python."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_root": {"type": "string", "description": "Project directory."},
                    "runtime": {"type": "string", "enum": ["auto", "node", "python"], "default": "auto"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 0},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["project_root"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_ci_pipeline",
            "description": (
                "Write a CI pipeline. target='github' emits .github/workflows/ci.yml with dependency "
                "caching, lint, tests (with a Postgres service), build and a Docker image build."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_root": {"type": "string", "description": "Project directory."},
                    "target": {"type": "string", "enum": ["github", "gitlab"], "default": "github"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["project_root"],
                "additionalProperties": False,
            },
        },
    },
]

SKILL_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Language + file helpers
# ---------------------------------------------------------------------------
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
SUPPORTED_SUFFIXES = PYTHON_SUFFIXES | JS_SUFFIXES

RISKY_PY_CALLS = frozenset(
    {
        "open", "exec", "eval", "connect", "execute", "commit", "request", "get", "post",
        "put", "delete", "patch", "loads", "load", "parse", "read_text", "write_text",
        "read_bytes", "write_bytes", "run", "call", "check_output", "create", "fetch",
        "query", "find", "insert", "update", "send", "publish", "consume",
    }
)
RISKY_JS_PATTERNS = (
    re.compile(r"\bawait\b"),
    re.compile(r"\bJSON\.parse\s*\("),
    re.compile(r"\bfs\.\w+\s*\("),
    re.compile(r"\bfetch\s*\("),
    re.compile(r"\baxios\.\w+\s*\("),
    re.compile(r"\bprisma\.\w+\s*\("),
    re.compile(r"\bdb\.\w+\s*\("),
    re.compile(r"\bsqlx?\.\w+\s*\("),
)


def _language(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return "python"
    if suffix in JS_SUFFIXES:
        return "typescript" if suffix in {".ts", ".tsx"} else "javascript"
    return "unknown"


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def insert_lines(lines: list[str], insertions: list[tuple[int, list[str]]]) -> list[str]:
    """Apply ``(line_index, [new lines])`` pairs in a single stable pass.

    ``line_index`` is a 0-based index in the *original* list: ``lines[i]`` stays
    above the inserted block.  Insertions are applied from the bottom up so
    earlier indices remain valid.
    """
    result = list(lines)
    for index, block in sorted(insertions, key=lambda pair: pair[0], reverse=True):
        index = max(0, min(index, len(result)))
        result[index:index] = block
    return result


async def _read_source(ctx: dict[str, Any], raw_path: str, *, allowed: set[str] | None = None) -> tuple[Path, str] | dict[str, Any]:
    """Resolve + read a file. Returns ``(path, text)`` or an error dict."""
    permissions = ctx.get("permissions")
    tools = ctx.get("tools")
    if permissions is None or tools is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "skill context is missing permissions or tools"}
    try:
        path = permissions.resolve_path(raw_path, must_exist=True, kind="file")
    except PermissionError as exc:
        return {"ok": False, "error": f"path rejected: {exc}"}
    if allowed and path.suffix.lower() not in allowed:
        return {
            "ok": False,
            "error": f"unsupported file type '{path.suffix}' (supported: {', '.join(sorted(allowed))})",
        }
    try:
        text = await tools.read(str(path))
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return path, text


async def _write_source(ctx: dict[str, Any], path: Path, content: str, *, dry_run: bool) -> dict[str, Any]:
    tools = ctx.get("tools")
    if tools is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "tool facade unavailable"}
    if dry_run:
        return {"written": False, "dry_run": True, "path": str(path)}
    outcome = await tools.write(str(path), content)
    if not outcome.get("ok"):
        return {"written": False, "path": str(path), "error": outcome.get("error")}
    return {"written": True, "path": str(path), "bytes": (outcome.get("result") or {}).get("bytes")}


def _diff_summary(before: str, after: str) -> dict[str, Any]:
    return {
        "lines_before": before.count("\n") + 1,
        "lines_after": after.count("\n") + 1,
        "changed": before != after,
    }


# ---------------------------------------------------------------------------
# JS / TS source scanning
# ---------------------------------------------------------------------------
_JS_KEYWORDS = frozenset(
    {"if", "for", "while", "switch", "catch", "try", "else", "do", "return", "function",
     "class", "typeof", "new", "delete", "void", "await", "yield", "case", "default"}
)

_FUNC_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)?\s*\(")
_ARROW_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?\([^)]*\)\s*(?::[^{]+)?=>\s*\{")
_METHOD_RE = re.compile(r"(?m)^(?P<indent>[ \t]+)(?:public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{]+)?\{[ \t]*$")


def code_mask(src: str) -> list[bool]:
    """Mark which characters are real code (not inside a string or comment).

    Handles ``//``, ``/* */``, single/double quotes and template literals
    (including the ``${...}`` holes, which are treated as strings for safety).
    """
    mask = [True] * len(src)
    i = 0
    length = len(src)
    while i < length:
        char = src[i]
        if char == "/" and i + 1 < length and src[i + 1] == "/":
            end = src.find("\n", i)
            end = length if end == -1 else end
            for k in range(i, end):
                mask[k] = False
            i = end
            continue
        if char == "/" and i + 1 < length and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            end = length if end == -1 else end + 2
            for k in range(i, min(end, length)):
                mask[k] = False
            i = end
            continue
        if char in "\"'`":
            quote = char
            start = i
            i += 1
            while i < length:
                if src[i] == "\\" and quote != "`":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                if quote == "`" and src[i] == "$" and i + 1 < length and src[i + 1] == "{":
                    depth = 1
                    i += 2
                    while i < length and depth:
                        if src[i] == "{":
                            depth += 1
                        elif src[i] == "}":
                            depth -= 1
                        i += 1
                    continue
                i += 1
            for k in range(start, min(i, length)):
                mask[k] = False
            continue
        i += 1
    return mask


def matching_brace(src: str, mask: list[bool], open_index: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_index`` (``-1`` if unbalanced)."""
    depth = 0
    for index in range(open_index, len(src)):
        if not mask[index]:
            continue
        char = src[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def next_code_char(src: str, mask: list[bool], start: int, target: str = "{") -> int:
    for index in range(start, len(src)):
        if mask[index] and src[index] == target:
            return index
    return -1


def find_js_functions(src: str) -> list[dict[str, Any]]:
    """Locate JS/TS function bodies (declarations, arrows, class methods)."""
    mask = code_mask(src)
    functions: list[dict[str, Any]] = []
    seen_spans: set[tuple[int, int]] = set()

    def _line_start(index: int) -> int:
        return src.rfind("\n", 0, index) + 1

    def _record(header: re.Match[str], kind: str, search_from: int) -> None:
        if not mask[header.start()]:
            return
        name = header.groupdict().get("name") or ""
        if kind == "method" and (not name or name in _JS_KEYWORDS):
            return
        open_index = next_code_char(src, mask, search_from, "{")
        if open_index == -1:
            return
        close_index = matching_brace(src, mask, open_index)
        if close_index == -1:
            return
        span = (open_index, close_index)
        if span in seen_spans:
            return
        seen_spans.add(span)
        header_line = src.count("\n", 0, header.start())
        close_line = src.count("\n", 0, close_index)
        indent = header.groupdict().get("indent") or ""
        if kind == "method":
            indent = indent
        functions.append(
            {
                "kind": kind,
                "name": name or "(anonymous)",
                "header_start": header.start(),
                "header_line": header_line,
                "open_index": open_index,
                "close_index": close_index,
                "body_start": open_index + 1,
                "body_end": close_index,
                "body_line_start": src.count("\n", 0, open_index + 1),
                "body_line_end": close_line,
                "indent": indent,
                "params": _js_params(src[header.end() - 1 : open_index + 1]),
                "src_line": _line_start(header.start()),
            }
        )

    for match in _FUNC_RE.finditer(src):
        _record(match, "function", match.end() - 1)
    for match in _ARROW_RE.finditer(src):
        _record(match, "arrow", match.end() - 1)
    for match in _METHOD_RE.finditer(src):
        _record(match, "method", match.end() - 1)

    functions.sort(key=lambda f: f["open_index"])
    return functions


def _js_params(signature: str) -> list[str]:
    """Best-effort parameter names from a JS signature fragment."""
    depth = 0
    start = -1
    for index, char in enumerate(signature):
        if char == "(":
            depth = 1
            start = index + 1
            break
    if start == -1:
        return []
    end = signature.rfind(")")
    fragment = signature[start:end if end > start else len(signature)]
    params: list[str] = []
    for part in _split_top_level(fragment):
        part = part.strip()
        if not part:
            continue
        name = part.split(":", 1)[0].split("=", 1)[0].strip()
        if name.startswith("...") or "{" in name or "[" in name:
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name or ""):
            params.append(name)
    return params


def _split_top_level(fragment: str) -> list[str]:
    """Split on commas that are not nested in brackets/parens."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in fragment:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def js_import_insert_line(lines: list[str]) -> int:
    """Index (0-based) where a new top-level import/const should be inserted."""
    last = 0
    in_multiline_import = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_multiline_import:
            if stripped.endswith((";", "from '", 'from "')) or stripped in {"}", "};"}:
                in_multiline_import = False
                last = index + 1
            continue
        if stripped.startswith("import ") or stripped.startswith("export ") and " from " in stripped:
            if stripped.endswith(";"):
                last = index + 1
            elif stripped.endswith("{"):
                in_multiline_import = True
            else:
                last = index + 1
            continue
        if re.match(r"^(?:const|let|var)\s+\w+\s*=\s*require\(", stripped):
            last = index + 1
            continue
        if stripped.startswith('"use strict"') or stripped.startswith("'use strict'"):
            last = index + 1
            continue
        if stripped.startswith('"use client"') or stripped.startswith("'use client'"):
            last = index + 1
            continue
        if stripped == "" or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        break
    return last
