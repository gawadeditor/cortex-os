"""
tests/test_muapi_canary_gate.py — Phase 3 correction: the paid canary tool
(tools/muapi_canary_submit.py) is part of the enforced surface. It must
require an authoritative --channel-id, refuse a blank one before ANY
provider/network activity, and forward that channel_id into the same gated
submit_scene boundary as production (verified plan -> grant/plan/finance ->
CHANNEL_WEEKLY -> child authorization -> submission gate).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "muapi_canary_submit.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("muapi_canary_under_test", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --manifest-path is satisfied so these tests exercise ONLY the
# channel-context contract (not argparse's other requirements).
_BASE_ARGS = [
    "--episode-id", "ep_canary",
    "--manifest-path", "/nonexistent/manifest.json",
    "--shot-id", "b_001__primary",
    "--scene-id", "s01",
    "--extra-index", "0",
    "--tier", "2",
    "--max-charge-usd", "1.00",
]


class TestCanaryChannelContextRequired:
    def test_missing_channel_id_flag_rejected_by_argparse(self, capsys):
        module = _load_tool()
        with pytest.raises(SystemExit) as excinfo:
            module.main([*_BASE_ARGS])
        assert excinfo.value.code == 2
        assert "--channel-id" in capsys.readouterr().err

    def test_blank_channel_id_fails_closed_before_any_network_or_preflight(self, monkeypatch, tmp_path):
        module = _load_tool()
        monkeypatch.chdir(tmp_path)
        with patch.object(module, "run_preflight") as mock_preflight, \
             patch.object(module, "_submit_canary") as mock_submit, \
             patch.object(module, "submit_scene") as mock_boundary:
            rc = module.main([*[_x for _x in _BASE_ARGS], "--channel-id", "   "])

        assert rc == 1
        mock_preflight.assert_not_called()
        mock_submit.assert_not_called()
        mock_boundary.assert_not_called()

    def test_authorized_canary_forwards_channel_id_into_enforced_boundary(self, monkeypatch, tmp_path):
        """The full _submit_canary body runs against minimal on-disk fixtures
        and proves channel_id reaches submit_scene's enforced boundary."""
        module = _load_tool()

        assets_root = tmp_path / "assets"
        episode_dir = assets_root / "episodes" / "ep_canary"
        episode_dir.mkdir(parents=True)

        manifest = {
            "authorizations": [{
                "scene_id": "s01",
                "shot_id": "b_001__primary",
                "extra_index": 0,
                "prompt_checksum": "0" * 64,
                "provenance": {"compiler": "test", "compiler_version": "1"},
                "model": "runway-text-to-video",
                "expected_charge_usd": 0.15,
                "deliverable": "main_episode",
            }],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
        visual_package = {
            "scenes": [{
                "scene_id": "s01",
                "short_id": None,
                "cinematic_coverage_plan": {"shots": [{
                    "shot_id": "b_001__primary",
                    "primary_prompt": "canary prompt",
                    "estimated_usable_duration": 8.0,
                }]},
            }],
        }
        (episode_dir / f"visual_package_ep_canary.json").write_text(json.dumps(visual_package))

        captured = {}

        def _capture_submit(*args, **kwargs):
            captured.update(kwargs)
            return {"generation_status": "generated"}

        out_file = episode_dir / "video" / "sc_s01__extra0.mp4"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"x" * 64)  # success receipt requires output existence
        preflight = {
            "output_path": str(out_file),
            "receipt_path": str(episode_dir / "canary_receipts" / "b_001__primary_receipt.json"),
        }

        # A REAL offline Config: submit_scene reads many flags off it.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_muapi import make_config as _make_config

        with patch.object(module, "run_preflight", return_value=preflight), \
             patch.object(module, "Config", _make_config), \
             patch.object(module, "submit_scene", side_effect=_capture_submit):
            rc = module.main([
                *_BASE_ARGS,
                "--channel-id", "ch_canary",
                "--manifest-path", str(tmp_path / "manifest.json"),
                "--assets-root", str(assets_root),
            ])

        assert rc == 0, captured
        assert captured.get("channel_id") == "ch_canary"

    def test_preflight_only_with_blank_channel_still_refuses(self, monkeypatch, tmp_path):
        """Even --preflight-only is refused for a blank channel context:
        the canary's purpose is a PAID probe, and its configuration contract
        is identical either way."""
        module = _load_tool()
        with patch.object(module, "Config"):
            rc = module.main([*_BASE_ARGS, "--channel-id", "", "--preflight-only"])
        assert rc == 1
