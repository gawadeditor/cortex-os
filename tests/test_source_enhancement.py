"""
tests/test_source_enhancement.py — Regression coverage for the
2026-08-02 Source Enhancement Stage (hypogeum/source_enhancement.py) and
its provider abstraction (hypogeum/providers/enhancement.py,
hypogeum/providers/muapi_video_upscaler.py).

Proves the six required properties:
  1. a below-required-resolution source clip is enhanced and passes
  2. a native-resolution source clip skips enhancement entirely (no
     provider call, no cost)
  3. an enhancement failure (provider-side or post-enhancement validation)
     fails closed -- the original clip is never touched
  4. a validly-enhanced clip is reused on a second pass (no re-enhancement)
  5. swapping the configured provider forces re-enhancement even though the
     on-disk clip already satisfies the resolution requirement
  6. a provider-internal version bump forces re-enhancement the same way

Uses a real ffmpeg testsrc clip (small, local, no network, no MUAPI spend)
for every source/candidate file, and a FakeEnhancementProvider registered
through the real provider registry (hypogeum.providers.enhancement.
register_provider) -- never a mocked hypogeum.muapi call -- so these tests
prove the PIPELINE STAGE's own policy and fail-closed behavior independent
of any concrete vendor, matching the architecture's own provider-agnostic
requirement. MuapiVideoUpscalerProvider itself is covered separately below
with mocked HTTP, proving it correctly implements the same contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.artifact_compatibility import (
    CompatibilityDecision,
    evaluate_source_enhancement_compatibility,
    probe_resolution,
)
from hypogeum.providers.enhancement import (
    EnhancementProvider,
    EnhancementResult,
    get_enhancement_provider,
    register_provider,
)
from hypogeum.source_enhancement import run_source_enhancement


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_clip(path: Path, width: int, height: int, duration: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s={width}x{height}:r=24:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=30,
    )


def _make_config(**overrides) -> SimpleNamespace:
    base = dict(
        ENHANCEMENT_ENABLED=True,
        ENHANCEMENT_PROVIDER="fake_default",
        ENHANCEMENT_REQUIRED_WIDTH=1920,
        ENHANCEMENT_REQUIRED_HEIGHT=1080,
        ENHANCEMENT_TARGET_RESOLUTION_LABEL="1080p",
        MUAPI_T1_MODEL="veo3-text-to-video",
        MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
        MUAPI_T3_MODEL="veo3.1-lite-text-to-video",
        MUAPI_DRY_RUN=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_visual_package(scene_id: str, asset_id: str, model: str = "veo3.1-lite-text-to-video", tier_used: int = 2) -> dict:
    return {
        "asset_manifest": {
            "assets": [
                {
                    "scene_id": scene_id,
                    "asset_id": asset_id,
                    "generation_status": "generated",
                    "model": model,
                    "tier_used": tier_used,
                }
            ]
        }
    }


class FakeEnhancementProvider(EnhancementProvider):
    """Deterministic, zero-network provider for pipeline-stage tests. A
    fresh instance's identity (name/model/version) is fixed at
    construction; .calls records every scene_id it was actually invoked
    for, so tests can assert a provider was (or was NOT) called."""

    def __init__(self, name="fake", model="fake-upscaler", version="1.0",
                 produce_width=1920, produce_height=1080, fail=False,
                 fail_validation=False, cost=0.05, gen_time=1.0):
        self.name = name
        self.model = model
        self.version = version
        self.produce_width = produce_width
        self.produce_height = produce_height
        self.fail = fail
        self.fail_validation = fail_validation  # provider "succeeds" but output stays under-resolution
        self.cost = cost
        self.gen_time = gen_time
        self.calls = []

    def enhance(self, source_path, dest_path, target_width, target_height,
                target_resolution_label, config, episode_id, scene_id, log):
        self.calls.append(scene_id)
        if self.fail:
            return EnhancementResult(
                success=False, provider=self.name, model=self.model,
                provider_version=self.version, failure_reason="fake_provider_failure",
                cost_usd=self.cost, generation_time_s=self.gen_time,
            )
        out_w = target_width if not self.fail_validation else 1280
        out_h = target_height if not self.fail_validation else 720
        _make_clip(dest_path, out_w, out_h, duration=1.0)
        return EnhancementResult(
            success=True, output_path=dest_path, provider=self.name, model=self.model,
            provider_version=self.version, cost_usd=self.cost, generation_time_s=self.gen_time,
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test registers its own fake provider keys; scrub the shared
    module-level registry before AND after so tests never see each other's
    registrations (register_provider mutates process-global state)."""
    import hypogeum.providers.enhancement as enhancement_mod
    saved = dict(enhancement_mod._REGISTRY)
    saved_flag = enhancement_mod._BUILTINS_REGISTERED
    yield
    enhancement_mod._REGISTRY.clear()
    enhancement_mod._REGISTRY.update(saved)
    enhancement_mod._BUILTINS_REGISTERED = saved_flag


