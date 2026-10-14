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
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is not None:
            continue  # keyword-only with a default: optional by contract
        if arg.arg in {name for name, _ in pairs}:
            continue
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


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------
async def _read_json(ctx: dict[str, Any], path: Path) -> dict[str, Any]:
    tools = ctx.get("tools")
    try:
        text = await tools.read(str(path))
    except (RuntimeError, AttributeError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _detect_project(ctx: dict[str, Any], root: Path, override: str = "auto") -> dict[str, Any]:
    """Inspect a project directory and return everything the generators need."""
    import anyio

    info: dict[str, Any] = {
        "root": str(root),
        "runtime": "generic",
        "package_manager": "npm",
        "framework": None,
        "port": 8000,
        "entrypoint": None,
        "node_version": "20",
        "python_version": "3.11",
        "lockfile": None,
        "scripts": {},
    }

    package_json = root / "package.json"
    if await anyio.Path(package_json).is_file():
        manifest = await _read_json(ctx, package_json)
        dependencies = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
        info.update(
            runtime="node",
            package_manager=_detect_package_manager(root),
            lockfile=_detect_lockfile(root),
            framework="nextjs" if "next" in dependencies else None,
            scripts=manifest.get("scripts") or {},
            port=3000,
            name=manifest.get("name") or root.name,
            package_json=manifest,
        )
        engines = manifest.get("engines") or {}
        node_engine = str(engines.get("node") or "")
        match = re.search(r"(\d+)", node_engine)
        if match:
            info["node_version"] = match.group(1)

    if info["runtime"] == "generic":
        for marker in ("requirements.txt", "pyproject.toml", "Pipfile", "manage.py"):
            if await anyio.Path(root / marker).exists():
                info.update(runtime="python", port=8000)
                break
    if info["runtime"] == "python":
        info["python_version"] = await _detect_python_version(ctx, root)
        info["entrypoint"] = await _detect_python_entrypoint(ctx, root)

    if info["runtime"] == "generic" and await anyio.Path(root / "Cargo.toml").is_file():
        cargo = (await anyio.Path(root / "Cargo.toml").read_text(encoding="utf-8", errors="replace")) if True else ""
        name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', cargo)
        info.update(runtime="rust", port=8080, name=name_match.group(1) if name_match else root.name)

    if override in {"node", "python", "rust"}:
        info["runtime"] = override
        if override == "node" and info.get("port") == 8000:
            info["port"] = 3000
        if override == "python" and info.get("port") == 3000:
            info["port"] = 8000
    return info


def _detect_package_manager(root: Path) -> str:
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
        ("bun.lockb", "bun"),
    ):
        if (root / lockfile).exists():
            return manager
    return "npm"


def _detect_lockfile(root: Path) -> str | None:
    for lockfile in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"):
        if (root / lockfile).exists():
            return lockfile
    return None


async def _detect_python_version(ctx: dict[str, Any], root: Path) -> str:
    import anyio

    pyproject = root / "pyproject.toml"
    if await anyio.Path(pyproject).is_file():
        text = await ctx["tools"].read(str(pyproject)) if ctx.get("tools") else ""
        match = re.search(r"requires-python\s*=\s*[\"'][>=~^]*\s*3\.(\d+)", text or "")
        if match:
            return f"3.{match.group(1)}"
    return "3.11"


async def _detect_python_entrypoint(ctx: dict[str, Any], root: Path) -> str | None:
    """Find ``module:app`` for ASGI apps, or a plain script to run."""
    import anyio

    candidates = ["app.py", "main.py", "src/app.py", "src/main.py", "api/main.py", "server.py", "wsgi.py"]
    for candidate in candidates:
        target = root / candidate
        if not await anyio.Path(target).is_file():
            continue
        try:
            text = await ctx["tools"].read(str(target))
        except RuntimeError:
            continue
        if re.search(r"\bFastAPI\s*\(", text) or re.search(r"\bFlask\s*\(", text) or re.search(r"\bapp\s*=\s*\w+\(", text):
            module = candidate[:-3].replace("/", ".").replace("\\", ".")
            return f"{module}:app"
    if await anyio.Path(root / "manage.py").exists():
        return "manage.py"
    return None


