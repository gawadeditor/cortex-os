"""
tests/test_muapi_canary_submit_cli.py — Unit tests for
tools/muapi_canary_submit.py (2026-08-14)

Loaded via importlib (tools/ has no __init__.py, matching this repo's
existing standalone-script convention, e.g. test_regenerate_coverage_plan_cli.py).
submit_scene() is always mocked here — this file proves gating, receipt
content, and state-isolation behavior; it never makes a real provider call
and never touches real episode files under .hypogeum-assets/.
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
_SCRIPT_PATH = _ROOT / "tools" / "muapi_canary_submit.py"

_spec = importlib.util.spec_from_file_location("muapi_canary_submit_cli", _SCRIPT_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


EPISODE_ID = "ep_test"
CHANNEL_ID = "test_channel"
SHOT_ID = "b_047__shot2"
SCENE_ID = "sh_short_003_s05"
EXTRA_INDEX = 2
DELIVERABLE = "short_003"
ASPECT = "9:16"
MODEL = "veo3.1-lite-text-to-video"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_fixtures(tmp_path: Path, *, prompt: str = "A schlupf constriction threshold prompt.",
                     manifest_checksum: str = None, canary_status: str = "active",
                     manifest_extra: dict = None, prompt_provenance: dict = None,
                     second_entry: dict = None) -> tuple[Path, Path]:
    assets_root = tmp_path
    episode_dir = assets_root / "episodes" / EPISODE_ID
    episode_dir.mkdir(parents=True)

    shot = {
        "shot_id": SHOT_ID, "parent_beat_id": "b_047", "shot_order": 2,
        "shot_purpose": "transition_beat", "primary_prompt": prompt,
        "fallback_prompt": prompt, "estimated_usable_duration": 8.0,
    }
    visual_package = {
        "scenes": [{
            "scene_id": SCENE_ID, "narration_reference": "b_047",
            "short_id": "short_003", "source_type": "short",
            "production_spec": {"aspect_ratio": ASPECT, "provider_routing": {}},
            "cinematic_coverage_plan": {"beat_id": "b_047", "shots": [shot]},
        }],
    }
    (episode_dir / f"visual_package_{EPISODE_ID}.json").write_text(json.dumps(visual_package))

    entry = {
        "target_shot_id": SHOT_ID, "scene": SCENE_ID, "deliverable": DELIVERABLE,
        "aspect_ratio": ASPECT, "extra_index": EXTRA_INDEX,
        "canary_status": canary_status,
        "primary_prompt_checksum": manifest_checksum if manifest_checksum is not None else _sha(prompt),
        "prompt_provenance": (
            prompt_provenance if prompt_provenance is not None
            else {"compiler": "hypogeum.video_prompt_writer", "source_shot_id": SHOT_ID}
        ),
    }
    if manifest_extra:
        entry.update(manifest_extra)
    entries = [entry]
    if second_entry:
        entries.append(second_entry)
    manifest = {"entries": entries}
    manifest_path = episode_dir / f"canary_authorization_{EPISODE_ID}.json"
    manifest_path.write_text(json.dumps(manifest))

    return assets_root, manifest_path


def _base_args(assets_root: Path, manifest_path: Path, preflight_only: bool = True) -> list:
    args = [
        "--episode-id", EPISODE_ID, "--channel-id", CHANNEL_ID,
        "--shot-id", SHOT_ID, "--scene-id", SCENE_ID,
        "--extra-index", str(EXTRA_INDEX), "--tier", "2",
        "--max-charge-usd", "0.39", "--deliverable", DELIVERABLE,
        "--aspect-ratio", ASPECT, "--manifest-path", str(manifest_path),
        "--assets-root", str(assets_root),
    ]
    if preflight_only:
        args.append("--preflight-only")
    return args


class TestPreflightOnly:

    def test_preflight_only_makes_zero_provider_calls(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        with patch.object(cli, "submit_scene") as mock_submit:
            args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
            receipt = cli.run_preflight(args)
        mock_submit.assert_not_called()
        assert receipt["provider_calls_made"] == 0
        assert receipt["preflight_status"] == "PASS"

    def test_preflight_receipt_has_required_fields(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        receipt = cli.run_preflight(args)
        for field in ("expected_model", "expected_charge_usd", "prompt_checksum",
                      "output_path", "rush_baseline_count", "extra_scene_id"):
            assert field in receipt, field
        assert receipt["expected_model"] == MODEL
        assert receipt["expected_charge_usd"] == 0.39
        assert receipt["aspect_ratio"] == ASPECT


class TestValidSubmission:

    def test_one_valid_invocation_submits_exactly_one_shot(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path, preflight_only=False))

        fake_output = cli._episode_dir(assets_root, EPISODE_ID) / "video" / "sh_short_003" / "sc_sh_short_003_s05__extra2.mp4"

        def fake_submit(**kwargs):
            fake_output.parent.mkdir(parents=True, exist_ok=True)
            fake_output.write_bytes(b"FAKE_VIDEO_BYTES")
            return {"generation_status": "generated", "provider": "muapi",
                    "model_used": MODEL, "request_id": "job-123", "asset_id": "x"}

        with patch.object(cli, "submit_scene", side_effect=fake_submit) as mock_submit:
            exit_code = cli.main(_base_args(assets_root, manifest_path, preflight_only=False))

        assert mock_submit.call_count == 1
        assert exit_code == 0

        receipt_path = cli._episode_dir(assets_root, EPISODE_ID) / "canary_receipts" / f"{SHOT_ID}_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        assert receipt["provider_calls_made"] == 1
        assert receipt["success"] is True
        assert receipt["execution_status"] == "SUCCESS"


class TestSecondEntryIsolation:

    def test_second_authorization_entry_cannot_leak_into_invocation(self, tmp_path):
        other_entry = {
            "target_shot_id": "b_047__shot3", "scene": SCENE_ID, "deliverable": DELIVERABLE,
            "aspect_ratio": ASPECT, "extra_index": 3, "canary_status": "active",
            "primary_prompt_checksum": "deadbeef",
            "prompt_provenance": {"compiler": "x", "source_shot_id": "b_047__shot3"},
        }
        assets_root, manifest_path = _write_fixtures(tmp_path, second_entry=other_entry)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        receipt = cli.run_preflight(args)
        # only the requested shot's entry was resolved; the other entry's
        # fields never appear anywhere in the receipt
        assert receipt["shot_id"] == SHOT_ID
        assert "b_047__shot3" not in json.dumps(receipt)


class TestFailClosedGates:

    def test_missing_prompt_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, prompt="")
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "primary_prompt_present"

    def test_prompt_checksum_mismatch_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, manifest_checksum="0" * 64)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "prompt_checksum_match"

    def test_scene_mismatch_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, manifest_extra={"scene": "wrong_scene"})
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "manifest_field_match"

    def test_extra_index_mismatch_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, manifest_extra={"extra_index": 5})
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "manifest_field_match"

    def test_inactive_authorization_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, canary_status="completed")
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "authorization_active"

    def test_no_matching_authorization_entry_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        args.shot_id = "b_999__shot1"
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "exactly_one_authorization_entry"

    def test_model_mismatch_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        args.model = "veo3-text-to-video"  # tier 2 will never resolve to this
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "model_matches_required"

    def test_cost_cap_violation_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        args = cli.build_arg_parser().parse_args(
            _base_args(assets_root, manifest_path) + ["--max-charge-usd", "0.01"]
        )
        # rebuild with the lower cap actually parsed (argparse takes the last value)
        argv = _base_args(assets_root, manifest_path)
        idx = argv.index("--max-charge-usd")
        argv[idx + 1] = "0.01"
        args = cli.build_arg_parser().parse_args(argv)
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "cost_within_cap"

    def test_existing_output_collision_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        collision_path = cli._episode_dir(assets_root, EPISODE_ID) / "video" / "sh_short_003" / "sc_sh_short_003_s05__extra2.mp4"
        collision_path.parent.mkdir(parents=True)
        collision_path.write_bytes(b"PRE_EXISTING_UNRELATED_FILE")
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "no_output_collision"
        # the pre-existing file must remain untouched
        assert collision_path.read_bytes() == b"PRE_EXISTING_UNRELATED_FILE"

    def test_missing_provenance_blocks_submission(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path, prompt_provenance={})
        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        with pytest.raises(cli.CanaryPreflightError) as exc:
            cli.run_preflight(args)
        assert exc.value.check == "prompt_provenance_present"


class TestProviderFailureHandling:

    def test_provider_failure_causes_no_retry(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)

        with patch.object(cli, "submit_scene", side_effect=RuntimeError("simulated provider error")) as mock_submit:
            cli.main(_base_args(assets_root, manifest_path, preflight_only=False))

        assert mock_submit.call_count == 1  # never retried

    def test_provider_failure_does_not_mark_shot_completed(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)

        with patch.object(cli, "submit_scene", side_effect=RuntimeError("simulated provider error")):
            cli.main(_base_args(assets_root, manifest_path, preflight_only=False))

        receipt_path = cli._episode_dir(assets_root, EPISODE_ID) / "canary_receipts" / f"{SHOT_ID}_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        assert receipt["success"] is False
        assert receipt["execution_status"] == "FAILED_NO_RETRY"
        assert receipt["provider_calls_made"] == 1


class TestOutputIsolation:

    def test_successful_download_registers_only_requested_shot(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        video_dir = cli._episode_dir(assets_root, EPISODE_ID) / "video"

        other_shot = video_dir / "sh_short_003" / "sc_sh_short_003_s05.mp4"
        other_shot.parent.mkdir(parents=True)
        other_shot.write_bytes(b"UNRELATED_EXISTING_PRIMARY")
        other_hash_before = hashlib.md5(other_shot.read_bytes()).hexdigest()

        fake_output = video_dir / "sh_short_003" / "sc_sh_short_003_s05__extra2.mp4"

        def fake_submit(**kwargs):
            fake_output.write_bytes(b"NEW_CANARY_BYTES")
            return {"generation_status": "generated", "provider": "muapi", "model_used": MODEL}

        with patch.object(cli, "submit_scene", side_effect=fake_submit):
            cli.main(_base_args(assets_root, manifest_path, preflight_only=False))

        assert fake_output.exists()
        assert hashlib.md5(other_shot.read_bytes()).hexdigest() == other_hash_before

    def test_existing_rushes_not_overwritten(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        video_dir = cli._episode_dir(assets_root, EPISODE_ID) / "video"
        rush = video_dir / "sc_s01.mp4"
        rush.parent.mkdir(parents=True)
        rush.write_bytes(b"EXISTING_RUSH_BYTES")

        args = cli.build_arg_parser().parse_args(_base_args(assets_root, manifest_path))
        receipt = cli.run_preflight(args)
        assert receipt["rush_baseline"]["sc_s01.mp4"] == hashlib.md5(b"EXISTING_RUSH_BYTES").hexdigest()
        # unchanged after preflight (preflight never writes)
        assert rush.read_bytes() == b"EXISTING_RUSH_BYTES"


class TestReceiptContent:

    def test_receipt_records_provider_call_count_and_spend(self, tmp_path):
        assets_root, manifest_path = _write_fixtures(tmp_path)
        fake_output = cli._episode_dir(assets_root, EPISODE_ID) / "video" / "sh_short_003" / "sc_sh_short_003_s05__extra2.mp4"

        def fake_submit(**kwargs):
            fake_output.parent.mkdir(parents=True, exist_ok=True)
            fake_output.write_bytes(b"X")
            return {"generation_status": "generated", "provider": "muapi", "model_used": MODEL}

        with patch.object(cli, "submit_scene", side_effect=fake_submit):
            cli.main(_base_args(assets_root, manifest_path, preflight_only=False))

        receipt_path = cli._episode_dir(assets_root, EPISODE_ID) / "canary_receipts" / f"{SHOT_ID}_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        assert receipt["provider_calls_made"] == 1
        assert receipt["expected_charge_usd"] == 0.39
        assert receipt["max_charge_usd"] == 0.39