# ---------------------------------------------------------------------------
# 1. 720p -> enhancement -> pass
# ---------------------------------------------------------------------------

class TestEnhancementRuns:
    def test_below_resolution_source_is_enhanced_and_passes(self, tmp_path):
        episode_id = "ep_enh_pass"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider()
        register_provider("fake_pass", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_pass")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["applied"] is True
        assert se["required"] is True
        assert se["validation_result"] == "passed"
        assert se["original_resolution"] == "1280x720"
        assert se["enhanced_resolution"] == "1920x1080"
        assert se["provider"] == "fake"
        assert se["model"] == "fake-upscaler"
        assert se["provider_version"] == "1.0"
        assert se["cost_usd"] == 0.05
        assert se["generation_time_s"] == 1.0
        assert provider.calls == ["s01"]

        # The canonical clip path itself is now the enhanced, real 1920x1080 file.
        assert probe_resolution(clip_path) == (1920, 1080)


# ---------------------------------------------------------------------------
# 2. native 1080 -> skip enhancement
# ---------------------------------------------------------------------------

class TestEnhancementSkipped:
    def test_native_resolution_source_skips_enhancement(self, tmp_path):
        episode_id = "ep_enh_skip"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1920, 1080)

        provider = FakeEnhancementProvider()
        register_provider("fake_skip", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_skip")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["applied"] is False
        assert se["required"] is False
        assert se["validation_result"] == "skipped_native_resolution"
        assert se["original_resolution"] == "1920x1080"
        assert se["cost_usd"] is None
        assert provider.calls == []  # never invoked -- zero cost


# ---------------------------------------------------------------------------
# 3. enhancement failure -> fail closed
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_provider_failure_leaves_original_untouched(self, tmp_path):
        episode_id = "ep_enh_fail"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)
        original_bytes = clip_path.read_bytes()

        provider = FakeEnhancementProvider(fail=True)
        register_provider("fake_fail", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_fail")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["applied"] is True
        assert se["validation_result"] == "failed"
        assert se["failure_reason"] == "fake_provider_failure"

        # Fail closed: original file byte-for-byte untouched, still 720p.
        assert clip_path.read_bytes() == original_bytes
        assert probe_resolution(clip_path) == (1280, 720)
        # No stray candidate file left behind either.
        assert not (ep_dir / "video" / "sc_s01.enhanced.mp4").exists()

        # The second Source Validation pass (already-existing coverage_check
        # machinery) still correctly rejects this clip -- fail-closed
        # propagates downstream unchanged.
        from hypogeum.artifact_compatibility import evaluate_video_clip_compatibility
        second_pass = evaluate_video_clip_compatibility(clip_path)
        assert second_pass.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE

    def test_provider_success_but_still_under_resolution_fails_closed(self, tmp_path):
        """A provider that reports success=True but whose actual output
        doesn't meet the resolution requirement must never be trusted --
        this stage re-probes independently and fails closed anyway."""
        episode_id = "ep_enh_fail_validate"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)
        original_bytes = clip_path.read_bytes()

        provider = FakeEnhancementProvider(fail_validation=True)
        register_provider("fake_fail_validate", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_fail_validate")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["validation_result"] == "failed"
        assert "post-enhancement validation failed" in se["failure_reason"]
        assert clip_path.read_bytes() == original_bytes
        assert probe_resolution(clip_path) == (1280, 720)


# ---------------------------------------------------------------------------
# 4. artifact reuse
# ---------------------------------------------------------------------------

class TestArtifactReuse:
    def test_second_pass_does_not_re_enhance(self, tmp_path):
        episode_id = "ep_enh_reuse"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider()
        register_provider("fake_reuse", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_reuse")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert provider.calls == ["s01"]

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert provider.calls == ["s01"]  # unchanged -- second pass reused the prior decision


# ---------------------------------------------------------------------------
# 5. provider replacement
# ---------------------------------------------------------------------------

class TestProviderReplacement:
    def test_swapping_provider_forces_re_enhancement(self, tmp_path):
        episode_id = "ep_enh_swap"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider_a = FakeEnhancementProvider(name="vendor_a", model="upscaler_a")
        provider_b = FakeEnhancementProvider(name="vendor_b", model="upscaler_b")
        register_provider("fake_vendor_a", lambda: provider_a)
        register_provider("fake_vendor_b", lambda: provider_b)

        config_a = _make_config(ENHANCEMENT_PROVIDER="fake_vendor_a")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")
        vp = run_source_enhancement(vp, config_a, episode_id, output_dir=ep_dir)
        assert provider_a.calls == ["s01"]
        assert provider_b.calls == []
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider"] == "vendor_a"

        # Even though the on-disk clip is already 1920x1080 (compliant),
        # switching the configured provider must trigger re-evaluation.
        config_b = _make_config(ENHANCEMENT_PROVIDER="fake_vendor_b")
        vp = run_source_enhancement(vp, config_b, episode_id, output_dir=ep_dir)
        assert provider_b.calls == ["s01"]
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider"] == "vendor_b"


# ---------------------------------------------------------------------------
# 6. compatibility version changes
# ---------------------------------------------------------------------------

class TestVersionChange:
    def test_provider_version_bump_forces_re_enhancement(self, tmp_path):
        episode_id = "ep_enh_version"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider(version="1.0")
        register_provider("fake_version", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_version")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert provider.calls == ["s01"]
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider_version"] == "1.0"

        # Same provider identity, internal version bump (e.g. the vendor
        # shipped a new upscaler model) -- must invalidate the prior record.
        provider.version = "2.0"
        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert provider.calls == ["s01", "s01"]
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider_version"] == "2.0"

    def test_required_resolution_change_forces_re_enhancement(self, tmp_path):
        episode_id = "ep_enh_reqres"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider()
        register_provider("fake_reqres", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_reqres")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert provider.calls == ["s01"]

        # Required source resolution policy raised to 4K -- even a
        # previously-compliant enhanced clip is now incompatible.
        provider_4k = FakeEnhancementProvider(produce_width=3840, produce_height=2160)
        register_provider("fake_reqres", lambda: provider_4k)
        config_4k = _make_config(
            ENHANCEMENT_PROVIDER="fake_reqres",
            ENHANCEMENT_REQUIRED_WIDTH=3840, ENHANCEMENT_REQUIRED_HEIGHT=2160,
        )
        vp = run_source_enhancement(vp, config_4k, episode_id, output_dir=ep_dir)
        assert provider_4k.calls == ["s01"]
        assert probe_resolution(clip_path) == (3840, 2160)


# ---------------------------------------------------------------------------
# evaluate_source_enhancement_compatibility() -- direct unit coverage
# ---------------------------------------------------------------------------

class TestCompatibilityFunction:
    def test_no_prior_record_is_incompatible(self, tmp_path):
        clip = tmp_path / "c.mp4"
        _make_clip(clip, 1920, 1080)
        config = _make_config()
        result = evaluate_source_enhancement_compatibility({}, clip, config)
        assert result.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE
        assert result.reusable is False

    def test_matching_record_is_compatible(self, tmp_path):
        clip = tmp_path / "c.mp4"
        _make_clip(clip, 1920, 1080)
        provider = FakeEnhancementProvider()
        register_provider("fake_match", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_match")
        asset = {
            "model": "veo3.1-lite-text-to-video",
            "tier_used": 2,
            "source_enhancement": {
                "applied": True,
                "validation_result": "passed",
                "provider": "fake", "model": "fake-upscaler", "provider_version": "1.0",
                "required_width": 1920, "required_height": 1080,
            },
        }
        result = evaluate_source_enhancement_compatibility(asset, clip, config)
        assert result.decision == CompatibilityDecision.VALID_AND_COMPATIBLE

    def test_generation_model_mismatch_is_incompatible(self, tmp_path):
        clip = tmp_path / "c.mp4"
        _make_clip(clip, 1920, 1080)
        provider = FakeEnhancementProvider()
        register_provider("fake_model_mismatch", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_model_mismatch")
        asset = {
            "model": "veo3.1-fast-text-to-video",  # stale -- current T2 model differs
            "tier_used": 2,
            "source_enhancement": {
                "applied": True, "validation_result": "passed",
                "provider": "fake", "model": "fake-upscaler", "provider_version": "1.0",
                "required_width": 1920, "required_height": 1080,
            },
        }
        result = evaluate_source_enhancement_compatibility(asset, clip, config)
        assert result.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE
        assert any("generation model changed" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# MuapiVideoUpscalerProvider -- concrete provider contract, mocked HTTP
# ---------------------------------------------------------------------------

class TestMuapiVideoUpscalerProvider:
    def test_full_success_flow(self, tmp_path):
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"
        config = _make_config(
            MUAPI_API_KEY="test-key", MUAPI_BASE_URL="https://api.muapi.ai/api/v1",
            MUAPI_TIMEOUT_SECONDS=30, MUAPI_MAX_POLL_ATTEMPTS=5, MUAPI_POLL_INTERVAL_SECONDS=0,
            MUAPI_DRY_RUN=False,
        )

        upload_resp = MagicMock(status_code=200)
        upload_resp.json.return_value = {"url": "https://cdn.muapi.ai/outputs/uploaded.mp4"}
        submit_resp = MagicMock(status_code=200)
        submit_resp.json.return_value = {"request_id": "req-123", "status": "processing", "cost": {"amount_usd": 0.08}}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/outputs/result.mp4"]}
        download_resp = MagicMock(status_code=200, content=source.read_bytes())

        with patch("hypogeum.providers.muapi_video_upscaler.requests.post",
                   side_effect=[upload_resp, submit_resp]), \
             patch("hypogeum.providers.muapi_video_upscaler.requests.get",
                   side_effect=[poll_resp, download_resp]), \
             patch("hypogeum.finance.record_cost"):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest,
                target_width=1920, target_height=1080, target_resolution_label="1080p",
                config=config, episode_id="ep_x", scene_id="s01", log=MagicMock(),
            )

        assert result.success is True
        assert result.output_path == dest
        assert result.cost_usd == 0.08
        assert dest.exists()
        assert provider.name == "muapi"
        assert provider.model == "ai-video-upscaler"

    def test_submit_http_error_reports_failure(self, tmp_path):
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"
        config = _make_config(MUAPI_API_KEY="test-key", MUAPI_BASE_URL="https://api.muapi.ai/api/v1",
                               MUAPI_TIMEOUT_SECONDS=30, MUAPI_DRY_RUN=False)

        upload_resp = MagicMock(status_code=200)
        upload_resp.json.return_value = {"url": "https://cdn.muapi.ai/outputs/uploaded.mp4"}
        submit_resp = MagicMock(status_code=500, text="internal error")

        with patch("hypogeum.providers.muapi_video_upscaler.requests.post",
                   side_effect=[upload_resp, submit_resp]):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest,
                target_width=1920, target_height=1080, target_resolution_label="1080p",
                config=config, episode_id="ep_x", scene_id="s01", log=MagicMock(),
            )

        assert result.success is False
        assert "submit_http_500" in result.failure_reason
        assert not dest.exists()

    def test_dry_run_produces_placeholder_at_target_resolution(self, tmp_path):
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720, duration=2.0)
        dest = tmp_path / "dest.mp4"
        config = _make_config(MUAPI_DRY_RUN=True)

        provider = MuapiVideoUpscalerProvider()
        result = provider.enhance(
            source_path=source, dest_path=dest,
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            config=config, episode_id="ep_x", scene_id="s01", log=MagicMock(),
        )

        assert result.success is True
        assert result.cost_usd == 0.0
        assert probe_resolution(dest) == (1920, 1080)


# ---------------------------------------------------------------------------
# Provider abstraction registry
# ---------------------------------------------------------------------------

class TestProviderRegistry:
    def test_unknown_provider_key_raises(self):
        from hypogeum.providers.enhancement import UnknownEnhancementProviderError

        config = _make_config(ENHANCEMENT_PROVIDER="does_not_exist")
        with pytest.raises(UnknownEnhancementProviderError):
            get_enhancement_provider(config)

    def test_builtin_muapi_provider_resolves(self):
        config = _make_config(ENHANCEMENT_PROVIDER="muapi_ai_video_upscaler")
        provider = get_enhancement_provider(config)
        assert provider.name == "muapi"
        assert provider.model == "ai-video-upscaler"
