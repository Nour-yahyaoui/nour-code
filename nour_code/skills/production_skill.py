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


# ---------------------------------------------------------------------------
# Python AST helpers
# ---------------------------------------------------------------------------
LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
RISKY_PY_NODES = (ast.Try,)


def _py_import_insert_index(tree: ast.Module) -> int:
    """0-based line index where module-level imports should be inserted."""
    body = list(tree.body)
    index = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        if isinstance(body[0].value.value, str):
            index = body[0].end_lineno or 0
    last_import = None
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue  # stray docstring / string literal
        elif last_import is not None:
            break
    if last_import is not None:
        index = max(index, last_import.end_lineno or index)
    return index


def _py_has_logging_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "logging" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "logging":
                return True
    return False


def _py_has_logger(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        target_names: list[str] = []
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        else:
            continue
        if "logger" not in target_names or value is None:
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            if value.func.attr == "getLogger":
                return True
        if isinstance(value, ast.Name) and value.id in {"logger", "log"}:
            return True
    return False


def _py_body_statements(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


def _py_has_log_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in LOG_LEVELS:
                return True
    return False


def _py_has_try(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Try) for child in ast.walk(node))


def _py_is_risky(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Await):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name.lower() in RISKY_PY_CALLS:
                return True
        if isinstance(child, ast.With):
            return True
    return False


def _py_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _py_annotation_name(annotation: ast.AST | None) -> str | None:
    """Return a simple builtin type name for an annotation, if it is one."""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id if annotation.id in _VALIDATABLE_TYPES else None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value if annotation.value in _VALIDATABLE_TYPES else None
    return None


_VALIDATABLE_TYPES = frozenset({"str", "int", "float", "bool", "list", "dict", "tuple", "set", "bytes", "Path"})


# ---------------------------------------------------------------------------
# add_logging
# ---------------------------------------------------------------------------
async def _add_logging_python(ctx: dict[str, Any], path: Path, source: str, level: str, dry_run: bool) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"cannot parse {path.name}: {exc.msg} (line {exc.lineno})"}

    lines = source.splitlines()
    insertions: list[tuple[int, list[str]]] = []
    changes: dict[str, Any] = {"logger_added": False, "instrumented": []}

    if not _py_has_logging_import(tree):
        insertions.append((_py_import_insert_index(tree), ["import logging", ""]))
        changes["logger_added"] = True
    if not _py_has_logger(tree):
        insertions.append((_py_import_insert_index(tree), ["logger = logging.getLogger(__name__)", ""]))
        changes["logger_added"] = True

    for node in _py_functions(tree):
        statements = _py_body_statements(node)
        if not statements:
            continue
        first = statements[0]
        if first.lineno == node.lineno:
            continue  # one-liner body: leave it alone rather than emit broken code
        if _py_has_log_call(node):
            continue
        indent = _indent_of(lines[first.lineno - 1])
        insertions.append(
            (first.lineno - 1, [f'{indent}logger.{level}("{node.name}() called")'])
        )
        changes["instrumented"].append(node.name)

    if not insertions:
        return {
            "ok": True,
            "tool": "add_logging",
            "file": str(path),
            "language": "python",
            "written": False,
            "already_compliant": True,
            "changes": changes,
            "note": "file already has a logger and every function logs",
        }

    new_source = _join_lines(insert_lines(lines, insertions), source)
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"refusing to write invalid Python: {exc.msg} (line {exc.lineno})"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_logging",
        "file": str(path),
        "language": "python",
        "dry_run": dry_run,
        "changes": changes,
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


def _join_lines(lines: list[str], original: str) -> str:
    text = "\n".join(lines)
    if original.endswith("\n"):
        text += "\n"
    return text


def _js_balanced(src: str) -> bool:
    mask = code_mask(src)
    depth = 0
    for index, char in enumerate(src):
        if not mask[index]:
            continue
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _js_logger_block(kind: str) -> list[str]:
    if kind == "pino":
        return [
            'import pino from "pino";',
            "",
            'const logger = pino({ level: process.env.LOG_LEVEL ?? "info" });',
            "",
        ]
    if kind == "winston":
        return [
            'import winston from "winston";',
            "",
            'const logger = winston.createLogger({',
            '  level: process.env.LOG_LEVEL ?? "info",',
            "  format: winston.format.json(),",
            "  transports: [new winston.transports.Console()],",
            "});",
            "",
        ]
    return [
        "// Structured logger — swap for pino/winston when the dependency is available.",
        "const logger = {",
        "  info: (message: string, meta?: unknown) =>",
        '    process.stdout.write(`${JSON.stringify({ level: "info", message, meta, ts: new Date().toISOString() })}\\n`);',
        "  error: (message: string, meta?: unknown) =>",
        '    process.stderr.write(`${JSON.stringify({ level: "error", message, meta, ts: new Date().toISOString() })}\\n`);',
        "};",
        "",
    ]


