"""
tests/test_deliverable_ids.py — hypogeum.publishing.deliverable_ids
canonical deliverable_id resolution.

2026-08-23: regression coverage for the double-prefix defect
(resolve_short_deliverable_id() re-prefixing an already-canonical
short_id, e.g. "short_001" -> "short_short_001") confirmed live against
the real ep_f3619022 render package after its earlier repair phase
corrected shorts_renders[].short_id to the canonical "short_NNN" form
directly.
"""

from __future__ import annotations

from hypogeum.publishing.deliverable_ids import (
    MAIN_EPISODE_DELIVERABLE_ID,
    classify_deliverable_class,
    resolve_required_deliverable_ids,
    resolve_short_deliverable_id,
)


class TestResolveShortDeliverableIdIdempotent:
    def test_already_canonical_short_id_is_never_re_prefixed(self):
        """The exact ep_f3619022 defect: shorts_renders[].short_id was
        repaired to 'short_001' directly (matching metadata_source.
        shorts_titles[].short_id's own convention) -- must resolve to
        'short_001', never 'short_short_001'."""
        assert resolve_short_deliverable_id({"short_id": "short_001"}, 0) == "short_001"
        assert resolve_short_deliverable_id({"short_id": "short_002"}, 1) == "short_002"
        assert resolve_short_deliverable_id({"short_id": "short_003"}, 2) == "short_003"

    def test_bare_legacy_short_id_still_gets_prefixed(self):
        """Backward compatibility: an unrepaired historical package's
        original, non-prefixed short_id convention (e.g. 'sh_000') is
        unaffected -- still gets 'short_' prepended exactly as before."""
        assert resolve_short_deliverable_id({"short_id": "sh_000"}, 0) == "short_sh_000"

    def test_placeholder_short_id_falls_back_to_index(self):
        assert resolve_short_deliverable_id({"short_id": "sh_000_placeholder"}, 0) == "short_001"
        assert resolve_short_deliverable_id({"short_id": "sh_001_placeholder"}, 1) == "short_002"

    def test_missing_short_entry_falls_back_to_index(self):
        assert resolve_short_deliverable_id(None, 0) == "short_001"
        assert resolve_short_deliverable_id({}, 2) == "short_003"

    def test_classify_deliverable_class_unaffected_by_the_fix(self):
        assert classify_deliverable_class(MAIN_EPISODE_DELIVERABLE_ID) == "main_episode"
        assert classify_deliverable_class("short_001") == "shorts"
        assert classify_deliverable_class("short_short_001") == "shorts"  # still classifiable even if seen


class TestResolveRequiredDeliverableIdsForRepairedPackage:
    def test_all_three_repaired_shorts_resolve_to_canonical_ids(self, tmp_path):
        import json
        assets_root = tmp_path
        ep_dir = assets_root / "episodes" / "ep_test"
        ep_dir.mkdir(parents=True)
        package = {
            "shorts_renders": [
                {"short_id": "short_001", "video_asset_id": "episodes/ep_test/short_0_ep_test.repaired_v1"},
                {"short_id": "short_002", "video_asset_id": "episodes/ep_test/short_1_ep_test.repaired_v1"},
                {"short_id": "short_003", "video_asset_id": "episodes/ep_test/short_2_ep_test.repaired_v1"},
            ]
        }
        (ep_dir / "final_render_package_ep_test.json").write_text(json.dumps(package), encoding="utf-8")

        ids = resolve_required_deliverable_ids("ep_test", assets_root)
        assert ids == ["main_episode", "short_001", "short_002", "short_003"]
        assert "short_short_001" not in ids
