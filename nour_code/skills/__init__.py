"""Runtime skill loader — the extensibility hook of nour-code.

A skill is a single ``.py`` file dropped into a skills directory:

    SKILL_NAME = "security_review"
    SKILL_DESCRIPTION = "..."
    SKILL_TRIGGERS = ["when user asks for a security review"]
    SKILL_TOOLS = [ { ...OpenAI tool schema... } ]

    async def handle(tool_name: str, args: dict, ctx: dict) -> dict: ...

The loader discovers every ``*.py`` file (skipping ``__init__.py`` and anything
starting with ``_``), imports it, validates the interface, and registers its
tools into the model's tool list.  A broken skill is logged and skipped — it can
never take the server down.  ``POST /skills/reload`` re-runs discovery without
restarting the process.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from ..config import Settings
from ..models import ReloadSkillsResponse, SkillError, SkillInfo, SkillListResponse
from ..tools import ToolContext, ToolRegistry, ToolSpec

logger = logging.getLogger("nour_code.skills")


class SkillLoadError(Exception):
    """Raised when a skill module does not satisfy the contract."""


class PermissiveArgs(BaseModel):
    """Argument model for skill tools: validated by the skill itself."""

    model_config = ConfigDict(extra="allow")


@dataclass
class SkillModule:
    """A validated, loaded skill."""

    name: str
    description: str
    triggers: list[str]
    tools: list[dict[str, Any]]
    module: ModuleType
    path: Path
    version: str = "0.1.0"
    loaded_at: float = field(default_factory=time.time)
    tool_names: list[str] = field(default_factory=list)

    def info(self) -> SkillInfo:
        return SkillInfo(
            name=self.name,
            description=self.description,
            triggers=list(self.triggers),
            tools=list(self.tool_names),
            source=str(self.path),
            version=self.version,
            loaded_at=self.loaded_at,
        )


class SkillsManager:
    """Discovers, validates, registers and dispatches skill tools."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.logger = logger or logging.getLogger("nour_code.skills")
        self._skills: dict[str, SkillModule] = {}
        self._tool_owner: dict[str, str] = {}
        self._last_errors: list[SkillError] = []
        self._lock = asyncio.Lock()
        registry.set_skill_dispatcher(self.dispatch)

    # -- discovery ---------------------------------------------------------
    @property
    def directories(self) -> list[Path]:
        return [d for d in self.settings.skill_directories]

    def discover_files(self) -> list[Path]:
        """All candidate skill files, de-duplicated, in directory priority order."""
        files: list[Path] = []
        seen: set[str] = set()
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                key = str(path.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(path)
        return files

    # -- loading -----------------------------------------------------------
    async def load_all(self) -> ReloadSkillsResponse:
        """Load every discovered skill (used at startup)."""
        async with self._lock:
            return self._load_sync()

    async def reload(self) -> ReloadSkillsResponse:
        """Hot reload: drop current skill tools, then rediscover."""
        async with self._lock:
            for name in list(self._skills):
                self.registry.unregister_skill(name)
            self._skills.clear()
            self._tool_owner.clear()
            return self._load_sync()

    def _load_sync(self) -> ReloadSkillsResponse:
        started = time.perf_counter()
        loaded: list[str] = []
        skipped: list[str] = []
        errors: list[SkillError] = []

        for path in self.discover_files():
            try:
                skill = self._load_file(path)
            except SkillLoadError as exc:
                errors.append(SkillError(path=str(path), error=str(exc), detail=""))
                skipped.append(path.name)
                self.logger.error("skill %s rejected: %s", path.name, exc)
                continue
            except Exception as exc:  # import-time explosion
                errors.append(
                    SkillError(path=str(path), error=f"{exc.__class__.__name__}: {exc}", detail=traceback.format_exc(limit=4))
                )
                skipped.append(path.name)
                self.logger.error("skill %s failed to import: %s", path.name, exc)
                continue

            if skill.name in self._skills:
                errors.append(
                    SkillError(path=str(path), error=f"duplicate SKILL_NAME {skill.name!r}", detail="")
                )
                skipped.append(path.name)
                continue

            self._skills[skill.name] = skill
            try:
                self._register_tools(skill)
            except SkillLoadError as exc:
                errors.append(SkillError(path=str(path), error=str(exc), detail=""))
                self._skills.pop(skill.name, None)
                skipped.append(path.name)
                continue
            loaded.append(f"{skill.name} ({', '.join(skill.tool_names) or 'no tools'})")
            self.logger.info(
                "skill loaded",
                extra={"skill": skill.name, "tools": skill.tool_names, "path": str(path)},
            )

        self._last_errors = errors
        return ReloadSkillsResponse(
            ok=not errors,
            loaded=loaded,
            skipped=skipped,
            errors=errors,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def _load_file(self, path: Path) -> SkillModule:
        module_name = f"nour_code_skill_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise SkillLoadError("cannot build an import spec for this file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise exc

        name = getattr(module, "SKILL_NAME", None)
        if not isinstance(name, str) or not name.strip():
            sys.modules.pop(module_name, None)
            raise SkillLoadError("SKILL_NAME must be a non-empty string")
        description = getattr(module, "SKILL_DESCRIPTION", "")
        if not isinstance(description, str):
            description = str(description)
        triggers = getattr(module, "SKILL_TRIGGERS", []) or []
        if not isinstance(triggers, (list, tuple)):
            raise SkillLoadError("SKILL_TRIGGERS must be a list of strings")
        triggers = [str(t) for t in triggers]
        handler = getattr(module, "handle", None)
        if handler is None or not callable(handler):
            raise SkillLoadError("skill must define `async def handle(tool_name, args, ctx)`")
        if not inspect.iscoroutinefunction(handler):
            raise SkillLoadError("`handle` must be async (async def handle(...))")

        raw_tools = getattr(module, "SKILL_TOOLS", []) or []
        if not isinstance(raw_tools, (list, tuple)):
            raise SkillLoadError("SKILL_TOOLS must be a list of tool schemas")
        try:
            tools = [self._normalise_tool(item, name) for item in raw_tools]
        except SkillLoadError:
            sys.modules.pop(module_name, None)
            raise

        version = getattr(module, "SKILL_VERSION", "0.1.0")
        return SkillModule(
            name=name.strip(),
            description=description.strip() or f"skill {name}",
            triggers=triggers,
            tools=tools,
            module=module,
            path=path,
            version=str(version),
        )

    @staticmethod
    def _normalise_tool(item: Any, skill_name: str) -> dict[str, Any]:
        """Accept a raw OpenAI schema dict or a Pydantic model class."""
        if isinstance(item, dict):
            if "function" in item and isinstance(item["function"], dict):
                schema = dict(item)
            elif "name" in item:
                schema = {
                    "type": "function",
                    "function": {
                        "name": str(item["name"]),
                        "description": str(item.get("description", "")),
                        "parameters": item.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            else:
                raise SkillLoadError(f"skill {skill_name}: unrecognised tool schema {item!r:.120}")
            function = schema.get("function") or {}
            if not function.get("name"):
                raise SkillLoadError(f"skill {skill_name}: tool schema has no name")
            function.setdefault("description", "")
            function.setdefault("parameters", {"type": "object", "properties": {}})
            schema.setdefault("type", "function")
            return schema

        if isinstance(item, type) and issubclass(item, BaseModel):
            name = getattr(item, "tool_name", None) or item.__name__
            description = getattr(item, "tool_description", None) or (item.__doc__ or "").strip()
            parameters = item.model_json_schema()
            parameters.pop("title", None)
            return {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": str(description).splitlines()[0] if description else "",
                    "parameters": parameters,
                },
            }

        raise SkillLoadError(f"skill {skill_name}: tool entries must be dicts or pydantic models")

    def _register_tools(self, skill: SkillModule) -> None:
        for schema in skill.tools:
            tool_name = str((schema.get("function") or {}).get("name"))
            if not tool_name:
                raise SkillLoadError("tool schema without a name")
            owner = self._tool_owner.get(tool_name)
            if owner and owner != skill.name:
                raise SkillLoadError(
                    f"tool '{tool_name}' is already provided by skill '{owner}'"
                )
            self.registry.register(
                ToolSpec(
                    name=tool_name,
                    description=str((schema.get("function") or {}).get("description") or ""),
                    args_model=PermissiveArgs,
                    handler=_skill_noop,  # real work happens in SkillsManager.dispatch
                    kind="skill",
                    skill=skill.name,
                    schema_override=schema,
                    scope=tool_name,
                )
            )
            self._tool_owner[tool_name] = skill.name
            skill.tool_names.append(tool_name)

    # -- introspection ------------------------------------------------------
    def info(self) -> SkillListResponse:
        return SkillListResponse(
            count=len(self._skills),
            skills=[s.info() for s in self._skills.values()],
            directories=[str(d) for d in self.directories],
        )

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> SkillModule | None:
        return self._skills.get(name)

    def owner_of(self, tool_name: str) -> str | None:
        return self._tool_owner.get(tool_name)

    @property
    def last_errors(self) -> list[SkillError]:
        return list(self._last_errors)

    def schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for skill in self._skills.values():
            out.extend(skill.tools)
        return out

    def trigger_index(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "description": s.description, "triggers": s.triggers, "tools": s.tool_names}
            for s in self._skills.values()
        ]

    # -- dispatch -----------------------------------------------------------
    async def dispatch(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Route a tool call to the owning skill. Never raises."""
        owner = self._tool_owner.get(tool_name)
        skill = self._skills.get(owner) if owner else None
        if skill is None:
            return {"ok": False, "error": f"no loaded skill provides tool '{tool_name}'"}

        handler: Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[Any]] = skill.module.handle
        skill_ctx = self.build_context(ctx, skill)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                handler(tool_name, dict(args or {}), skill_ctx),
                timeout=self.settings.skill_tool_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": f"skill '{skill.name}' timed out after {self.settings.skill_tool_timeout_seconds}s",
            }
        except PermissionError as exc:  # surfaced to the model, never fatal
            return {"ok": False, "error": f"permission error: {exc}"}
        except Exception as exc:
            self.logger.exception("skill %s failed on %s", skill.name, tool_name)
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

        duration_ms = (time.perf_counter() - started) * 1000.0
        if isinstance(result, dict):
            result.setdefault("ok", True)
            result.setdefault("skill", skill.name)
            result.setdefault("duration_ms", round(duration_ms, 2))
            return result
        return {"ok": True, "skill": skill.name, "result": result, "duration_ms": round(duration_ms, 2)}

    def build_context(self, ctx: ToolContext, skill: SkillModule) -> dict[str, Any]:
        """Context handed to ``handle()`` — everything a skill may need, nothing more."""
        ctx.extra.setdefault("registry", self.registry)
        return {
            "session_id": ctx.session_id,
            "working_dir": ctx.working_dir,
            "settings": ctx.settings,
            "permissions": ctx.permissions,
            "logger": self.logger.getChild(skill.name),
            "skill": skill.name,
            "skill_path": str(skill.path),
            "tools": _ToolFacade(ctx),
            "registry": self.registry,
            "skills": self,
            "emit": ctx.emit,
            "now": time.time(),
        }


async def _skill_noop(args: BaseModel, ctx: ToolContext) -> dict[str, Any]:  # pragma: no cover
    """Placeholder handler; skill calls are routed through :meth:`SkillsManager.dispatch`."""
    return {"ok": False, "error": "skill handler was called directly"}


class _ToolFacade:
    """Lets a skill reuse built-in tools: ``await ctx["tools"].call("read_file", path=...)``."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    async def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        registry = self._ctx.extra.get("registry")
        if registry is None:  # pragma: no cover - wiring guarantee
            return {"ok": False, "error": "tool registry unavailable in this context"}
        result = await registry.call(name, kwargs, self._ctx)
        return {
            "ok": result.ok,
            "status": result.status,
            "result": result.output,
            "error": result.error,
            "duration_ms": round(result.duration_ms, 2),
        }

    async def read(self, path: str, **kwargs: Any) -> str:
        outcome = await self.call("read_file", path=path, **kwargs)
        if not outcome["ok"]:
            raise RuntimeError(str(outcome["error"]))
        return str(outcome["result"])

    async def write(self, path: str, content: str, **kwargs: Any) -> dict[str, Any]:
        return await self.call("write_file", path=path, content=content, **kwargs)
