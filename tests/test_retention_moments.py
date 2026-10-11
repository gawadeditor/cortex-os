import json
from hypogeum.retention_moments import aggregate, beat_at, exit_events, moments_for_video, timeline_for_episode


def _curve(vals):
    n = len(vals)
    return [{"elapsedVideoTimeRatio": round((i + 1) / n, 4), "audienceWatchRatio": v} for i, v in enumerate(vals)]


def test_exit_events_finds_the_cliff_and_reports_it_once():
    vals = [1.0] * 10 + [0.5] * 10          # a cliff at 50% of a 20s video
    ev = exit_events(_curve(vals), 20.0)
    exits = [e for e in ev if e["kind"] == "exit"]
    assert len(exits) == 1
    assert 8 <= exits[0]["t"] <= 12
    assert exits[0]["loss"] >= 0.4


def test_flat_curve_has_no_exit():
    assert exit_events(_curve([0.9] * 30), 30.0) == []


def test_timeline_binds_beats_scenes_and_text_in_playback_order(tmp_path):
    (tmp_path / "narration_package_e.json").write_text(json.dumps({"narration_segments": [
        {"beat_reference": "b1", "duration_seconds": 3.0}, {"beat_reference": "b2", "duration_seconds": 4.0}]}))
    (tmp_path / "script_package_e.json").write_text(json.dumps({"sections": [{"narration_beats": [
        {"beat_id": "b1", "text": "Hook line."}, {"beat_id": "b2", "text": "Second line."}]}]}))
    (tmp_path / "visual_package_e.json").write_text(json.dumps({"scenes": [
        {"scene_id": "s01", "narration_reference": "b1", "narrative_signals": ["hook"], "visual_spec": {"subject": "a door"}},
        {"scene_id": "s02", "narration_reference": "b2", "narrative_signals": ["reframe"], "visual_spec": {"subject": "a room"}}]}))
    tl = timeline_for_episode(tmp_path)
    assert [b["beat_id"] for b in tl] == ["b1", "b2"]
    assert tl[1]["start"] == 3.0 and tl[1]["end"] == 7.0
    assert beat_at(tl, 4.5)["text"] == "Second line."
    ms = moments_for_video(_curve([1.0] * 10 + [0.5] * 10), 7.0, tl)
    assert any(m["kind"] == "exit" and m["scene_id"] for m in ms)


def test_aggregate_counts_worst_exit_per_video():
    tl = [{"beat_id": "b1", "start": 0, "end": 3, "text": "x", "scene_id": "s1", "signals": ["hook"]},
          {"beat_id": "b2", "start": 3, "end": 8, "text": "y", "scene_id": "s2", "signals": ["reframe"]}]
    m = moments_for_video(_curve([1.0] * 5 + [0.4] * 15), 8.0, tl)
    agg = aggregate({"v1": m})
    assert agg["videos"] == 1 and agg["worst_exit_in_first_quarter"] in (0.0, 1.0)
