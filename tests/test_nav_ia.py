"""Navigation IA — the nav was restructured to the six-core-section shape in
docs/dashboard-surface-area.md. These tests lock in: the primary nav is
exactly the six sections, everything else is in the collapsed "More" menu,
every nav href resolves to a real route, and both skins render."""
from __future__ import annotations

import re

import pytest

from hypogeum.db import initialize_schema


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "nav.db"
    initialize_schema(db)
    from hypogeum import db as _db
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", db)
    import run_dashboard as server
    server.app.config["TESTING"] = True
    monkeypatch.setattr(server, "_db_path", lambda: db)
    return server.app.test_client()


CORE = {"/dashboard", "/channels", "/create", "/attention", "/providers", "/channel-profile"}


def _nav_html(client) -> str:
    r = client.get("/dashboard")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_primary_nav_is_exactly_the_six_core_sections(client):
    html = _nav_html(client)
    # primary <a> tags carry class="nav-link ..." (href before or after class)
    tags = re.findall(r'<a\b[^>]*class="nav-link[^"]*"[^>]*>', html)
    hrefs = set()
    for t in tags:
        m = re.search(r'href="([^"]+)"', t)
        if m:
            hrefs.add(m.group(1))
    assert hrefs == CORE, f"primary nav drifted from the six-core spec: {hrefs ^ CORE}"


def test_more_menu_holds_the_module_and_advanced_destinations(client):
    html = _nav_html(client)
    m = re.search(r"<details[^>]*class=\"nav-ops-details\"[^>]*>(.*?)</details>", html, re.DOTALL)
    assert m, "the 'More' menu must be a <details>"
    inner = m.group(1)
    for href in ("/analytics", "/intelligence/learning", "/infrastructure",
                 "/decisions", "/command", "/prompts", "/compare"):
        assert href in inner, f"{href} missing from the More menu"


def test_production_and_review_detail_reachable_via_section_subnav(client):
    # these are NOT in the top nav anymore — they live in the section tab rows
    prod = client.get("/create").get_data(as_text=True)
    for href in ("/live", "/runs", "/failures", "/discover"):
        assert href in prod, f"{href} not reachable from the Production sub-nav"
    rev = client.get("/attention").get_data(as_text=True)
    for href in ("/approvals", "/episodes", "/preflight"):
        assert href in rev, f"{href} not reachable from the Review sub-nav"


def test_every_nav_href_resolves(client):
    html = _nav_html(client)
    hrefs = sorted(set(re.findall(r'href="(/[^"#?]+)"', html)))
    broken = []
    for href in hrefs:
        code = client.get(href).status_code
        if code >= 500 or code == 404:
            broken.append((href, code))
    assert not broken, f"dead nav links: {broken}"


def test_subnav_renders_for_section_pages(client):
    for path, tab in [("/create", "Queue"), ("/live", "Live"), ("/attention", "Attention"),
                      ("/providers", "AI providers"), ("/channel-profile", "Channel profile")]:
        html = client.get(path).get_data(as_text=True)
        assert 'class="sub-nav"' in html, f"{path} has no section sub-nav"
        assert tab in html, f"{path} sub-nav missing the {tab!r} tab"


def test_skin_toggle_present_and_both_skins_are_defined(client):
    html = _nav_html(client)
    assert 'id="skin-toggle"' in html
    assert 'factoryos-skin' in html  # localStorage key
    base = client.get("/dashboard").get_data(as_text=True)
    assert '[data-skin="presentation"]' in base  # overlay tokens shipped in _base_style