# ---------------------------------------------------------------------------
# generate_dockerfile
# ---------------------------------------------------------------------------
DOCKERIGNORE = """\
.git
.gitignore
.github
node_modules
__pycache__
*.py[cod]
.venv
venv
target
dist
build
.next
out
coverage
.env
.env.*
!.env.example
logs
*.log
nour-code-sessions.json
Dockerfile*
docker-compose*.yml
README.md
"""


def _node_dockerfile(info: dict[str, Any], port: int) -> str:
    manager = info["package_manager"]
    node_version = info["node_version"]
    lockfile = info["lockfile"]
    install = {
        "npm": "npm ci",
        "pnpm": "npm i -g pnpm@9 && pnpm install --frozen-lockfile",
        "yarn": "yarn install --frozen-lockfile",
        "bun": "npm i -g bun && bun install --frozen-lockfile",
    }[manager]
    copy_manifest = f"COPY package.json {lockfile + ' ' if lockfile else ''}./"
    if not lockfile:
        install = {
            "npm": "npm install",
            "pnpm": "npm i -g pnpm@9 && pnpm install",
            "yarn": "yarn install",
            "bun": "npm i -g bun && bun install",
        }[manager]
        copy_manifest = "COPY package.json ./"

    if info.get("framework") == "nextjs":
        return f"""# syntax=docker/dockerfile:1
# generated by nour-code production_skill
FROM node:{node_version}-alpine AS base
ENV NODE_ENV=production
WORKDIR /app

FROM base AS deps
{copy_manifest}
RUN {install}

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN {"npm run build" if manager == "npm" else f"{manager} build"}

FROM base AS runner
RUN addgroup --system --gid 1001 nodejs \\
 && adduser --system --uid 1001 nextjs
WORKDIR /app
ENV PORT={port} \\
    HOSTNAME=0.0.0.0 \\
    NEXT_TELEMETRY_DISABLED=1
COPY --from=builder --chown=nextjs:nodejs /app/public ./public 2>/dev/null || true
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD wget -qO- "http://127.0.0.1:${{PORT}}/api/health" >/dev/null 2>&1 || exit 1
CMD ["node", "server.js"]
"""

    start_script = (info.get("scripts") or {}).get("start") or "node server.js"
    start_cmd = " ".join(f'"{part}"' for part in _shell_words(start_script))
    return f"""# syntax=docker/dockerfile:1
# generated by nour-code production_skill
FROM node:{node_version}-alpine AS base
ENV NODE_ENV=production
WORKDIR /app

FROM base AS deps
{copy_manifest}
RUN {install}

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN if [ -f tsconfig.json ]; then {"npm run build" if manager == "npm" else f"{manager} build"}; fi

FROM base AS runner
RUN addgroup --system --gid 1001 nodejs \\
 && adduser --system --uid 1001 appuser
COPY --from=builder --chown=appuser:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=appuser:nodejs /app/package.json ./package.json
COPY --from=builder --chown=appuser:nodejs /app .
USER appuser
ENV PORT={port}
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD wget -qO- "http://127.0.0.1:${{PORT}}/health" >/dev/null 2>&1 || exit 1
CMD [{start_cmd}]
"""


def _python_dockerfile(info: dict[str, Any], port: int) -> str:
    version = info["python_version"]
    entrypoint = info.get("entrypoint")
    if entrypoint == "manage.py":
        cmd = f'CMD ["gunicorn", "--bind", "0.0.0.0:{port}", "--workers", "4", "--access-logfile", "-", "config.wsgi:application"]'
    elif entrypoint:
        cmd = f'CMD ["uvicorn", "{entrypoint}", "--host", "0.0.0.0", "--port", "{port}", "--proxy-headers"]'
    else:
        cmd = f'CMD ["python", "-m", "http.server", "{port}"]'
    return f"""# syntax=docker/dockerfile:1
# generated by nour-code production_skill
FROM python:{version}-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements*.txt pyproject.toml* ./
RUN --mount=type=cache,target=/root/.cache/pip \\
    if [ -f requirements.txt ]; then pip install --prefix=/install -r requirements.txt; \\
    elif [ -f pyproject.toml ]; then pip install --prefix=/install .; fi

FROM python:{version}-slim AS runner
ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONPATH=/app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .
RUN useradd --create-home --uid 10001 --shell /bin/bash appuser \\
 && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:{port}/health').status == 200 else 1)"
{cmd}
"""


