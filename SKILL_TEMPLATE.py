"""Skill template for nour-code — copy this file into a skills directory.

Quick start
-----------
1. Copy this file into the skills directory (default: ``nour_code/skills/`` or
   ``./skills``), e.g. ``my_skill.py``.
2. Fill in ``SKILL_NAME``, ``SKILL_DESCRIPTION``, ``SKILL_TRIGGERS`` and
   ``SKILL_TOOLS``.
3. Implement ``async def handle(tool_name, args, ctx)``.
4. Hot reload without restarting the server::

       curl -X POST http://localhost:8000/skills/reload

   or restart::

       curl http://localhost:8000/skills

Contract
--------
``SKILL_NAME``         unique, non-empty string (required)
``SKILL_DESCRIPTION``  shown to the model in the system prompt (required)
``SKILL_TRIGGERS``     list of "when to use me" hints for the model (optional)
``SKILL_TOOLS``        list of OpenAI function schemas (or pydantic models)
``SKILL_VERSION``      free-form version string (optional, default "0.1.0")
``SKILL_ENABLED``      set False to skip the skill (optional)
``handle(tool_name, args, ctx)``
    Required async coroutine.  It must return a JSON-serialisable ``dict``.
    Returning ``{"ok": False, "error": "..."}`` is reported to the model as a
    tool error; adding ``"denied": True`` reports a permission denial.

Context (``ctx``) contents
--------------------------
``ctx["tools"]``       facade over the built-in tools:
                       ``await ctx["tools"].call("read_file", path=...)``
                       ``await ctx["tools"].read(path)``  -> str
                       ``await ctx["tools"].write(path, content)``
``ctx["permissions"]`` permission manager (``resolve_path``, ``check_shell``, ...)
``ctx["settings"]``    the server :class:`Settings`
``ctx["session_id"]``  current session id (for logging)
``ctx["working_dir"]`` :class:`pathlib.Path` of the session working directory
``ctx["logger"]``      logger namespaced with your skill name
``ctx["skills"]``      the skill manager (introspect other skills)
``ctx["registry"]``    the tool registry (advanced)

Rules of thumb
--------------
* Never trust a path from ``args``: always go through
  ``ctx["permissions"].resolve_path(...)`` — it enforces the allowed roots.
* Keep results small: return summaries, not whole files.  The agent truncates
  anything above ``NOUR_CODE_MAX_TOOL_OUTPUT_CHARS`` anyway.
* Be idempotent where you can: the model may call the same tool twice.
* Raise nothing you cannot explain: an exception is caught, logged and turned
  into ``{"ok": False, "error": "..."}`` — but a clear message is better.
* Skills are loaded with :mod:`importlib` from source; anything you import must
  already be installed in the server's environment.
"""

from __future__ import annotations

import time
from typing import Any

# ---------------------------------------------------------------------------
# Metadata (edit me)
# ---------------------------------------------------------------------------
SKILL_NAME = "my_skill"
SKILL_DESCRIPTION = "One sentence describing what this skill does (shown to the model)."
SKILL_TRIGGERS = [
    "when the user asks for X",
    "before doing Y",
]
SKILL_VERSION = "0.1.0"
SKILL_ENABLED = True

# ---------------------------------------------------------------------------
# Tools this skill exposes (edit me)
#
# Each entry is an OpenAI "function" schema.  You may also pass a pydantic
# model class instead of a dict (its json schema is derived automatically, and
# the name/description come from ``tool_name`` / ``tool_description`` / docstring).
# ---------------------------------------------------------------------------
SKILL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "Does something useful. Be specific: the model chooses tools from this text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File or directory to work on (must be inside an allowed root).",
                    },
                    "verbose": {
                        "type": "boolean",
                        "description": "Include extra detail in the result.",
                        "default": False,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Optional setup hook (runs once after the skill is loaded)
# ---------------------------------------------------------------------------
async def setup(ctx: dict[str, Any]) -> None:
    """Called after loading; use it to pre-compute tables or validate the env."""
    ctx["logger"].debug("my_skill ready at %s", time.time())


# ---------------------------------------------------------------------------
# Dispatcher (edit me)
# ---------------------------------------------------------------------------
async def handle(tool_name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to the right implementation.

    Args:
        tool_name: which of ``SKILL_TOOLS`` the model called.
        args:      the (already JSON-decoded) arguments from the model.
        ctx:       the skill context described in the module docstring.

    Returns:
        A JSON-serialisable dict. ``{"ok": True, ...}`` on success, or
        ``{"ok": False, "error": "..."}`` (optionally ``"denied": True``).
    """
    logger = ctx["logger"]

    if tool_name != "my_tool":
        return {"ok": False, "error": f"{SKILL_NAME} has no tool named {tool_name!r}"}

    try:
        raw_path = str(args["path"])
        # Always resolve through the permission layer: it rejects traversal,
        # symlink escapes and anything outside the allowed roots.
        path = ctx["permissions"].resolve_path(raw_path, must_exist=True)
    except KeyError:
        return {"ok": False, "error": "missing required argument: path"}
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": f"path rejected: {exc}"}

    try:
        # Reuse the built-in tools instead of touching the filesystem directly:
        # they are audited, permission-checked and output-capped.
        content = await ctx["tools"].read(str(path))
    except RuntimeError as exc:
        return {"ok": False, "error": f"could not read {path}: {exc}"}

    lines = content.splitlines()
    summary = {
        "path": str(path),
        "lines": len(lines),
        "bytes": len(content),
    }
    if bool(args.get("verbose")):
        summary["head"] = lines[:20]

    logger.info("my_tool summarised %s", path)
    return {"ok": True, "tool": tool_name, **summary}
