"""
tests/test_dashboard_control.py — Phase B: Factory Control Layer tests

Coverage:
    TestReadFunctions       — get_*_detail, list_secret_status (unit, DB)
    TestSecretNeverExposed  — credential_data never leaks from any read path
    TestEditFunctions       — edit_*_profile, set_schedule_status (unit, DB)
    TestActionFunctions     — action_approve, action_reject, action_resolve_signal
    TestFlaskReadRoutes     — GET /api/<entity>/<id>, GET /api/secrets
    TestFlaskEditRoutes     — POST /api/<entity>/<id>
    TestFlaskHtmlRoutes     — GET/POST /channel/<id>, /brand/.../edit, etc.
    TestFlaskActionRoutes   — POST /api/action/*
    TestMethodEnforcement   — POST-only routes reject GET; GET routes reject POST
    TestNoProductionSideEffects — actions do not create episodes or runs
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import bootstrap_channel
from hypogeum.approvals import create_approval_request
from hypogeum.scheduler import create_schedule
from hypogeum.credential_manager import create_secret_profile

from hypogeum.dashboard_control import (
    get_channel_detail,
    get_brand_detail,
    get_model_detail,
    get_publishing_detail,
    get_schedule_detail,
    list_secret_status,
    edit_channel_profile,
    edit_brand_profile,
    edit_model_profile,
    edit_publishing_profile,
    set_schedule_status,
    action_approve,
    action_reject,
    action_resolve_signal,
    VALID_SCHEDULE_STATUSES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "ctrl_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def channel(db_path):
    """Bootstrap a fully configured channel; return its ChannelProfile."""
    cp = bootstrap_channel({
        "channel_id":  "ctrl_ch",
        "name":        "Control Channel",
        "brand":       {"persona_name": "Host", "language": "en"},
        "publishing":  {"enabled_platforms": ["youtube"]},
        "models":      {"narration_voice_id": "voice_x"},
    }, db_path=db_path)
    return cp


@pytest.fixture()
def schedule_id(db_path):
    return create_schedule(db_path=db_path, scope="channel",
                           channel_id="ctrl_ch", cadence="weekly")


@pytest.fixture()
def secret_id(db_path):
    p = create_secret_profile(
        provider="openrouter",
        label="test key",
        credential_type="api_key",
        credential_data="sk-super-secret-value",
        db_path=db_path,
    )
    return p.secret_profile_id


@pytest.fixture()
def approval_id(db_path):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status) "
        "VALUES ('ar1','ae1','2026-01-01T00:00:00','awaiting_review')"
    )
    conn.execute(
        "INSERT INTO episodes (episode_id, created_at, run_id) "
        "VALUES ('ae1','2026-01-01T00:00:00','ar1')"
    )
    conn.commit()
    conn.close()
    return create_approval_request(
        run_id="ar1",
        episode_id="ae1",
        approval_type="human_review",
        requested_by="system:pipeline",
        db_path=db_path,
    )


@pytest.fixture()
def signal_id(db_path):
    conn = get_connection(db_path)
    sid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO signal_records
           (signal_id, episode_id, run_id, ingested_at, window_days,
            data_quality, metadata_version, signal_type, severity,
            full_record, source_system, blocking, lifecycle_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, "ae1", "ar1", "2026-01-01T00:00:00", 0,
         "ok", "v1", "test_signal", "INFO",
         json.dumps({"message": "test"}), "test", 0, "open"),
    )
    conn.commit()
    conn.close()
    return sid


@pytest.fixture()
def flask_client(db_path, channel, schedule_id, secret_id, approval_id, signal_id):
    """Flask test client with test DB injected via app.config."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client, {
            "channel_id":            "ctrl_ch",
            "brand_profile_id":      channel.brand_profile_id,
            "model_profile_id":      channel.model_profile_id,
            "publishing_profile_id": channel.publishing_profile_id,
            "schedule_id":           schedule_id,
            "secret_id":             secret_id,
            "approval_id":           approval_id,
            "signal_id":             signal_id,
        }


# ===========================================================================
# TestReadFunctions
# ===========================================================================

