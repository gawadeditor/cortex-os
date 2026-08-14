"""
tests/test_recompile_authorized_shot_prompts.py — Unit tests for
tools/recompile_authorized_shot_prompts.py (2026-08-14)

Loaded via importlib (tools/ has no __init__.py, matching this repo's
existing standalone-script convention). Never makes a real provider call,
never touches real episode files under .hypogeum-assets/ -- every fixture
lives under tmp_path.
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
_SCRIPT_PATH = _ROOT / "tools" / "recompile_authorized_shot_prompts.py"

_spec = importlib.util.spec_from_file_location("recompile_cli", _SCRIPT_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

EPISODE_ID = "ep_test_recompile"
CHANNEL_ID = "test_channel"
FAKE_HEAD = "deadbeef" * 5  # 40 hex chars, stand-in git HEAD


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shot(shot_id: str, beat_id: str, *, order: int = 1, subject="subj", shot_scale="medium",
          camera_angle="eye_level", camera_direction="frontal", composition="centered",
          camera_movement="locked_off_hold", stale_prompt: str = None) -> dict:
    shot = {
        "shot_id": shot_id, "parent_beat_id": beat_id, "shot_order": order,
        "shot_purpose": "coverage", "narrative_function": "establish",
        "estimated_usable_duration": 8.0, "subject": subject, "shot_scale": shot_scale,
        "camera_angle": camera_angle, "camera_height": "eye_level",
        "camera_direction": camera_direction, "camera_movement": camera_movement,
        "composition": composition, "background_depth": "medium",
        "screen_direction": "neutral", "motion_energy": "low",
        "transition_in": "cut", "transition_out": "cut",
    }
    if stale_prompt is not None:
        shot["primary_prompt"] = stale_prompt
        shot["fallback_prompt"] = stale_prompt
    return shot


def _scene(scene_id: str, beat_id: str, shots: list, short_id=None, base_primary="scene base text") -> dict:
    return {
        "scene_id": scene_id, "narration_reference": beat_id, "short_id": short_id,
        "source_type": "short" if short_id else "main_episode",
        "production_spec": {
            "aspect_ratio": "9:16" if short_id else "16:9",
            "provider_routing": {"primary_prompt": base_primary, "fallback_prompt": base_primary},
        },
        "cinematic_coverage_plan": {"beat_id": beat_id, "shots": shots},
    }


def _entry(shot_id: str, beat_id: str, deliverable: str, checksum: str = None, provenance: dict = None) -> dict:
    return {
        "target_shot_id": shot_id, "beat": beat_id, "deliverable": deliverable,
        "aspect_ratio": "9:16", "required_duration_s": 8.0,
        "primary_prompt_checksum": checksum or "0" * 64,
        "prompt_provenance": provenance or {"compiler": "old.compiler", "compiler_version": "stale@1", "source_shot_id": shot_id},
    }


def _write_fixtures(tmp_path: Path, scenes: list, entries: list) -> tuple[Path, Path]:
    assets_root = tmp_path
    episode_dir = assets_root / "episodes" / EPISODE_ID
    episode_dir.mkdir(parents=True)
    (episode_dir / f"visual_package_{EPISODE_ID}.json").write_text(
        json.dumps({"scenes": scenes, "asset_manifest": {"assets": []}})
    )
    manifest_path = episode_dir / f"generation_authorization_manifest_{EPISODE_ID}.json"
    manifest_path.write_text(json.dumps({"manifest_id": "genauth_test", "entries": entries}))
    return assets_root, manifest_path


def _base_args(assets_root, manifest_path, report_path, mode="--dry-run", **overrides) -> list:
    return [
        "--episode-id", EPISODE_ID, "--channel-id", CHANNEL_ID,
        "--manifest-path", str(manifest_path), "--tier", str(overrides.get("tier", 2)),
        mode, "--report-path", str(report_path),
        "--assets-root", str(assets_root),
        "--max-total-charge-usd", str(overrides.get("max_total_charge_usd", 100.0)),
    ]


def _one_beat_fixture(stale=True):
    prompt = "GARBAGE STALE TEXT unrelated to structured fields" if stale else None
    shot = _shot("b_001__shot1", "b_001", stale_prompt=prompt)
    scenes = [_scene("s01", "b_001", [shot])]
    entries = [_entry("b_001__shot1", "b_001", "main", checksum=_sha(prompt) if prompt else "0" * 64)]
    return scenes, entries


def _multi_beat_fixture():
    shot1 = _shot("b_001__shot1", "b_001", subject="a", shot_scale="wide", camera_angle="high_angle")
    shot2 = _shot("b_002__shot1", "b_002", subject="b", shot_scale="close_up", camera_angle="low_angle")
    shot3 = _shot("b_003__shot1", "b_003", subject="c", shot_scale="extreme_close_up", camera_angle="overhead")
    scenes = [
        _scene("s01", "b_001", [shot1]),
        _scene("s02", "b_002", [shot2], short_id="short_001"),
        _scene("s03", "b_003", [shot3], short_id="short_002"),
    ]
    entries = [
        _entry("b_001__shot1", "b_001", "main"),
        _entry("b_002__shot1", "b_002", "short_001"),
        _entry("b_003__shot1", "b_003", "short_002"),
    ]
    return scenes, entries


@pytest.fixture(autouse=True)
def _fake_git_head():
    with patch.object(cli, "_git_head", return_value=FAKE_HEAD):
        yield


class TestUsesCurrentCompiler:

    def test_recompiled_prompt_has_authoritative_marker(self, tmp_path):
        scenes, entries = _one_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        report = cli.run_dry_run(args)
        assert report["compiler"] == cli._COMPILER_QUALNAME
        entry = report["per_entry"][0]
        assert entry["compiler_version"] == FAKE_HEAD

    def test_old_stale_prompt_text_never_appears_in_new_prompt(self, tmp_path):
        scenes, entries = _one_beat_fixture(stale=True)
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        new_manifest, new_vp, per_entry = cli.recompile_all_active(
            json.loads(manifest_path.read_text()),
            json.loads((assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json").read_text()),
            FAKE_HEAD,
        )
        new_shot = new_vp["scenes"][0]["cinematic_coverage_plan"]["shots"][0]
        assert "GARBAGE STALE TEXT" not in new_shot["primary_prompt"]
        assert "SHOT-SPECIFIC DIRECTION FOR b_001__shot1" in new_shot["primary_prompt"]
        assert "AUTHORITATIVE" in new_shot["primary_prompt"]


class TestDryRunSafety:

    def test_dry_run_makes_no_mutation(self, tmp_path):
        scenes, entries = _one_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        vp_path = assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json"
        manifest_before = manifest_path.read_bytes()
        vp_before = vp_path.read_bytes()

        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        cli.main(_base_args(assets_root, manifest_path, report_path))

        assert manifest_path.read_bytes() == manifest_before
        assert vp_path.read_bytes() == vp_before

    def test_dry_run_makes_zero_provider_calls(self):
        assert not hasattr(cli, "submit_scene")
        module_names = [n.lower() for n in dir(cli)]
        assert not any("submit_scene" in n for n in module_names)


class TestApplyAtomicity:

    def test_apply_updates_all_eligible_entries(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        report = cli.run_apply(args)
        assert report["applied"] is True
        assert report["entries_recompiled"] == 3

        new_manifest = json.loads(manifest_path.read_text())
        for e in new_manifest["entries"]:
            assert e["prompt_provenance"]["compiler"] == cli._COMPILER_QUALNAME
            assert e["prompt_provenance"]["compiler_version"] == FAKE_HEAD

        vp = json.loads((assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json").read_text())
        for scene in vp["scenes"]:
            for shot in scene["cinematic_coverage_plan"]["shots"]:
                assert "AUTHORITATIVE" in shot["primary_prompt"]

    def test_one_failed_entry_prevents_all_mutations(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        entries.append({
            "target_shot_id": "b_999__shot1", "beat": "b_999", "deliverable": "main",
            "primary_prompt_checksum": "0" * 64,
            "prompt_provenance": {"compiler": "x", "compiler_version": "y", "source_shot_id": "b_999__shot1"},
        })
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        vp_path = assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json"
        manifest_before = manifest_path.read_bytes()
        vp_before = vp_path.read_bytes()

        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        report = cli.run_apply(args)

        assert report["applied"] is False
        assert report["overall_result"] == "FAIL"
        # partial apply is impossible: not even the 3 resolvable entries were written
        assert manifest_path.read_bytes() == manifest_before
        assert vp_path.read_bytes() == vp_before

    def test_scene_construction_conflict_blocks_apply(self, tmp_path):
        scenes, entries = _one_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        vp_path = assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json"
        manifest_before = manifest_path.read_bytes()
        vp_before = vp_path.read_bytes()

        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )

        def _raise(*a, **kw):
            raise cli.ShotConstructionConflictError("b_001__shot1", [])

        with patch.object(cli, "compile_authoritative_shot_prompt", side_effect=_raise):
            report = cli.run_apply(args)

        assert report["applied"] is False
        assert manifest_path.read_bytes() == manifest_before
        assert vp_path.read_bytes() == vp_before

    def test_authorization_and_allocation_fields_unchanged(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        entries[0]["qualified"] = False
        entries[0]["allocatable"] = True
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        cli.run_apply(args)
        new_manifest = json.loads(manifest_path.read_text())
        new_entry = next(e for e in new_manifest["entries"] if e["target_shot_id"] == "b_001__shot1")
        assert new_entry["qualified"] is False
        assert new_entry["allocatable"] is True
        assert new_entry["deliverable"] == "main"
        assert new_entry["aspect_ratio"] == "9:16"
        assert new_entry["required_duration_s"] == 8.0

    def test_prompt_checksum_matches_exact_compiled_utf8_string(self, tmp_path):
        scenes, entries = _one_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        cli.run_apply(args)
        new_manifest = json.loads(manifest_path.read_text())
        vp = json.loads((assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json").read_text())
        shot = vp["scenes"][0]["cinematic_coverage_plan"]["shots"][0]
        entry = new_manifest["entries"][0]
        assert entry["primary_prompt_checksum"] == hashlib.sha256(shot["primary_prompt"].encode("utf-8")).hexdigest()

    def test_scene_invariant_not_mutated(self, tmp_path):
        scenes, entries = _one_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        cli.run_apply(args)
        vp = json.loads((assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json").read_text())
        assert vp["scenes"][0]["production_spec"]["provider_routing"]["primary_prompt"] == "scene base text"

    def test_rollback_restores_state_after_simulated_failure(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        vp_path = assets_root / "episodes" / EPISODE_ID / f"visual_package_{EPISODE_ID}.json"
        manifest_before = manifest_path.read_bytes()
        vp_before = vp_path.read_bytes()

        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )

        real_replace = Path.replace
        call_count = {"n": 0}

        def _flaky_replace(self, target):
            call_count["n"] += 1
            if call_count["n"] == 2:  # fail on the second file's atomic rename
                raise OSError("simulated disk failure")
            return real_replace(self, target)

        with patch.object(Path, "replace", _flaky_replace):
            report = cli.run_apply(args)

        assert report["applied"] is False
        assert manifest_path.read_bytes() == manifest_before
        assert vp_path.read_bytes() == vp_before


class TestDistinctnessGate:

    def test_structural_duplicate_within_beat_blocks_apply(self, tmp_path):
        shot1 = _shot("b_001__shot1", "b_001")
        shot2 = _shot("b_001__shot2", "b_001", order=2)  # identical structure signature
        scenes = [_scene("s01", "b_001", [shot1, shot2])]
        entries = [_entry("b_001__shot1", "b_001", "main"), _entry("b_001__shot2", "b_001", "main")]
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        report = cli.run_dry_run(args)
        assert report["overall_result"] == "FAIL"
        assert any(b["check"].startswith("distinctness_") for b in report["batch_preflight_validation"]["blockers"])


class TestPhysicalMediaUntouched:

    def test_no_video_directory_created(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path, report_path, mode="--apply")
        )
        cli.run_apply(args)
        video_dir = assets_root / "episodes" / EPISODE_ID / "video"
        assert not video_dir.exists()


class TestPassesBatchPreflightFixture:

    def test_recompiled_multi_beat_fixture_passes_batch_preflight(self, tmp_path):
        scenes, entries = _multi_beat_fixture()
        assets_root, manifest_path = _write_fixtures(tmp_path, scenes, entries)
        report_path = tmp_path / "report.json"
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, report_path))
        report = cli.run_dry_run(args)
        assert report["overall_result"] == "PASS"
        assert report["batch_preflight_validation"]["overall_result"] == "PASS"
