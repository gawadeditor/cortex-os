"""
tests/test_muapi_batch_preflight.py — Unit tests for
tools/muapi_batch_preflight.py (2026-08-14)

Loaded via importlib (tools/ has no __init__.py, matching this repo's
existing standalone-script convention, e.g. test_muapi_canary_submit_cli.py).
Never makes a real provider call, never touches real episode files under
.hypogeum-assets/ -- every fixture lives under tmp_path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _ROOT / "tools" / "muapi_batch_preflight.py"

_spec = importlib.util.spec_from_file_location("muapi_batch_preflight_cli", _SCRIPT_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


EPISODE_ID = "ep_test_batch"
CHANNEL_ID = "test_channel"
MODEL = "veo3.1-lite-text-to-video"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt(shot_id: str, beat_id: str, marker: bool = True, suffix: str = "") -> str:
    marker_text = f"SHOT-SPECIFIC DIRECTION FOR {shot_id} (AUTHORITATIVE — supersedes any conflicting camera angle)." if marker else ""
    return f"SCENE CONTINUITY generic text for {beat_id}. {marker_text} distinctive detail {shot_id} {suffix}".strip()


def _shot(shot_id: str, *, subject="subj", shot_scale="medium", camera_angle="eye_level",
          camera_direction="frontal", composition="centered", camera_movement="locked_off_hold",
          lighting="warm", prompt: str = None, checksum: str = None, provenance: dict = None) -> dict:
    p = prompt if prompt is not None else _prompt(shot_id, shot_id.split("__")[0])
    return {
        "shot_id": shot_id, "subject": subject, "shot_scale": shot_scale,
        "camera_angle": camera_angle, "camera_direction": camera_direction,
        "composition": composition, "camera_movement": camera_movement, "lighting": lighting,
        "primary_prompt": p, "fallback_prompt": p, "estimated_usable_duration": 8.0,
    }


def _scene(scene_id: str, beat_id: str, shots: list, short_id=None) -> dict:
    return {
        "scene_id": scene_id, "narration_reference": beat_id, "short_id": short_id,
        "source_type": "short" if short_id else "main_episode",
        "production_spec": {"aspect_ratio": "9:16" if short_id else "16:9", "provider_routing": {}},
        "cinematic_coverage_plan": {"beat_id": beat_id, "shots": shots},
    }


def _entry(shot_id: str, beat_id: str, deliverable: str, shot_dict: dict,
           canary_status: str = None, authorization_status: str = None,
           checksum: str = None, provenance: dict = None) -> dict:
    prompt = shot_dict["primary_prompt"]
    e = {
        "target_shot_id": shot_id, "beat": beat_id, "deliverable": deliverable,
        "primary_prompt_checksum": checksum if checksum is not None else _sha(prompt),
        "prompt_provenance": (
            provenance if provenance is not None
            else {"compiler": "hypogeum.video_prompt_writer.x", "source_shot_id": shot_id}
        ),
    }
    if canary_status is not None:
        e["canary_status"] = canary_status
    if authorization_status is not None:
        e["authorization_status"] = authorization_status
    return e


def _write_fixtures(tmp_path: Path, scenes: list, entries: list, asset_manifest_assets: list = None) -> tuple[Path, Path]:
    assets_root = tmp_path
    episode_dir = assets_root / "episodes" / EPISODE_ID
    episode_dir.mkdir(parents=True)

    visual_package = {
        "scenes": scenes,
        "asset_manifest": {"assets": asset_manifest_assets or []},
    }
    (episode_dir / f"visual_package_{EPISODE_ID}.json").write_text(json.dumps(visual_package))

    manifest = {"manifest_id": "genauth_test", "entries": entries}
    manifest_path = episode_dir / f"generation_authorization_manifest_{EPISODE_ID}.json"
    manifest_path.write_text(json.dumps(manifest))

    return assets_root, manifest_path


def _base_args(assets_root: Path, manifest_path: Path, report_path: Path, **overrides) -> list:
    args = [
        "--episode-id", EPISODE_ID, "--channel-id", CHANNEL_ID,
        "--manifest-path", str(manifest_path),
        "--tier", str(overrides.get("tier", 2)),
        "--max-total-charge-usd", str(overrides.get("max_total_charge_usd", 100.0)),
        "--report-path", str(report_path),
        "--assets-root", str(assets_root),
        "--required-model", str(overrides.get("required_model", MODEL)),
    ]
    return args


def _run(tmp_path, scenes, entries, asset_manifest_assets=None, **arg_overrides):
    assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries, asset_manifest_assets)
    report_path = tmp_path / "report.json"
    args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path, **arg_overrides))
    return cli.run_batch_preflight(args), assets_root, manifest_path, report_path


# ---------------------------------------------------------------------------
# Happy path fixture: 4 independent single-shot beats across different
# deliverables, plus one 2-shot beat used by dominance/duplicate tests.
# ---------------------------------------------------------------------------

def _happy_scenes_and_entries():
    shot_b1 = _shot("b_001__shot1")
    shot_b2 = _shot("b_002__shot1", shot_scale="close_up", camera_angle="low_angle")
    shot_b3 = _shot("b_003__shot1", shot_scale="wide", camera_angle="high_angle")
    shot_b4 = _shot("b_004__shot1", shot_scale="extreme_close_up", camera_angle="overhead")

    scenes = [
        _scene("s01", "b_001", [shot_b1]),
        _scene("s02", "b_002", [shot_b2], short_id="short_001"),
        _scene("s03", "b_003", [shot_b3], short_id="short_002"),
        _scene("s04", "b_004", [shot_b4], short_id="short_003"),
    ]
    entries = [
        _entry("b_001__shot1", "b_001", "main", shot_b1),
        _entry("b_002__shot1", "b_002", "short_001", shot_b2),
        _entry("b_003__shot1", "b_003", "short_002", shot_b3),
        _entry("b_004__shot1", "b_004", "short_003", shot_b4),
    ]
    return scenes, entries


class TestZeroProviderCalls:

    def test_module_never_imports_submit_scene(self):
        assert not hasattr(cli, "submit_scene")

    def test_report_records_zero_calls_and_spend(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["provider_calls_made"] == 0
        assert report["spend_usd"] == 0.0
        assert report["stage_plan"]["stage_1"]["executed"] is False
        assert report["stage_plan"]["stage_2"]["executed"] is False


class TestActiveEntryInspection:

    def test_every_active_entry_inspected_exactly_once(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["active_entry_count"] == 4
        shot_ids = [e["shot_id"] for e in report["entries_inspected"]]
        assert sorted(shot_ids) == sorted(["b_001__shot1", "b_002__shot1", "b_003__shot1", "b_004__shot1"])
        assert len(shot_ids) == len(set(shot_ids))

    def test_inactive_fulfilled_rejected_entries_excluded(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        entries.append(_entry("b_001__shot1", "b_001", "main", _shot("b_005__shot1"), canary_status="fulfilled_qualified"))
        # rename to a distinct shot id so it isn't a duplicate-id blocker
        entries[-1]["target_shot_id"] = "b_005__shot1"
        entries[-1]["beat"] = "b_005"
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["active_entry_count"] == 4
        assert report["inactive_entry_count"] == 1
        assert "b_005__shot1" not in [e["shot_id"] for e in report["entries_inspected"]]

    def test_one_failed_entry_does_not_hide_the_rest(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        entries[1]["primary_prompt_checksum"] = "0" * 64  # break b_002__shot1 only
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        shot_ids = [e["shot_id"] for e in report["entries_inspected"]]
        # all 4 entries still appear in the report, not silently dropped
        assert sorted(shot_ids) == sorted(["b_001__shot1", "b_002__shot1", "b_003__shot1", "b_004__shot1"])
        assert any(b["check"] == "prompt_checksum_match" for b in report["blockers"])


class TestFailClosedGates:

    def test_missing_prompt_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        scenes[0]["cinematic_coverage_plan"]["shots"][0]["primary_prompt"] = ""
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "missing_or_empty_prompt" for b in report["blockers"])

    def test_checksum_mismatch_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        entries[0]["primary_prompt_checksum"] = "0" * 64
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "prompt_checksum_match" for b in report["blockers"])

    def test_provenance_failure_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        entries[0]["prompt_provenance"] = {}
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "prompt_provenance_present" for b in report["blockers"])

    def test_model_mismatch_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries, required_model="veo3-text-to-video")
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "model_matches_required" for b in report["blockers"])

    def test_cost_cap_violation_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries, max_total_charge_usd=0.10)
        assert report["overall_result"] == "FAIL"
        checks = [b["check"] for b in report["blockers"]]
        assert "cost_within_cap" in checks or "aggregate_cost_exceeds_cap" in checks

    def test_duplicate_shot_ids_block_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        dup = dict(entries[0])
        entries.append(dup)
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "duplicate_shot_id" for b in report["blockers"])

    def test_duplicate_output_paths_block_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))

        fake_allocations = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None,
             "extra_scene_id": "s01__extra1", "output_path": "/tmp/collide.mp4"},
            {"beat_id": "b_002", "shot_id": "b_002__shot1", "scene_id": "s02", "short_id": "short_001",
             "extra_scene_id": "s02__extra1", "output_path": "/tmp/collide.mp4"},
            {"beat_id": "b_003", "shot_id": "b_003__shot1", "scene_id": "s03", "short_id": "short_002",
             "extra_scene_id": "s03__extra1", "output_path": "/tmp/other.mp4"},
            {"beat_id": "b_004", "shot_id": "b_004__shot1", "scene_id": "s04", "short_id": "short_003",
             "extra_scene_id": "s04__extra1", "output_path": "/tmp/other2.mp4"},
        ]
        with patch.object(cli, "plan_confirmed_shot_allocations", return_value=fake_allocations):
            report = cli.run_batch_preflight(args)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "duplicate_output_path" for b in report["blockers"])

    def test_prompt_dominance_leak_blocks_whole_batch(self, tmp_path):
        shot1 = _shot("b_001__shot1")
        shot2 = _shot("b_001__shot2", shot_scale="close_up", camera_angle="low_angle",
                       prompt=_prompt("b_001__shot2", "b_001", marker=False))
        scenes = [_scene("s01", "b_001", [shot1, shot2])]
        entries = [
            _entry("b_001__shot1", "b_001", "main", shot1),
            _entry("b_001__shot2", "b_001", "main", shot2),
        ]
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "scene_level_prompt_dominance_leak" for b in report["blockers"])

    def test_per_shot_direction_remains_authoritative_when_marker_present(self, tmp_path):
        shot1 = _shot("b_001__shot1")
        shot2 = _shot("b_001__shot2", shot_scale="close_up", camera_angle="low_angle")
        scenes = [_scene("s01", "b_001", [shot1, shot2])]
        entries = [
            _entry("b_001__shot1", "b_001", "main", shot1),
            _entry("b_001__shot2", "b_001", "main", shot2),
        ]
        report, *_ = _run(tmp_path, scenes, entries)
        for e in report["entries_inspected"]:
            assert e["per_shot_direction_authoritative"] is True
        assert not any(b["check"] == "scene_level_prompt_dominance_leak" for b in report["blockers"])
        assert report["overall_result"] in ("PASS", "PASS_WITH_SEMANTIC_WARNINGS")

    def test_already_registered_output_blocks_whole_batch(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        episode_dir = assets_root / "episodes" / EPISODE_ID
        clip_path = episode_dir / "video" / "sc_s01.mp4"
        clip_path.parent.mkdir(parents=True)
        clip_path.write_bytes(b"X")
        vp_path = episode_dir / f"visual_package_{EPISODE_ID}.json"
        vp = json.loads(vp_path.read_text())
        vp["asset_manifest"]["assets"] = [{
            "planned_shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None,
            "generation_status": "generated",
        }]
        vp_path.write_text(json.dumps(vp))
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        report = cli.run_batch_preflight(args)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "output_already_registered" for b in report["blockers"])


class TestDistinctnessAudit:

    def test_exact_compiled_prompt_duplicate_blocks_batch(self, tmp_path):
        shared_prompt = _prompt("shared", "b_001")
        shot1 = _shot("b_001__shot1", prompt=shared_prompt)
        shot2 = _shot("b_001__shot2", shot_scale="close_up", prompt=shared_prompt)
        scenes = [_scene("s01", "b_001", [shot1, shot2])]
        entries = [
            _entry("b_001__shot1", "b_001", "main", shot1, checksum=_sha(shared_prompt)),
            _entry("b_001__shot2", "b_001", "main", shot2, checksum=_sha(shared_prompt)),
        ]
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "distinctness_fail_exact_duplicate" for b in report["blockers"])
        assert any(f["classification"] == "FAIL_EXACT_DUPLICATE" for f in report["distinctness_findings"])

    def test_structural_duplicate_blocks_batch(self, tmp_path):
        shot1 = _shot("b_001__shot1", prompt=_prompt("b_001__shot1", "b_001", suffix="one"))
        shot2 = _shot("b_001__shot2", prompt=_prompt("b_001__shot2", "b_001", suffix="two"))
        # identical structure signature (all _STRUCTURE_FIELDS equal) but different prompt text
        scenes = [_scene("s01", "b_001", [shot1, shot2])]
        entries = [
            _entry("b_001__shot1", "b_001", "main", shot1),
            _entry("b_001__shot2", "b_001", "main", shot2),
        ]
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"] == "distinctness_fail_structural_duplicate" for b in report["blockers"])

    def test_distinct_shots_pass(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "PASS"
        assert all(f["classification"] == "PASS_DISTINCT" for f in report["distinctness_findings"])


class TestStage1Cohort:

    def test_cohort_has_exactly_three_distinct_eligible_entries(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, *_ = _run(tmp_path, scenes, entries)
        cohort = report["stage_plan"]["stage_1"]["cohort"]
        assert cohort is not None
        assert len(cohort) == 3
        shot_ids = [c["shot_id"] for c in cohort]
        beats = [c["beat"] for c in cohort]
        assert len(set(shot_ids)) == 3
        assert len(set(beats)) == 3

    def test_cohort_absent_when_batch_fails(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        entries[0]["primary_prompt_checksum"] = "0" * 64
        report, *_ = _run(tmp_path, scenes, entries)
        assert report["overall_result"] == "FAIL"
        assert report["stage_plan"]["stage_1"]["cohort"] is None
        assert report["stage_plan"]["stage_1"]["eligible"] is False


class TestNoProviderAccessReachable:

    def test_no_retry_fallback_upscaler_or_submission_symbols_imported(self, tmp_path):
        forbidden = ("submit_scene", "MUAPIBudgetGuard", "upscale", "publish", "encode", "assemble")
        module_names = dir(cli)
        for name in forbidden:
            assert not any(name.lower() in n.lower() for n in module_names if not n.startswith("_TOOL")), name

    def test_report_path_written_and_no_video_files_created(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        report, assets_root, manifest_path, report_path = _run(tmp_path, scenes, entries)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report))
        episode_dir = assets_root / "episodes" / EPISODE_ID
        video_dir = episode_dir / "video"
        assert not video_dir.exists() or not any(video_dir.rglob("*.mp4"))


class TestDeterministicReport:

    def test_report_deterministic_apart_from_timestamp(self, tmp_path):
        scenes, entries = _happy_scenes_and_entries()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        report1 = cli.run_batch_preflight(args)
        report2 = cli.run_batch_preflight(args)
        r1 = {k: v for k, v in report1.items() if k != "generated_at"}
        r2 = {k: v for k, v in report2.items() if k != "generated_at"}
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
