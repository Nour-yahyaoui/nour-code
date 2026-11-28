"""The interface, tested the way the rest of the service is.

These run without a browser: they read the file the server ships and assert the
promises the page makes (honest numbers, a keyboard path, motion opt-out).
"""

from __future__ import annotations

import inspect
from pathlib import Path

PAGE = Path(__file__).parents[1] / "ui-2050.html"


def test_the_page_is_self_contained():
    text = PAGE.read_text()
    assert "<script src" not in text, "no CDN: the page has to work offline"
    assert "fonts.googleapis" not in text, "no web fonts either"


def test_the_page_has_an_accessible_cart():
    text = PAGE.read_text()
    assert 'role="status"' in text, "the add result must be announced"
    assert 'aria-live="polite"' in text, "the total must be announced"
    assert "<button" in text, "adding to the cart needs a real button"
    assert 'for="sku"' in text and 'id="sku"' in text, "every field needs a label"


def test_motion_is_optional():
    text = PAGE.read_text()
    assert "prefers-reduced-motion" in text


def test_the_total_comes_from_the_server():
    text = PAGE.read_text()
    assert "payload.total" in text or "response.total" in text, "the total is read back, not summed in the browser"


def test_the_server_serves_the_page():
    import shop.app as app

    source = inspect.getsource(app.Handler.do_GET)
    assert "ui-2050.html" in source, "a page nobody can reach is not an interface"
