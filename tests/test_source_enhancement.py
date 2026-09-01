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
from hypogeum.source_enhancement import _clip_has_black_tail, qualify_asset, run_source_enhancement


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


def _make_black_tail_clip(
    path: Path, width: int, height: int, real_seconds: float, black_seconds: float,
) -> None:
    """A clip that plays real (testsrc) content for `real_seconds` then
    cuts to pure black for `black_seconds` -- the real ep_f3619022 Short
    001 defect shape: an 8s provider-generated clip that faded to black
    4.83s in and never recovered."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=s={width}x{height}:r=24:d={real_seconds}",
         "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=24:d={black_seconds}",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
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
                 fail_validation=False, ignore_target=False, cost=0.05, gen_time=1.0):
        self.name = name
        self.model = model
        self.version = version
        self.produce_width = produce_width
        self.produce_height = produce_height
        self.fail = fail
        self.fail_validation = fail_validation  # provider "succeeds" but output stays under-resolution
        # ignore_target=True models a real provider's actual physical
        # behavior (e.g. MUAPI's AI Video Upscaler preserving the source's
        # own aspect ratio): output is whatever produce_width/produce_height
        # says, regardless of what target_width/target_height was
        # requested -- our POLICY decides whether that's acceptable,
        # independent of what we asked for. Default False preserves every
        # existing test's behavior (obediently produces the requested
        # target) unchanged.
        self.ignore_target = ignore_target
        self.cost = cost
        self.gen_time = gen_time
        self.calls = []

    def enhance(self, source_path, dest_path, target_width, target_height,
                target_resolution_label, config, episode_id, scene_id, log,
                channel_id=None, output_dir=None):
        self.calls.append(scene_id)
        if self.fail:
            return EnhancementResult(
                success=False, provider=self.name, model=self.model,
                provider_version=self.version, failure_reason="fake_provider_failure",
                cost_usd=self.cost, generation_time_s=self.gen_time,
            )
        if self.fail_validation:
            out_w, out_h = 1280, 720
        elif self.ignore_target:
            out_w, out_h = self.produce_width, self.produce_height
        else:
            out_w, out_h = target_width, target_height
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

    def test_short_extra_clip_identity_resolves_parent_source_type(self, tmp_path):
        """2026-08-26 fix: a coverage-fill extra's scene_id carries an
        '__extra<N>' suffix with no base scene row of its own. The
        scene→source_type lookup must strip the suffix back to the owning
        scene so a portrait Short extra is judged against the PORTRAIT
        requirement — not the landscape default that made MUAPI's correct
        1080x1920 output get rejected after real paid calls."""
        episode_id = "ep_enh_extra_identity"
        ep_dir = tmp_path
        # A portrait Short extra clip already at its (portrait) required
        # resolution: 1080x1920 >= 1080x1920.
        clip_path = ep_dir / "video" / "sh_short_001" / "sc_sh_short_001_s01__extra1.mp4"
        _make_clip(clip_path, 1080, 1920)

        provider = FakeEnhancementProvider()
        register_provider("fake_extra", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_extra")
        vp = {
            "scenes": [
                {"scene_id": "sh_short_001_s01", "source_type": "short"},
            ],
            "asset_manifest": {
                "assets": [{
                    "scene_id": "sh_short_001_s01__extra1",
                    "asset_id": f"episodes/{episode_id}/video/sh_short_001/sc_sh_short_001_s01__extra1",
                    "generation_status": "generated",
                    "model": "veo3.1-lite-text-to-video",
                    "tier_used": 2,
                }]
            },
        }

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        # Native-compliant under the SHORT (portrait) requirement: skipped,
        # provider never invoked — no paid enhancement attempt, no archive.
        assert se["validation_result"] == "skipped_native_resolution"
        assert se["required"] is False
        assert provider.calls == []


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

        # Fail closed on the SOURCE clip: original file byte-for-byte
        # untouched, still 720p.
        assert clip_path.read_bytes() == original_bytes
        assert probe_resolution(clip_path) == (1280, 720)
        # No candidate file exists in this scenario -- the provider itself
        # reported failure before ever writing dest_path (see
        # FakeEnhancementProvider.enhance()'s fail=True branch), so there
        # was nothing to archive, not "correctly deleted."
        assert not (ep_dir / "video" / "sc_s01.enhanced.mp4").exists()
        assert se["archived_path"] is None
        assert se["archived_checksum"] is None

        # The second Source Validation pass (already-existing coverage_check
        # machinery) still correctly rejects this clip -- fail-closed
        # propagates downstream unchanged.
        from hypogeum.artifact_compatibility import evaluate_video_clip_compatibility
        second_pass = evaluate_video_clip_compatibility(clip_path)
        assert second_pass.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE

    def test_provider_success_but_still_under_resolution_fails_closed(self, tmp_path):
        """A provider that reports success=True but whose actual output
        doesn't meet the resolution requirement must never be trusted --
        this stage re-probes independently and fails closed on the SOURCE
        clip anyway. But the real, paid-for candidate itself must never be
        deleted (2026-08-04 architecture fix) -- it's quarantined and
        described in full instead."""
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

        # The generated candidate (real output, real charge) is NEVER
        # deleted -- it's archived and fully described.
        candidate_path = ep_dir / "video" / "sc_s01.enhanced.mp4"
        assert candidate_path.exists()
        assert se["archived_path"] == str(candidate_path)
        assert se["archived_checksum"] is not None
        assert se["archived_at"] is not None
        assert se["provider"] == "fake"
        assert se["model"] == "fake-upscaler"
        assert se["provider_version"] == "1.0"
        assert se["cost_usd"] == 0.05
        assert se["generation_time_s"] == 1.0
        assert se["original_resolution"] == "1280x720"

        import hashlib
        expected_checksum = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        assert se["archived_checksum"] == expected_checksum


# ---------------------------------------------------------------------------
# 3b. Asset lifecycle: validation failure must never destroy a paid artifact
# (2026-08-04 architectural fix)
# ---------------------------------------------------------------------------

class TestAssetLifecyclePreservation:
    def test_reconsideration_promotes_archived_candidate_at_zero_new_cost(self, tmp_path):
        """The exact real-world scenario this fix addresses: a candidate
        failed validation under an old (wrong) required resolution: the
        SAME real file, re-probed under a corrected policy, must be
        promoted to canonical without any new provider call."""
        episode_id = "ep_enh_reconsider"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 720, 1280)  # portrait source, e.g. a Short

        # First pass: provider correctly produces 1080x1920 portrait output
        # regardless of what target it's asked for (ignore_target=True
        # models MUAPI's real aspect-ratio-preserving physics), but an
        # (incorrect, landscape) required resolution rejects it -- exactly
        # the bug found in production.
        provider = FakeEnhancementProvider(produce_width=1080, produce_height=1920, ignore_target=True)
        register_provider("fake_reconsider", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_reconsider")
        vp = {
            "scenes": [{"scene_id": "s01", "source_type": "short"}],
            "asset_manifest": {"assets": [{
                "scene_id": "s01",
                "asset_id": f"episodes/{episode_id}/video/sc_s01",
                "model": "veo3.1-lite-text-to-video", "tier_used": 2,
            }]},
        }

        with patch.dict(
            "hypogeum.artifact_compatibility.DELIVERABLE_RESOLUTION_POLICY",
            {"short": (1920, 1080)},  # simulate the old, wrong (landscape) requirement
        ):
            vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = vp["asset_manifest"]["assets"][0]
        assert asset["source_enhancement"]["validation_result"] == "failed"
        assert provider.calls == ["s01"]
        candidate_path = ep_dir / "video" / "sc_s01.enhanced.mp4"
        assert candidate_path.exists()

        # Second pass: corrected policy (the real, current portrait
        # requirement for "short"). Must promote the ALREADY-EXISTING
        # candidate -- zero new provider calls.
        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["validation_result"] == "passed"
        assert se["enhanced_resolution"] == "1080x1920"
        assert se["cost_usd"] == 0.05  # unchanged -- the ORIGINAL charge, no new spend
        assert provider.calls == ["s01"]  # still just the one real call, ever
        assert probe_resolution(clip_path) == (1080, 1920)
        assert not candidate_path.exists()  # promoted (moved) onto the canonical path

    def test_reconsideration_refuses_tampered_archive(self, tmp_path):
        """A checksum mismatch must never be trusted as 'the same paid
        output' -- refuses to promote, and does NOT silently spend again
        either (no automatic provider fallback)."""
        episode_id = "ep_enh_tampered"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider(fail_validation=True)
        register_provider("fake_tamper", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_tamper")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        candidate_path = ep_dir / "video" / "sc_s01.enhanced.mp4"
        assert candidate_path.exists()

        # Tamper with the archived candidate after the fact.
        candidate_path.write_bytes(b"corrupted-not-the-real-file")

        # Even if a future policy correction would have accepted the real
        # (720p, still-failing) content, a tampered file must never be
        # silently promoted or silently re-purchased.
        with patch.dict(
            "hypogeum.artifact_compatibility.DELIVERABLE_RESOLUTION_POLICY",
            {"main_episode": (100, 100)},  # trivially satisfiable if trusted
        ):
            vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = vp["asset_manifest"]["assets"][0]
        assert asset["source_enhancement"]["validation_result"] == "failed"
        assert provider.calls == ["s01"]  # no new spend triggered

    def test_reconsideration_refuses_when_provider_identity_drifted(self, tmp_path):
        """An archived candidate produced by a since-replaced provider/
        version must never be blindly promoted under a NEW provider's
        identity -- a fresh attempt is required, not a free reconsideration
        (and this test proves no such automatic fresh attempt happens
        silently either -- that remains a separate, explicit decision)."""
        episode_id = "ep_enh_drift"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        old_provider = FakeEnhancementProvider(name="vendor_old", fail_validation=True)
        register_provider("fake_drift", lambda: old_provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_drift")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider"] == "vendor_old"

        new_provider = FakeEnhancementProvider(name="vendor_new", fail_validation=True)
        register_provider("fake_drift", lambda: new_provider)
        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        # A fresh attempt WAS made (new provider, different identity) --
        # not a free promotion of the old vendor's archived candidate.
        assert new_provider.calls == ["s01"]
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["provider"] == "vendor_new"

    def test_legacy_failed_record_with_no_archive_still_falls_through_to_real_retry(self, tmp_path):
        """A failed source_enhancement record written BEFORE this archive
        architecture existed (no archived_path at all -- its candidate was
        deleted under the old, now-corrected behavior) must NOT be silently
        skipped by the no-auto-retry guard. There is nothing to reconsider,
        so a genuine fresh provider call is the correct fallback -- exactly
        the real ep_f3619022 situation this fix must handle correctly."""
        episode_id = "ep_enh_legacy"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider()
        register_provider("fake_legacy", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_legacy")
        vp = _make_visual_package("s01", f"episodes/{episode_id}/video/sc_s01")

        # Simulate a pre-2026-08-04 failed record: same provider identity,
        # validation_result="failed", but no archived_path/checksum at all.
        vp["asset_manifest"]["assets"][0]["source_enhancement"] = {
            "applied": True, "required": True, "validation_result": "failed",
            "provider": "fake", "model": "fake-upscaler", "provider_version": "1.0",
            "required_width": 1920, "required_height": 1080,
            "archived_path": None, "archived_checksum": None, "archived_at": None,
        }

        vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        assert provider.calls == ["s01"]  # a genuine fresh attempt was made
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["validation_result"] == "passed"


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
        """Required resolution is deliverable-aware (2026-08-03) -- derived
        from hypogeum.artifact_compatibility.DELIVERABLE_RESOLUTION_POLICY
        keyed by source_type, not a config field. Simulates a policy change
        by patching that table directly, proving an already-enhanced clip
        becomes incompatible and is re-enhanced when the deliverable's own
        required resolution changes."""
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

        # Deliverable resolution policy for main_episode raised to 4K --
        # even a previously-compliant enhanced clip is now incompatible.
        provider_4k = FakeEnhancementProvider(produce_width=3840, produce_height=2160)
        register_provider("fake_reqres", lambda: provider_4k)
        config_4k = _make_config(ENHANCEMENT_PROVIDER="fake_reqres")
        with patch.dict(
            "hypogeum.artifact_compatibility.DELIVERABLE_RESOLUTION_POLICY",
            {"main_episode": (3840, 2160)},
        ):
            vp = run_source_enhancement(vp, config_4k, episode_id, output_dir=ep_dir)
        assert provider_4k.calls == ["s01"]
        assert probe_resolution(clip_path) == (3840, 2160)

    def test_short_deliverable_uses_portrait_resolution(self, tmp_path):
        """The bug found in real production: a Short's asset_manifest entry
        must resolve the portrait (1080x1920) requirement, not the
        landscape default -- confirmed against a REAL provider output
        shape (720x1280 -> 1080x1920), matching what MUAPI's AI Video
        Upscaler actually returned for a Short in ep_f3619022."""
        episode_id = "ep_enh_short_portrait"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sh_short_001" / "sc_sh_short_001_s01.mp4"
        _make_clip(clip_path, 720, 1280)

        provider = FakeEnhancementProvider(produce_width=1080, produce_height=1920)
        register_provider("fake_portrait", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_portrait")
        vp = {
            "scenes": [{"scene_id": "sh_short_001_s01", "source_type": "short"}],
            "asset_manifest": {"assets": [{
                "scene_id": "sh_short_001_s01",
                "asset_id": f"episodes/{episode_id}/video/sh_short_001/sc_sh_short_001_s01",
                "model": "veo3.1-lite-text-to-video", "tier_used": 2,
            }]},
        }

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["validation_result"] == "passed"
        assert se["required_width"] == 1080
        assert se["required_height"] == 1920
        assert se["enhanced_resolution"] == "1080x1920"
        assert probe_resolution(clip_path) == (1080, 1920)

    def test_short_native_main_episode_uses_portrait_resolution(self, tmp_path):
        """Real production bug found running a shorts-only channel end to
        end: a short-native channel's single deliverable is tagged
        source_type='main_episode' (it IS the episode, there is no separate
        Short alongside it), so this stage judged its correct portrait clips
        against the landscape (1920x1080) requirement and rejected every
        one, even though the clip's real shape (e.g. 1092x1902) was exactly
        what SHORT_NATIVE_MODE produces. config.SHORT_NATIVE_MODE=True must
        route 'main_episode' to the same portrait policy as a real Short."""
        episode_id = "ep_enh_short_native"
        ep_dir = tmp_path
        clip_path = ep_dir / "video" / "sc_s01.mp4"
        _make_clip(clip_path, 720, 1280)

        provider = FakeEnhancementProvider(produce_width=1080, produce_height=1920)
        register_provider("fake_portrait_native", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_portrait_native",
                              SHORT_NATIVE_MODE=True)
        vp = {
            "scenes": [{"scene_id": "s01", "source_type": "main_episode"}],
            "asset_manifest": {"assets": [{
                "scene_id": "s01",
                "asset_id": f"episodes/{episode_id}/video/sc_s01",
                "model": "veo3.1-lite-text-to-video", "tier_used": 2,
            }]},
        }

        result_vp = run_source_enhancement(vp, config, episode_id, output_dir=ep_dir)

        asset = result_vp["asset_manifest"]["assets"][0]
        se = asset["source_enhancement"]
        assert se["validation_result"] == "passed"
        assert se["required_width"] == 1080
        assert se["required_height"] == 1920


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
    def test_pre_plan_real_upscale_fails_closed_before_network(self, tmp_path, monkeypatch):
        """2026-08-28 hardening: a real MUAPI upscale with no compiled
        Production Plan at the canonical episode dir (PRE_PLAN) fails
        closed before any reservation or network call. The raw HTTP
        sequence is no longer a provider method -- it lives only in
        hypogeum.providers.muapi_upscale_adapter and is reachable only
        through a gate-minted, persisted-state-verified capability."""
        import requests as _requests
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        monkeypatch.setattr(
            "hypogeum.production_plan.canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep,
        )
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        config = _make_config(
            MUAPI_API_KEY="test-key", MUAPI_BASE_URL="https://api.muapi.ai/api/v1",
            MUAPI_TIMEOUT_SECONDS=30, MUAPI_MAX_POLL_ATTEMPTS=5, MUAPI_POLL_INTERVAL_SECONDS=0,
            MUAPI_DRY_RUN=False,
        )
        with patch.object(_requests, "post", side_effect=AssertionError("network reached pre-plan")), \
             patch.object(_requests, "get", side_effect=AssertionError("network reached pre-plan")):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=tmp_path / "dest.mp4",
                target_width=1920, target_height=1080, target_resolution_label="1080p",
                config=config, episode_id="ep_x", scene_id="s01", log=MagicMock(),
                channel_id="test_channel",
            )

        assert result.success is False
        assert "pre_plan_unauthorized" in result.failure_reason
        assert provider.name == "muapi"
        assert provider.model == "ai-video-upscaler"

    def test_no_provider_method_reaches_raw_http(self, tmp_path):
        """The provider exposes no method that reaches the MUAPI network
        boundary -- the removed _execute_upscale_http() is gone for good."""
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        provider = MuapiVideoUpscalerProvider()
        assert not hasattr(provider, "_execute_upscale_http")

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


# ---------------------------------------------------------------------------
# TestQualifyAsset (2026-08-05) — the canonical asset-qualification
# boundary called directly, standalone from run_source_enhancement()'s
# asset_manifest loop. This is what proves the boundary is genuinely
# reusable by any caller (VPD primaries via run_source_enhancement(), or
# clip_repair_agent's coverage-fill extras calling qualify_asset() itself)
# rather than logic only reachable through one fixed pipeline stage.
# ---------------------------------------------------------------------------

class TestQualifyAsset:
    def test_accepts_valid_native_target_resolution_clip(self, tmp_path):
        """A clip already at or above the required resolution is accepted
        without any provider call — zero cost, zero enhancement."""
        clip_path = tmp_path / "sc_x01.mp4"
        _make_clip(clip_path, 1920, 1080)

        provider = FakeEnhancementProvider()
        register_provider("fake_native", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_native")
        asset = {"scene_id": "x01", "source_enhancement": None}

        changed = qualify_asset(asset, clip_path, "main_episode", config, "ep_q", MagicMock())

        assert changed is True
        se = asset["source_enhancement"]
        assert se["validation_result"] == "skipped_native_resolution"
        assert se["applied"] is False
        assert provider.calls == []

    def test_enhances_a_valid_below_target_clip(self, tmp_path):
        """A real, valid clip below the required resolution is sent through
        the configured provider and promoted on a confirmed pass."""
        clip_path = tmp_path / "sc_x02.mp4"
        _make_clip(clip_path, 1280, 720)

        provider = FakeEnhancementProvider()
        register_provider("fake_enhance", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_enhance")
        asset = {"scene_id": "x02", "source_enhancement": None}

        changed = qualify_asset(asset, clip_path, "main_episode", config, "ep_q", MagicMock())

        assert changed is True
        se = asset["source_enhancement"]
        assert se["validation_result"] == "passed"
        assert se["enhanced_resolution"] == "1920x1080"
        assert provider.calls == ["x02"]
        assert probe_resolution(clip_path) == (1920, 1080)

    def test_rejects_corrupt_video(self, tmp_path):
        """An unreadable/corrupt source file must never be silently passed
        through as usable coverage — it is explicitly marked unusable and
        the (misconfigured-for-this-test-on-purpose) provider is never
        invoked, since there is nothing valid to enhance."""
        clip_path = tmp_path / "sc_x03.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"not a real video file, moov atom not found")

        provider = FakeEnhancementProvider()
        register_provider("fake_corrupt", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_corrupt")
        asset = {"scene_id": "x03", "source_enhancement": None}

        changed = qualify_asset(asset, clip_path, "main_episode", config, "ep_q", MagicMock())

        assert changed is True
        se = asset["source_enhancement"]
        assert se["validation_result"] == "rejected_corrupt_source"
        assert se["applied"] is False
        assert provider.calls == []

    def test_decision_is_invariant_to_provider_and_model_identity(self, tmp_path):
        """qualify_asset() must classify two clips identically by measured
        resolution alone — never by which provider/model produced them.
        Two below-target clips claiming totally different, unrelated
        provider/model identities on the asset dict must both be enhanced
        the same way; two native-resolution clips with different
        identities must both be skipped the same way."""
        provider = FakeEnhancementProvider()
        register_provider("fake_invariant", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_invariant")

        below_a = tmp_path / "sc_below_veo.mp4"
        _make_clip(below_a, 1280, 720)
        asset_a = {"scene_id": "below_veo", "model": "veo3-text-to-video", "tier_used": 1,
                   "source_enhancement": None}
        qualify_asset(asset_a, below_a, "main_episode", config, "ep_q", MagicMock())

        below_b = tmp_path / "sc_below_runway.mp4"
        _make_clip(below_b, 1280, 720)
        asset_b = {"scene_id": "below_runway", "model": "runway-text-to-video", "tier_used": 2,
                   "source_enhancement": None}
        qualify_asset(asset_b, below_b, "main_episode", config, "ep_q", MagicMock())

        assert asset_a["source_enhancement"]["validation_result"] == "passed"
        assert asset_b["source_enhancement"]["validation_result"] == "passed"
        assert asset_a["source_enhancement"]["enhanced_resolution"] == \
            asset_b["source_enhancement"]["enhanced_resolution"]

        native_a = tmp_path / "sc_native_veo.mp4"
        _make_clip(native_a, 1920, 1080)
        asset_c = {"scene_id": "native_veo", "model": "veo3-text-to-video", "tier_used": 1,
                   "source_enhancement": None}
        qualify_asset(asset_c, native_a, "main_episode", config, "ep_q", MagicMock())

        native_b = tmp_path / "sc_native_runway.mp4"
        _make_clip(native_b, 1920, 1080)
        asset_d = {"scene_id": "native_runway", "model": "runway-text-to-video", "tier_used": 2,
                   "source_enhancement": None}
        qualify_asset(asset_d, native_b, "main_episode", config, "ep_q", MagicMock())

        assert asset_c["source_enhancement"]["validation_result"] == "skipped_native_resolution"
        assert asset_d["source_enhancement"]["validation_result"] == "skipped_native_resolution"


# ---------------------------------------------------------------------------
# TestBlackTailDetection / TestQualifyAssetRejectsBlackTail
# 2026-08-07 — Finding 2 root-cause fix
#
# Reproduces the real ep_f3619022 Short 001 defect: an extra coverage clip
# whose content faded to pure black partway through and stayed black
# through the end of the file. Confirmed against the real source clip
# (.hypogeum-assets/episodes/ep_f3619022/video/sh_short_001/
# sc_sh_short_001_s06__extra1.mp4): blackdetect measured black_start=4.83,
# black_end=7.96 on an 8.0s clip. hypogeum.agents.video_renderer only
# trims/stretches the LAST clip in a multi-clip beat, so a black tail on
# any earlier clip reaches the final render completely unexamined --
# qualify_asset() is the correct, single boundary to catch it before that
# can happen, for every caller (VPD primaries and clip_repair_agent extras
# alike).
# ---------------------------------------------------------------------------

class TestBlackTailDetection:

    def test_clean_clip_has_no_black_tail(self, tmp_path):
        clip_path = tmp_path / "clean.mp4"
        _make_clip(clip_path, 1280, 720, duration=5.0)
        assert _clip_has_black_tail(clip_path) is False

    def test_real_defect_shape_is_detected(self, tmp_path):
        """Same proportions as the real defect: ~60% real content, ~40%
        black tail, on an 8s clip."""
        clip_path = tmp_path / "black_tail.mp4"
        _make_black_tail_clip(clip_path, 1080, 1920, real_seconds=4.83, black_seconds=3.17)
        assert _clip_has_black_tail(clip_path) is True

    def test_black_flash_that_recovers_is_not_a_tail(self, tmp_path):
        """A brief black region in the MIDDLE of a clip that recovers to
        real content before the file ends is not this defect -- only a
        black region touching (or almost touching) the clip's own end
        counts."""
        clip_path = tmp_path / "recovers.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=24:d=3",
             "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=24:d=2",
             "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=24:d=3",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            capture_output=True, timeout=30,
        )
        assert _clip_has_black_tail(clip_path) is False

    def test_corrupt_clip_is_not_flagged_as_black_tail(self, tmp_path):
        """Corrupt/unreadable files are the corrupt-source check's job, not
        this one -- must not raise, must return False so qualify_asset()'s
        own corrupt-source check (which runs first) is the one that
        classifies it."""
        clip_path = tmp_path / "corrupt.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"not a real video file")
        assert _clip_has_black_tail(clip_path) is False


class TestQualifyAssetRejectsBlackTail:

    def test_rejects_clip_with_black_tail(self, tmp_path):
        """The exact real-world shape: an otherwise-valid, correctly-
        resolved clip whose content goes black 4.83s into an 8s duration
        and never recovers must be rejected -- and, since there is
        nothing valid to enhance, the provider must never be called."""
        clip_path = tmp_path / "sc_black_tail.mp4"
        _make_black_tail_clip(clip_path, 1920, 1080, real_seconds=4.83, black_seconds=3.17)

        provider = FakeEnhancementProvider()
        register_provider("fake_black_tail", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_black_tail")
        asset = {"scene_id": "s06__extra1", "source_enhancement": None}

        changed = qualify_asset(asset, clip_path, "short", config, "ep_q", MagicMock())

        assert changed is True
        se = asset["source_enhancement"]
        assert se["validation_result"] == "rejected_black_tail"
        assert se["applied"] is False
        assert provider.calls == []

    def test_clean_clip_at_native_resolution_still_passes(self, tmp_path):
        """The new check must not introduce any false positive on the
        existing, already-covered clean/native-resolution path."""
        clip_path = tmp_path / "sc_clean.mp4"
        _make_clip(clip_path, 1920, 1080, duration=5.0)

        provider = FakeEnhancementProvider()
        register_provider("fake_black_tail_clean", lambda: provider)
        config = _make_config(ENHANCEMENT_PROVIDER="fake_black_tail_clean")
        asset = {"scene_id": "s01", "source_enhancement": None}

        changed = qualify_asset(asset, clip_path, "main_episode", config, "ep_q", MagicMock())

        assert changed is True
        se = asset["source_enhancement"]
        assert se["validation_result"] == "skipped_native_resolution"
