"""A tiny stand-in Ollama daemon for developing the landing page's live panel.

    python landing/scripts/mock-ollama.py        # serves on 127.0.0.1:11999
    NOUR_CODE_OLLAMA_BASE_URL=http://127.0.0.1:11999 python -m nour_code --port 8000

It implements only what /health and /ollama/{warmup,unload} touch: version,
tags, ps and generate. Not part of the shipped server.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 11999
STATE: dict[str, object] = {"running": False, "expires_at": None}


def _expiry(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep the console quiet
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/api/version":
            self._send({"version": "0.12.0"})
        elif self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3:8b", "size": 5_900_000_000}]})
        elif self.path == "/api/ps":
            models = (
                [
                    {
                        "name": "qwen3:8b",
                        "size": 5_900_000_000,
                        "size_vram": 0,
                        "context_length": 4096,
                        "expires_at": STATE["expires_at"],
                    }
                ]
                if STATE["running"]
                else []
            )
            self._send({"models": models})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/api/generate":
            self._send({"error": "not found"}, 404)
            return
        keep = payload.get("keep_alive")
        if keep in (0, "0"):
            STATE["running"] = False
            STATE["expires_at"] = None
        else:
            minutes = 30
            if isinstance(keep, str) and keep.endswith("m") and keep[:-1].isdigit():
                minutes = int(keep[:-1])
            elif keep == -1:
                minutes = 60 * 24
            STATE["running"] = True
            STATE["expires_at"] = _expiry(minutes)
        self._send({"model": payload.get("model"), "done": True, "done_reason": "load"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock ollama on http://127.0.0.1:{PORT}", flush=True)
    threading.Event().wait()
