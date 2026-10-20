"""The console layout: collapsible panes, persisted state, small screens."""

from __future__ import annotations

import re

from nour_code.main import _console_html

HTML = _console_html()


def test_sidebar_can_collapse_and_expand() -> None:
    assert 'id="collapseWs"' in HTML
    assert "aside.workspace.collapsed" in HTML
    # collapsing is a rail, not a disappearance
    assert "width: 46px" in HTML
    assert "main.split.ws-collapsed .gutter { display: none; }" in HTML


def test_layout_state_is_persisted() -> None:
    for key in ("nour.ui.wsCollapsed", "nour.ui.wsWidth", "nour.ui.drawerHeight"):
        assert key in HTML, key


def test_keyboard_shortcuts_exist() -> None:
    assert "key === 'b'" in HTML
    assert "key === 'j'" in HTML
    assert "ctrlKey || event.metaKey" in HTML


def test_narrow_screens_get_a_drawer_instead_of_a_hidden_sidebar() -> None:
    assert "@media (max-width: 850px)" in HTML
    assert "aside.workspace { display: none; }" not in HTML
    assert "position: fixed" in HTML
    assert "body.ws-open aside.workspace" in HTML
    assert 'id="wsBtn"' in HTML


def test_icon_is_embedded_so_the_page_has_no_extra_requests() -> None:
    match = re.search(r'<link rel="icon" href="(data:image/svg\+xml,[^"]+)"', HTML)
    assert match
    href = match.group(1)
    assert "%23d9ff63" in href and "%236c8cff" in href
