"""
tests/test_channel_i2v_policy.py — channel_i2v_disabled() (channel_package_
scaffold.py) and its wiring into Visual Package Director's I2V kill switch.

Regression for the 2026-09-05 operator-authorized fallback: "if you can't
reliably solve the I2V reference-image-leak problem, cancel I2V entirely"
(see channel_i2v_disabled()'s own docstring for the full evidence chain —
ep_5f72565c / "Pip the Hedgehog" had scenes with an unbounded crossfade no
downstream trim can fix).
"""
from __future__ import annotations

import json

import pytest

from hypogeum.channel_package_scaffold import channel_i2v_disabled


@pytest.fixture()
def i2v_channels(tmp_path, monkeypatch):
    pkgs = tmp_path / "channel_packages"
    (pkgs / "disabled_ch").mkdir(parents=True)
    (pkgs / "disabled_ch" / "i2v_policy.json").write_text(
        json.dumps({"mode": "disabled"})
    )
    (pkgs / "enabled_ch").mkdir(parents=True)
    (pkgs / "enabled_ch" / "i2v_policy.json").write_text(
        json.dumps({"mode": "enabled", "_note": "explicit opt-in, not that this does anything special"})
    )
    (pkgs / "no_policy_ch").mkdir(parents=True)
    monkeypatch.setattr("hypogeum.channel_package_scaffold._PACKAGES", pkgs)
    return pkgs


class TestChannelI2VDisabled:

    def test_disabled_mode_returns_true(self, i2v_channels):
        assert channel_i2v_disabled("disabled_ch") is True

    def test_other_mode_returns_false(self, i2v_channels):
        assert channel_i2v_disabled("enabled_ch") is False

    def test_no_policy_file_returns_false(self, i2v_channels):
        assert channel_i2v_disabled("no_policy_ch") is False

    def test_unknown_channel_returns_false(self, i2v_channels):
        assert channel_i2v_disabled("no_such_channel") is False

    def test_none_channel_id_returns_false(self, i2v_channels):
        assert channel_i2v_disabled(None) is False
        assert channel_i2v_disabled("") is False

    def test_malformed_json_returns_false(self, i2v_channels):
        (i2v_channels / "disabled_ch" / "i2v_policy.json").write_text("not json{")
        assert channel_i2v_disabled("disabled_ch") is False

    def test_mode_check_is_case_insensitive(self, i2v_channels):
        (i2v_channels / "disabled_ch" / "i2v_policy.json").write_text(
            json.dumps({"mode": "DISABLED"})
        )
        assert channel_i2v_disabled("disabled_ch") is True


class TestVPDSkipsReferenceImagesWhenDisabled:
    """Wiring test: run_visual_package_director's Phase 2 must never call
    generate_entity_reference_images() for a channel with I2V disabled, and
    every scene must resolve without a reference_image_url."""

    def test_generate_entity_reference_images_never_called(self, tmp_path, monkeypatch):
        pkgs = tmp_path / "channel_packages"
        (pkgs / "twinkle_test").mkdir(parents=True)
        (pkgs / "twinkle_test" / "i2v_policy.json").write_text(json.dumps({"mode": "disabled"}))
        monkeypatch.setattr("hypogeum.channel_package_scaffold._PACKAGES", pkgs)

        from unittest.mock import patch, MagicMock
        import hypogeum.agents.visual_package_director as vpd_mod

        visual_package = {
            "visual_continuity_record": {"locked_entities": [
                {"entity_id": "ent_1", "role": "primary", "lock_status": "final"},
            ]},
            "scenes": [{"scene_id": "s01", "scene_index": 0}],
        }
        with patch.object(vpd_mod, "generate_entity_reference_images") as mock_gen_ref, \
             patch.object(vpd_mod, "apply_reference_image_asset_ids") as mock_apply, \
             patch.object(vpd_mod, "order_and_filter_scenes", return_value=visual_package["scenes"]), \
             patch.object(vpd_mod, "generate_all_scene_videos") as mock_gen_videos, \
             patch.object(vpd_mod, "update_routing_manifest"):
            vpd_mod._execute_muapi_phase(
                visual_package, MagicMock(SHORT_NATIVE_MODE=True),
                "ep_t", tmp_path, MagicMock(),
                budget_guard=None, channel_id="twinkle_test",
            )
        mock_gen_ref.assert_not_called()
        # entity_hosted_urls passed to generate_all_scene_videos must be empty
        assert mock_gen_videos.call_args.args[2] == {}
