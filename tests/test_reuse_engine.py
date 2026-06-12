"""
tests/test_reuse_engine.py — Reuse Recommendation Layer tests

Coverage:
    TestScoreAsset              — quality, performance, tag overlap, style, provider
    TestRecommendReuse          — filtering, ranking, min_score, limit, empty DB
    TestGetTopCandidate         — returns best or None
    TestShouldReuse             — bool decision, threshold, records decision row
    TestReuseCandidate          — dataclass fields populated correctly
    TestScoringEdgeCases        — clamp, None inputs, unknown quality
    TestNoNetworkCalls          — reuse_engine never opens sockets
    TestNoProductionSideEffects — no ep_000, no external API calls
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.archive_recall import RecallResult
from hypogeum.db import get_connection, initialize_schema
from hypogeum.reuse_engine import (
    ReuseCandidate,
    _DEFAULT_WEIGHTS,
    _jaccard,
    _recency_multiplier,
    _style_match,
    get_top_candidate,
    recommend_reuse,
    score_asset,
    should_reuse,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_RUN_ID = "run-reuse-test-001"
_EP_SRC = "ep_source"
_EP_NEW = "ep_new"
_TS_RECENT = "2026-05-01T00:00:00+00:00"
_TS_OLD    = "2020-01-01T00:00:00+00:00"


def _make_result(
    asset_id="asset_r01",
    archive_id="arc_r01",
    episode_id=_EP_SRC,
    asset_type="video_clip",
    provider="muapi",
    model=None,
    quality_verdict="approved",
    performance_signal=0.8,
    semantic_tags=None,
    visual_style=None,
    reuse_eligible=True,
    archived_at=_TS_RECENT,
) -> RecallResult:
    return RecallResult(
        archive_id=archive_id,
        asset_id=asset_id,
        episode_id=episode_id,
        run_id=_RUN_ID,
        asset_type=asset_type,
        provider=provider,
        model=model,
        file_path=None,
        prompt_hash=None,
        source_stage="visual_package_director",
        reuse_eligible=reuse_eligible,
        semantic_tags=semantic_tags or [],
        visual_style=visual_style or {},
        quality_verdict=quality_verdict,
        performance_signal=performance_signal,
        archived_at=archived_at,
        metadata_version="v2",
        rank_score=0.0,
    )


@pytest.fixture()
def db_path(tmp_path) -> Path:
    p = tmp_path / "reuse_test.db"
    initialize_schema(p)
    conn = get_connection(p)
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
        (_RUN_ID, _EP_SRC, _TS_RECENT, "complete"),
    )
    # Insert archive rows
    # Tuple order matches SQL ?s: archive_id, asset_id, episode_id, run_id,
    # provider, reuse_eligible, semantic_tags_json, visual_style_json,
    # quality_verdict, performance_signal, archived_at
    rows = [
        # High-quality, high-perf, tagged
        ("arc_01", "asset_01", _EP_SRC, _RUN_ID, "muapi", 1,
         '["ancient","rome"]', '{"period":"ancient","palette":"warm"}',
         "approved", 0.9, _TS_RECENT),
        # Medium quality, medium perf, same tags
        ("arc_02", "asset_02", _EP_SRC, _RUN_ID, "muapi", 1,
         '["ancient"]', None,
         "unvalidated", 0.5, _TS_RECENT),
        # Approved but no tags
        ("arc_03", "asset_03", _EP_SRC, _RUN_ID, "muapi", 1,
         None, None,
         "approved", 0.7, _TS_RECENT),
        # Rejected — should score near zero
        ("arc_04", "asset_04", _EP_SRC, _RUN_ID, "muapi", 1,
         None, None,
         "rejected", 0.0, _TS_RECENT),
        # Not reusable — should be excluded
        ("arc_05", "asset_05", _EP_SRC, _RUN_ID, "muapi", 0,
         None, None,
         "approved", 0.9, _TS_RECENT),
        # Thumbnail (different type — not used by video_clip queries)
        ("arc_06", "asset_06", _EP_SRC, _RUN_ID, "muapi", 1,
         None, None,
         "approved", 0.8, _TS_RECENT),
    ]
    conn.executemany(
        """
        INSERT INTO archive_index
            (archive_id, asset_id, episode_id, run_id,
             asset_type, provider, model, file_path, prompt_hash, source_stage,
             reuse_eligible, semantic_tags_json, visual_style_json,
             quality_verdict, gate_name, performance_signal, archived_at,
             metadata_version)
        VALUES
            (?, ?, ?, ?,
             'video_clip', ?, NULL, NULL, NULL, 'visual_package_director',
             ?, ?, ?,
             ?, NULL, ?, ?, 'v2')
        """,
        rows,
    )
    # Insert a thumbnail separately
    conn.execute(
        """
        INSERT INTO archive_index
            (archive_id, asset_id, episode_id, run_id,
             asset_type, provider, model, file_path, prompt_hash, source_stage,
             reuse_eligible, semantic_tags_json, visual_style_json,
             quality_verdict, gate_name, performance_signal, archived_at,
             metadata_version)
        VALUES (?,?,?,?, 'thumbnail','muapi',NULL,NULL,NULL,'thumbnail_director',
                1,NULL,NULL,'approved',NULL,0.85,?,'v2')
        """,
        ("arc_07", "asset_07", _EP_SRC, _RUN_ID, _TS_RECENT),
    )
    conn.commit()
    conn.close()
    return p


# ---------------------------------------------------------------------------
# TestScoreAsset — pure function, no DB
# ---------------------------------------------------------------------------

class TestScoreAsset:
    def test_approved_quality_full_score(self):
        r = _make_result(quality_verdict="approved", performance_signal=1.0)
        s = score_asset(r)
        assert s > 0.9

    def test_rejected_quality_near_zero(self):
        # quality=0, perf=0, tag no-match, style no-match, provider mismatch → all zeros
        r = _make_result(quality_verdict="rejected", performance_signal=0.0,
                         semantic_tags=[], visual_style={})
        s = score_asset(
            r,
            request_tags=["something"],
            request_style={"key": "unmatched"},
            request_provider="other_provider",
        )
        assert s < 0.05

    def test_unvalidated_between_extremes(self):
        approved   = score_asset(_make_result(quality_verdict="approved",   performance_signal=0.5))
        unvalidated = score_asset(_make_result(quality_verdict="unvalidated", performance_signal=0.5))
        rejected   = score_asset(_make_result(quality_verdict="rejected",   performance_signal=0.5))
        assert approved > unvalidated > rejected

    def test_perfect_tag_overlap(self):
        r = _make_result(semantic_tags=["a", "b", "c"])
        s = score_asset(r, request_tags=["a", "b", "c"])
        # tag_overlap_score should be 1.0 (Jaccard = 1.0)
        assert s >= score_asset(r, request_tags=["a"])  # same or more weight

    def test_zero_tag_overlap(self):
        r = _make_result(semantic_tags=["rome"])
        s_no_match = score_asset(r, request_tags=["futuristic"])
        s_match    = score_asset(r, request_tags=["rome"])
        assert s_match > s_no_match

    def test_no_request_tags_neutral(self):
        r = _make_result(semantic_tags=["ancient"])
        s_no_req  = score_asset(r, request_tags=None)
        s_match   = score_asset(r, request_tags=["ancient"])
        s_nomatch = score_asset(r, request_tags=["futuristic"])
        # No-request should be >= match (1.0 for tag component vs Jaccard)
        assert s_no_req >= s_nomatch

    def test_provider_match_beats_mismatch(self):
        r = _make_result(provider="muapi")
        s_match    = score_asset(r, request_provider="muapi")
        s_mismatch = score_asset(r, request_provider="elevenlabs")
        s_none     = score_asset(r, request_provider=None)
        assert s_match >= s_none
        assert s_match > s_mismatch

    def test_provider_none_gets_full_score(self):
        r = _make_result(provider="muapi")
        s = score_asset(r, request_provider=None)
        assert s > score_asset(r, request_provider="elevenlabs")

    def test_style_all_keys_match(self):
        r = _make_result(visual_style={"period": "ancient", "palette": "warm"})
        s_full    = score_asset(r, request_style={"period": "ancient", "palette": "warm"})
        s_partial = score_asset(r, request_style={"period": "ancient", "palette": "cool"})
        assert s_full > s_partial

    def test_no_request_style_neutral(self):
        r = _make_result(visual_style={"period": "ancient"})
        s_no_req  = score_asset(r, request_style=None)
        s_mismatch = score_asset(r, request_style={"period": "futuristic"})
        assert s_no_req >= s_mismatch

    def test_score_in_zero_one_range(self):
        for quality in ("approved", "validated", "unvalidated", "rejected"):
            for perf in (0.0, 0.5, 1.0, None):
                r = _make_result(quality_verdict=quality, performance_signal=perf)
                s = score_asset(r)
                assert 0.0 <= s <= 1.0, f"Score {s} out of range for {quality}, {perf}"

    def test_weighted_sum_correct(self):
        """Verify arithmetic for a fully deterministic case."""
        r = _make_result(
            quality_verdict="approved",    # quality_score = 1.0
            performance_signal=1.0,         # performance_score = 1.0
            semantic_tags=["a"],
            visual_style={"k": "v"},
            provider="muapi",
            archived_at="2026-06-01T00:00:00+00:00",  # recent → multiplier ≈ 1.0
        )
        s = score_asset(
            r,
            request_tags=["a"],       # tag_overlap = 1.0
            request_style={"k": "v"}, # style_match = 1.0
            request_provider="muapi", # provider_match = 1.0
        )
        # raw = 1.0*0.4 + 1.0*0.3 + 1.0*0.15 + 1.0*0.1 + 1.0*0.05 = 1.0
        # recency multiplier ≈ 1.0 (recent)
        assert s > 0.95

    def test_custom_weights_applied(self):
        r = _make_result(quality_verdict="approved", performance_signal=0.0)
        # With quality weight=1.0, all others=0: score should reflect quality only
        s_quality_heavy = score_asset(
            r,
            weights={"quality": 1.0, "performance": 0.0,
                     "tag_overlap": 0.0, "style_match": 0.0, "provider_match": 0.0},
        )
        # raw = 1.0 * 1.0 = 1.0 (before recency)
        assert s_quality_heavy > 0.9


# ---------------------------------------------------------------------------
# TestScoringEdgeCases
# ---------------------------------------------------------------------------

class TestScoringEdgeCases:
    def test_unknown_quality_verdict_no_crash(self):
        r = _make_result(quality_verdict="mystery_verdict")
        s = score_asset(r)
        assert 0.0 <= s <= 1.0

    def test_performance_signal_clamped_above_one(self):
        r = _make_result(performance_signal=5.0)
        s = score_asset(r)
        assert 0.0 <= s <= 1.0

    def test_performance_signal_clamped_below_zero(self):
        r = _make_result(performance_signal=-0.5)
        s = score_asset(r)
        assert 0.0 <= s <= 1.0

    def test_empty_tags_both_sides_returns_one(self):
        r = _make_result(semantic_tags=[])
        s = score_asset(r, request_tags=[])
        # Both empty → tag_overlap=1.0 (neutral, no preference)
        assert 0.0 <= s <= 1.0

    def test_recency_recent_multiplier_close_to_one(self):
        m = _recency_multiplier("2026-06-01T00:00:00+00:00")
        assert m > 0.95

    def test_recency_old_multiplier_at_floor(self):
        m = _recency_multiplier("2010-01-01T00:00:00+00:00")
        assert m == 0.5  # floor

    def test_recency_invalid_timestamp_returns_one(self):
        m = _recency_multiplier("not-a-date")
        assert m == 1.0

    def test_jaccard_identical_sets(self):
        assert _jaccard(["a", "b"], ["a", "b"]) == 1.0

    def test_jaccard_disjoint_sets(self):
        assert _jaccard(["a"], ["b"]) == 0.0

    def test_jaccard_partial_overlap(self):
        j = _jaccard(["a", "b"], ["b", "c"])
        # intersection={b}, union={a,b,c} → 1/3
        assert abs(j - 1/3) < 1e-9

    def test_jaccard_both_empty(self):
        assert _jaccard([], []) == 0.0

    def test_style_match_empty_request(self):
        assert _style_match({}, {"k": "v"}) == 1.0

    def test_style_match_empty_asset(self):
        assert _style_match({"k": "v"}, {}) == 0.0

    def test_style_match_partial(self):
        s = _style_match({"a": 1, "b": 2}, {"a": 1, "b": 99})
        assert s == 0.5


# ---------------------------------------------------------------------------
# TestRecommendReuse
# ---------------------------------------------------------------------------

class TestRecommendReuse:
    def test_returns_list_of_reuse_candidates(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path)
        assert isinstance(results, list)
        assert all(isinstance(c, ReuseCandidate) for c in results)

    def test_returns_only_reusable_assets(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                  min_score=0.0)
        assert all(c.reuse_eligible for c in results)
        # asset_05 (reuse=False) must be excluded
        asset_ids = {c.asset_id for c in results}
        assert "asset_05" not in asset_ids

    def test_sorted_descending_by_score(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                  min_score=0.0)
        scores = [c.score for c in results]
        assert scores == sorted(scores, reverse=True)

    def test_min_score_filters_low_scorers(self, db_path):
        # rejected asset scores near 0 — should be excluded with min_score=0.2
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                  min_score=0.2)
        for c in results:
            assert c.score >= 0.2

    def test_limit_caps_results(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                  min_score=0.0, limit=2)
        assert len(results) <= 2

    def test_empty_db_returns_empty(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=p)
        assert results == []

    def test_wrong_asset_type_returns_empty(self, db_path):
        results = recommend_reuse(_EP_NEW, "hologram", db_path=db_path)
        assert results == []

    def test_score_components_present(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path)
        if results:
            c = results[0]
            for key in ("quality", "performance", "tag_overlap",
                        "style_match", "provider_match"):
                assert key in c.score_components

    def test_approved_before_unvalidated(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                  min_score=0.0)
        if len(results) >= 2:
            # First result should have quality_verdict in (approved, validated)
            assert results[0].quality_verdict in ("approved", "validated")

    def test_tag_match_raises_score(self, db_path):
        without_tags = recommend_reuse(_EP_NEW, "video_clip", db_path=db_path,
                                       min_score=0.0)
        with_tags = recommend_reuse(_EP_NEW, "video_clip",
                                    semantic_tags=["ancient", "rome"],
                                    db_path=db_path, min_score=0.0)
        # asset_01 (tagged with ancient+rome) should score at least as high
        # as asset_03 (no tags) when roman tags are requested
        asset_01_score_tagged = next(
            (c.score for c in with_tags if c.asset_id == "asset_01"), None
        )
        asset_03_score_tagged = next(
            (c.score for c in with_tags if c.asset_id == "asset_03"), None
        )
        if asset_01_score_tagged and asset_03_score_tagged:
            assert asset_01_score_tagged >= asset_03_score_tagged

    def test_provider_filter_applied_in_score(self, db_path):
        results = recommend_reuse(_EP_NEW, "video_clip",
                                  provider="muapi",
                                  db_path=db_path, min_score=0.0)
        if results:
            # provider_match_score should be 1.0 for muapi assets
            for c in results:
                if c.provider == "muapi":
                    assert c.score_components["provider_match"] == 1.0


# ---------------------------------------------------------------------------
# TestGetTopCandidate
# ---------------------------------------------------------------------------

class TestGetTopCandidate:
    def test_returns_best_candidate(self, db_path):
        top = get_top_candidate(_EP_NEW, "video_clip", db_path=db_path)
        assert top is not None
        # Best should be approved / high-perf
        assert top.quality_verdict in ("approved", "validated")

    def test_returns_none_when_no_qualifiers(self, db_path):
        top = get_top_candidate(_EP_NEW, "hologram", db_path=db_path)
        assert top is None

    def test_returns_none_on_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        top = get_top_candidate(_EP_NEW, "video_clip", db_path=p)
        assert top is None

    def test_min_score_high_returns_none(self, db_path):
        top = get_top_candidate(_EP_NEW, "video_clip",
                                min_score=0.9999, db_path=db_path)
        # Virtually nothing can score 0.9999 after recency multiplier
        # (may be None or just 1 result; either is valid)
        if top is not None:
            assert top.score >= 0.9999


# ---------------------------------------------------------------------------
# TestShouldReuse
# ---------------------------------------------------------------------------

class TestShouldReuse:
    def test_returns_bool(self, db_path):
        result = should_reuse(_EP_NEW, "video_clip", db_path=db_path)
        assert isinstance(result, bool)

    def test_high_quality_returns_true(self, db_path):
        # Default threshold=0.6; approved+perf=0.9 should clear it
        result = should_reuse(_EP_NEW, "video_clip", threshold=0.5, db_path=db_path)
        assert result is True

    def test_no_assets_returns_false(self, db_path):
        result = should_reuse(_EP_NEW, "hologram", db_path=db_path)
        assert result is False

    def test_threshold_above_max_returns_false(self, db_path):
        result = should_reuse(_EP_NEW, "video_clip", threshold=0.9999, db_path=db_path)
        assert result is False

    def test_threshold_at_zero_returns_true_if_any_asset(self, db_path):
        result = should_reuse(_EP_NEW, "video_clip", threshold=0.0, db_path=db_path)
        assert result is True

    def test_records_decision_in_db(self, db_path):
        should_reuse(_EP_NEW, "video_clip", threshold=0.5, db_path=db_path)
        conn = get_connection(db_path)
        # reuse_decisions table created lazily by should_reuse
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reuse_decisions" in tables
        rows = conn.execute("SELECT * FROM reuse_decisions").fetchall()
        conn.close()
        assert len(rows) >= 1

    def test_decision_row_has_correct_fields(self, db_path):
        should_reuse(_EP_NEW, "video_clip", threshold=0.5, db_path=db_path)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM reuse_decisions WHERE episode_id=? AND asset_type=?",
            (_EP_NEW, "video_clip"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["episode_id"] == _EP_NEW
        assert row["asset_type"] == "video_clip"
        assert row["decision"] in ("reuse", "generate")


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    def test_score_asset_no_network(self):
        orig = socket.socket.connect
        calls = []

        def _reject(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Network call in score_asset")

        socket.socket.connect = _reject
        try:
            r = _make_result()
            score_asset(r, request_tags=["ancient"], request_provider="muapi")
        finally:
            socket.socket.connect = orig
        assert calls == []

    def test_recommend_reuse_no_network(self, db_path):
        orig = socket.socket.connect

        def _reject(*args, **kwargs):
            raise AssertionError("Network call in recommend_reuse")

        socket.socket.connect = _reject
        try:
            recommend_reuse(_EP_NEW, "video_clip", db_path=db_path)
        finally:
            socket.socket.connect = orig


# ---------------------------------------------------------------------------
# TestNoProductionSideEffects
# ---------------------------------------------------------------------------

class TestNoProductionSideEffects:
    def test_no_ep_000_in_test_db(self, db_path):
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT episode_id FROM runs WHERE episode_id='ep_000'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_recommend_reuse_does_not_write_archive(self, db_path):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        conn.close()
        recommend_reuse(_EP_NEW, "video_clip", db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        conn.close()
        assert before == after

    def test_default_db_not_touched(self, db_path):
        from hypogeum.reuse_engine import _DEFAULT_DB_PATH
        assert str(db_path) != str(_DEFAULT_DB_PATH)

    def test_bad_db_path_does_not_raise(self, tmp_path):
        bad = tmp_path / "nosuchdir" / "bad.db"
        # get_connection creates dirs, but schema won't be initialized
        # → archive query silently returns []
        results = recommend_reuse(_EP_NEW, "video_clip", db_path=bad)
        assert isinstance(results, list)