def _rust_dockerfile(info: dict[str, Any], port: int) -> str:
    name = info.get("name") or "app"
    return f"""# syntax=docker/dockerfile:1
# generated by nour-code production_skill
FROM rust:1.82-bookworm AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev \\
 && rm -rf /var/lib/apt/lists/*
COPY Cargo.toml Cargo.lock* ./
RUN mkdir -p src && echo "fn main() {{}}" > src/main.rs && cargo build --release && rm -rf src
COPY . .
RUN touch src/main.rs && cargo build --release --locked

FROM debian:bookworm-slim AS runner
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libpq5 curl \\
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/{name} /usr/local/bin/{name}
RUN useradd --create-home --uid 10001 appuser
USER appuser
ENV RUST_LOG=info
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \\
  CMD curl -f "http://127.0.0.1:{port}/health" || exit 1
CMD ["{name}"]
"""


def _shell_words(command: str) -> list[str]:
    return [word for word in command.split() if word]


async def _generate_dockerfile(
    ctx: dict[str, Any], project_root: str, runtime: str = "auto", port: int = 0, overwrite: bool = False
) -> dict[str, Any]:
    permissions = ctx.get("permissions")
    if permissions is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "permissions unavailable"}
    try:
        root = permissions.resolve_path(project_root, must_exist=True, kind="dir")
    except PermissionError as exc:
        return {"ok": False, "error": f"path rejected: {exc}"}

    info = await _detect_project(ctx, root, runtime)
    if info["runtime"] == "generic":
        return {
            "ok": False,
            "error": "could not detect the runtime (no package.json, requirements.txt/pyproject.toml or Cargo.toml). "
            "Pass runtime='node' or runtime='python' explicitly.",
        }

    port = int(port) if port else int(info.get("port") or 8000)
    builders = {"node": _node_dockerfile, "python": _python_dockerfile, "rust": _rust_dockerfile}
    content = builders[info["runtime"]](info, port)

    target = root / "Dockerfile"
    import anyio

    exists = await anyio.Path(target).is_file()
    if exists and not overwrite:
        return {
            "ok": True,
            "tool": "generate_dockerfile",
            "path": str(target),
            "written": False,
            "reason": "Dockerfile already exists; pass overwrite=true to replace it",
            "runtime": info["runtime"],
            "preview": content,
        }

    outcome = await _write_source(ctx, target, content, dry_run=False)
    dockerignore_outcome: dict[str, Any] = {}
    ignore_path = root / ".dockerignore"
    if not await anyio.Path(ignore_path).is_file():
        dockerignore_outcome = await _write_source(ctx, ignore_path, DOCKERIGNORE, dry_run=False)

    notes = [
        f"Multi-stage build for {info['runtime']}"
        + (f" ({info['framework']} standalone output)" if info.get("framework") else ""),
        "Runs as a non-root user and exposes a healthcheck on port " + str(port),
    ]
    if info["runtime"] == "node" and not info.get("lockfile"):
        notes.append("No lockfile found: commit one so builds are reproducible (npm ci).")
    if info["runtime"] == "node" and info.get("framework") == "nextjs":
        notes.append("Requires `output: 'standalone'` in next.config.js (add it if missing).")
    if info["runtime"] == "python" and not info.get("entrypoint"):
        notes.append("No ASGI entrypoint detected: adjust the CMD to your app (e.g. uvicorn package.module:app).")
    if dockerignore_outcome:
        notes.append("Wrote .dockerignore to keep the build context small.")

    return {
        "ok": True,
        "tool": "generate_dockerfile",
        "path": str(target),
        "written": bool(outcome.get("written")),
        "runtime": info["runtime"],
        "framework": info.get("framework"),
        "port": port,
        "detected": {k: v for k, v in info.items() if k not in {"package_json", "scripts"}},
        "notes": notes,
        "preview": "\n".join(content.splitlines()[:40]),
        "write": outcome,
    }


