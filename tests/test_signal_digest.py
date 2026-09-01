"""Operator-readable signal presentation + auto-resolve of noise."""

from __future__ import annotations

from hypogeum.db import get_connection, initialize_schema
from hypogeum.signal_digest import (
    auto_resolve_stale_signals,
    digest_signals,
    present_signal,
)


def _seed(db):
    from hypogeum.ois import emit_signal
    initialize_schema(db)
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, title, status, created_at) VALUES "
            "('ep_pub', 'The Vanished Skulls', 'published', '2026-08-01T00:00:00+00:00'), "
            "('ep_live', 'Live One', 'in_progress', '2026-08-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "s_info": emit_signal("run_completed", "INFO",
                              "Run 'abc' transitioned to 'completed'.",
                              None, "test", db_path=db),
        "s_stale": emit_signal("retry_exhausted", "WARNING",
                               "Queue item def exhausted auto-retry.",
                               None, "test", episode_id="ep_pub", db_path=db),
        "s_real": emit_signal("backup_unhealthy", "WARNING",
                              "no backup has run yet", None, "test", db_path=db),
        "s_live": emit_signal("run_failed", "WARNING",
                              "Run 'ghi' transitioned to 'failed'.",
                              None, "test", episode_id="ep_live", db_path=db),
    }


def _open_signals(db):
    from hypogeum.dashboard_api import get_active_signals
    return get_active_signals(db_path=db)


def test_present_signal_uses_plain_language_and_episode_title(tmp_path):
    db = tmp_path / "t.db"
    ids = _seed(db)
    sig = next(s for s in _open_signals(db) if s["signal_id"] == ids["s_live"])
    p = present_signal(sig, db_path=db)
    assert p["title"] == "A production run failed"
    assert "Live One" in p["explain"]
    assert p["cta_url"] == "/runs?status=failed"
    assert "transitioned to" not in p["explain"]


def test_digest_splits_attention_from_notices(tmp_path):
    db = tmp_path / "t.db"
    ids = _seed(db)
    d = digest_signals(_open_signals(db), db_path=db)
    titles = {s["title"] for s in d["attention"]}
    assert "Database backup has not run" in titles
    assert "A production run failed" in titles
    assert {s["signal_id"] for s in d["notices"]} == {ids["s_info"], ids["s_stale"]}


def test_auto_resolve_clears_noise_keeps_real(tmp_path):
    db = tmp_path / "t.db"
    ids = _seed(db)
    n = auto_resolve_stale_signals(db_path=db)
    assert n == 2  # s_info (informational) + s_stale (episode published)
    remaining = {s["signal_id"] for s in _open_signals(db)}
    assert remaining == {ids["s_real"], ids["s_live"]}


def test_auto_resolve_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    auto_resolve_stale_signals(db_path=db)
    assert auto_resolve_stale_signals(db_path=db) == 0
