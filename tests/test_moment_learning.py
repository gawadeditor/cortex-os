import json
from pathlib import Path

from hypogeum.db import get_connection, initialize_schema
from hypogeum import moment_learning as ml


def _curve(vals):
    n = len(vals)
    return [{"elapsedVideoTimeRatio": round((i + 1) / n, 4), "audienceWatchRatio": v} for i, v in enumerate(vals)]


def _episode(root: Path, ep: str, texts):
    d = root / ep
    d.mkdir(parents=True)
    (d / f"narration_package_{ep}.json").write_text(json.dumps({"narration_segments": [
        {"beat_reference": f"b{i}", "duration_seconds": 4.0} for i in range(len(texts))]}))
    (d / f"script_package_{ep}.json").write_text(json.dumps({"sections": [{"narration_beats": [
        {"beat_id": f"b{i}", "text": t} for i, t in enumerate(texts)]}]}))
    (d / f"visual_package_{ep}.json").write_text(json.dumps({"scenes": [
        {"scene_id": f"s{i}", "narration_reference": f"b{i}", "narrative_signals": ["x"],
         "visual_spec": {"subject": f"subject {i}", "framing": "wide"}} for i in range(len(texts))]}))
    return d


def _seed(conn, ep, vid, views, curve, dur, channel="ch"):
    conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, topic_text, status, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (f"t_{ep}", channel, ep, "x", "consumed", "x", "x"))
    avp = 0.6
    conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, window_days, views, "
                 "avg_view_pct, avg_watch_time_s, platform_video_id, source, imported_at, retention_curve_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"r_{vid}", ep, "youtube", "2026-10-01", 28, views, avp, avp * dur, vid, "api", "x", json.dumps(curve)))


def test_learns_the_leaking_beat_and_writes_a_directive_with_real_examples(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    root = tmp_path / "eps"
    monkeypatch.setattr(ml, "_roots", lambda cid: [root])
    monkeypatch.setattr(ml, "FRAME_DIR", tmp_path / "frames")
    conn = get_connection(db)
    # 7 videos of 9 beats x 4s = 36s; beat index 1 (4-8s) always loses a lot, everything else is flat
    vals = []
    for i in range(36):
        sec = i + 1
        vals.append(1.0 if sec <= 4 else (1.0 - 0.3 * min((sec - 4) / 4, 1.0)))
    for n in range(7):
        ep = f"ep_{n}"
        _episode(root, ep, [f"hook {n}", f"explaining line {n}"] + [f"line {j}" for j in range(7)])
        _seed(conn, ep, f"V{n}", 100 + n, _curve(vals), 36.0)
    conn.commit()
    conn.close()

    out = ml.learn_channel("ch", db_path=db, frames=False)
    assert out["videos"] == 7 and out["moments"] > 0 and out["pattern_position"] == 2 and out["directive"]
    from hypogeum.channel_learning import get_active_directives
    d = get_active_directives("ch", dimension="retention_moments", db_path=db)
    assert len(d) == 1 and "beat 2" in d[0]["directive_text"] and "explaining line" in d[0]["directive_text"]
    # idempotent: a second pass with the same evidence does not write a new directive
    again = ml.learn_channel("ch", db_path=db, frames=False)
    assert again["directive"] is None
    assert len(get_active_directives("ch", dimension="retention_moments", db_path=db)) == 1


def test_too_few_videos_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    monkeypatch.setattr(ml, "_roots", lambda cid: [tmp_path / "eps"])
    out = ml.learn_channel("ch", db_path=db, frames=False)
    assert out["directive"] is None and out["videos"] == 0


def test_timeline_is_cached_and_used_when_the_episode_folder_is_unreachable(tmp_path, monkeypatch):
    """The archive (NAS) can be down; episodes are moved there after release. Learning must keep working
    from the cached timeline once the folder has been read once."""
    db = tmp_path / "t.db"
    initialize_schema(db)
    root = tmp_path / "eps"
    monkeypatch.setattr(ml, "FRAME_DIR", tmp_path / "frames")
    vals = [1.0] * 4 + [0.6] * 32
    conn = get_connection(db)
    for n in range(7):
        ep = f"ep_{n}"
        _episode(root, ep, [f"hook {n}", f"second {n}"] + [f"line {j}" for j in range(7)])
        _seed(conn, ep, f"V{n}", 100 + n, _curve(vals), 36.0)
    conn.commit()
    conn.close()

    monkeypatch.setattr(ml, "_roots", lambda cid: [root])
    first = ml.learn_channel("ch", db_path=db, frames=False)
    assert first["videos"] == 7

    # the archive goes away
    monkeypatch.setattr(ml, "_roots", lambda cid: [tmp_path / "unreachable"])
    second = ml.learn_channel("ch", db_path=db, frames=False)
    assert second["videos"] == 7 and second["moments"] > 0