# ---------------------------------------------------------------------------
# generate_ci_pipeline
# ---------------------------------------------------------------------------
def _github_workflow(info: dict[str, Any]) -> str:
    runtime = info["runtime"]
    if runtime == "node":
        manager = info["package_manager"]
        setup = f"""      - uses: actions/setup-node@v4
        with:
          node-version: "{info['node_version']}"
          cache: "{manager}" """
        install = {
            "npm": "npm ci",
            "pnpm": "npm i -g pnpm@9 && pnpm install --frozen-lockfile",
            "yarn": "yarn install --frozen-lockfile",
            "bun": "npm i -g bun && bun install --frozen-lockfile",
        }[manager]
        if not info.get("lockfile"):
            install = install.replace(" ci", " install").replace(" --frozen-lockfile", "")
        lint = f"      - run: {manager} run lint --if-present"
        test = f"      - run: {manager} test --if-present"
        build = f"      - run: {manager} run build --if-present"
        audit = f"""  audit:
    name: dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
{setup}
      - run: {install}
      - run: {"npm audit --audit-level=high" if manager == "npm" else manager + " audit"}
        continue-on-error: true
"""
    elif runtime == "python":
        setup = f"""      - uses: actions/setup-python@v5
        with:
          python-version: "{info['python_version']}"
          cache: "pip" """
        install = """      - name: install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          if [ -f requirements-dev.txt ]; then pip install -r requirements-dev.txt; fi
          if [ -f pyproject.toml ]; then pip install -e .; fi
          pip install pytest ruff"""
        lint = "      - run: ruff check . || true"
        test = "      - run: pytest -q"
        build = "      - run: python -m compileall . >/dev/null"
        audit = """  audit:
    name: dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""" + setup + """
      - run: pip install pip-audit
      - run: pip-audit -r requirements.txt || pip-audit
        continue-on-error: true
"""
    else:  # rust
        setup = """      - uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy, rustfmt
      - uses: Swatinem/rust-cache@v2"""
        install = "      - run: cargo fetch --locked"
        lint = "      - run: cargo fmt --all -- --check && cargo clippy --all-targets -- -D warnings"
        test = "      - run: cargo test --locked --all-features"
        build = "      - run: cargo build --release --locked"
        audit = """  audit:
    name: dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo install cargo-audit || true
      - run: cargo audit
        continue-on-error: true
"""

    return f"""# generated by nour-code production_skill
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  test:
    name: lint, test, build
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: app_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      DATABASE_URL: postgres://postgres:postgres@localhost:5432/app_test
      CI: "true"
    steps:
      - uses: actions/checkout@v4
{setup}
{install}
{lint}
{test}
{build}

  docker:
    name: container image
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: build image
        run: docker build -t {'$'}{{{{ github.repository }}}}:${{{{ github.sha }}}} .

{audit}"""


def _gitlab_pipeline(info: dict[str, Any]) -> str:
    runtime = info["runtime"]
    if runtime == "node":
        image = f"node:{info['node_version']}-alpine"
        install = "  - npm ci" if info.get("lockfile") else "  - npm install"
        lint = "  - npm run lint --if-present"
        test = "  - npm test --if-present"
        build = "  - npm run build --if-present"
    elif runtime == "python":
        image = f"python:{info['python_version']}-slim"
        install = "  - pip install -r requirements.txt\n  - pip install pytest ruff"
        lint = "  - ruff check . || true"
        test = "  - pytest -q"
        build = "  - python -m compileall . > /dev/null"
    else:
        image = "rust:1.82-bookworm"
        install = "  - cargo fetch --locked"
        lint = "  - cargo fmt --all -- --check"
        test = "  - cargo test --locked"
        build = "  - cargo build --release --locked"

    return f"""# generated by nour-code production_skill
stages:
  - lint
  - test
  - build

variables:
  POSTGRES_USER: postgres
  POSTGRES_PASSWORD: postgres
  POSTGRES_DB: app_test
  DATABASE_URL: postgres://postgres:postgres@postgres:5432/app_test

default:
  image: {image}
  services:
    - name: postgres:16
      alias: postgres
  interruptible: true

lint:
  stage: lint
  script:
{install}
{lint}

test:
  stage: test
  script:
{install}
{test}

build:
  stage: build
  script:
{install}
{build}
"""


