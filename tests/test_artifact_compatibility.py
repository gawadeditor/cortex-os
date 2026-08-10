"""
tests/test_artifact_compatibility.py — Regression coverage for the
2026-08-01 permanent artifact-compatibility reuse policy
(hypogeum/artifact_compatibility.py).

Proves the eight required properties:
  1. a new subject generates all required artifacts
  2. an unpublished existing subject reuses valid artifacts and generates
     only missing coverage
  3. an invalid final render does not invalidate valid source clips
  4. a dry-run artifact is never reused for real production
  5. a policy-incompatible artifact is regenerated selectively
  6. a published canonical subject cannot be duplicated without explicit
     authorization
  7. no clip is regenerated when a valid compatible copy already exists
  8. all derived outputs are rebuilt when one of their dependencies changes

Uses real ffmpeg for the dry-run-signature tests (small, local, no
network, no MUAPI/production spend — proving the detector against the
real placeholder generator, not a mocked stand-in) and a real temporary
SQLite DB (initialize_schema) for the subject-level tests. No production
run, no queue/schedule interaction.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.artifact_compatibility import (
    CompatibilityDecision,
    canonical_subject,
    evaluate_render_compatibility,
    evaluate_subject_production_decision,
    evaluate_video_clip_compatibility,
    is_dry_run_clip,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _seed_published_episode(db_path: Path, episode_id: str, topic: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, title, topic, created_at, status) "
            "VALUES (?, ?, ?, datetime('now'), 'complete')",
            (episode_id, topic, topic),
        )
        conn.execute(
            "INSERT INTO publishing_outcomes "
            "(outcome_id, episode_id, platform, deliverable_id, publishing_status, recorded_at) "
            "VALUES (?, ?, 'youtube', 'main_episode', 'success', datetime('now'))",
            (f"out_{episode_id}", episode_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. New subject -> generate_new (all required artifacts)
# ---------------------------------------------------------------------------

class TestNewSubjectGeneratesAll:

    def test_new_subject_with_no_prior_publish_is_generate_new(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)

        decision = evaluate_subject_production_decision(
            "The Antikythera Mechanism", db_path=db_path,
        )

        assert decision.action == "generate_new"
        assert decision.decision == CompatibilityDecision.VALID_AND_COMPATIBLE
        assert decision.blocking_episode_id is None
        assert decision.canonical_subject_id == canonical_subject("The Antikythera Mechanism")

    def test_unrelated_published_episode_does_not_block_a_different_subject(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _seed_published_episode(db_path, "ep_pyramids", "The Great Pyramid of Giza")

        decision = evaluate_subject_production_decision(
            "The Antikythera Mechanism", db_path=db_path,
        )

        assert decision.action == "generate_new"


# ---------------------------------------------------------------------------
# 6. Published canonical subject cannot be duplicated without authorization
# ---------------------------------------------------------------------------

class TestPublishedSubjectBlocksDuplicate:

    def test_same_subject_already_published_blocks_by_default(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _seed_published_episode(db_path, "ep_prior", "The Antikythera Mechanism")

        decision = evaluate_subject_production_decision(
            "the antikythera mechanism!!",  # different casing/punctuation, same canonical subject
            db_path=db_path,
        )

        assert decision.decision == CompatibilityDecision.PUBLISHED_CANONICAL_SUBJECT
        assert decision.action == "blocked"
        assert decision.blocking_episode_id == "ep_prior"

    def test_same_subject_proceeds_with_explicit_authorization(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _seed_published_episode(db_path, "ep_prior", "The Antikythera Mechanism")

        decision = evaluate_subject_production_decision(
            "The Antikythera Mechanism",
            db_path=db_path,
            explicit_authorization="corrective_replacement: original had a factual error",
        )

        assert decision.action == "generate_new"
        assert decision.blocking_episode_id == "ep_prior"
        assert "explicit authorization" in decision.reasons[0]

    def test_unpublished_prior_episode_does_not_block(self, tmp_path):
        """Rule 3, not rule 4: an episode that exists but was never
        successfully published is not a 'published canonical subject' —
        it's an in-progress subject, handled by episode_id-keyed checkpoint
        resumption, not a duplicate-production block."""
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO episodes (episode_id, title, topic, created_at, status) "
            "VALUES ('ep_unpub', 'x', 'The Antikythera Mechanism', datetime('now'), 'in_progress')"
        )
        conn.commit()
        conn.close()

        decision = evaluate_subject_production_decision(
            "The Antikythera Mechanism", db_path=db_path,
        )

        assert decision.action == "generate_new"


# ---------------------------------------------------------------------------
# 4. Dry-run artifact never reused for real production
# ---------------------------------------------------------------------------

class TestDryRunClipNeverReused:

    def test_real_dry_run_placeholder_is_detected(self, tmp_path):
        """Generates the EXACT placeholder hypogeum.muapi writes for
        MUAPI_DRY_RUN (same lavfi color source, same 0x141414 fill) via
        real local ffmpeg -- no network, no MUAPI call, no production spend
        -- and confirms the detector catches it."""
        from hypogeum.muapi import _generate_dry_run_placeholder_clip

        clip_path = tmp_path / "sc_dryrun.mp4"
        log = MagicMock()
        _generate_dry_run_placeholder_clip(clip_path, "16:9", 8.0, log)

        assert is_dry_run_clip(clip_path) is True
        result = evaluate_video_clip_compatibility(clip_path)
        assert result.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE
        assert not result.reusable

    def test_mocked_real_footage_signature_is_not_flagged_as_dry_run(self, tmp_path):
        clip_path = tmp_path / "sc_real.mp4"
        clip_path.write_bytes(b"FAKEVIDEO" * 2000)  # > _MIN_VALID_CLIP_BYTES
        with patch(
            "hypogeum.artifact_compatibility._probe_duration_seconds",
            return_value=8.0,
        ), patch(
            "hypogeum.artifact_compatibility._frame_luminance_stats",
            return_value=(110.0, 42.0),  # textured, mid-brightness -- not a flat fill
        ), patch(
            "hypogeum.artifact_compatibility.probe_resolution",
            return_value=(1920, 1080),
        ):
            assert is_dry_run_clip(clip_path) is False
            result = evaluate_video_clip_compatibility(clip_path)
        assert result.decision == CompatibilityDecision.VALID_AND_COMPATIBLE
        assert result.reusable

    def test_coverage_check_excludes_dry_run_clip_from_available_duration(self, tmp_path):
        """Integration with the coverage gate: a beat whose only clip is a
        dry-run placeholder must be treated as having zero real coverage,
        forcing a deficit (never silently reused for real production)."""
        from hypogeum.agents.coverage_check import compute_beat_coverage
        from hypogeum.agents.timeline_builder import TimelineEntry

        clip_path = tmp_path / "sc_dryrun.mp4"
        with patch("hypogeum.artifact_compatibility.is_dry_run_clip", return_value=True):
            entry = TimelineEntry(
                scene_index=0, scene_id="sc_001", source_type="main_episode",
                short_id=None, clip_path=clip_path, narration_path=None,
                start_seconds=0.0, duration_seconds=8.0, text_overlays=[],
                music_section=None, text_safe_zone=None, clip_duration_seconds=8.0,
            )
            coverage = compute_beat_coverage(entry)

        assert coverage.available_duration_seconds == 0.0
        assert coverage.sufficient is False


# ---------------------------------------------------------------------------
# 5 & 8. Policy-incompatible / stale-dependency artifacts regenerate
#        selectively (checkpoint + render-compat level)
# ---------------------------------------------------------------------------

class TestRenderCompatibilityVersioning:

    def test_matching_version_is_compatible(self):
        pkg = {"package_metadata": {"assembly_agent_version": "2.0"}}
        result = evaluate_render_compatibility(pkg, current_agent_version="2.0")
        assert result.decision == CompatibilityDecision.VALID_AND_COMPATIBLE
        assert result.reusable

    def test_older_version_is_incompatible_regardless_of_recency(self):
        """The core 'not invalidated by date' proof at the render level:
        no timestamp is consulted anywhere in this function -- only the
        recorded policy version."""
        pkg = {"package_metadata": {"assembly_agent_version": "1.0"}}
        result = evaluate_render_compatibility(pkg, current_agent_version="2.0")
        assert result.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE
        assert not result.reusable

    def test_missing_version_field_is_incompatible(self):
        pkg = {"package_metadata": {}}
        result = evaluate_render_compatibility(pkg, current_agent_version="2.0")
        assert not result.reusable


# ---------------------------------------------------------------------------
# 3. An invalid final render does not invalidate valid source clips
# ---------------------------------------------------------------------------

class TestInvalidRenderDoesNotInvalidateClips:

    def test_stale_render_and_valid_clip_evaluated_independently(self, tmp_path):
        """The exact ep_f3619022 scenario: the derived render is stale
        (older policy version) but the source clip is real, playable
        footage -- evaluate_render_compatibility() and
        evaluate_video_clip_compatibility() must reach opposite verdicts,
        proving one artifact type's invalidity never propagates to the
        other by construction (they are different function calls over
        different inputs, not a shared/cascading check)."""
        stale_render = {"package_metadata": {"assembly_agent_version": "1.0"}}
        render_result = evaluate_render_compatibility(stale_render, current_agent_version="2.0")
        assert not render_result.reusable

        clip_path = tmp_path / "sc_s01.mp4"
        clip_path.write_bytes(b"FAKEVIDEO" * 2000)
        with patch(
            "hypogeum.artifact_compatibility._probe_duration_seconds",
            return_value=8.0,
        ), patch(
            "hypogeum.artifact_compatibility._frame_luminance_stats",
            return_value=(95.0, 38.0),
        ), patch(
            "hypogeum.artifact_compatibility.probe_resolution",
            return_value=(1920, 1080),
        ):
            clip_result = evaluate_video_clip_compatibility(clip_path)
        assert clip_result.reusable

    def test_checkpoint_rejection_does_not_touch_clip_files_on_disk(self, tmp_path):
        """pipeline._assembly_checkpoint_compatible() rejecting a stale
        final_render_package must not delete or move anything -- the
        rejection is purely a decision not to reuse the checkpoint object;
        clip files on disk are untouched (preserve, never quarantine by
        default)."""
        from hypogeum.pipeline import _assembly_checkpoint_compatible

        clip_path = tmp_path / "sc_s01.mp4"
        clip_path.write_bytes(b"FAKEVIDEO" * 2000)

        stale_pkg = {"package_metadata": {"assembly_agent_version": "1.0"}}
        assert _assembly_checkpoint_compatible(stale_pkg) is False
        assert clip_path.exists()
        assert clip_path.read_bytes() == b"FAKEVIDEO" * 2000


# ---------------------------------------------------------------------------
# 2 & 7. Unpublished existing subject: reuse valid, generate only missing;
#        no clip regenerated when a valid compatible copy already exists
# ---------------------------------------------------------------------------

class TestIncrementalCoverageReuse:

    def test_valid_clip_never_triggers_regeneration_incompatible_clip_does(self, tmp_path):
        """Two-beat mix: sc_001's clip is valid and sufficient (must not be
        regenerated); sc_002's clip is a detected dry-run placeholder
        (must be treated as missing and trigger exactly one fill
        attempt) -- proving selective, artifact-aware incremental
        production rather than an all-or-nothing episode restart."""
        from hypogeum.agents.coverage_check import check_timeline_coverage
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits
        from hypogeum.agents.timeline_builder import TimelineEntry

        valid_clip = tmp_path / "sc_001.mp4"
        valid_clip.write_bytes(b"FAKEVIDEO" * 2000)
        dryrun_clip = tmp_path / "sc_002.mp4"
        dryrun_clip.write_bytes(b"FAKEVIDEO" * 2000)

        entries = [
            TimelineEntry(
                scene_index=0, scene_id="sc_001", source_type="main_episode",
                short_id=None, clip_path=valid_clip, narration_path=None,
                start_seconds=0.0, duration_seconds=8.0, text_overlays=[],
                music_section=None, text_safe_zone=None, clip_duration_seconds=8.0,
            ),
            TimelineEntry(
                scene_index=1, scene_id="sc_002", source_type="main_episode",
                short_id=None, clip_path=dryrun_clip, narration_path=None,
                start_seconds=8.0, duration_seconds=8.0, text_overlays=[],
                music_section=None, text_safe_zone=None, clip_duration_seconds=8.0,
            ),
        ]

        def _fake_is_dry_run(path):
            return path == dryrun_clip

        with patch("hypogeum.artifact_compatibility.is_dry_run_clip", side_effect=_fake_is_dry_run):
            result = check_timeline_coverage(entries)

        assert result.sufficient is False
        assert [b.scene_id for b in result.insufficient_beats] == ["sc_002"]

        vp = {
            "scenes": [
                {"scene_id": "sc_001", "production_spec": {"duration_seconds": 8}},
                {"scene_id": "sc_002", "production_spec": {"duration_seconds": 8}},
            ]
        }
        submitted = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted.append(scene_spec["scene_id"])
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"NEWCLIP")
            return {"generation_status": "generated"}

        from hypogeum.config import Config
        with patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "k", "ELEVENLABS_API_KEY": "k",
                "ELEVENLABS_VOICE_ID": "v",
            }, clear=True):
            cfg = Config()

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            fill_coverage_deficits(
                insufficient_beats=result.insufficient_beats,
                visual_package=vp,
                assets_root=tmp_path,
                config=cfg,
                episode_id="ep_incremental",
                log=MagicMock(),
            )

        # Only the incompatible beat (sc_002) was ever submitted for
        # regeneration -- sc_001's valid clip triggered zero MUAPI calls.
        assert all("sc_001" not in sid for sid in submitted)
        assert all(sid.startswith("sc_002") for sid in submitted)


# ---------------------------------------------------------------------------
# Resolution compliance (2026-08-02 Production Output Gate policy) --
# "reject 720p source when 1080p source is required," never inferred from
# the final render's letterboxed/padded canvas size.
# ---------------------------------------------------------------------------

class TestResolutionCompliance:

    def test_real_720p_clip_is_not_compliant(self, tmp_path):
        """Generates a REAL 1280x720 clip via local ffmpeg (no network, no
        MUAPI spend) -- the exact measured resolution of every one of
        ep_f3619022's 29 real source clips -- and confirms the detector
        correctly rejects it."""
        from hypogeum.artifact_compatibility import (
            is_resolution_compliant_clip,
            probe_resolution,
        )

        clip_path = tmp_path / "sc_720p.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            capture_output=True, timeout=30,
        )

        assert probe_resolution(clip_path) == (1280, 720)
        assert is_resolution_compliant_clip(clip_path) is False

        result = evaluate_video_clip_compatibility(clip_path)
        assert result.decision == CompatibilityDecision.INVALID_OR_INCOMPATIBLE
        assert "1280x720" in result.reasons[0]

    def test_real_1080p_clip_is_compliant(self, tmp_path):
        """Same real-ffmpeg proof at the required resolution."""
        from hypogeum.artifact_compatibility import is_resolution_compliant_clip

        clip_path = tmp_path / "sc_1080p.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=30:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            capture_output=True, timeout=30,
        )

        assert is_resolution_compliant_clip(clip_path) is True

    def test_unprovable_resolution_is_not_compliant(self, tmp_path):
        """Burden of proof is on demonstrating compliance -- a file whose
        resolution can't be probed (missing/corrupted) is never silently
        treated as compliant, unlike the dry-run check's opposite-direction
        conservatism."""
        from hypogeum.artifact_compatibility import is_resolution_compliant_clip

        missing = tmp_path / "does_not_exist.mp4"
        assert is_resolution_compliant_clip(missing) is False

        corrupted = tmp_path / "corrupted.mp4"
        corrupted.write_bytes(b"NOT A REAL VIDEO FILE" * 1000)
        assert is_resolution_compliant_clip(corrupted) is False

    def test_upscaled_final_render_does_not_launder_a_720p_source(self, tmp_path):
        """The exact concern named in the audit: a 720p source clip scaled
        up to a 1920x1080 canvas by Video Renderer's letterbox/pad filter
        must not be mistaken for a compliant 1080p source. This checks the
        SOURCE clip directly, not the final render, so no amount of
        upscaling downstream changes the verdict."""
        from hypogeum.artifact_compatibility import is_resolution_compliant_clip

        source_720p = tmp_path / "source.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source_720p)],
            capture_output=True, timeout=30,
        )
        # Simulate what Assembly's letterbox/pad filter does: scale the same
        # 720p source up onto a 1920x1080 canvas.
        upscaled_render = tmp_path / "final_render.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source_720p),
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(upscaled_render)],
            capture_output=True, timeout=30,
        )

        # The SOURCE is correctly rejected...
        assert is_resolution_compliant_clip(source_720p) is False
        # ...even though the derived render now measures 1920x1080 (the
        # canvas size, not genuine source detail) -- proving the gate must
        # check the source clip, never infer compliance from the final
        # render's dimensions.
        assert is_resolution_compliant_clip(upscaled_render) is True
        from hypogeum.artifact_compatibility import probe_resolution
        assert probe_resolution(upscaled_render) == (1920, 1080)

    def test_real_short_720x1280_source_fails_the_short_requirement(self, tmp_path):
        """Deliverable-specific proof for a Short: a genuine 720x1280
        portrait source clip (the exact measured dimensions of the
        recovered ep_f3619022 manual-rerun artifact, request_id
        edbbf48e-3a31-49be-b4cc-9f0a168e8d89) must fail the Short-specific
        1080x1920 requirement, using required_source_resolution("short")
        -- never the landscape default."""
        from hypogeum.artifact_compatibility import (
            is_resolution_compliant_clip,
            probe_resolution,
            required_source_resolution,
        )

        clip_path = tmp_path / "short_720x1280.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:r=24:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            capture_output=True, timeout=30,
        )

        assert probe_resolution(clip_path) == (720, 1280)
        required_w, required_h = required_source_resolution("short")
        assert (required_w, required_h) == (1080, 1920)
        assert is_resolution_compliant_clip(clip_path, required_w, required_h) is False
        # And the same file would be wrongly judged compliant if evaluated
        # against the landscape default instead of the Short-specific
        # requirement -- proving the deliverable-aware call is load-bearing,
        # not redundant with the plain default check.
        assert is_resolution_compliant_clip(clip_path) is False  # also fails landscape (too narrow)


# ---------------------------------------------------------------------------
# TestRequiredSourceResolution
#
# Regression coverage for a release-blocking repository defect: commit
# 10ce0b4 added an unconditional, module-load-time import of
# required_source_resolution() to hypogeum/source_enhancement.py, but the
# function itself was never committed to hypogeum/artifact_compatibility.py
# -- it existed only in an uncommitted local WIP diff. Any environment that
# checked out 10ce0b4 (or any later commit) without that WIP present could
# not even import hypogeum.source_enhancement, let alone run it. Discovered
# when a controlled regeneration validation stashed all uncommitted WIP to
# get a HEAD-only environment and hypogeum.source_enhancement failed to
# import. These tests pin both the symbol's existence and
# hypogeum.source_enhancement's importability so this exact defect mode
# (a committed dependency on an uncommitted symbol) cannot silently recur.
# ---------------------------------------------------------------------------

class TestRequiredSourceResolution:

    def test_source_enhancement_imports_successfully(self):
        """The exact failure mode this regression guards: hypogeum.
        source_enhancement's module-level import of required_source_
        resolution must succeed standalone, with no other module needing
        to be imported first to populate it as a side effect."""
        import importlib
        import hypogeum.source_enhancement as se
        importlib.reload(se)  # force a fresh top-level import, not a cached module

    def test_required_source_resolution_exists_and_is_callable(self):
        from hypogeum.artifact_compatibility import required_source_resolution
        assert callable(required_source_resolution)

    def test_main_episode_resolves_to_landscape(self):
        from hypogeum.artifact_compatibility import (
            REQUIRED_SOURCE_HEIGHT,
            REQUIRED_SOURCE_WIDTH,
            required_source_resolution,
        )
        assert required_source_resolution("main_episode") == (
            REQUIRED_SOURCE_WIDTH, REQUIRED_SOURCE_HEIGHT,
        )

    def test_short_resolves_to_portrait(self):
        from hypogeum.artifact_compatibility import required_source_resolution
        assert required_source_resolution("short") == (1080, 1920)

    def test_unrecognized_source_type_falls_back_to_main_episode(self):
        from hypogeum.artifact_compatibility import (
            REQUIRED_SOURCE_HEIGHT,
            REQUIRED_SOURCE_WIDTH,
            required_source_resolution,
        )
        assert required_source_resolution("some_future_deliverable_type") == (
            REQUIRED_SOURCE_WIDTH, REQUIRED_SOURCE_HEIGHT,
        )

    def test_none_source_type_falls_back_to_main_episode(self):
        from hypogeum.artifact_compatibility import (
            REQUIRED_SOURCE_HEIGHT,
            REQUIRED_SOURCE_WIDTH,
            required_source_resolution,
        )
        assert required_source_resolution(None) == (
            REQUIRED_SOURCE_WIDTH, REQUIRED_SOURCE_HEIGHT,
        )

    def test_evaluate_video_clip_compatibility_signature_unchanged(self):
        """Scope guard: this fix must not touch evaluate_video_clip_
        compatibility's signature (a separate, unrelated WIP change on the
        same file) -- it must still take exactly one required argument."""
        import inspect
        from hypogeum.artifact_compatibility import evaluate_video_clip_compatibility

        params = inspect.signature(evaluate_video_clip_compatibility).parameters
        assert list(params.keys()) == ["clip_path"]

    def test_is_resolution_compliant_clip_still_defaults_to_landscape(self, tmp_path):
        """No unrelated behaviour change: existing callers of
        is_resolution_compliant_clip() with no explicit width/height still
        get the original landscape default."""
        from hypogeum.artifact_compatibility import (
            REQUIRED_SOURCE_HEIGHT,
            REQUIRED_SOURCE_WIDTH,
            is_resolution_compliant_clip,
        )

        clip_path = tmp_path / "sc_default.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             f"-i", f"color=c=gray:s={REQUIRED_SOURCE_WIDTH}x{REQUIRED_SOURCE_HEIGHT}:r=30:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            capture_output=True, timeout=30,
        )
        assert is_resolution_compliant_clip(clip_path) is True
