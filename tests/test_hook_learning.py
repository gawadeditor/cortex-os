from hypogeum import hook_learning as hl


def _rows(spec):
    out = []
    for pat, n, vp, hold in spec:
        for i in range(n):
            out.append({"hook_pattern": pat, "hook_text": f"{pat} line {i}", "avg_view_pct": vp + i * 0.001, "views": 100, "hold5": hold})
    return out


def test_aggregate_drops_patterns_without_enough_videos_and_none():
    st = hl.aggregate(_rows([("a", 3, 0.7, 0.9), ("b", 2, 0.4, 0.5), ("none", 5, 0.5, 0.5)]))
    assert set(st) == {"a"} and st["a"]["n"] == 3


def test_directive_needs_two_patterns_and_a_real_gap():
    assert hl.directive_text(hl.aggregate(_rows([("a", 3, 0.7, 0.9)]))) is None                    # one pattern
    assert hl.directive_text(hl.aggregate(_rows([("a", 3, 0.70, 0.9), ("b", 3, 0.68, 0.9)]))) is None  # no gap
    t = hl.directive_text(hl.aggregate(_rows([("accepted_fact_wrong", 4, 0.75, 0.92), ("impossible_number", 3, 0.50, 0.7)])))
    assert t and 'Prefer "accepted_fact_wrong"' in t and 'avoid "impossible_number"' in t and "measured comparison" in t


def test_early_hold_is_share_of_opening_audience_at_5s():
    curve = [{"elapsedVideoTimeRatio": round((i + 1) / 40, 4), "audienceWatchRatio": (1.2 if i < 5 else 0.6)} for i in range(40)]
    assert round(hl.early_hold(curve, 40.0), 2) == 1.0          # 5s is still before the drop (it happens at 6s)
    assert round(hl.early_hold(curve, 40.0, at_s=8.0), 2) == 0.5   # by 8s half the opening audience is gone (0.6 / 1.2)
    assert hl.early_hold(curve, 4.0) is None                    # shorter than the probe point


def test_traits_found_in_a_script_package(tmp_path):
    import json
    d = tmp_path / "ep"
    d.mkdir()
    (d / "script_package_ep.json").write_text(json.dumps({"hook_pattern": "withheld_knowledge",
                                                           "sections": [{"narration_beats": [{"beat_id": "b1", "text": "Nobody read it."}]}]}))
    t = hl.traits_for_episode(d)
    assert t == {"hook_pattern": "withheld_knowledge", "hook_text": "Nobody read it."}