def _js_logger_kind(source: str) -> str:
    if re.search(r"""from\s+["']pino["']|require\(\s*["']pino["']\s*\)""", source):
        return "pino"
    if re.search(r"""from\s+["']winston["']|require\(\s*["']winston["']\s*\)""", source):
        return "winston"
    return "builtin"


def _js_has_logger(source: str) -> bool:
    return bool(
        re.search(r"(?m)^\s*(?:const|let|var)\s+logger\b", source)
        or re.search(r"(?m)^\s*import\s+.*\blogger\b", source)
    )


async def _add_logging_js(ctx: dict[str, Any], path: Path, source: str, level: str, dry_run: bool) -> dict[str, Any]:
    lines = source.splitlines()
    functions = find_js_functions(source)
    insertions: list[tuple[int, list[str]]] = []
    changes: dict[str, Any] = {"logger_added": False, "instrumented": []}

    if not _js_has_logger(source):
        kind = _js_logger_kind(source)
        insertions.append((js_import_insert_line(lines), _js_logger_block(kind)))
        changes["logger_added"] = True
        changes["logger_kind"] = kind

    for function in functions:
        body = source[function["body_start"] : function["body_end"]]
        if re.search(r"\blogger\.(info|debug|warn|warning|error)\s*\(", body):
            continue
        body_lines = [line for line in body.splitlines() if line.strip()]
        if not body_lines:
            continue
        indent = _indent_of(body_lines[0])
        insertions.append(
            (function["body_line_start"], [f'{indent}logger.{level}("{function["name"]}() called");'])
        )
        changes["instrumented"].append(function["name"])

    if not insertions:
        return {
            "ok": True,
            "tool": "add_logging",
            "file": str(path),
            "language": _language(path),
            "written": False,
            "already_compliant": True,
            "changes": changes,
            "note": "file already has a logger and every function logs",
        }

    new_source = _join_lines(insert_lines(lines, insertions), source)
    if not _js_balanced(new_source):
        return {"ok": False, "error": "refusing to write JS/TS with unbalanced braces"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_logging",
        "file": str(path),
        "language": _language(path),
        "dry_run": dry_run,
        "changes": changes,
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


async def _add_logging(ctx: dict[str, Any], file_path: str, level: str = "info", dry_run: bool = False) -> dict[str, Any]:
    result = await _read_source(ctx, file_path, allowed=SUPPORTED_SUFFIXES)
    if isinstance(result, dict):
        return result
    path, source = result
    language = _language(path)
    if language == "python":
        return await _add_logging_python(ctx, path, source, level, dry_run)
    return await _add_logging_js(ctx, path, source, level, dry_run)


# ---------------------------------------------------------------------------
# add_error_handling
# ---------------------------------------------------------------------------
def apply_edits(lines: list[str], edits: list[tuple[int, int, list[str]]]) -> list[str]:
    """Apply ``(start_index, num_removed, new_lines)`` edits bottom-up.

    All indices refer to the *original* line list, so callers can compute them
    from an AST/source scan without compensating for earlier edits.
    """
    result = list(lines)
    for start, removed, block in sorted(edits, key=lambda edit: edit[0], reverse=True):
        start = max(0, min(start, len(result)))
        result[start : start + removed] = block
    return result


def _py_logger_edits(tree: ast.Module) -> list[tuple[int, int, list[str]]]:
    """Edits that guarantee ``import logging`` + a module logger exist."""
    edits: list[tuple[int, int, list[str]]] = []
    if not _py_has_logging_import(tree):
        edits.append((_py_import_insert_index(tree), 0, ["import logging", ""]))
    if not _py_has_logger(tree):
        edits.append((_py_import_insert_index(tree), 0, ["logger = logging.getLogger(__name__)", ""]))
    return edits


async def _add_error_handling_python(ctx: dict[str, Any], path: Path, source: str, dry_run: bool) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"cannot parse {path.name}: {exc.msg} (line {exc.lineno})"}

    lines = source.splitlines()
    edits: list[tuple[int, int, list[str]]] = []
    wrapped: list[str] = []

    for node in _py_functions(tree):
        if _py_has_try(node) or not _py_is_risky(node):
            continue
        statements = _py_body_statements(node)
        if not statements:
            continue
        first, last = statements[0], statements[-1]
        if first.lineno == node.lineno:
            continue  # one-liner body: not safe to wrap
        start = first.lineno - 1
        end = last.end_lineno or last.lineno
        indent = _indent_of(lines[start])
        body = ["    " + line if line.strip() else line for line in lines[start:end]]
        block = (
            [f"{indent}try:"]
            + body
            + [
                f"{indent}except Exception as exc:",
                f'{indent}    logger.error("{node.name}() failed: %s", exc, exc_info=True)',
                f"{indent}    raise",
            ]
        )
        edits.append((start, end - start, block))
        wrapped.append(node.name)

    if not edits:
        return {
            "ok": True,
            "tool": "add_error_handling",
            "file": str(path),
            "language": "python",
            "written": False,
            "already_compliant": True,
            "changes": {"wrapped": []},
            "note": "every risky function already has a try/except (or none were risky)",
        }

    edits.extend(_py_logger_edits(tree))
    new_source = _join_lines(apply_edits(lines, edits), source)
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"refusing to write invalid Python: {exc.msg} (line {exc.lineno})"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_error_handling",
        "file": str(path),
        "language": "python",
        "dry_run": dry_run,
        "changes": {"wrapped": wrapped},
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