async def _generate_ci_pipeline(
    ctx: dict[str, Any], project_root: str, target: str = "github", overwrite: bool = False
) -> dict[str, Any]:
    permissions = ctx.get("permissions")
    if permissions is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "permissions unavailable"}
    try:
        root = permissions.resolve_path(project_root, must_exist=True, kind="dir")
    except PermissionError as exc:
        return {"ok": False, "error": f"path rejected: {exc}"}

    if target not in {"github", "gitlab"}:
        return {
            "ok": False,
            "error": f"unsupported CI target {target!r} (supported: github, gitlab)",
        }

    import anyio

    info = await _detect_project(ctx, root)
    if info["runtime"] == "generic":
        info["runtime"] = "node"  # sensible default; user can edit the file

    content = _github_workflow(info) if target == "github" else _gitlab_pipeline(info)
    relative = ".github/workflows/ci.yml" if target == "github" else ".gitlab-ci.yml"
    target_path = root / relative
    target_path.parent.mkdir(parents=True, exist_ok=True)

    exists = await anyio.Path(target_path).is_file()
    if exists and not overwrite:
        return {
            "ok": True,
            "tool": "generate_ci_pipeline",
            "path": str(target_path),
            "written": False,
            "reason": f"{relative} already exists; pass overwrite=true to replace it",
            "preview": content,
        }

    outcome = await _write_source(ctx, target_path, content, dry_run=False)
    return {
        "ok": True,
        "tool": "generate_ci_pipeline",
        "path": str(target_path),
        "written": bool(outcome.get("written")),
        "target": target,
        "runtime": info["runtime"],
        "notes": [
            "Pipeline caches dependencies, lints, runs tests against a Postgres service and builds the image.",
            "Add the secrets your deploy step needs (Settings > Secrets and variables > Actions).",
        ],
        "preview": "\n".join(content.splitlines()[:40]),
        "write": outcome,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def handle(tool_name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to the right implementation."""
    logger = ctx.get("logger")
    try:
        if tool_name == "add_logging":
            return await _add_logging(
                ctx,
                file_path=str(args["file_path"]),
                level=str(args.get("level", "info")),
                dry_run=bool(args.get("dry_run", False)),
            )
        if tool_name == "add_error_handling":
            return await _add_error_handling(
                ctx, file_path=str(args["file_path"]), dry_run=bool(args.get("dry_run", False))
            )
        if tool_name == "add_input_validation":
            return await _add_input_validation(
                ctx, file_path=str(args["file_path"]), dry_run=bool(args.get("dry_run", False))
            )
        if tool_name == "generate_dockerfile":
            return await _generate_dockerfile(
                ctx,
                project_root=str(args["project_root"]),
                runtime=str(args.get("runtime", "auto")),
                port=int(args.get("port", 0) or 0),
                overwrite=bool(args.get("overwrite", False)),
            )
        if tool_name == "generate_ci_pipeline":
            return await _generate_ci_pipeline(
                ctx,
                project_root=str(args["project_root"]),
                target=str(args.get("target", "github")),
                overwrite=bool(args.get("overwrite", False)),
            )
    except KeyError as exc:
        return {"ok": False, "error": f"missing required argument: {exc.args[0]}"}
    except PermissionError as exc:
        return {"ok": False, "error": f"permission denied: {exc}"}
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid arguments: {exc}"}
    except OSError as exc:
        if logger:
            logger.warning("io error in %s: %s", tool_name, exc)
        return {"ok": False, "error": f"io error: {exc}"}

    return {"ok": False, "error": f"production_skill has no tool named {tool_name!r}"}
