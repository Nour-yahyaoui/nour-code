#!/usr/bin/env python3
"""A stand-in for a local Ollama model, scripted to act out one scenario.

Why this exists
---------------
The demo has to show nour-code's *loop* — build, ask permission, test, read the
failure, fix, ask again — without a 4 GB model on a laptop. This server speaks
enough of Ollama's OpenAI-compatible API (``/v1/chat/completions`` streaming and
not, ``/v1/models``, and the native ``/api/tags``, ``/api/ps`` and ``/api/version``
probes) for nour-code to treat it as Ollama.

The important rule: **it decides from the tail of the conversation, not from
word-matching the whole history.** An append-only message list means the first
failure is still in the transcript forever; a model that keeps re-reading it
repeats itself, and the agent's loop detector then (correctly) fires. So each
call looks only at the tool results that came back *after the last assistant
message*, plus the names of the calls that produced them.

The scenario is ``shop``: a stdlib cart service, the 2050 interface, and a real
bug in how the page is served — three acts, each driven by what the tools
actually returned.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import re
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEBUG = [False]
STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {"turns": 0, "log": []}


def log(message: str) -> None:
    with STATE_LOCK:
        STATE["log"].append(message)
    print(f"[fake-model] {message}", flush=True)


class Turn:
    """One assistant step: reasoning, an optional remark, then tool calls."""

    def __init__(self, thinking: str, calls: list[tuple[str, dict[str, object]]], say: str = "") -> None:
        self.thinking = thinking
        self.calls = calls
        self.say = say


class ShopScenario:
    """Three acts, selected by the tool results of the step just finished.

    The files are read from ``demo/workspaces/shop`` at startup: the broken
    version, the fixed one and the page. The scenario never invents content, so
    what the agent writes is what the tests then run.
    """

    name = "shop"

    def __init__(self, fixture: pathlib.Path) -> None:
        self.broken = (fixture / "shop" / "app.py").read_text()
        self.fixed = self.broken.replace('i["unit_price"]', 'i["price"]')
        if self.fixed == self.broken:
            raise SystemExit('fixture bug is missing: expected i["unit_price"] in app.py')
        # The page is written to the workspace root, one level above the
        # package, so a handler that looks next to app.py cannot find it.
        self.served = self.fixed.replace(
            '.parent.parent.joinpath("ui-2050.html")',
            '.with_name("ui-2050.html")',
        )
        if self.served == self.fixed:
            raise SystemExit("fixture bug is missing: expected the page path in app.py")
        self.test_app = (fixture / "shop" / "test_app.py").read_text()
        self.test_ui = (fixture / "shop" / "test_ui.py").read_text()
        self.test_serve = (fixture / "shop" / "test_serve.py").read_text()
        self.page = (fixture / "ui-2050.html").read_text()
        self.denials = 0
        self.stage = "new"
        self.reported = False

    # -- reading the conversation ------------------------------------------
    @staticmethod
    def _last_user(messages: list[dict[str, object]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def next_turn(self, messages: list[dict[str, object]]) -> Turn:
        """Decide the next step from what the *tools* just returned.

        The history is append-only, so "what just happened" is the tail: the
        tool results after the last assistant message. The scenario tracks its
        own stage, because a real model does too — and a step it already
        finished must not be repeated.
        """
        tail = self._tail(messages)
        text = "\n".join(str(m.get("content") or "") for m in tail["tools"])
        user = self._last_user(messages)
        exit_code = self._exit_code(text)
        # The buggy file also mentions i["price"] (in snapshot()), so the fix is
        # the write that removes `unit_price` from app.py entirely.
        wrote_fix = any(
            "shop/app.py" in a and "unit_price" not in a for a in tail["args"]
        )

        # --- rejected ----------------------------------------------------
        if "DENIED" in text:
            self.denials += 1
            note = text.split("DENIED", 1)[1].strip().split("\n")[0][:200]
            log(f"user replied to a denial: {note}")
            if self.denials >= 2:
                return Turn(
                    thinking="Challenged twice: stop and ask instead of proposing again.",
                    calls=[],
                    say="I stopped: two proposals were rejected. Tell me which part you want changed.",
                )
            self.stage = "challenged"
            return Turn(
                thinking=(
                    "The write was rejected, so nothing ran — the file on disk is still the "
                    "buggy one. Read it, show the exact line, then propose the change again."
                ),
                calls=[("read_file", {"path": "shop/app.py", "max_chars": 4000})],
            )

        # --- the shell could not find pytest -----------------------------
        if "not found" in text and "pytest" in text:
            log("pytest is not on PATH — saying so, not guessing")
            self.stage = "blocked"
            return Turn(
                thinking=(
                    "Exit 127: `pytest: not found`. No test ran, so there is no evidence about "
                    "the code either way. Editing now would be guessing."
                ),
                calls=[],
                say="The tests did not run: the shell could not find `pytest` (exit 127).",
            )

        # --- the 2050 interface ------------------------------------------
        if "2050" in user and self.stage in {"fixed", "ui_files"}:
            if self.stage == "fixed":
                self.stage = "ui_files"
                log("ui: writing the page")
                return Turn(
                    thinking=(
                        "The service is green and has no interface. One page, in the service's "
                        "own palette: a field instead of a grid, a total that is the loudest "
                        "object on screen, keyboard-first controls. No build step, no CDN — it "
                        "runs from the same stdlib server, offline."
                    ),
                    calls=[
                        ("write_file", {"path": "ui-2050.html", "content": self.page}),
                        ("write_file", {"path": "shop/test_ui.py", "content": self.test_ui}),
                        # the page is new; the handler was written before it
                        ("write_file", {"path": "shop/app.py", "content": self.served}),
                    ],
                )
            self.stage = "ui"
            log("ui: running both suites")
            return Turn(
                thinking="The page is on disk; prove it before claiming it.",
                calls=[
                    (
                        "run_shell",
                        {"command": "pytest shop/test_ui.py shop/test_app.py -v", "timeout_seconds": 120},
                    )
                ],
            )

        # --- act 3: the user says `/` is down -----------------------------
        if "404" in user and self.stage == "done":
            self.stage = "serve_probe"
            log("user reports 404 — writing a test that fetches the page")
            return Turn(
                thinking=(
                    "The page is on disk and the server cannot find it. `test_ui.py` read the "
                    "file directly, so it never asked whether a browser could fetch it — that "
                    "gap is the actual bug. Write a test that starts the server and asks for "
                    "`/`, watch it fail, then fix the path."
                ),
                calls=[("write_file", {"path": "shop/test_serve.py", "content": self.test_serve})],
            )

        if self.stage == "serve_probe" and tail["wrote_app"]:
            self.stage = "serve_red"
            log("probe written — running it")
            return Turn(
                thinking="The probe exists; run it. A failing test is evidence, not an opinion.",
                calls=[("run_shell", {"command": "pytest shop/test_serve.py -v", "timeout_seconds": 120})],
            )

        if self.stage == "serve_red" and tail["ran_tests"]:
            if exit_code == 0:
                log("probe unexpectedly green")
                self.stage = "done"
                return Turn(
                    thinking="The probe already passes, so `/` was reachable after all.",
                    calls=[],
                    say="`GET /` already works, so I could not reproduce the 404. Nothing to fix.",
                )
            self.stage = "serve_fix"
            log("probe failed as reported — reading the handler")
            return Turn(
                thinking=(
                    "`FileNotFoundError` on a path next to `app.py`: the page is at the "
                    "workspace root, one level above the package. Read the handler before "
                    "touching it."
                ),
                calls=[("read_file", {"path": "shop/app.py", "max_chars": 4000})],
            )

        if self.stage == "serve_fix" and tail["read_app"]:
            self.stage = "serve_fixed"
            log("handler read — patching the path")
            return Turn(
                thinking="One path, one line: point the handler at the file that exists.",
                calls=[("write_file", {"path": "shop/app.py", "content": self.fixed})],
            )

        if self.stage == "serve_fixed" and tail["wrote_app"]:
            self.stage = "serve_verify"
            log("patched — proving it with all three suites")
            return Turn(
                thinking="A fix is a claim; the probe is how the claim gets checked.",
                calls=[
                    (
                        "run_shell",
                        {
                            "command": "pytest shop/test_serve.py shop/test_app.py shop/test_ui.py -v",
                            "timeout_seconds": 120,
                        },
                    )
                ],
            )

        if self.stage == "serve_verify" and tail["ran_tests"]:
            self.stage = "done"
            if exit_code == 0:
                log("everything green")
                total = self._passed(text)
                return Turn(
                    thinking="Everything green: the cart, the page and the route to it.",
                    calls=[],
                    say=(
                        "That was a real bug: `test_ui.py` only read the file off disk, so it "
                        "passed while `GET /` raised `FileNotFoundError` — the handler looked "
                        "next to `app.py` and the page is one level up. It now serves the page "
                        "from the workspace root, and `shop/test_serve.py` starts the server "
                        "and fetches `/` for real. "
                        f"{total} tests pass." if total
                        else "All suites pass."
                    ),
                )
            return Turn(
                thinking="Still red after the fix; stop and report rather than guess again.",
                calls=[],
                say="The probe still fails after the path change. I stopped instead of guessing.",
            )

        # --- a file was just written -------------------------------------
        if tail["wrote_app"]:
            if wrote_fix:
                log("fix written — proving it")
                self.stage = "fixed_attempt"
                return Turn(
                    thinking="The fix is on disk. A claim without a test run is a guess.",
                    calls=[("run_shell", {"command": "pytest shop/test_app.py -v", "timeout_seconds": 120})],
                )
            log("code written — running the suite")
            if self.stage == "new":
                self.stage = "scaffold"
            return Turn(
                thinking="Everything is on disk; run the tests before saying anything about them.",
                calls=[("run_shell", {"command": "pytest shop/test_app.py -v", "timeout_seconds": 120})],
            )

        # --- a read came back --------------------------------------------
        if tail["read_app"] and self.stage == "challenged":
            log("file read — re-proposing the one line")
            return Turn(
                thinking="The file on disk is unchanged, so the one-line fix is still the change.",
                calls=[("write_file", {"path": "shop/app.py", "content": self.fixed})],
            )

        # --- the suite just ran ------------------------------------------
        if tail["ran_tests"]:
            if exit_code == 0:
                log("suite green")
                if self.stage == "ui":
                    self.stage = "done"
                    served = self._passed(text)
                    return Turn(
                        thinking="The interface tests pass as well.",
                        calls=[],
                        say=(
                            "`ui-2050.html` is served at `/`. The interface tests pass, and so "
                            f"do the cart tests — {served} in total."
                            if served
                            else "`ui-2050.html` is served at `/`; the interface tests pass."
                        ),
                    )
                self.stage = "fixed"
                return Turn(
                    thinking="The suite passes. Summarise, and say what is next.",
                    calls=[],
                    say=(
                        "Fixed. `Cart.total` summed a field that does not exist; it reads "
                        '`i["price"]` now. Three tests pass. Say the word and I will build '
                        "the interface."
                    ),
                )

            log(f"the suite fails (exit {exit_code})")
            if self.stage == "fixed_attempt":
                self.stage = "stuck"
                return Turn(
                    thinking=(
                        "The fixed file is on disk and the same test still fails, so my "
                        "diagnosis was wrong. Repeating the edit would be a loop; stop and hand "
                        "the evidence to the user."
                    ),
                    calls=[],
                    say=(
                        "I wrote the fix and the same test still fails, so my diagnosis was "
                        "wrong. Here is the traceback — tell me how you want to proceed rather "
                        "than watch me guess."
                    ),
                )
            if self.stage == "scaffold":
                self.stage = "diagnosed"
                log("first failure — reading the file and proposing the fix")
                return Turn(
                    thinking=(
                        'One of three tests fails: `total()` reads `i["unit_price"]` while '
                        "`add()` stores `price`. The failure names the line, so this is a "
                        "rename, not a redesign."
                    ),
                    calls=[
                        ("read_file", {"path": "shop/app.py", "max_chars": 4000}),
                        ("write_file", {"path": "shop/app.py", "content": self.fixed}),
                    ],
                )

        # --- nothing has happened yet ------------------------------------
        if not tail["tools"] and self.stage == "new":
            self.stage = "scaffold"
            return Turn(
                thinking=(
                    "A cart service with no framework pinned. I will use the standard library "
                    "only — http.server and Decimal — so it runs anywhere without a download. "
                    "Code and tests first, then run them for real."
                ),
                calls=[
                    ("write_file", {"path": "shop/app.py", "content": self.broken}),
                    ("write_file", {"path": "shop/test_app.py", "content": self.test_app}),
                ],
            )

        return Turn(thinking="No scripted next step.", calls=[], say="Waiting for the next instruction.")

    # -- reading the tail --------------------------------------------------
    @staticmethod
    def _tail(messages: list[dict[str, object]]) -> dict[str, object]:
        """Tool results after the last assistant message, and what it called."""
        tools: list[dict[str, object]] = []
        calls: list[str] = []
        args: list[str] = []
        for message in reversed(messages):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    calls.append(str(function.get("name") or ""))
                    args.append(str(function.get("arguments") or ""))
                break
            if message.get("role") == "tool":
                tools.append(message)
        return {
            "tools": list(reversed(tools)),
            "calls": calls,
            "args": args,
            "wrote_app": "write_file" in calls,
            "read_app": "read_file" in calls,
            "ran_tests": "run_shell" in calls,
        }

    @staticmethod
    def _passed(text: str) -> int | None:
        """How many tests pytest says passed — read from its own summary line.

        Hard-coding "all ten tests pass" in the script is how a demo ends up
        claiming a number nobody checked; the count comes from the output.
        """
        match = re.search(r"(\d+) passed", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _exit_code(text: str) -> int | None:
        """The exit code of the command in a tool result.

        ``run_shell`` returns its payload as JSON — prettified on success, and
        after an ``ERROR:`` line on failure. Both carry ``"exit_code"``, so look
        for the key anywhere rather than assuming a shape.
        """
        match = re.search(r'"exit_code"\s*:\s*(-?\d+)', text)
        if match:
            return int(match.group(1))
        marker = "exited with "
        if marker in text:
            try:
                return int(text.split(marker, 1)[1].split()[0])
            except (IndexError, ValueError):
                return None
        return None


SCENARIOS = {"shop": ShopScenario}


# ---------------------------------------------------------------------------
# the HTTP surface nour-code talks to
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    scenario: ShopScenario | None = None

    def log_message(self, *args: object) -> None:
        pass

    # -- helpers -----------------------------------------------------------
    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: Iterable[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/version":
            return self._json({"version": "0.0.0-fake-model"})
        if path in {"/api/tags", "/v1/models"}:
            return self._json(
                {
                    "models": [
                        {
                            "name": "qwen3:8b",
                            "model": "qwen3:8b",
                            "modified_at": "2026-01-01T00:00:00Z",
                            "size": 5_000_000_000,
                        }
                    ]
                }
            )
        if path == "/api/ps":
            return self._json(
                {
                    "models": [
                        {
                            "name": "qwen3:8b",
                            "model": "qwen3:8b",
                            "size": 5_000_000_000,
                            "digest": "fake",
                            "expires_at": "2026-01-01T00:05:00Z",
                        }
                    ]
                }
            )
        if path == "/state":
            return self._json(
                {"turns": STATE["turns"], "stage": self.scenario.stage if self.scenario else None,
                 "log": STATE["log"][-40:]}
            )
        return self._json({"error": "not found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/v1/chat/completions", "/api/chat"}:
            return self._json({"error": "not found", "path": path}, 404)
        payload = self._read_body()

        # A tool-less client asking for tools would get prose; that is why the
        # agent always sends its tools with every request.
        assert self.scenario is not None, "no scenario configured"
        turn = self.scenario.next_turn(payload.get("messages") or [])
        with STATE_LOCK:
            STATE["turns"] = int(STATE["turns"]) + 1
            turns = STATE["turns"]
        log(f"turn {turns}: {len(turn.calls)} tool call(s)")

        if DEBUG[0]:
            print("[debug] stage:", self.scenario.stage, flush=True)

        if payload.get("stream"):
            return self._sse(self._chunks(turn, payload, turns))
        return self._json(self._completion(turn, payload, turns))

    # -- payloads ----------------------------------------------------------
    @staticmethod
    def _message(turn: Turn, turns: int) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": turn.say if not turn.calls else None,
            "reasoning": turn.thinking,
        }
        if turn.calls:
            message["tool_calls"] = [
                {
                    "id": f"call_demo_{turns}_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
                for index, (name, arguments) in enumerate(turn.calls)
            ]
        return message

    def _completion(self, turn: Turn, payload: dict[str, Any], turns: int) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-demo-{turns}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or "qwen3:8b",
            "choices": [
                {
                    "index": 0,
                    "message": self._message(turn, turns),
                    "finish_reason": "tool_calls" if turn.calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _chunks(self, turn: Turn, payload: dict[str, Any], turns: int) -> Iterable[dict[str, Any]]:
        base = {
            "id": f"chatcmpl-demo-{turns}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload.get("model") or "qwen3:8b",
        }
        # reasoning first, so the console shows thinking the way Qwen3 does
        if turn.thinking:
            yield {
                **base,
                "choices": [{"index": 0, "delta": {"reasoning": turn.thinking}, "finish_reason": None}],
            }
        if turn.calls:
            for index, (name, arguments) in enumerate(turn.calls):
                yield {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": f"call_demo_{turns}_{index}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(arguments)},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
            return
        if turn.say:
            # a few words at a time, the way a real stream arrives
            words = turn.say.split(" ")
            for index, word in enumerate(words):
                text = word if index == 0 else " " + word
                yield {**base, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def main() -> int:
    parser = argparse.ArgumentParser(description="A scripted stand-in for Ollama.")
    parser.add_argument("--port", type=int, default=11934)
    parser.add_argument("--scenario", default="shop", choices=sorted(SCENARIOS))
    parser.add_argument("--fixture", default="demo/workspaces/shop")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true", help="Print what each request carried.")
    args = parser.parse_args()

    DEBUG[0] = args.debug
    scenario_class = SCENARIOS[args.scenario]
    Handler.scenario = scenario_class(pathlib.Path(args.fixture).resolve())

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[fake-model] fake model on http://{args.host}:{args.port} (scenario={args.scenario})", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
