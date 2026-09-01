"""Navigation IA — the dashboard uses a fixed left rail that separates the
Factory from the individual channels. The rail is a pure scope switcher
(Factory · each channel · + New); section tabs render in the topbar and
depend on the active scope. There is no "More" dropdown."""
from __future__ import annotations

import re

import pytest

from hypogeum.db import initialize_schema


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "nav.db"
    initialize_schema(db)
    # a channel so the rail + channel-scope tabs have something to render
    from hypogeum.channel_config import create_channel_profile
    create_channel_profile(channel_id="acme", name="Acme Docs", db_path=db)
    from hypogeum import db as _db
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", db)
    import run_dashboard as server
    server.app.config["TESTING"] = True
    monkeypatch.setattr(server, "_db_path", lambda: db)
    return server.app.test_client()


def _html(client, path="/dashboard") -> str:
    r = client.get(path)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_rail_has_factory_every_channel_and_new(client):
    html = _html(client)
    assert 'class="rail"' in html
    assert re.search(r'rail-item[^>]*>\s*(<svg[^>]*>.*?</svg>)?\s*<span>Factory</span>', html, re.DOTALL)
    assert ">Acme Docs<" in html
    assert "New channel" in html
    assert "nav-ops-details" not in html and "nav-ops-menu" not in html  # no More menu


def test_factory_scope_shows_factory_section_tabs(client):
    html = _html(client, "/dashboard?scope=factory")
    for tab in ("Overview", "Channels", "Production", "Episodes", "Learning",
                "Providers", "Infrastructure", "Governance", "Storage", "Advanced"):
        assert (">" + tab + "<") in html, f"factory topbar missing {tab!r}"


def test_channel_scope_shows_channel_section_tabs(client):
    html = _html(client, "/channel/acme?scope=acme")
    for tab in ("Overview", "Production", "Episodes", "Discovery",
                "Analytics", "Publishing", "Settings"):
        assert (">" + tab + "<") in html, f"channel topbar missing {tab!r}"
    # breadcrumb reflects the channel
    assert "Acme Docs" in html


def test_advanced_page_collects_the_old_more_links(client):
    html = _html(client, "/advanced")
    for href in ("/prompts", "/compare", "/providers/video-test", "/command",
                 "/published/import", "/analytics/import", "/signals"):
        assert f'href="{href}"' in html, f"/advanced missing {href}"


def test_every_rail_and_topbar_href_resolves(client):
    html = _html(client)
    hrefs = sorted(set(re.findall(r'href="(/[^"#?]+)', html)))
    broken = []
    for href in hrefs:
        code = client.get(href).status_code
        if code >= 500 or code == 404:
            broken.append((href, code))
    assert not broken, f"dead nav links: {broken}"


def test_channel_profile_legacy_route_redirects(client):
    r = client.get("/channel-profile")
    assert r.status_code in (301, 302)


def test_skin_toggle_present_and_both_skins_defined(client):
    html = _html(client)
    assert 'id="skin-toggle"' in html
    assert 'factoryos-skin' in html
    assert '[data-skin="presentation"]' in html