def _js_wrap_body(source: str, function: dict[str, Any], logger_name: str = "logger") -> str:
    """Return the replacement text for the inside of a JS function's braces."""
    base_indent = _indent_of(source[function["src_line"] : function["header_start"]] or function["indent"] or "")
    if not base_indent:
        base_indent = function["indent"] or ""
    inner_indent = base_indent + "  "
    body = source[function["body_start"] : function["body_end"]]
    body_lines = body.split("\n")
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    indented = ["  " + line if line.strip() else line for line in body_lines]
    name = function["name"]
    return (
        "\n"
        + inner_indent
        + "try {\n"
        + "\n".join(indented)
        + "\n"
        + inner_indent
        + "} catch (error) {\n"
        + inner_indent
        + f'  {logger_name}.error("{name}() failed", '
        + "{ error: error instanceof Error ? error.message : String(error) });\n"
        + inner_indent
        + "  throw error;\n"
        + inner_indent
        + "}\n"
        + base_indent
    )


async def _add_error_handling_js(ctx: dict[str, Any], path: Path, source: str, dry_run: bool) -> dict[str, Any]:
    functions = find_js_functions(source)
    to_wrap: list[dict[str, Any]] = []
    for function in functions:
        body = source[function["body_start"] : function["body_end"]]
        if re.search(r"\btry\s*\{", body):
            continue
        if not any(pattern.search(body) for pattern in RISKY_JS_PATTERNS):
            continue
        if not [line for line in body.splitlines() if line.strip()]:
            continue
        to_wrap.append(function)

    if not to_wrap:
        return {
            "ok": True,
            "tool": "add_error_handling",
            "file": str(path),
            "language": _language(path),
            "written": False,
            "already_compliant": True,
            "changes": {"wrapped": []},
            "note": "every risky function already has a try/catch (or none were risky)",
        }

    new_source = source
    for function in sorted(to_wrap, key=lambda f: f["open_index"], reverse=True):
        new_source = (
            new_source[: function["open_index"] + 1]
            + _js_wrap_body(source, function)
            + new_source[function["close_index"] :]
        )

    lines = new_source.splitlines()
    if not _js_has_logger(source):
        kind = _js_logger_kind(source)
        lines = insert_lines(lines, [(js_import_insert_line(source.splitlines()), _js_logger_block(kind))])
        new_source = _join_lines(lines, source)

    if not _js_balanced(new_source):
        return {"ok": False, "error": "refusing to write JS/TS with unbalanced braces"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_error_handling",
        "file": str(path),
        "language": _language(path),
        "dry_run": dry_run,
        "changes": {"wrapped": [f["name"] for f in to_wrap]},
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


async def _add_error_handling(ctx: dict[str, Any], file_path: str, dry_run: bool = False) -> dict[str, Any]:
    result = await _read_source(ctx, file_path, allowed=SUPPORTED_SUFFIXES)
    if isinstance(result, dict):
        return result
    path, source = result
    if _language(path) == "python":
        return await _add_error_handling_python(ctx, path, source, dry_run)
    return await _add_error_handling_js(ctx, path, source, dry_run)


# ---------------------------------------------------------------------------
# add_input_validation
# ---------------------------------------------------------------------------
def _py_function_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.AST | None]]:
    """Return ``(name, annotation)`` pairs, dropping ``self``/``cls``."""
    pairs: list[tuple[str, ast.AST | None]] = []
    positional = list(getattr(node.args, "posonlyargs", []) or []) + list(node.args.args)
    defaults_offset = len(positional) - len(node.args.defaults)
    for index, arg in enumerate(positional):
        if index == 0 and arg.arg in {"self", "cls"}:
            continue
        if index >= defaults_offset:
            continue  # has a default: optional by contract
        pairs.append((arg.arg, arg.annotation))
    for arg, default in zip(node.args.kwonlyargs, node.args.defaults):
        if arg.arg in node.args.kw_defaults and False:  # pragma: no cover - defensive
            continue
        _ = default
        if arg.arg not in {name for name, _ in pairs}:
            pairs.append((arg.arg, arg.annotation))
    return pairs


