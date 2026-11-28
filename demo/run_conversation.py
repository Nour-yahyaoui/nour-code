#!/usr/bin/env python3
"""Play the user, against a real nour-code server.

This is the other half of the demo's cast: the local model is a fake Ollama
(``landing/scripts/fake-ollama/daemon.py``), and this script is the human at the
keyboard. It talks to ``POST /chat`` over SSE exactly like the console does, and
it resolves every proposal through the real approval endpoint — approving most
of them, and rejecting one on purpose with a reason, because an agent that is
never contradicted proves nothing.

Two acts are scripted:

1. "build a cart API" — write, run, fail, read, fix (rejected once, with a
   note), re-propose, pass.
2. "make the interface feel like 2050" — page, UI test, run both.

and a third, added after the fact: the page is unreachable, so the *user*
reports the 404 and the agent has to write a test, watch it fail, and fix it.

Output: ``demo/transcript.md`` (the readable conversation) and
``demo/events.jsonl`` (every SSE frame, verbatim).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
TRANSCRIPT = HERE / "transcript.md"
EVENTS = HERE / "events.jsonl"
APP = HERE / "workspaces" / "shop" / "shop" / "app.py"
BUG = 'i["unit_price"] * i["quantity"]'
BASE = "http://127.0.0.1:8001"


#: ``(occurrence of this tool, tool name)`` -> decision + the note the model
#: reads back. Occurrence, not turn number: the demo cannot know a proposal id
#: in advance, and turn numbers move when the model takes a different route.
#: The third ``write_file`` is the fix — rejecting it (with a reason) is how the
#: note travels to the model.
SCRIPT: dict[tuple[int, str], tuple[str, str]] = {
    (3, "write_file"): (
        "reject",
        "Not yet. Show me the diff before you overwrite app.py — I want to see which line changes.",
    ),
}


class User:
    """The person in the loop: sends messages, decides proposals, writes it down."""

    def __init__(self, base: str, transcript: Path) -> None:
        self.base = base.rstrip("/")
        self.transcript = transcript
        self.turn = 0
        self._in_thinking = False
        self.seen_tools: dict[str, int] = {}
        self.frames: list[tuple[str, dict[str, Any]]] = []
        transcript.write_text("")

    # -- transport ---------------------------------------------------------
    def get(self, path: str) -> dict[str, Any]:
        with httpx.Client(base_url=self.base, timeout=30) as client:
            response = client.get(path)
            response.raise_for_status()
            return response.json()

    def write(self, text: str) -> None:
        with self.transcript.open("a") as handle:
            handle.write(text)

    # -- one turn ----------------------------------------------------------
    def say(self, message: str, session: str | None, *, starts_the_ui: bool = False) -> str:
        self.turn += 1
        self.write(f"\n## turn {self.turn} — the user\n\n> {message.strip()}\n\n")
        body: dict[str, Any] = {"message": message, "approval": "mutating"}
        if session:
            body["session_id"] = session

        current = session
        with (
            httpx.Client(base_url=self.base, timeout=None) as client,
            client.stream("POST", "/chat", json=body) as response,
        ):
            response.raise_for_status()
            current = response.headers.get("x-session-id", current)
            event = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event = line[len("event: ") :].strip()
                elif line.startswith("data: "):
                    payload = json.loads(line[len("data: ") :])
                    self._handle(client, current or "", event, payload)
        self.write("\n")
        return current or ""

    # -- frame handling ----------------------------------------------------
    def _handle(self, client: httpx.Client, session: str, event: str, data: dict[str, Any]) -> None:
        self.frames.append((event, data))
        if event == "thinking":
            text = data.get("text") or ""
            if not self._in_thinking:
                self.write("<sub>thinking</sub> ")
                self._in_thinking = True
            self.write(text.replace("\n", " "))
            return
        if self._in_thinking and event != "thinking":
            self.write("\n\n")
            self._in_thinking = False

        if event == "token":
            self.write(data.get("text") or "")
        elif event == "tool_proposal":
            self.decide(client, session, data)
        elif event == "tool_result":
            status = data.get("status")
            tool = data.get("tool")
            result = data.get("result")
            if isinstance(result, dict | list):
                shown = json.dumps(result)[:180]
            else:
                shown = str(result or data.get("error") or "")[:180].replace("\n", " ⏎ ")
            self.write(f"\n\n**← {tool} · {status} · {data.get('duration_ms', 0):.0f} ms** `{shown}`\n\n")
        elif event == "done":
            payload = data.get("response") or {}
            reason = payload.get("finish_reason", "stop")
            iterations = payload.get("iterations", 0)
            calls = len(payload.get("tool_calls") or [])
            self.write(
                f"\n\n<sub>finish_reason: `{reason}` · iterations {iterations} · "
                f"{calls} tool call(s)</sub>\n\n"
            )
            content = (payload.get("content") or "").strip()
            if content:
                self.write(f"**the model:** {content}\n")
        elif event == "error":
            self.write(f"\n\n**error:** {data.get('message') or data}\n")

    def decide(self, client: httpx.Client, session: str, data: dict[str, Any]) -> None:
        tool = str(data.get("tool"))
        self.seen_tools[tool] = self.seen_tools.get(tool, 0) + 1
        decision, note = SCRIPT.get((self.seen_tools[tool], tool), ("approve", "Yes, that is correct."))

        arguments = data.get("arguments") or {}
        detail = arguments.get("command") or arguments.get("path") or ""
        self.write(
            f"\n\n**→ {tool}** `{json.dumps(arguments)[:150]}`\n\n"
        )
        verb = "approves" if decision == "approve" else "rejects"
        self.write(
            f"> the user: **{verb}** `{tool}` — risk {data.get('risk')}, "
            f"{float(data.get('timeout_seconds', 0)):.1f}s to decide. note: *{note}*\n\n"
        )
        response = client.post(
            f"/sessions/{session}/approve",
            json={"proposal_id": data["id"], "decision": decision, "note": note},
        )
        echoed = response.json() if response.status_code == 200 else {"status": response.text}
        self.write(
            f"<sub>approve → ok={response.status_code == 200} "
            f"decision={decision} resolved={echoed.get('resolved')}</sub>\n"
        )
        if not detail:
            return


PROMPT = """Build me a small shop API, then make its interface feel like it is from 2050.

