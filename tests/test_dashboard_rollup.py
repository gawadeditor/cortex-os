"""hypogeum/dashboard_rollup.py — factory totals == sum of channel slices."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "rollup.db"
    initialize_schema(p)
    from hypogeum.channel_config import create_channel_profile
    create_channel_profile(channel_id="cha", name="Channel A", db_path=p)
    create_channel_profile(channel_id="chb", name="Channel B", db_path=p)

    conn = get_connection(p)
    try:
        # two episodes, one per channel, each published + with analytics + costs
        for ep, ch in (("ep_a", "cha"), ("ep_b", "chb")):
            conn.execute("INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,?)",
                         (ep, "complete", "2026-08-01T00:00:00+00:00"))
            conn.execute("INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
                         "VALUES (?,?,?,?,?)",
                         (f"run_{ep}", ep, ch, "completed", "2026-08-01T00:00:00+00:00"))
            conn.execute("INSERT INTO publication_intents (publication_intent_id, channel_id, "
                         "episode_id, deliverable_id, platform, status, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (f"pi_{ep}", ch, ep, "main", "youtube", "succeeded",
                          "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"))
            conn.execute("INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, "
                         "cost_usd, called_at) VALUES (?,?,?,?,?,?,?)",
                         (f"c_{ep}", f"run_{ep}", ep, "muapi", "video", 10.0,
                          "2026-08-01T00:00:00+00:00"))
        # analytics: ep_a 100 views on youtube, ep_b 250 on tiktok
        conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                     "snapshot_date, window_days, views, total_watch_time_s, likes, source, imported_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("r1", "ep_a", "youtube", "2026-08-10", 28, 100, 6000, 5, "api",
                      "2026-08-10T00:00:00+00:00"))
        conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                     "snapshot_date, window_days, views, total_watch_time_s, likes, source, imported_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("r2", "ep_b", "tiktok", "2026-08-10", 28, 250, 9000, 12, "api",
                      "2026-08-10T00:00:00+00:00"))
        # a mock row that must be ignored
        conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, "
                     "snapshot_date, window_days, views, source, imported_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("r3", "ep_a", "youtube", "2026-08-11", 28, 99999, "mock",
                      "2026-08-11T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()
    return p


def test_factory_totals_equal_sum_of_channels(dbp):
    from hypogeum.dashboard_rollup import get_factory_rollup, get_channel_rollup
    f = get_factory_rollup(dbp)
    a = get_channel_rollup("cha", dbp)
    b = get_channel_rollup("chb", dbp)

    assert f["views"] == 350                      # 100 + 250, mock excluded
    assert f["episodes_published"] == 2
    assert f["spend_all_usd"] == 20.0
    assert a["views"] == 100 and b["views"] == 250
    assert a["episodes_published"] == 1 and b["episodes_published"] == 1
    assert a["spend_all_usd"] == 10.0 and b["spend_all_usd"] == 10.0
    assert (a["views"] + b["views"]) == f["views"]

    by = {c["channel_id"]: c for c in f["by_channel"]}
    assert by["cha"]["views"] == 100 and by["chb"]["views"] == 250


def test_mock_analytics_never_counted(dbp):
    from hypogeum.dashboard_rollup import get_factory_rollup
    assert get_factory_rollup(dbp)["views"] == 350  # not 99999+


def test_empty_channel_is_all_none_or_zero(dbp):
    from hypogeum.dashboard_rollup import get_channel_rollup
    from hypogeum.channel_config import create_channel_profile
    create_channel_profile(channel_id="chc", name="Channel C", db_path=dbp)
    c = get_channel_rollup("chc", dbp)
    assert c["views"] is None
    assert c["episodes_published"] == 0
    assert c["spend_all_usd"] == 0.0