def _py_has_raise(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Raise) for child in ast.walk(node))


async def _add_input_validation_python(ctx: dict[str, Any], path: Path, source: str, dry_run: bool) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"cannot parse {path.name}: {exc.msg} (line {exc.lineno})"}

    lines = source.splitlines()
    edits: list[tuple[int, int, list[str]]] = []
    validated: dict[str, list[str]] = {}

    for node in _py_functions(tree):
        if _py_has_raise(node):
            continue
        statements = _py_body_statements(node)
        if not statements:
            continue
        first = statements[0]
        if first.lineno == node.lineno:
            continue
        pairs = _py_function_args(node)
        if not pairs:
            continue
        indent = _indent_of(lines[first.lineno - 1])
        block: list[str] = []
        for name, annotation in pairs:
            declared = _py_annotation_name(annotation)
            if declared:
                block.append(f"{indent}if not isinstance({name}, {declared}):")
                block.append(
                    f'{indent}    raise TypeError(f"{name} must be {declared}, '
                    f'got {{type({name}).__name__}}")'
                )
            else:
                block.append(f"{indent}if {name} is None:")
                block.append(f'{indent}    raise ValueError("{name} must not be None")')
        if block:
            edits.append((first.lineno - 1, 0, block))
            validated[node.name] = [name for name, _ in pairs]

    if not edits:
        return {
            "ok": True,
            "tool": "add_input_validation",
            "file": str(path),
            "language": "python",
            "written": False,
            "already_compliant": True,
            "changes": {"validated": {}},
            "note": "functions already validate their arguments (or take none)",
        }

    new_source = _join_lines(apply_edits(lines, edits), source)
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"refusing to write invalid Python: {exc.msg} (line {exc.lineno})"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_input_validation",
        "file": str(path),
        "language": "python",
        "dry_run": dry_run,
        "changes": {"validated": validated},
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


async def _add_input_validation_js(ctx: dict[str, Any], path: Path, source: str, dry_run: bool) -> dict[str, Any]:
    lines = source.splitlines()
    insertions: list[tuple[int, list[str]]] = []
    validated: dict[str, list[str]] = {}

    for function in find_js_functions(source):
        if not function["params"]:
            continue
        body = source[function["body_start"] : function["body_end"]]
        body_lines = [line for line in body.splitlines() if line.strip()]
        if not body_lines:
            continue
        indent = _indent_of(body_lines[0])
        block: list[str] = []
        guarded: list[str] = []
        for name in function["params"]:
            if re.search(rf"\b{re.escape(name)}\s*===?\s*(?:undefined|null)|throw new TypeError", body):
                continue
            block.append(
                f"{indent}if ({name} === undefined || {name} === null) "
                f'throw new TypeError("{name} is required");'
            )
            guarded.append(name)
        if block:
            insertions.append((function["body_line_start"], block))
            validated[function["name"]] = guarded

    if not insertions:
        return {
            "ok": True,
            "tool": "add_input_validation",
            "file": str(path),
            "language": _language(path),
            "written": False,
            "already_compliant": True,
            "changes": {"validated": {}},
            "note": "functions already guard their arguments (or take none)",
        }

    new_source = _join_lines(insert_lines(lines, insertions), source)
    if not _js_balanced(new_source):
        return {"ok": False, "error": "refusing to write JS/TS with unbalanced braces"}

    outcome = await _write_source(ctx, path, new_source, dry_run=dry_run)
    return {
        "ok": True,
        "tool": "add_input_validation",
        "file": str(path),
        "language": _language(path),
        "dry_run": dry_run,
        "changes": {"validated": validated},
        "diff": _diff_summary(source, new_source),
        "write": outcome,
    }


async def _add_input_validation(ctx: dict[str, Any], file_path: str, dry_run: bool = False) -> dict[str, Any]:
    result = await _read_source(ctx, file_path, allowed=SUPPORTED_SUFFIXES)
    if isinstance(result, dict):
        return result
    path, source = result
    if _language(path) == "python":
        return await _add_input_validation_python(ctx, path, source, dry_run)
    return await _add_input_validation_js(ctx, path, source, dry_run)
