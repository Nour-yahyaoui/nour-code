"""Human-in-the-loop approvals for mutating tool calls (1.2.0).

A turn can ask for approval before anything is written, patched, deleted or
executed.  The agent emits a ``tool_proposal`` event and then *parks* the call on
an :class:`asyncio.Future` until a client resolves it through
``POST /sessions/{id}/approve`` (or until the timeout rejects it).

Design notes
------------
* One gate per running turn, keyed by session id, held in :class:`ApprovalRegistry`
  on the DI container — never in module-level state.
* A gate is created when a turn starts and discarded when it ends, so a crashed
  turn cannot leave a stale future behind.
* ``approve_all`` flips a flag: the remaining proposals of that turn are approved
  without another round trip.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import new_id

logger = logging.getLogger("nour_code.approvals")

Decision = Literal["approved", "rejected", "timeout", "cancelled"]

#: Tools that change the machine. Everything else is considered read-only.
MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "apply_patch", "delete_file", "run_shell"})

#: Skill tools known to be read-only (they only report, never write).
READ_ONLY_SKILL_TOOLS: frozenset[str] = frozenset({"security_scan", "check_secrets", "check_dependency_cves"})


def is_mutating(tool: str, *, skill: str | None = None) -> bool:
    """True when a tool call can change files or run a process."""
    if tool in MUTATING_TOOLS:
        return True
    if skill and tool in READ_ONLY_SKILL_TOOLS:
        return False
    # Skill tools default to mutating: most of them rewrite source files.
    return bool(skill)


def risk_of(tool: str) -> str:
    """Coarse risk label used by the UI to colour proposal cards."""
    if tool == "run_shell":
        return "high"
    if tool in {"delete_file", "apply_patch"}:
        return "medium"
    return "low"


@dataclass
class Proposal:
    """A tool call waiting for a human decision."""

    id: str
    tool: str
    arguments: dict[str, Any]
    iteration: int = 0
    skill: str | None = None
    risk: str = "low"
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "iteration": self.iteration,
            "skill": self.skill,
            "risk": self.risk,
            "age_seconds": round(time.time() - self.created_at, 2),
        }


class ApprovalGate:
    """Pending proposals and the futures parked behind them, for one turn."""

    def __init__(
        self,
        *,
        session_id: str,
        timeout_seconds: float = 300.0,
        emit: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.timeout_seconds = timeout_seconds
        self._emit = emit
        self._pending: dict[str, tuple[Proposal, asyncio.Future[str]]] = {}
        self.auto_approve = False
        self.history: list[dict[str, Any]] = []

    # -- queries -----------------------------------------------------------
    @property
    def pending(self) -> list[dict[str, Any]]:
        return [proposal.as_dict() for proposal, _ in self._pending.values()]

    def get(self, proposal_id: str) -> Proposal | None:
        entry = self._pending.get(proposal_id)
        return entry[0] if entry else None

    # -- the gate itself ---------------------------------------------------
    async def request(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        iteration: int = 0,
        skill: str | None = None,
        emit: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Decision:
        """Emit a proposal and wait for a decision (or the timeout)."""
        emitter = emit or self._emit
        if self.auto_approve:
            decision: Decision = "approved"
            self.history.append({"tool": tool, "decision": decision, "auto": True})
            return decision

        proposal = Proposal(
            id=new_id("prop_"),
            tool=tool,
            arguments=arguments,
            iteration=iteration,
            skill=skill,
            risk=risk_of(tool),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[proposal.id] = (proposal, future)

        if emitter is not None:
            from .models import StreamEvent  # local import: models imports nothing from here

            await emitter(
                StreamEvent(
                    type="tool_proposal",
                    data={
                        **proposal.as_dict(),
                        "timeout_seconds": round(self.timeout_seconds, 1),
                    },
                )
            )

        try:
            decision = await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout_seconds)
        except TimeoutError:
            decision = "timeout"
        except asyncio.CancelledError:
            decision = "cancelled"
            self._pending.pop(proposal.id, None)
            raise
        finally:
            self._pending.pop(proposal.id, None)

        self.history.append({"tool": tool, "decision": decision, "auto": False})
        logger.info(
            "proposal %s (%s) -> %s", proposal.id, tool, decision, extra={"session_id": self.session_id}
        )
        return decision

    # -- resolution --------------------------------------------------------
    def resolve(self, proposal_id: str, decision: str) -> bool:
        """Resolve a parked proposal. Returns False when nothing was pending."""
        entry = self._pending.get(proposal_id)
        if entry is None:
            return False
        _proposal, future = entry
        if not future.done():
            future.set_result(decision)
        return True

    def resolve_all(self, decision: str) -> int:
        count = 0
        for proposal_id in list(self._pending):
            count += int(self.resolve(proposal_id, decision))
        return count

    def cancel(self) -> int:
        """Cancel every parked proposal (used when a turn ends or is aborted)."""
        count = 0
        for _proposal, future in list(self._pending.values()):
            if not future.done():
                future.set_result("cancelled")
                count += 1
        self._pending.clear()
        return count

    def close(self) -> None:
        self.cancel()


class ApprovalRegistry:
    """Container-scoped map of session id -> active :class:`ApprovalGate`."""

    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._gates: dict[str, ApprovalGate] = {}

    def create(self, session_id: str, *, emit: Callable[[Any], Awaitable[None]] | None = None) -> ApprovalGate:
        gate = ApprovalGate(session_id=session_id, timeout_seconds=self.timeout_seconds, emit=emit)
        self._gates[session_id] = gate
        return gate

    def get(self, session_id: str) -> ApprovalGate | None:
        return self._gates.get(session_id)

    def discard(self, session_id: str) -> None:
        gate = self._gates.pop(session_id, None)
        if gate is not None:
            gate.close()

    def cancel_all(self) -> int:
        total = 0
        for gate in list(self._gates.values()):
            total += gate.cancel()
        self._gates.clear()
        return total

    @property
    def active(self) -> list[str]:
        return list(self._gates)
