"""The page, fetched the way a browser fetches it.

`test_ui.py` reads the file off disk, so it happily passed while `GET /` was
broken: it proved the page existed, not that anyone could reach it. This test
starts the real server on an ephemeral port and asks it for `/`.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer

from shop.app import Handler


def test_the_server_answers_get_slash_with_the_page():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
            html = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert "<title>cart / 2050</title>" in html
