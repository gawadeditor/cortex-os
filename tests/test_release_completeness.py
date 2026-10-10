"""tests/test_release_completeness.py — partial releases (2026-10-10)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import hypogeum.db as db
from hypogeum import release_completeness as rc

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "rc.db"
    db.initialize_schema(p)
    return p


def _intent(dbp, ep, plat, status, when, deliverable="main_episode", ch="ch", error=None):
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    pid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO publication_intents (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", (pid, ch, ep, deliverable, plat, status, when.isoformat(), when.isoformat()))
    if error is not None:
        conn.execute(
            "INSERT INTO publishing_attempts (attempt_id, publication_intent_id, channel_id, episode_id, deliverable_id, platform, "
            "provider_id, attempt_number, state, last_error_message, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), pid, ch, ep, deliverable, plat, "postiz", 1, "failed_definitive", error, when.isoformat(), when.isoformat()))
    conn.commit()
    conn.close()


@pytest.fixture()
def targets(monkeypatch):
    import hypogeum.publishing.zernio_channel_release_trigger as t
    monkeypatch.setattr(t, "_resolve_target_platforms", lambda ch, db: ["facebook", "instagram", "tiktok", "youtube"])
    monkeypatch.setattr(t, "_resolve_platform_deliverable_policy", lambda ch, db: {})


def test_partial_release_reports_the_missing_platforms_age_and_error(dbp, targets):
    _intent(dbp, "ep1", "tiktok", "succeeded", NOW - timedelta(minutes=50))
    _intent(dbp, "ep1", "youtube", "open", NOW - timedelta(minutes=49), error="HTTP 400: tags too long")
    inc = rc.incomplete_release("ep1", "ch", db_path=dbp, now=NOW)
    assert inc["succeeded"] == ["tiktok"] and inc["missing"] == ["facebook", "instagram", "youtube"]
    assert inc["age_s"] == pytest.approx(50 * 60) and "tags too long" in inc["last_errors"]["youtube"]


def test_complete_or_unreleased_episodes_are_not_partial(dbp, targets):
    assert rc.incomplete_release("nothing", "ch", db_path=dbp, now=NOW) is None
    for plat in ("facebook", "instagram", "tiktok", "youtube"):
        _intent(dbp, "ep2", plat, "succeeded", NOW - timedelta(hours=1))
    assert rc.incomplete_release("ep2", "ch", db_path=dbp, now=NOW) is None


def test_a_platform_the_policy_does_not_enable_is_not_missing(dbp, monkeypatch):
    import hypogeum.publishing.zernio_channel_release_trigger as t
    monkeypatch.setattr(t, "_resolve_target_platforms", lambda ch, db: ["facebook", "tiktok"])
    monkeypatch.setattr(t, "_resolve_platform_deliverable_policy",
                        lambda ch, db: {"tiktok": {"main_episode": {"enabled": True}}, "facebook": {"shorts": {"enabled": True}}})
    _intent(dbp, "ep3", "tiktok", "succeeded", NOW - timedelta(hours=1))
    assert rc.incomplete_release("ep3", "ch", db_path=dbp, now=NOW) is None


def test_it_fails_open(dbp, monkeypatch):
    import hypogeum.publishing.zernio_channel_release_trigger as t
    monkeypatch.setattr(t, "_resolve_target_platforms", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    _intent(dbp, "ep4", "tiktok", "succeeded", NOW)
    assert rc.incomplete_release("ep4", "ch", db_path=dbp, now=NOW) is None
