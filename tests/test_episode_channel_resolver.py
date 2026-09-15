"""hypogeum/episode_channel_resolver.py — the single canonical
episode/analytics-key -> channel_id resolver shared by dashboard_analytics,
dashboard_rollup, learning_engine, and monetization_eligibility."""
from __future__ import annotations

from hypogeum.db import initialize_schema, get_connection
from hypogeum.episode_channel_resolver import build_episode_channel_map


def _mk(tmp_path, name="resolver.db"):
    p = tmp_path / name
    initialize_schema(p)
    return p


def test_resolves_real_episode_id_via_runs(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    conn.execute("INSERT INTO episodes (episode_id, created_at) VALUES ('ep_1', '2026-01-01')")
    conn.execute("INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
                 "VALUES ('run_1', 'ep_1', 'cha', 'completed', '2026-01-01')")
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                 "snapshot_date, window_days, views, source, imported_at) "
                 "VALUES ('r1', 'ep_1', 'youtube', '2026-01-02', 7, 10, 'api', '2026-01-02')")
    conn.commit()
    m = build_episode_channel_map(conn)
    conn.close()
    assert m["ep_1"] == "cha"


def test_resolves_legacy_title_key_via_exact_title_match(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    conn.execute("INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
                 "VALUES ('run_2', 'ep_2', 'cha', 'completed', '2026-01-01')")
    conn.execute("INSERT INTO episodes (episode_id, title, created_at, run_id) VALUES "
                 "('ep_2', 'The Lost City', '2026-01-01', 'run_2')")
    # A legacy row keyed by title text instead of ep_2's real id.
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                 "snapshot_date, window_days, views, source, imported_at) "
                 "VALUES ('r2', 'The Lost City', 'youtube', '2026-01-02', 7, 5, 'api', '2026-01-02')")
    conn.commit()
    m = build_episode_channel_map(conn)
    conn.close()
    assert m["The Lost City"] == "cha"


def test_platform_owner_fallback_only_when_single_owner(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    conn.execute("INSERT INTO channel_provider_bindings "
                 "(binding_id, channel_id, capability, platform, provider_id, "
                 "logical_alias, enabled, created_at, updated_at) "
                 "VALUES ('b1', 'cha', 'publishing', 'tiktok', 'zernio', "
                 "'default', 1, '2026-01-01', '2026-01-01')")
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                 "snapshot_date, window_days, views, source, imported_at) "
                 "VALUES ('r3', 'Some Unmatched Caption', 'tiktok', '2026-01-02', 7, 3, "
                 "'api', '2026-01-02')")
    conn.commit()
    m = build_episode_channel_map(conn)
    conn.close()
    assert m["Some Unmatched Caption"] == "cha"


def test_platform_owner_fallback_skipped_when_shared_by_two_channels(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    for i, ch in enumerate(("cha", "chb")):
        conn.execute(
            "INSERT INTO channel_provider_bindings "
            "(binding_id, channel_id, capability, platform, provider_id, "
            "logical_alias, enabled, created_at, updated_at) "
            "VALUES (?, ?, 'publishing', 'tiktok', 'zernio', 'default', 1, "
            "'2026-01-01', '2026-01-01')",
            (f"b{i}", ch),
        )
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                 "snapshot_date, window_days, views, source, imported_at) "
                 "VALUES ('r4', 'Ambiguous Caption', 'tiktok', '2026-01-02', 7, 3, "
                 "'api', '2026-01-02')")
    conn.commit()
    m = build_episode_channel_map(conn)
    conn.close()
    assert "Ambiguous Caption" not in m


def test_unresolvable_key_simply_absent(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                 "snapshot_date, window_days, views, source, imported_at) "
                 "VALUES ('r5', 'Totally Unknown Thing', 'youtube', '2026-01-02', 7, 1, "
                 "'api', '2026-01-02')")
    conn.commit()
    m = build_episode_channel_map(conn)
    conn.close()
    assert "Totally Unknown Thing" not in m


def test_never_raises_on_missing_tables(tmp_path):
    db = _mk(tmp_path)
    conn = get_connection(db)
    m = build_episode_channel_map(conn)
    conn.close()
    assert m == {}
