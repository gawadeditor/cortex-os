import json

from hypogeum.dashboard_discovery import get_discovery_panel
from hypogeum.db import get_connection, initialize_schema


def test_panel_assembles_mix_terms_exits_and_directives(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    conn = get_connection(db)
    ins = ("INSERT INTO youtube_traffic_insights (channel_id, snapshot_date, dimension, key, subkey, views, minutes_watched, "
           "avg_view_duration_s, avg_view_pct, window_days, created_at) VALUES ('ch','2026-10-12',?,?,'',?,0,10,?,89,'x')")
    for dim, key, v, pct in [("traffic_type", "SHORTS", 900, 70.0), ("traffic_type", "YT_SEARCH", 60, 96.0),
                             ("search_term", "valley of the kings", 5, 120.0), ("search_term", "voynich", 4, 90.0),
                             ("search_term", "herculaneum", 3, 70.0), ("subscribed", "UNSUBSCRIBED", 95, 60.0),
                             ("device", "MOBILE", 80, 65.0)]:
        conn.execute(ins, (dim, key, v, pct))
    conn.execute("INSERT INTO retention_moments (moment_id, channel_id, episode_id, video_id, kind, t0, t1, loss, video_views, beat_id, "
                 "beat_index, beats_total, signals_json, spoken_text, scene_id, visual_json, frame_path, created_at) "
                 "VALUES ('V1:exit:0','ch','ep_1','V1','exit',4.0,5.0,0.24,456,'b2',1,9,'[]','Beneath the arena floor','s02',?, '/x/V1_exit_45.jpg','x')",
                 (json.dumps({"subject": "corridors", "framing": "low angle"}),))
    conn.execute("INSERT INTO channel_learning_directives (directive_id, channel_id, dimension, action_type, directive_text, created_at, active) "
                 "VALUES ('d1','ch','search_demand','prefer','Use real search phrases','2026-10-12T00:00:00',1)")
    conn.commit()
    conn.close()
    p = get_discovery_panel("ch", db_path=db)
    assert p["has_data"] and p["snapshot_date"] == "2026-10-12"
    assert p["mix"][0]["label"] == "the Shorts feed" and round(p["mix"][0]["share"], 2) == 0.94
    assert [t["key"] for t in p["search_terms"]][:1] == ["valley of the kings"]
    assert p["exits"][0]["frame"] == "V1_exit_45.jpg" and p["exits"][0]["loss_pts"] == 24 and p["exits"][0]["beat"] == 2
    assert p["directives"][0]["title"] == "What viewers search for"
    assert p["subscribed"][0]["label"] == "Unsubscribed" and p["devices"][0]["label"] == "Mobile"


def test_empty_panel_does_not_raise(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    p = get_discovery_panel("nobody", db_path=db)
    assert p["has_data"] is False and p["exits"] == []


def test_episode_discovery_joins_curve_exits_and_traffic(tmp_path):
    from hypogeum.dashboard_discovery import get_episode_discovery
    db = tmp_path / "t.db"
    initialize_schema(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, topic_text, status, created_at, updated_at) "
                 "VALUES ('t','ch','ep_1','x','consumed','x','x')")
    curve = [{"elapsedVideoTimeRatio": round((i + 1) / 20, 3), "audienceWatchRatio": 1.0 - i * 0.03} for i in range(20)]
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, window_days, views, avg_view_pct, "
                 "avg_watch_time_s, platform_video_id, source, imported_at, retention_curve_json) VALUES "
                 "('r1','ep_1','youtube','2026-10-12',28,300,0.6,24,'V1','api','x',?)", (json.dumps(curve),))
    conn.execute("INSERT INTO retention_moments (moment_id, channel_id, episode_id, video_id, kind, t0, t1, loss, video_views, beat_id, beat_index, "
                 "beats_total, signals_json, spoken_text, scene_id, visual_json, frame_path, created_at) VALUES "
                 "('V1:exit:0','ch','ep_1','V1','exit',4,5,0.2,300,'b2',1,9,'[]','Said line','s2','{}','/x/V1_exit_45.jpg','x')")
    ins = ("INSERT INTO youtube_traffic_insights (channel_id, snapshot_date, dimension, key, subkey, views, minutes_watched, avg_view_duration_s, "
           "avg_view_pct, window_days, created_at) VALUES ('ch','2026-10-12',?,?,?,?,0,10,?,89,'x')")
    conn.execute(ins, ("video_traffic", "V1", "SHORTS", 250, 60.0))
    conn.execute(ins, ("video_traffic", "V1", "YT_SEARCH", 50, 90.0))
    conn.execute(ins, ("video_search", "V1", "stonehenge mystery", 3, 96.0))
    conn.commit()
    conn.close()
    d = get_episode_discovery("ep_1", db_path=db)["videos"][0]
    assert d["video_id"] == "V1" and len(d["curve"]) > 5
    assert d["exits"][0]["frame"] == "V1_exit_45.jpg" and d["exits"][0]["loss_pts"] == 20
    assert d["found_by"][0]["label"] == "the Shorts feed" and round(d["found_by"][1]["share"], 2) == round(50 / 300, 2)
    assert d["searches"][0]["term"] == "stonehenge mystery"