class TestReadFunctions:
    def test_get_channel_detail_returns_dict(self, db_path, channel):
        d = get_channel_detail("ctrl_ch", db_path=db_path)
        assert isinstance(d, dict)
        assert d["channel_id"] == "ctrl_ch"

    def test_get_channel_detail_missing_returns_none(self, db_path):
        assert get_channel_detail("no_such_channel", db_path=db_path) is None

    def test_get_brand_detail_returns_dict(self, db_path, channel):
        d = get_brand_detail(channel.brand_profile_id, db_path=db_path)
        assert isinstance(d, dict)
        assert d["brand_profile_id"] == channel.brand_profile_id

    def test_get_brand_detail_missing_returns_none(self, db_path):
        assert get_brand_detail("no-such-brand", db_path=db_path) is None

    def test_get_model_detail_returns_dict(self, db_path, channel):
        d = get_model_detail(channel.model_profile_id, db_path=db_path)
        assert isinstance(d, dict)
        assert d["model_profile_id"] == channel.model_profile_id

    def test_get_model_detail_missing_returns_none(self, db_path):
        assert get_model_detail("no-such-model", db_path=db_path) is None

    def test_get_publishing_detail_returns_dict(self, db_path, channel):
        d = get_publishing_detail(channel.publishing_profile_id, db_path=db_path)
        assert isinstance(d, dict)
        assert d["publishing_profile_id"] == channel.publishing_profile_id

    def test_get_publishing_detail_missing_returns_none(self, db_path):
        assert get_publishing_detail("no-such-pub", db_path=db_path) is None

    def test_get_schedule_detail_returns_dict(self, db_path, schedule_id):
        d = get_schedule_detail(schedule_id, db_path=db_path)
        assert isinstance(d, dict)
        assert d["schedule_id"] == schedule_id

    def test_get_schedule_detail_missing_returns_none(self, db_path):
        assert get_schedule_detail("no-such-schedule", db_path=db_path) is None

    def test_list_secret_status_returns_list(self, db_path, secret_id):
        result = list_secret_status(db_path=db_path)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_list_secret_status_empty_db(self, db_path):
        result = list_secret_status(db_path=db_path)
        assert isinstance(result, list)

    def test_bad_db_read_functions_return_none_or_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert get_channel_detail("x", db_path=bad) is None
        assert get_brand_detail("x", db_path=bad) is None
        assert get_model_detail("x", db_path=bad) is None
        assert get_publishing_detail("x", db_path=bad) is None
        assert get_schedule_detail("x", db_path=bad) is None
        assert list_secret_status(db_path=bad) == []


# ===========================================================================
# TestSecretNeverExposed
# ===========================================================================

class TestSecretNeverExposed:
    def test_list_secret_status_no_credential_data(self, db_path, secret_id):
        profiles = list_secret_status(db_path=db_path)
        for p in profiles:
            assert "credential_data" not in p

    def test_get_channel_detail_no_credential_data(self, db_path, channel):
        d = get_channel_detail("ctrl_ch", db_path=db_path)
        assert "credential_data" not in str(d)

    def test_get_brand_detail_no_credential_data(self, db_path, channel):
        d = get_brand_detail(channel.brand_profile_id, db_path=db_path)
        assert "credential_data" not in str(d)

    def test_secret_raw_value_absent(self, db_path, secret_id):
        profiles = list_secret_status(db_path=db_path)
        text = json.dumps(profiles)
        assert "sk-super-secret-value" not in text


# ===========================================================================
# TestEditFunctions
# ===========================================================================

