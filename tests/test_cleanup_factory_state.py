"""tools/cleanup_factory_state.py — keeps published episodes, removes the rest."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection


@pytest.fixture()
def seeded(tmp_path):
    db = tmp_path / "factory.db"
    initialize_schema(db)
    conn = get_connection(db)
    try:
        # episodes: one published, one complete-but-unpublished, one stale
        for ep, status in (("ep_pub", "complete"), ("ep_hold", "complete"),
                           ("ep_stale", "in_progress")):
            conn.execute("INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,?)",
                         (ep, status, "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO publication_intents (publication_intent_id, channel_id, episode_id, "
            "deliverable_id, platform, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("pi", "hypogeum", "ep_pub", "main", "youtube", "succeeded",
             "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"))
        # runs
        for rid, ep, st in (("run_pub", "ep_pub", "completed"),
                            ("run_stale", "ep_stale", "failed"),
                            ("run_orphan", "ep_ghost", "failed")):
            conn.execute("INSERT INTO runs (run_id, episode_id, status, started_at) VALUES (?,?,?,?)",
                         (rid, ep, st, "2026-01-01T00:00:00+00:00"))
        conn.execute("INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, "
                     "cost_usd, called_at) VALUES (?,?,?,?,?,?,?)",
                     ("c1", "run_stale", "ep_stale", "muapi", "video", 1.0,
                      "2026-01-01T00:00:00+00:00"))
        conn.execute("INSERT INTO archive_index (archive_id, asset_id, episode_id, run_id, "
                     "asset_type, provider, archived_at) VALUES (?,?,?,?,?,?,?)",
                     ("ar1", "a1", "ep_ghost", "run_orphan", "clip", "muapi",
                      "2026-01-01T00:00:00+00:00"))
        # topics
        for tid, st in (("t_ok", "queued"), ("t_cancel", "cancelled"), ("t_skip", "skipped")):
            conn.execute("INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         (tid, "hypogeum", "x", st, "2026-01-01T00:00:00+00:00",
                          "2026-01-01T00:00:00+00:00"))
        # duplicate channel_operating grants
        from hypogeum.authorization_store import ensure_tables
        ensure_tables(conn)
        for i in range(3):
            conn.execute(
                "INSERT INTO run_authorization_grants (grant_id, grant_version, grant_checksum, "
                "channel_id, episode_id, workflow_id, workflow_version, workflow_checksum, "
                "bindings_json, max_total_spend_all_bindings, currency, expires_at, approval_id, "
                "approval_version, status, registered_at, grant_kind) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"g{i}", 1, "ck", "hypogeum", "*", "wf", "1.0.0", "wck", "[]", 100.0, "USD",
                 "2027-01-01T00:00:00+00:00", "ap", 1, "active",
                 f"2026-08-3{i}T00:00:00+00:00", "channel_operating"))
        # disabled schedule
        conn.execute("INSERT INTO schedules (schedule_id, scope, channel_id, cadence, status, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     ("s_dead", "channel", "hypogeum", "daily", "disabled",
                      "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()
    return db


def _counts(db):
    conn = get_connection(db)
    try:
        return {
            "episodes": [r[0] for r in conn.execute("SELECT episode_id FROM episodes ORDER BY episode_id")],
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "costs": conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0],
            "archive_index": conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0],
            "topics": [r[0] for r in conn.execute("SELECT topic_id FROM episode_topics ORDER BY topic_id")],
            "grants": conn.execute(
                "SELECT COUNT(*) FROM run_authorization_grants WHERE grant_kind='channel_operating'"
            ).fetchone()[0],
            "schedules": conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0],
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        conn.close()


def test_dry_run_touches_nothing(seeded):
    from tools.cleanup_factory_state import main
    before = _counts(seeded)
    assert main(["--db", str(seeded)]) == 0
    assert _counts(seeded) == before


def test_apply_keeps_published_and_held_only(seeded):
    from tools.cleanup_factory_state import main
    assert main(["--db", str(seeded), "--skip-local-files", "--skip-logs", "--apply"]) == 0
    after = _counts(seeded)
    assert after["episodes"] == ["ep_hold", "ep_pub"]      # stale gone, held kept
    assert after["archive_index"] == 0                      # ghost-episode orphan row gone
    assert after["costs"] == 0                              # stale run cascade
    assert after["topics"] == ["t_ok"]                      # cancelled + skipped gone
    assert after["grants"] == 1                             # newest kept
    assert after["schedules"] == 0                          # disabled removed
    assert after["integrity"] == "ok"
    conn = get_connection(seeded)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='run_pub'").fetchone()[0] == 1
    finally:
        conn.close()


def test_purge_unpublished_complete_flag(seeded):
    from tools.cleanup_factory_state import main
    main(["--db", str(seeded), "--skip-local-files", "--skip-logs",
          "--purge-unpublished-complete", "--apply"])
    assert _counts(seeded)["episodes"] == ["ep_pub"]
