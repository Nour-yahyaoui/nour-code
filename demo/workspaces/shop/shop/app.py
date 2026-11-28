"""A cart service small enough to read in one sitting."""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Cart:
    def __init__(self) -> None:
        self._items: list[dict] = []

    def add(self, sku: str, unit_price: str, quantity: int = 1) -> dict:
        if quantity < 1:
            raise ValueError("quantity must be positive")
        item = {"sku": sku, "price": Decimal(unit_price), "quantity": quantity}
        self._items.append(item)
        return item

    def total(self) -> Decimal:
        return sum((i["price"] * i["quantity"] for i in self._items), Decimal("0"))

    def snapshot(self) -> list[dict]:
        return [
            {"sku": i["sku"], "price": str(i["price"]), "quantity": i["quantity"]}
            for i in self._items
        ]


class Handler(BaseHTTPRequestHandler):
    cart = Cart()

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/index.html"}:
            body = pathlib.Path(__file__).parent.parent.joinpath("ui-2050.html").read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/cart":
            self._send(200, {"items": self.cart.snapshot(), "total": str(self.cart.total())})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/cart":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            item = self.cart.add(payload["sku"], payload["price"], int(payload.get("quantity", 1)))
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})
        self._send(201, {"item": {"sku": item["sku"], "quantity": item["quantity"]}})

    def log_message(self, *args: object) -> None:
        pass


def serve(port: int = 8099) -> None:
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