For the API: a cart with add/total/snapshot, JSON over HTTP, and pytest tests that
actually prove the total is right. Keep it to the standard library — I do not want
to install anything.

Then show me it works: run the tests, and if something fails, read the failure and
fix it rather than guessing. Ask before you overwrite anything. If I reject a change,
do not silently retry it — stop and tell me what you would do instead.

For the interface: I want it to feel like 2050 — spatial, calm, alive, nothing that
looks like a 2015 dashboard. But it has to stay honest: every number on screen must
come from the cart, and it has to work with a keyboard and with motion turned off."""

UI_PROMPT = """Good. Now the interface: make it feel like 2050 — spatial, calm, alive.
Keep every number honest and make it work with a keyboard and with reduced motion."""

BUG_REPORT = """I opened http://127.0.0.1:8100/ and got a 404 — the page never arrives.
The total works, the page does not. Find out why and fix it, with a test that would
have caught it."""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--out", default=str(TRANSCRIPT))
    parser.add_argument("--only", choices=["turn1", "turn2", "turn3", "both", "all"], default="all")
    parser.add_argument("--ui-prompt", default=UI_PROMPT)
    args = parser.parse_args()

    # Turn 1 only makes sense on the broken fixture: put the bug back.
    if args.only in {"turn1", "both", "all"}:
        text = APP.read_text()
        if BUG not in text:
            APP.write_text(
                text.replace('i["price"] * i["quantity"]', BUG, 1)
            )

    user = User(args.base, Path(args.out))
    health = user.get("/health")
    summary = {key: health[key] for key in ("status", "version", "ollama_reachable", "model", "model_running") if key in health}
    user.write(f"# The conversation\n\n```\n{json.dumps(summary)}\n```\n")

    session = None
    if args.only in {"turn1", "both", "all"}:
        session = user.say(PROMPT, None)
        user.write(f"\n<sub>session: `{session}`</sub>\n\n")
    if args.only in {"turn2", "both", "all"}:
        session = user.say(args.ui_prompt, session)
        user.write(f"\n<sub>session: `{session}`</sub>\n\n")
    if args.only in {"turn3", "all"}:
        session = user.say(BUG_REPORT, session)
        user.write(f"\n<sub>session: `{session}`</sub>\n\n")

    with EVENTS.open("w") as handle:
        for event, data in user.frames:
            handle.write(json.dumps({"event": event, "data": data}) + "\n")
    print(f"\n{len(user.frames)} frames written to events.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