class TestEditFunctions:
    def test_edit_channel_profile_name(self, db_path, channel):
        result = edit_channel_profile("ctrl_ch", {"name": "Renamed"}, db_path=db_path)
        assert result["ok"] is True
        assert result["data"]["name"] == "Renamed"

    def test_edit_channel_profile_not_found(self, db_path):
        result = edit_channel_profile("ghost", {"name": "X"}, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_edit_channel_profile_unknown_field_ignored(self, db_path, channel):
        result = edit_channel_profile("ctrl_ch", {"_injected": "evil"}, db_path=db_path)
        assert result["ok"] is True

    def test_edit_brand_profile_persona(self, db_path, channel):
        result = edit_brand_profile(
            channel.brand_profile_id, {"persona_name": "NewHost"}, db_path=db_path
        )
        assert result["ok"] is True
        assert result["data"]["persona_name"] == "NewHost"

    def test_edit_brand_profile_not_found(self, db_path):
        result = edit_brand_profile("ghost-brand", {"persona_name": "X"}, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_edit_model_profile_voice(self, db_path, channel):
        result = edit_model_profile(
            channel.model_profile_id, {"narration_voice_id": "voice_new"}, db_path=db_path
        )
        assert result["ok"] is True
        assert result["data"]["narration_voice_id"] == "voice_new"

    def test_edit_model_profile_not_found(self, db_path):
        result = edit_model_profile("ghost-model", {}, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_edit_publishing_profile_platforms(self, db_path, channel):
        result = edit_publishing_profile(
            channel.publishing_profile_id,
            {"enabled_platforms": ["youtube", "instagram"]},
            db_path=db_path,
        )
        assert result["ok"] is True
        assert "youtube" in result["data"]["enabled_platforms"]

    def test_edit_publishing_profile_not_found(self, db_path):
        result = edit_publishing_profile("ghost-pub", {}, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_set_schedule_status_paused(self, db_path, schedule_id):
        result = set_schedule_status(schedule_id, "paused", db_path=db_path)
        assert result["ok"] is True
        assert result["data"]["status"] == "paused"

    def test_set_schedule_status_resumed(self, db_path, schedule_id):
        set_schedule_status(schedule_id, "paused", db_path=db_path)
        result = set_schedule_status(schedule_id, "active", db_path=db_path)
        assert result["ok"] is True
        assert result["data"]["status"] == "active"

    def test_set_schedule_status_disabled(self, db_path, schedule_id):
        result = set_schedule_status(schedule_id, "disabled", db_path=db_path)
        assert result["ok"] is True
        assert result["data"]["status"] == "disabled"

    def test_set_schedule_status_invalid(self, db_path, schedule_id):
        result = set_schedule_status(schedule_id, "running", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "invalid"

    def test_set_schedule_status_not_found(self, db_path):
        result = set_schedule_status("no-such-schedule", "paused", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_valid_schedule_statuses_constant(self):
        assert VALID_SCHEDULE_STATUSES == {"active", "paused", "disabled"}


# ===========================================================================
# TestActionFunctions
# ===========================================================================

class TestActionFunctions:
    def test_action_approve_returns_ok(self, db_path, approval_id):
        result = action_approve(approval_id, db_path=db_path)
        assert result["ok"] is True
        assert result["status"] == "approved"
        assert result["approval_id"] == approval_id

    def test_action_approve_not_found(self, db_path):
        result = action_approve("no-such-approval", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_action_approve_already_decided(self, db_path, approval_id):
        action_approve(approval_id, db_path=db_path)
        result = action_approve(approval_id, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "already_decided"

    def test_action_reject_returns_ok(self, db_path, approval_id):
        result = action_reject(approval_id, db_path=db_path)
        assert result["ok"] is True
        assert result["status"] == "rejected"
        assert result["approval_id"] == approval_id

    def test_action_reject_not_found(self, db_path):
        result = action_reject("no-such-approval", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_action_reject_already_decided(self, db_path, approval_id):
        action_reject(approval_id, db_path=db_path)
        result = action_reject(approval_id, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "already_decided"

    def test_action_approve_with_notes(self, db_path, approval_id):
        result = action_approve(approval_id, notes="LGTM", db_path=db_path)
        assert result["ok"] is True

    def test_action_reject_with_reason(self, db_path, approval_id):
        result = action_reject(approval_id, reason="Quality too low", db_path=db_path)
        assert result["ok"] is True

    def test_action_resolve_signal_returns_ok(self, db_path, signal_id):
        result = action_resolve_signal(signal_id, db_path=db_path)
        assert result["ok"] is True
        assert result["status"] == "resolved"
        assert result["signal_id"] == signal_id

    def test_action_resolve_signal_idempotent_returns_ok(self, db_path, signal_id):
        action_resolve_signal(signal_id, db_path=db_path)
        result = action_resolve_signal(signal_id, db_path=db_path)
        assert result["ok"] is True

    def test_action_approve_does_not_write_new_runs(self, db_path, approval_id):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        action_approve(approval_id, db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_action_reject_does_not_write_new_episodes(self, db_path, approval_id):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        action_reject(approval_id, db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after


# ===========================================================================
# TestFlaskReadRoutes
# ===========================================================================

class TestFlaskReadRoutes:
    def test_get_channel_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/channel/{ids['channel_id']}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["channel_id"] == ids["channel_id"]

    def test_get_channel_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/channel/no-such-channel")
        assert r.status_code == 404

    def test_get_brand_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/brand/{ids['brand_profile_id']}")
        assert r.status_code == 200
        data = r.get_json()
        assert "brand_profile_id" in data

    def test_get_brand_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/brand/no-such-brand")
        assert r.status_code == 404

    def test_get_model_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/model/{ids['model_profile_id']}")
        assert r.status_code == 200

    def test_get_model_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/model/no-such-model")
        assert r.status_code == 404

    def test_get_publishing_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/publishing/{ids['publishing_profile_id']}")
        assert r.status_code == 200

    def test_get_publishing_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/publishing/no-such-pub")
        assert r.status_code == 404

    def test_get_schedule_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/schedule/{ids['schedule_id']}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["schedule_id"] == ids["schedule_id"]

    def test_get_schedule_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/schedule/no-such-schedule")
        assert r.status_code == 404

    def test_get_secrets_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/secrets")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_get_secrets_no_credential_data(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/secrets")
        text = r.get_data(as_text=True)
        assert "credential_data" not in text
        assert "sk-super-secret-value" not in text

    def test_get_secrets_has_status_field(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/secrets")
        profiles = r.get_json()
        assert len(profiles) >= 1
        assert "status" in profiles[0]

    def test_get_routes_return_json(self, flask_client):
        client, ids = flask_client
        for url in [
            f"/api/channel/{ids['channel_id']}",
            f"/api/brand/{ids['brand_profile_id']}",
            "/api/secrets",
        ]:
            r = client.get(url)
            assert "application/json" in r.content_type


# ===========================================================================
# TestFlaskEditRoutes
# ===========================================================================

class TestFlaskEditRoutes:
    def _post(self, client, url, body):
        return client.post(
            url,
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_post_channel_edit_200(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/channel/{ids['channel_id']}", {"name": "Updated"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["data"]["name"] == "Updated"

    def test_post_channel_edit_404(self, flask_client):
        client, _ = flask_client
        r = self._post(client, "/api/channel/ghost", {"name": "X"})
        assert r.status_code == 404
        data = r.get_json()
        assert data["ok"] is False
        assert data["code"] == "not_found"

    def test_post_brand_edit_200(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/brand/{ids['brand_profile_id']}",
            {"persona_name": "NewHost"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

    def test_post_brand_edit_404(self, flask_client):
        client, _ = flask_client
        r = self._post(client, "/api/brand/ghost-brand", {})
        assert r.status_code == 404

    def test_post_model_edit_200(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/model/{ids['model_profile_id']}",
            {"narration_voice_id": "v2"},
        )
        assert r.status_code == 200

    def test_post_publishing_edit_200(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/publishing/{ids['publishing_profile_id']}",
            {"enabled_platforms": ["youtube"]},
        )
        assert r.status_code == 200

    def test_post_schedule_status_pause(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/schedule/{ids['schedule_id']}/status",
            {"status": "paused"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "paused"

    def test_post_schedule_status_invalid(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/schedule/{ids['schedule_id']}/status",
            {"status": "broken"},
        )
        assert r.status_code == 400
        data = r.get_json()
        assert data["ok"] is False
        assert data["code"] == "invalid"

    def test_post_schedule_status_not_found(self, flask_client):
        client, _ = flask_client
        r = self._post(client, "/api/schedule/ghost/status", {"status": "paused"})
        assert r.status_code == 404

    def test_post_edit_empty_body_is_noop(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/channel/{ids['channel_id']}", {})
        assert r.status_code == 200

    def test_post_edit_unknown_fields_ignored(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/brand/{ids['brand_profile_id']}",
            {"credential_data": "evil", "unknown_field": "x"},
        )
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "credential_data" not in text


# ===========================================================================
# TestFlaskActionRoutes
# ===========================================================================

class TestFlaskActionRoutes:
    def _post(self, client, url, body=None):
        return client.post(
            url,
            data=json.dumps(body or {}),
            content_type="application/json",
        )

    def test_approve_200(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/action/approve/{ids['approval_id']}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["status"] == "approved"

    def test_approve_404(self, flask_client):
        client, _ = flask_client
        r = self._post(client, "/api/action/approve/no-such-approval")
        assert r.status_code == 404
        data = r.get_json()
        assert data["ok"] is False
        assert data["code"] == "not_found"

    def test_approve_already_decided_409(self, flask_client):
        client, ids = flask_client
        self._post(client, f"/api/action/approve/{ids['approval_id']}")
        r = self._post(client, f"/api/action/approve/{ids['approval_id']}")
        assert r.status_code == 409
        data = r.get_json()
        assert data["code"] == "already_decided"

    def test_reject_200(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/action/reject/{ids['approval_id']}", {"reason": "bad"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["status"] == "rejected"

    def test_reject_404(self, flask_client):
        client, _ = flask_client
        r = self._post(client, "/api/action/reject/no-such-approval")
        assert r.status_code == 404

    def test_reject_already_decided_409(self, flask_client):
        client, ids = flask_client
        self._post(client, f"/api/action/reject/{ids['approval_id']}")
        r = self._post(client, f"/api/action/reject/{ids['approval_id']}")
        assert r.status_code == 409

    def test_resolve_signal_200(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/action/resolve-signal/{ids['signal_id']}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["status"] == "resolved"

    def test_resolve_signal_with_user_id(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/action/resolve-signal/{ids['signal_id']}",
            {"user_id": "system:operator"},
        )
        assert r.status_code == 200

    def test_approve_with_notes(self, flask_client):
        client, ids = flask_client
        r = self._post(
            client,
            f"/api/action/approve/{ids['approval_id']}",
            {"notes": "Looks great"},
        )
        assert r.status_code == 200

    def test_action_routes_return_json(self, flask_client):
        client, ids = flask_client
        r = self._post(client, f"/api/action/approve/{ids['approval_id']}")
        assert "application/json" in r.content_type


# ===========================================================================
# TestMethodEnforcement
# ===========================================================================

class TestMethodEnforcement:
    """POST-only routes must reject GET. GET-only routes must reject POST."""

    def test_action_approve_rejects_get(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/action/approve/{ids['approval_id']}")
        assert r.status_code == 405

    def test_action_reject_rejects_get(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/action/reject/{ids['approval_id']}")
        assert r.status_code == 405

    def test_action_resolve_signal_rejects_get(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/action/resolve-signal/{ids['signal_id']}")
        assert r.status_code == 405

    def test_schedule_status_rejects_get(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/schedule/{ids['schedule_id']}/status")
        assert r.status_code == 405

    def test_get_secrets_rejects_post(self, flask_client):
        client, _ = flask_client
        r = client.post("/api/secrets", data="{}", content_type="application/json")
        assert r.status_code == 405

    def test_get_channel_rejects_put(self, flask_client):
        client, ids = flask_client
        r = client.put(f"/api/channel/{ids['channel_id']}")
        assert r.status_code == 405


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_read_functions_do_not_write_runs(self, db_path, channel):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()

        get_channel_detail("ctrl_ch", db_path=db_path)
        get_brand_detail(channel.brand_profile_id, db_path=db_path)
        get_model_detail(channel.model_profile_id, db_path=db_path)
        get_publishing_detail(channel.publishing_profile_id, db_path=db_path)
        list_secret_status(db_path=db_path)

        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_edit_functions_do_not_write_episodes(self, db_path, channel):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()

        edit_channel_profile("ctrl_ch", {"name": "X"}, db_path=db_path)
        edit_brand_profile(channel.brand_profile_id, {"persona_name": "Y"}, db_path=db_path)

        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after

    def test_no_ep_000_directory_created(self, db_path, tmp_path, approval_id):
        action_approve(approval_id, db_path=db_path)
        ep_dirs = list(tmp_path.rglob("ep_000"))
        assert ep_dirs == []


# ===========================================================================
# TestFlaskHtmlRoutes  — browser-navigable operator UI routes
# ===========================================================================

class TestFlaskHtmlRoutes:
    """Every operator workflow must be completable from a browser without curl."""

    # ── Dashboard main page ─────────────────────────────────────────────────

    def test_dashboard_has_view_link_for_channels(self, flask_client):
        client, ids = flask_client
        r = client.get("/dashboard")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert f"/channel/{ids['channel_id']}" in text

    def test_dashboard_has_approve_button(self, flask_client):
        client, ids = flask_client
        r = client.get("/dashboard")
        text = r.get_data(as_text=True)
        assert "Approve" in text or "approve" in text

    def test_dashboard_has_reject_button(self, flask_client):
        client, ids = flask_client
        r = client.get("/dashboard")
        text = r.get_data(as_text=True)
        assert "Reject" in text or "reject" in text

    def test_dashboard_has_resolve_button_for_signals(self, flask_client):
        client, ids = flask_client
        r = client.get("/dashboard")
        text = r.get_data(as_text=True)
        assert "resolve-signal" in text or "Resolve" in text

    def test_dashboard_has_pause_resume_for_schedules(self, flask_client):
        client, ids = flask_client
        r = client.get("/dashboard")
        text = r.get_data(as_text=True)
        assert "paused" in text or "Pause" in text or "Resume" in text or "status" in text

    def test_dashboard_postaction_js_present(self, flask_client):
        client, _ = flask_client
        r = client.get("/dashboard")
        text = r.get_data(as_text=True)
        assert "postAction" in text

    # ── Channel detail page ─────────────────────────────────────────────────

    def test_channel_detail_page_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Control Channel" in text

    def test_channel_detail_shows_brand(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert "Brand Profile" in text

    def test_channel_detail_shows_model(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert "Model Profile" in text

    def test_channel_detail_shows_publishing(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert "Publishing Profile" in text

    def test_channel_detail_has_edit_links(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert "Edit Channel" in text
        assert "Edit Brand" in text
        assert "Edit Model" in text
        assert "Edit Publishing" in text

    def test_channel_detail_has_brand_edit_href(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert f"/brand/{ids['brand_profile_id']}/edit" in text

    def test_channel_detail_has_model_edit_href(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert f"/model/{ids['model_profile_id']}/edit" in text

    def test_channel_detail_has_publishing_edit_href(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}")
        text = r.get_data(as_text=True)
        assert f"/publishing/{ids['publishing_profile_id']}/edit" in text

    def test_channel_detail_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/channel/no-such-channel")
        assert r.status_code == 404

    # ── Channel edit page ───────────────────────────────────────────────────

    def test_channel_edit_get_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/channel/{ids['channel_id']}/edit")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Control Channel" in text
        assert '<form' in text
        assert 'name="name"' in text

    def test_channel_edit_post_redirects(self, flask_client):
        client, ids = flask_client
        r = client.post(
            f"/channel/{ids['channel_id']}/edit",
            data={"name": "Renamed Channel", "description": "Updated desc"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert f"/channel/{ids['channel_id']}" in r.headers["Location"]

    def test_channel_edit_post_saves_name(self, flask_client, db_path):
        client, ids = flask_client
        client.post(
            f"/channel/{ids['channel_id']}/edit",
            data={"name": "New Name", "description": ""},
            follow_redirects=True,
        )
        updated = get_channel_detail(ids["channel_id"], db_path=db_path)
        assert updated["name"] == "New Name"

    def test_channel_edit_get_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/channel/ghost/edit")
        assert r.status_code == 404

    # ── Brand edit page ─────────────────────────────────────────────────────

    def test_brand_edit_get_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/brand/{ids['brand_profile_id']}/edit")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert '<form' in text
        assert 'name="persona_name"' in text
        assert 'name="tone"' in text
        assert 'name="language"' in text

    def test_brand_edit_post_saves(self, flask_client, db_path):
        client, ids = flask_client
        client.post(
            f"/brand/{ids['brand_profile_id']}/edit",
            data={
                "name": "Brand X",
                "persona_name": "New Host",
                "tone": "friendly",
                "target_audience": "developers",
                "visual_style": "dark",
                "language": "en",
                "voice_guidelines": "speak slowly",
            },
            follow_redirects=True,
        )
        updated = get_brand_detail(ids["brand_profile_id"], db_path=db_path)
        assert updated["persona_name"] == "New Host"
        assert updated["tone"] == "friendly"

    def test_brand_edit_post_redirects_back(self, flask_client):
        client, ids = flask_client
        r = client.post(
            f"/brand/{ids['brand_profile_id']}/edit",
            data={"name": "B", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert f"/brand/{ids['brand_profile_id']}/edit" in r.headers["Location"]

    def test_brand_edit_get_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/brand/ghost-brand/edit")
        assert r.status_code == 404

    # ── Model edit page ─────────────────────────────────────────────────────

    def test_model_edit_get_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/model/{ids['model_profile_id']}/edit")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert '<form' in text
        assert 'name="narration_voice_id"' in text
        assert 'name="image_provider"' in text

    def test_model_edit_post_saves(self, flask_client, db_path):
        client, ids = flask_client
        client.post(
            f"/model/{ids['model_profile_id']}/edit",
            data={"name": "Models v2", "narration_voice_id": "abc123",
                  "image_provider": "muapi", "video_provider": "muapi",
                  "episode_producer": "anthropic/claude-sonnet-4.6",
                  "research_director": "anthropic/claude-opus-4.6",
                  "thumbnail_planner": "anthropic/claude-sonnet-4.6",
                  "brand_guardian_p1": "anthropic/claude-sonnet-4.6",
                  "brand_guardian_p2": "openai/gpt-5.5-pro"},
            follow_redirects=True,
        )
        updated = get_model_detail(ids["model_profile_id"], db_path=db_path)
        assert updated["narration_voice_id"] == "abc123"

    def test_model_edit_get_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/model/ghost/edit")
        assert r.status_code == 404

    # ── Publishing edit page ────────────────────────────────────────────────

    def test_publishing_edit_get_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/publishing/{ids['publishing_profile_id']}/edit")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert '<form' in text
        assert 'enabled_platforms' in text
        assert 'ai_disclosure_required' in text

    def test_publishing_edit_post_saves_platforms(self, flask_client, db_path):
        client, ids = flask_client
        client.post(
            f"/publishing/{ids['publishing_profile_id']}/edit",
            data={
                "name": "Pub v2",
                "enabled_platforms": "youtube, instagram",
                "ai_disclosure_required": "on",
            },
            follow_redirects=True,
        )
        updated = get_publishing_detail(ids["publishing_profile_id"], db_path=db_path)
        assert "youtube" in updated["enabled_platforms"]
        assert "instagram" in updated["enabled_platforms"]

    def test_publishing_edit_unchecked_means_false(self, flask_client, db_path):
        client, ids = flask_client
        client.post(
            f"/publishing/{ids['publishing_profile_id']}/edit",
            data={"name": "Pub v2", "enabled_platforms": "youtube"},
            follow_redirects=True,
        )
        updated = get_publishing_detail(ids["publishing_profile_id"], db_path=db_path)
        assert updated["ai_disclosure_required"] is False
        assert updated["auto_thumbnail"] is False
        assert updated["auto_captions"] is False

    def test_publishing_edit_post_redirects(self, flask_client):
        client, ids = flask_client
        r = client.post(
            f"/publishing/{ids['publishing_profile_id']}/edit",
            data={"name": "P", "enabled_platforms": "youtube"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert f"/publishing/{ids['publishing_profile_id']}/edit" in r.headers["Location"]

    def test_publishing_edit_get_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/publishing/ghost/edit")
        assert r.status_code == 404
