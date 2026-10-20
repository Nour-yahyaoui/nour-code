"""Mock Ollama with /v1/chat/completions, to time the SSE path end to end."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 11999
LOAD_SECONDS = 12.0  # pretend the 5.9 GB model has to be loaded first
TOKENS = ["Hello", " from", " qwen", "!", " Here", " is", " your", " code."]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/version":
            self._json({"version": "0.12.0"})
        elif self.path == "/api/tags":
            self._json({"models": [{"name": "qwen3:8b"}]})
        elif self.path == "/api/ps":
            self._json({"models": [{"name": "qwen3:8b", "context_length": 4096, "expires_at": "2026-10-20T19:00:00Z"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/chat/completions":
            self._chat(payload)
        elif self.path == "/api/generate":
            self._json({"model": payload.get("model"), "done": True})
        else:
            self._json({"error": "not found"}, 404)

    def _chat(self, payload: dict) -> None:
        print(f"[mock] /v1/chat/completions  stream={payload.get('stream')}  tools={len(payload.get('tools') or [])}", flush=True)
        time.sleep(LOAD_SECONDS)  # model load: nothing arrives during this window
        if not payload.get("stream"):
            self._json(
                {
                    "id": "chatcmpl-1",
                    "model": payload.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": " ".join(TOKENS)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                }
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(data: str) -> None:
            chunk = data.encode()
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()

        for token in TOKENS:
            frame = {
                "id": "chatcmpl-1",
                "model": payload.get("model"),
                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
            }
            write(f"data: {json.dumps(frame)}\n\n")
            time.sleep(0.05)

        final = {
            "id": "chatcmpl-1",
            "model": payload.get("model"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        }
        write(f"data: {json.dumps(final)}\n\n")
        write("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock ollama (chat) on http://127.0.0.1:{PORT}", flush=True)
    threading.Event().wait()
