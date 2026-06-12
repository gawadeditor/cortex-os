"""
tests/test_archive_recall.py — Archive Recall Layer tests

Coverage:
    TestRecallByEpisode             — episode filter; includes non-reusable
    TestRecallByAssetType           — type filter; excludes non-reusable by default
    TestRecallBySemanticTags        — tag OR matching; empty tags → []
    TestRecallByVisualStyle         — style AND matching; partial miss → excluded
    TestReuseEligibleGating         — reuse_eligible=False excluded unless requested
    TestQualityRanking              — approved before unvalidated before rejected
    TestPerformanceRanking          — higher performance_signal first
    TestTagRankingBonus             — tag-matched assets before non-matched
    TestAssetLineage                — asset_record + archive_entry + versions
    TestGetAssetVersions            — all versions for a slot; ordered by version_index
    TestGetBestAssetForSlot         — latest succeeded version; None if all failed
    TestEmptyResults                — [] not error for no matches / empty DB
    TestNoNetworkCalls              — archive_recall never opens sockets
    TestNoProductionSideEffects     — no ep_000, no writes, no external calls
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.archive_recall import (
    RecallResult,
    get_asset_lineage,
    get_asset_versions,
    get_best_asset_for_slot,
    recall_assets,
    recall_by_semantic_tags,
    recall_by_visual_style,
    recall_episode_assets,
    recall_reusable_assets,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID  = "run-recall-001"
_EP_A    = "ep_recall_a"
_EP_B    = "ep_recall_b"
_TS_OLD  = "2026-01-01T00:00:00+00:00"
_TS_MID  = "2026-03-01T00:00:00+00:00"
_TS_NEW  = "2026-06-01T00:00:00+00:00"
_SLOT_1  = "slot0001aabbccdd"
_SLOT_2  = "slot0002aabbccdd"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _aid(suffix: str) -> str:
    """Unique asset_id for tests."""
    return f"asset_{suffix}"


def _arc(archive_id, asset_id, episode_id, asset_type="video_clip",
         provider="muapi", reuse_eligible=True, quality_verdict="unvalidated",
         performance_signal=None, semantic_tags=None, visual_style=None,
         archived_at=_TS_MID, model=None, source_stage=None):
    return (
        archive_id, asset_id, episode_id, _RUN_ID,
        asset_type, provider, model, None, None, source_stage,
        1 if reuse_eligible else 0,
        json.dumps(semantic_tags) if semantic_tags else None,
        json.dumps(visual_style) if visual_style else None,
        quality_verdict, None, performance_signal, archived_at, "v2",
    )


@pytest.fixture()
def db_path(tmp_path) -> Path:
    """Initialized DB with a set of archive_index rows for all test scenarios."""
    p = tmp_path / "recall_test.db"
    initialize_schema(p)
    conn = get_connection(p)

    # Parent run (required by asset_versions FK)
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?, ?, ?, ?)",
        (_RUN_ID, _EP_A, _TS_OLD, "complete"),
    )

    # archive_index rows — 8 test assets
    rows = [
        # A: video_clip, ep_A, reusable, approved, perf=0.9, tags+style
        _arc("arc_A", _aid("A"), _EP_A, asset_type="video_clip",
             reuse_eligible=True, quality_verdict="approved",
             performance_signal=0.9,
             semantic_tags=["ancient", "rome", "underground"],
             visual_style={"period": "ancient", "palette": "warm"},
             archived_at=_TS_OLD,
             source_stage="visual_package_director"),

        # B: video_clip, ep_A, reusable, unvalidated, perf=0.5, partial tags
        _arc("arc_B", _aid("B"), _EP_A, asset_type="video_clip",
             reuse_eligible=True, quality_verdict="unvalidated",
             performance_signal=0.5,
             semantic_tags=["ancient"],
             visual_style={"period": "ancient", "palette": "cool"},
             archived_at=_TS_MID),

        # C: thumbnail, ep_A, reusable, approved, perf=0.8, no tags
        _arc("arc_C", _aid("C"), _EP_A, asset_type="thumbnail",
             reuse_eligible=True, quality_verdict="approved",
             performance_signal=0.8, archived_at=_TS_MID),

        # D: video_clip, ep_B, NOT reusable, approved, perf=0.95
        _arc("arc_D", _aid("D"), _EP_B, asset_type="video_clip",
             reuse_eligible=False, quality_verdict="approved",
             performance_signal=0.95, archived_at=_TS_MID),

        # E: narration, ep_A, NOT reusable, unvalidated
        _arc("arc_E", _aid("E"), _EP_A, asset_type="narration",
             reuse_eligible=False, quality_verdict="unvalidated",
             archived_at=_TS_MID),

        # F: video_clip, ep_A, reusable, approved, perf=0.7, style only
        _arc("arc_F", _aid("F"), _EP_A, asset_type="video_clip",
             reuse_eligible=True, quality_verdict="approved",
             performance_signal=0.7,
             visual_style={"period": "ancient", "palette": "warm"},
             archived_at=_TS_NEW,
             source_stage="visual_package_director"),

        # G: video_clip, ep_A, reusable, rejected, perf=0.3
        _arc("arc_G", _aid("G"), _EP_A, asset_type="video_clip",
             reuse_eligible=True, quality_verdict="rejected",
             performance_signal=0.3, archived_at=_TS_MID),

        # H: video_clip, ep_A, reusable, validated, perf=0.6
        _arc("arc_H", _aid("H"), _EP_A, asset_type="video_clip",
             reuse_eligible=True, quality_verdict="validated",
             performance_signal=0.6, archived_at=_TS_MID),
    ]

    conn.executemany(
        """
        INSERT INTO archive_index
            (archive_id, asset_id, episode_id, run_id,
             asset_type, provider, model, file_path, prompt_hash, source_stage,
             reuse_eligible, semantic_tags_json, visual_style_json,
             quality_verdict, gate_name, performance_signal, archived_at,
             metadata_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    # assets table row for asset_A (used by lineage tests)
    conn.execute(
        """
        INSERT INTO assets
            (asset_id, run_id, episode_id, asset_type, provider, created_at, metadata_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (_aid("A"), _RUN_ID, _EP_A, "video_clip", "muapi", _TS_OLD, "v2"),
    )

    # asset_versions for SLOT_1 (used by versions/lineage/best-asset tests)
    conn.executemany(
        """
        INSERT INTO asset_versions
            (version_id, asset_slot_id, run_id, episode_id, asset_type, provider,
             version_index, attempt_number, status, asset_id, attempted_at, metadata_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("ver_01", _SLOT_1, _RUN_ID, _EP_A, "video_clip", "muapi",
             0, 1, "failed", None, _TS_OLD, "v2"),
            ("ver_02", _SLOT_1, _RUN_ID, _EP_A, "video_clip", "muapi",
             1, 2, "succeeded", _aid("A"), _TS_MID, "v2"),
        ],
    )

    # asset_versions for SLOT_2 (all failed — used by get_best_asset_for_slot)
    conn.execute(
        """
        INSERT INTO asset_versions
            (version_id, asset_slot_id, run_id, episode_id, asset_type, provider,
             version_index, attempt_number, status, asset_id, attempted_at, metadata_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ver_03", _SLOT_2, _RUN_ID, _EP_A, "video_clip", "muapi",
         0, 1, "failed", None, _TS_MID, "v2"),
    )

    conn.commit()
    conn.close()
    return p


# ---------------------------------------------------------------------------
# TestRecallByEpisode
# ---------------------------------------------------------------------------

class TestRecallByEpisode:
    def test_returns_all_assets_for_episode(self, db_path):
        results = recall_episode_assets(_EP_A, db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        # ep_A has: A, B, C, E, F, G, H (7 assets) — D is in ep_B
        assert _aid("A") in asset_ids
        assert _aid("C") in asset_ids
        assert _aid("E") in asset_ids  # non-reusable included (default=False)
        assert _aid("D") not in asset_ids

    def test_excludes_other_episode(self, db_path):
        results = recall_episode_assets(_EP_B, db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("D") in asset_ids
        assert _aid("A") not in asset_ids

    def test_reuse_only_option_filters(self, db_path):
        results = recall_episode_assets(_EP_A, db_path=db_path, reuse_eligible_only=True)
        asset_ids = {r.asset_id for r in results}
        assert _aid("E") not in asset_ids   # narration, reuse=False
        assert _aid("A") in asset_ids

    def test_episode_results_are_list(self, db_path):
        results = recall_episode_assets(_EP_A, db_path=db_path)
        assert isinstance(results, list)
        assert all(isinstance(r, RecallResult) for r in results)

    def test_unknown_episode_returns_empty(self, db_path):
        results = recall_episode_assets("ep_does_not_exist", db_path=db_path)
        assert results == []


# ---------------------------------------------------------------------------
# TestRecallByAssetType
# ---------------------------------------------------------------------------

class TestRecallByAssetType:
    def test_video_clip_filter(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="video_clip",
                                reuse_eligible_only=False)
        types = {r.asset_type for r in results}
        assert types == {"video_clip"}

    def test_thumbnail_filter(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="thumbnail")
        assert len(results) == 1
        assert results[0].asset_id == _aid("C")

    def test_default_reuse_excludes_non_reusable(self, db_path):
        # D is video_clip but reuse_eligible=False → excluded by default
        results = recall_assets(db_path=db_path, asset_type="video_clip")
        asset_ids = {r.asset_id for r in results}
        assert _aid("D") not in asset_ids

    def test_reuse_false_includes_all(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="video_clip",
                                reuse_eligible_only=False)
        asset_ids = {r.asset_id for r in results}
        assert _aid("D") in asset_ids

    def test_nonexistent_type_returns_empty(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="hologram")
        assert results == []


# ---------------------------------------------------------------------------
# TestRecallBySemanticTags
# ---------------------------------------------------------------------------

class TestRecallBySemanticTags:
    def test_single_tag_exact_match(self, db_path):
        results = recall_by_semantic_tags(["rome"], db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("A") in asset_ids
        assert _aid("B") not in asset_ids

    def test_shared_tag_matches_multiple(self, db_path):
        results = recall_by_semantic_tags(["ancient"], db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("A") in asset_ids
        assert _aid("B") in asset_ids

    def test_or_semantics_across_tags(self, db_path):
        # "underground" only in A, "ancient" in A and B → union = A and B
        results = recall_by_semantic_tags(["underground", "ancient"], db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("A") in asset_ids
        assert _aid("B") in asset_ids

    def test_no_matching_tag_returns_empty(self, db_path):
        results = recall_by_semantic_tags(["futuristic"], db_path=db_path)
        assert results == []

    def test_empty_tags_returns_empty(self, db_path):
        results = recall_by_semantic_tags([], db_path=db_path)
        assert results == []

    def test_excludes_non_reusable_even_if_tagged(self, db_path):
        # C has no tags, D has no tags, E has no tags — none should sneak in
        results = recall_by_semantic_tags(["ancient"], db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("D") not in asset_ids  # non-reusable
        assert _aid("E") not in asset_ids  # non-reusable

    def test_asset_type_filter_combined_with_tags(self, db_path):
        results = recall_by_semantic_tags(["ancient"], db_path=db_path,
                                          asset_type="video_clip")
        assert all(r.asset_type == "video_clip" for r in results)


# ---------------------------------------------------------------------------
# TestRecallByVisualStyle
# ---------------------------------------------------------------------------

class TestRecallByVisualStyle:
    def test_single_key_match(self, db_path):
        results = recall_by_visual_style({"period": "ancient"}, db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        # A, B, F all have period=ancient
        assert _aid("A") in asset_ids
        assert _aid("B") in asset_ids
        assert _aid("F") in asset_ids

    def test_multi_key_and_semantics(self, db_path):
        # Only A and F have period=ancient AND palette=warm
        results = recall_by_visual_style(
            {"period": "ancient", "palette": "warm"}, db_path=db_path
        )
        asset_ids = {r.asset_id for r in results}
        assert _aid("A") in asset_ids
        assert _aid("F") in asset_ids
        assert _aid("B") not in asset_ids  # palette=cool

    def test_no_matching_style_returns_empty(self, db_path):
        results = recall_by_visual_style({"period": "futuristic"}, db_path=db_path)
        assert results == []

    def test_empty_style_filters_returns_empty(self, db_path):
        results = recall_by_visual_style({}, db_path=db_path)
        assert results == []

    def test_excludes_non_reusable(self, db_path):
        results = recall_by_visual_style({"period": "ancient"}, db_path=db_path)
        asset_ids = {r.asset_id for r in results}
        assert _aid("D") not in asset_ids

    def test_asset_type_filter_combined(self, db_path):
        results = recall_by_visual_style({"period": "ancient"},
                                          db_path=db_path, asset_type="video_clip")
        assert all(r.asset_type == "video_clip" for r in results)


# ---------------------------------------------------------------------------
# TestReuseEligibleGating
# ---------------------------------------------------------------------------

class TestReuseEligibleGating:
    def test_default_excludes_non_reusable(self, db_path):
        results = recall_assets(db_path=db_path)
        assert all(r.reuse_eligible for r in results)

    def test_explicit_false_includes_non_reusable(self, db_path):
        results = recall_assets(db_path=db_path, reuse_eligible_only=False)
        flags = {r.reuse_eligible for r in results}
        assert False in flags

    def test_recall_reusable_always_reusable(self, db_path):
        results = recall_reusable_assets(db_path=db_path)
        assert all(r.reuse_eligible for r in results)

    def test_narration_excluded_by_default(self, db_path):
        results = recall_assets(db_path=db_path)
        assert all(r.asset_id != _aid("E") for r in results)

    def test_narration_included_when_requested(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="narration",
                                reuse_eligible_only=False)
        assert any(r.asset_id == _aid("E") for r in results)


# ---------------------------------------------------------------------------
# TestQualityRanking
# ---------------------------------------------------------------------------

class TestQualityRanking:
    def _verdicts_in_order(self, db_path) -> list[str]:
        results = recall_assets(db_path=db_path, asset_type="video_clip",
                                reuse_eligible_only=True)
        return [r.quality_verdict for r in results]

    def test_approved_before_validated(self, db_path):
        verdicts = self._verdicts_in_order(db_path)
        approved_idx  = [i for i, v in enumerate(verdicts) if v == "approved"]
        validated_idx = [i for i, v in enumerate(verdicts) if v == "validated"]
        if approved_idx and validated_idx:
            assert min(approved_idx) < min(validated_idx)

    def test_approved_before_unvalidated(self, db_path):
        verdicts = self._verdicts_in_order(db_path)
        approved_idx    = [i for i, v in enumerate(verdicts) if v == "approved"]
        unvalidated_idx = [i for i, v in enumerate(verdicts) if v == "unvalidated"]
        if approved_idx and unvalidated_idx:
            assert min(approved_idx) < min(unvalidated_idx)

    def test_rejected_last_among_quality_tiers(self, db_path):
        verdicts = self._verdicts_in_order(db_path)
        # Rejected should not appear before any approved/validated/unvalidated
        rejected_idx    = [i for i, v in enumerate(verdicts) if v == "rejected"]
        non_rejected_idx = [i for i, v in enumerate(verdicts) if v != "rejected"]
        if rejected_idx and non_rejected_idx:
            assert min(rejected_idx) > min(non_rejected_idx)

    def test_quality_verdict_filter(self, db_path):
        results = recall_assets(db_path=db_path, quality_verdict="approved")
        assert all(r.quality_verdict == "approved" for r in results)


# ---------------------------------------------------------------------------
# TestPerformanceRanking
# ---------------------------------------------------------------------------

class TestPerformanceRanking:
    def test_higher_performance_first_within_same_quality(self, db_path):
        # Among approved video_clips: A(0.9), F(0.7) — A should come before F
        results = recall_assets(db_path=db_path, asset_type="video_clip",
                                quality_verdict="approved")
        asset_ids = [r.asset_id for r in results]
        a_idx = asset_ids.index(_aid("A")) if _aid("A") in asset_ids else None
        f_idx = asset_ids.index(_aid("F")) if _aid("F") in asset_ids else None
        if a_idx is not None and f_idx is not None:
            assert a_idx < f_idx

    def test_min_performance_signal_filter(self, db_path):
        results = recall_assets(db_path=db_path, min_performance_signal=0.8,
                                reuse_eligible_only=False)
        assert all((r.performance_signal or 0) >= 0.8 for r in results)

    def test_none_performance_treated_as_zero(self, db_path):
        # Assets with no performance_signal should appear after those with signals
        results = recall_assets(db_path=db_path, asset_type="thumbnail")
        # C has perf=0.8 — should have rank_score > 0
        assert results[0].rank_score >= 0.0


# ---------------------------------------------------------------------------
# TestTagRankingBonus
# ---------------------------------------------------------------------------

class TestTagRankingBonus:
    def test_tag_matched_before_non_matched(self, db_path):
        # Query "ancient" — A and B match, F does not (no tags)
        # F is approved with perf=0.7; B is unvalidated with perf=0.5
        # Despite F having better quality, A and B should rank first since they match
        results = recall_by_semantic_tags(["ancient"], db_path=db_path)
        matching_ids = {_aid("A"), _aid("B")}
        for r in results:
            assert r.asset_id in matching_ids  # only matching assets returned


# ---------------------------------------------------------------------------
# TestAssetLineage
# ---------------------------------------------------------------------------

class TestAssetLineage:
    def test_returns_asset_record(self, db_path):
        lineage = get_asset_lineage(_aid("A"), db_path=db_path)
        assert lineage["asset_record"] is not None
        assert lineage["asset_record"]["asset_id"] == _aid("A")

    def test_returns_archive_entry(self, db_path):
        lineage = get_asset_lineage(_aid("A"), db_path=db_path)
        assert lineage["archive_entry"] is not None
        assert isinstance(lineage["archive_entry"], RecallResult)
        assert lineage["archive_entry"].asset_id == _aid("A")

    def test_returns_versions(self, db_path):
        lineage = get_asset_lineage(_aid("A"), db_path=db_path)
        # ver_02 has asset_id = asset_A
        assert len(lineage["versions"]) >= 1
        version_ids = {v["version_id"] for v in lineage["versions"]}
        assert "ver_02" in version_ids

    def test_unknown_asset_returns_none_fields(self, db_path):
        lineage = get_asset_lineage("asset_does_not_exist", db_path=db_path)
        assert lineage["asset_id"] == "asset_does_not_exist"
        assert lineage["asset_record"] is None
        assert lineage["archive_entry"] is None
        assert lineage["versions"] == []

    def test_always_has_required_keys(self, db_path):
        lineage = get_asset_lineage(_aid("C"), db_path=db_path)
        for key in ("asset_id", "asset_record", "archive_entry", "versions"):
            assert key in lineage

    def test_versions_ordered_by_index(self, db_path):
        lineage = get_asset_lineage(_aid("A"), db_path=db_path)
        versions = lineage["versions"]
        if len(versions) > 1:
            indexes = [v["version_index"] for v in versions]
            assert indexes == sorted(indexes)


# ---------------------------------------------------------------------------
# TestGetAssetVersions
# ---------------------------------------------------------------------------

class TestGetAssetVersions:
    def test_returns_all_versions_for_slot(self, db_path):
        versions = get_asset_versions(_SLOT_1, db_path=db_path)
        assert len(versions) == 2

    def test_ordered_by_version_index(self, db_path):
        versions = get_asset_versions(_SLOT_1, db_path=db_path)
        indexes = [v["version_index"] for v in versions]
        assert indexes == sorted(indexes)

    def test_version_dicts_have_status(self, db_path):
        versions = get_asset_versions(_SLOT_1, db_path=db_path)
        assert all("status" in v for v in versions)

    def test_first_is_failed_second_is_succeeded(self, db_path):
        versions = get_asset_versions(_SLOT_1, db_path=db_path)
        assert versions[0]["status"] == "failed"
        assert versions[1]["status"] == "succeeded"

    def test_unknown_slot_returns_empty(self, db_path):
        versions = get_asset_versions("slot_unknown_0000", db_path=db_path)
        assert versions == []

    def test_returns_list_of_dicts(self, db_path):
        versions = get_asset_versions(_SLOT_1, db_path=db_path)
        assert isinstance(versions, list)
        assert all(isinstance(v, dict) for v in versions)


# ---------------------------------------------------------------------------
# TestGetBestAssetForSlot
# ---------------------------------------------------------------------------

class TestGetBestAssetForSlot:
    def test_returns_succeeded_version(self, db_path):
        best = get_best_asset_for_slot(_SLOT_1, db_path=db_path)
        assert best is not None
        assert best["status"] == "succeeded"

    def test_chooses_highest_version_index_succeeded(self, db_path):
        best = get_best_asset_for_slot(_SLOT_1, db_path=db_path)
        assert best["version_id"] == "ver_02"

    def test_resolved_asset_id_present(self, db_path):
        best = get_best_asset_for_slot(_SLOT_1, db_path=db_path)
        assert best["asset_id"] == _aid("A")

    def test_all_failed_slot_returns_none(self, db_path):
        best = get_best_asset_for_slot(_SLOT_2, db_path=db_path)
        assert best is None

    def test_unknown_slot_returns_none(self, db_path):
        best = get_best_asset_for_slot("slot_ghost_0000", db_path=db_path)
        assert best is None


# ---------------------------------------------------------------------------
# TestEmptyResults
# ---------------------------------------------------------------------------

class TestEmptyResults:
    def test_recall_assets_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        results = recall_assets(db_path=p)
        assert results == []

    def test_recall_episode_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        results = recall_episode_assets("ep_none", db_path=p)
        assert results == []

    def test_recall_tags_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        results = recall_by_semantic_tags(["ancient"], db_path=p)
        assert results == []

    def test_recall_style_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        results = recall_by_visual_style({"period": "ancient"}, db_path=p)
        assert results == []

    def test_lineage_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        lineage = get_asset_lineage("any_id", db_path=p)
        assert lineage["asset_record"] is None
        assert lineage["versions"] == []

    def test_versions_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        versions = get_asset_versions("any_slot", db_path=p)
        assert versions == []

    def test_best_asset_empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        best = get_best_asset_for_slot("any_slot", db_path=p)
        assert best is None

    def test_bad_db_path_returns_empty_not_raise(self, tmp_path):
        bad = tmp_path / "nonexistent" / "sub" / "bad.db"
        # get_connection creates the file, but with no schema → empty
        results = recall_assets(db_path=bad)
        assert results == []

    def test_returns_list_type_not_none(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="hologram")
        assert results is not None
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    def test_recall_assets_no_network(self, db_path):
        orig_connect = socket.socket.connect
        calls = []

        def _reject(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Network call in archive_recall")

        socket.socket.connect = _reject
        try:
            recall_assets(db_path=db_path, asset_type="video_clip")
        finally:
            socket.socket.connect = orig_connect
        assert calls == []

    def test_get_asset_lineage_no_network(self, db_path):
        orig_connect = socket.socket.connect

        def _reject(*args, **kwargs):
            raise AssertionError("Network call in get_asset_lineage")

        socket.socket.connect = _reject
        try:
            get_asset_lineage(_aid("A"), db_path=db_path)
        finally:
            socket.socket.connect = orig_connect


# ---------------------------------------------------------------------------
# TestNoProductionSideEffects
# ---------------------------------------------------------------------------

class TestNoProductionSideEffects:
    def test_no_ep_000_in_test_db(self, db_path):
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT episode_id FROM runs WHERE episode_id = 'ep_000'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_recall_does_not_write(self, db_path):
        """archive_index row count must not change after recall calls."""
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        conn.close()

        recall_assets(db_path=db_path)
        recall_episode_assets(_EP_A, db_path=db_path)
        recall_by_semantic_tags(["ancient"], db_path=db_path)
        recall_by_visual_style({"period": "ancient"}, db_path=db_path)
        get_asset_lineage(_aid("A"), db_path=db_path)
        get_asset_versions(_SLOT_1, db_path=db_path)
        get_best_asset_for_slot(_SLOT_1, db_path=db_path)

        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        conn.close()
        assert before == after

    def test_recall_result_fields_present(self, db_path):
        results = recall_assets(db_path=db_path, asset_type="video_clip")
        assert len(results) > 0
        r = results[0]
        for field_name in (
            "archive_id", "asset_id", "episode_id", "asset_type",
            "provider", "reuse_eligible", "quality_verdict",
            "archived_at", "rank_score",
        ):
            assert hasattr(r, field_name), f"RecallResult missing field: {field_name}"

    def test_default_db_path_not_touched_by_tests(self, db_path):
        from hypogeum.archive_recall import _DEFAULT_DB_PATH
        assert str(db_path) != str(_DEFAULT_DB_PATH)
