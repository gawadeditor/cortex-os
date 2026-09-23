"""
tests/test_discover_route.py — /discover page, /api/discover/run, /api/discover/state

Covers:
    GET  /discover              — idle, running, completed, failed states
    POST /api/discover/run      — returns running immediately; background-safe
    GET  /api/discover/state    — polling endpoint for JS auto-reload
    State persistence           — candidates survive reload and navigation
    Running state               — persists across page refresh
    Failed state                — error shown inline, old candidates preserved
    Double-run prevention       — second POST while running is rejected cleanly
    No alert()                  — errors shown inline, never via alert()
    type=button                 — button has explicit type to prevent form submission
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "discover_test.db"
    from hypogeum.db import initialize_schema
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    os.environ.pop("DISCOVERY_ENGINE", None)
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c
    server.app.config.pop("DB_PATH", None)


# ---------------------------------------------------------------------------
# Page load — idle state
# ---------------------------------------------------------------------------

class TestPageLoadIdle:

    def test_returns_200(self, client):
        assert client.get("/discover").status_code == 200

    def test_empty_state_shown_when_no_candidates(self, client):
        page = client.get("/discover").data.decode()
        assert "No candidates yet" in page

    def test_button_has_type_button(self, client):
        page = client.get("/discover").data.decode()
        assert 'type="button"' in page

    def test_no_alert_in_template(self, client):
        page = client.get("/discover").data.decode()
        assert "alert(" not in page

    def test_status_div_present(self, client):
        page = client.get("/discover").data.decode()
        assert 'id="discover-status"' in page

    def test_spinner_css_present(self, client):
        page = client.get("/discover").data.decode()
        assert "spinner" in page

    def test_inline_script_has_no_unterminated_js_string(self, client):
        """Regression: `icon()` used to render its <svg ...> opening tag
        across two lines (a raw newline before the closing '>' when a
        label was involved). discover.html inlines `{{ icon('warning') }}`
        straight into a single-quoted JS string inside <script> --
        _setStatus('error', '{{ icon('warning') }} ' + ...) -- so that
        embedded newline broke the string literal, which is a JS
        SyntaxError at parse time. That doesn't just break the one call:
        it silently aborts the whole inline <script> block, so every
        function declared after the break point (resetDiscovery(), at
        minimum) was never defined, on every single page load. Confirmed
        live via the browser console before the fix. The exact class of
        symptom this repo has hit before: a broken control silently does
        nothing instead of showing an error."""
        page = client.get("/discover").data.decode()
        script_start = page.index("<script>")
        script_end = page.index("</script>", script_start)
        script = page[script_start:script_end]
        for match in re.finditer(r"_setStatus\('error',\s*'([^']*)'", script):
            assert "\n" not in match.group(1), (
                "a raw newline inside a single-quoted JS string will throw "
                "'Invalid or unexpected token' and abort the whole script block"
            )


# ---------------------------------------------------------------------------
# POST /api/discover/run — async dispatch
# ---------------------------------------------------------------------------

class TestDiscoverRunEndpoint:

    def test_returns_200(self, client):
        r = client.post("/api/discover/run",
                        data=json.dumps({"channel_id": "hypogeum"}),
                        content_type="application/json")
        assert r.status_code == 200

    def test_returns_ok_true(self, client):
        body = json.loads(client.post(
            "/api/discover/run",
            data=json.dumps({"channel_id": "hypogeum"}),
            content_type="application/json",
        ).data)
        assert body["ok"] is True

    def test_returns_running_status_immediately(self, client):
        body = json.loads(client.post(
            "/api/discover/run",
            data=json.dumps({"channel_id": "hypogeum"}),
            content_type="application/json",
        ).data)
        assert body["status"] == "running"

    def test_uses_default_channel_when_none_provided(self, client):
        body = json.loads(client.post(
            "/api/discover/run",
            data=json.dumps({}),
            content_type="application/json",
        ).data)
        assert body["ok"] is True

    def test_double_run_rejected_cleanly(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "running", db_path=db)
        body = json.loads(client.post(
            "/api/discover/run",
            data=json.dumps({"channel_id": "hypogeum"}),
            content_type="application/json",
        ).data)
        assert body["ok"] is True
        assert "already in progress" in body.get("message", "")


# ---------------------------------------------------------------------------
# GET /api/discover/state — polling
# ---------------------------------------------------------------------------

class TestDiscoverStateEndpoint:

    def test_returns_200(self, client):
        assert client.get("/api/discover/state?channel_id=hypogeum").status_code == 200

    def test_returns_idle_when_no_run(self, client):
        body = json.loads(client.get("/api/discover/state?channel_id=hypogeum").data)
        assert body["status"] == "idle"

    def test_returns_running_when_state_is_running(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "running", db_path=db)
        body = json.loads(client.get("/api/discover/state?channel_id=hypogeum").data)
        assert body["status"] == "running"

    def test_returns_completed_after_run(self, client, db):
        from hypogeum.topic_discovery import run_discovery_cycle
        run_discovery_cycle("hypogeum", db_path=db)
        body = json.loads(client.get("/api/discover/state?channel_id=hypogeum").data)
        assert body["status"] == "completed"
        assert body["candidate_count"] > 0


# ---------------------------------------------------------------------------
# State persistence — candidates survive reload and navigation
# ---------------------------------------------------------------------------

class TestStatePersistence:

    def _run_cycle(self, client):
        client.post("/api/discover/run",
                    data=json.dumps({"channel_id": "hypogeum"}),
                    content_type="application/json")
        time.sleep(1.5)  # wait for background thread

    def test_candidates_appear_after_cycle(self, client, db):
        self._run_cycle(client)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        rows = len(re.findall(r'class="candidate-row', page))
        assert rows > 0

    def test_empty_state_gone_after_cycle(self, client, db):
        self._run_cycle(client)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "No candidates yet" not in page

    def test_candidates_persist_on_nav_back_no_params(self, client, db):
        """Navigate away (GET without query params) — candidates still show."""
        self._run_cycle(client)
        page = client.get("/discover").data.decode()
        rows = len(re.findall(r'class="candidate-row', page))
        assert rows > 0

    def test_candidates_persist_on_nav_back_with_channel_param(self, client, db):
        self._run_cycle(client)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        rows = len(re.findall(r'class="candidate-row', page))
        assert rows > 0

    def test_no_cycle_id_url_param_required(self, client, db):
        """Candidates load without cycle_id in URL — state is DB-backed."""
        self._run_cycle(client)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "candidate-row" in page

    def test_selected_candidate_marked(self, client, db):
        self._run_cycle(client)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "selected-rank" in page or "top pick" in page


# ---------------------------------------------------------------------------
# Running state persistence
# ---------------------------------------------------------------------------

class TestRunningState:

    def test_running_banner_shown_on_reload(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "running", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "Cycle in progress" in page or "running" in page.lower()

    def test_button_disabled_when_running(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "running", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "disabled" in page

    def test_old_candidates_shown_during_running(self, client, db):
        """If a previous cycle's candidates exist, show them while next cycle runs."""
        from hypogeum.topic_discovery import run_discovery_cycle, upsert_discovery_state
        run_discovery_cycle("hypogeum", db_path=db)
        upsert_discovery_state("hypogeum", "running", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        rows = len(re.findall(r'class="candidate-row', page))
        assert rows > 0

    def test_auto_poll_script_injected_when_running(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "running", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "api/discover/state" in page  # polling URL in script


# ---------------------------------------------------------------------------
# Failed state
# ---------------------------------------------------------------------------

class TestFailedState:

    def test_error_shown_inline(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "failed",
                               error="Perplexity API timeout", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "Perplexity API timeout" in page

    def test_no_alert_on_failure(self, client, db):
        from hypogeum.topic_discovery import upsert_discovery_state
        upsert_discovery_state("hypogeum", "failed", error="some error", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        assert "alert(" not in page

    def test_old_candidates_preserved_after_failure(self, client, db):
        from hypogeum.topic_discovery import run_discovery_cycle, upsert_discovery_state
        run_discovery_cycle("hypogeum", db_path=db)
        upsert_discovery_state("hypogeum", "failed", error="some error", db_path=db)
        page = client.get("/discover?channel_id=hypogeum").data.decode()
        rows = len(re.findall(r'class="candidate-row', page))
        assert rows > 0
