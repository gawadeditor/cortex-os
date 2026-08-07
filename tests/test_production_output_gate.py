"""
tests/test_production_output_gate.py — Regression coverage for the
2026-08-02 Production Output Gate (hypogeum/agents/production_output_gate.py),
the single gate immediately before Publishing.

Uses real, small, local ffmpeg-generated fixtures for every pixel/frame
check (resolution, dry-run placeholder, burned captions) — no network, no
MUAPI/production spend, no queue/schedule interaction, no real production
run. Proves the gate re-inspects real files rather than trusting JSON
status fields, that it fails closed on any single capability, and that
publishing_agent.py actually halts on a failed gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.production_output_gate import (
    GateCheckResult,
    ProductionOutputGateFailure,
    enforce_production_output_gate,
    run_production_output_gate,
)


# ---------------------------------------------------------------------------
# Fixture helpers — real, tiny, local ffmpeg output only
# ---------------------------------------------------------------------------

def _make_clip(path: Path, width: int, height: int, duration: float = 1.0, source: str = "testsrc") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"{source}=s={width}x{height}:r=30:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=30,
    )


def _make_dry_run_placeholder(path: Path, width: int = 1280, height: int = 720, duration: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x141414:s={width}x{height}:r=30:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=30,
    )


def _minimal_episode(tmp_path: Path, episode_id: str) -> tuple[Path, Path]:
    """Sets up assets_root/episodes/{episode_id}/ with a compliant, passing
    episode: one 1920x1080 source clip, a current-version final_render_package,
    a current-version concept-driven real thumbnail, no Shorts, no end-page."""
    assets_root = tmp_path
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)

    _make_clip(ep_dir / "video" / "sc_s01.mp4", 1920, 1080)
    _make_clip(ep_dir / f"main_{episode_id}.mp4", 1920, 1080, duration=2.0)

    from hypogeum.agents.assembly_agent import _AGENT_VERSION as assembly_version
    from hypogeum.agents.thumbnail_director import _AGENT_VERSION as thumbnail_version

    visual_package = {
        "scenes": [{
            "scene_id": "s01",
            "asset_id": f"episodes/{episode_id}/video/sc_s01",
            "source_type": "main_episode",
        }],
        "asset_manifest": {"assets": [{
            "scene_id": "s01",
            "asset_id": f"episodes/{episode_id}/video/sc_s01",
            "model": "veo3.1-lite-text-to-video",
            "tier_used": 2,
            "fallback_used": False,
        }]},
    }
    (ep_dir / f"visual_package_{episode_id}.json").write_text(json.dumps(visual_package))

    # A real (non-placeholder) thumbnail: textured, not a flat fill, with a
    # real burned headline in the deterministic typography zone (x 2-42%,
    # y 30-88% of 1280x720 — hypogeum.agents.thumbnail_compositor.
    # _render_typography_pil's own documented zone) so the gate's OCR-based
    # embedded-headline check has real, findable pixels.
    thumb_path = ep_dir / "thumbnails" / "candidate_1.jpg"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=1:d=1",
         "-vframes", "1", str(thumb_path)],
        capture_output=True, timeout=30,
    )
    from PIL import Image, ImageDraw
    _thumb_img = Image.open(thumb_path).convert("RGB")
    _draw = ImageDraw.Draw(_thumb_img)
    _draw.rectangle((30, 220, 530, 630), fill=(0, 0, 0))
    _draw.text((45, 240), "ANCIENT SECRETS", fill=(255, 255, 255))
    _thumb_img.save(thumb_path)

    thumbnail_package = {
        "main_thumbnail": {"asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"},
        "short_thumbnails": [],
        "candidates": [
            {
                "asset_id": f"episodes/{episode_id}/thumbnails/candidate_1",
                "typography_plan": {"headline": "ANCIENT SECRETS", "emphasis": "REVEALED"},
            },
            {
                "asset_id": f"episodes/{episode_id}/thumbnails/candidate_2",
                "typography_plan": {"headline": "ALT HEADLINE"},
            },
            {
                "asset_id": f"episodes/{episode_id}/thumbnails/candidate_3",
                "typography_plan": {"headline": "ALT HEADLINE TWO"},
            },
        ],
        "candidates_generated": 3,
        "candidates_required": 3,
        "generation_metadata": {
            "thumbnail_director_version": thumbnail_version,
            "concept_driven": True,
        },
    }
    (ep_dir / f"thumbnail_package_{episode_id}.json").write_text(json.dumps(thumbnail_package))

    final_render_package = {
        "package_metadata": {"assembly_agent_version": assembly_version},
        "episode_render": {
            "video_asset_id": f"episodes/{episode_id}/main_{episode_id}",
            "duration_seconds": 2.0,
        },
        "shorts_renders": [],
        "coverage_check_summary": {
            "sufficient": True,
            "beats_checked": 1,
            "insufficient_beats": [],
            "max_editorial_speed_factor": 1.15,
        },
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(final_render_package))

    return assets_root, ep_dir


# ---------------------------------------------------------------------------
# A. VIDEO
# ---------------------------------------------------------------------------

class TestVideoCheck:

    def test_passes_when_all_clips_meet_required_resolution(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_video_pass")
        result = run_production_output_gate("ep_video_pass", assets_root, MagicMock(), MagicMock())
        video_check = next(c for c in result.checks if c.capability == "A_VIDEO")
        assert video_check.status == "pass"

    def test_fails_on_a_real_720p_source_clip(self, tmp_path):
        """The exact ep_f3619022 scenario: real, measured 1280x720 source
        footage must fail this check regardless of what model/tier
        generated it."""
        episode_id = "ep_video_720p"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        # Overwrite the compliant clip with a real 720p one.
        _make_clip(ep_dir / "video" / "sc_s01.mp4", 1280, 720)

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        video_check = next(c for c in result.checks if c.capability == "A_VIDEO")
        assert video_check.status == "fail"
        assert "1280x720" in json.dumps(video_check.evidence)
        assert result.passed is False


# ---------------------------------------------------------------------------
# B. VISUAL PLAYBACK
# ---------------------------------------------------------------------------

class TestVisualPlaybackCheck:

    def test_passes_with_sufficient_coverage_and_no_freeze(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_playback_pass")
        result = run_production_output_gate("ep_playback_pass", assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "pass"
        assert "coverage_check_summary" in check.evidence
        assert check.evidence["provenance_only_guarantees"]["properties"]

    def test_fails_when_coverage_check_summary_missing(self, tmp_path):
        episode_id = "ep_playback_no_coverage"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        del pkg["coverage_check_summary"]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "fail"
        assert "coverage_check_summary" in check.reason

    def test_fails_when_coverage_insufficient(self, tmp_path):
        episode_id = "ep_playback_insufficient_coverage"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["coverage_check_summary"] = {
            "sufficient": False, "beats_checked": 2,
            "insufficient_beats": ["s01"], "max_editorial_speed_factor": 1.15,
        }
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "fail"
        assert "coverage_check.py" in check.reason or "insufficient" in check.reason.lower()

    def test_fails_when_renderer_version_stale(self, tmp_path):
        episode_id = "ep_playback_stale_version"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["package_metadata"]["assembly_agent_version"] = "1.0"
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "fail"
        assert "renderer" in check.reason.lower() or "version" in check.reason.lower()

    def test_legitimate_end_page_hold_is_not_misclassified_as_body_freeze(self, tmp_path):
        """Regression test for the confirmed bug: _check_visual_playback used
        to hardcode end_page_duration=0.0, which made ANY episode with a
        real end-page fail this check (the end-page's own intentional
        static hold was misread as an illegal body freeze). Builds a real
        video: 2s of motion (body) + 3s of a static held frame (the
        end-page hold), with a real end-page.png present, and confirms the
        gate now passes."""
        episode_id = "ep_playback_endpage_ok"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        (assets_root / "end-page.png").write_bytes(
            (assets_root / "episodes" / episode_id / "thumbnails" / "candidate_1.jpg").read_bytes()
        )

        main_mp4 = ep_dir / f"main_{episode_id}.mp4"
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1920x1080:r=30:d=2",
             "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=30:d=3",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(main_mp4)],
            capture_output=True, timeout=30,
        )

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["episode_render"]["duration_seconds"] = 2.0
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "pass", check.evidence

    def test_real_freeze_in_body_still_fails_even_with_end_page_present(self, tmp_path):
        """The fix must not swallow genuine body freezes -- only the
        end-page's own tail hold is exempted. Body: 3s static hold
        (illegal) + 2s motion, then a 3s end-page hold. The gate must
        still catch the body freeze."""
        episode_id = "ep_playback_real_body_freeze"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        (assets_root / "end-page.png").write_bytes(
            (assets_root / "episodes" / episode_id / "thumbnails" / "candidate_1.jpg").read_bytes()
        )

        main_mp4 = ep_dir / f"main_{episode_id}.mp4"
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30:d=3",
             "-f", "lavfi", "-i", "testsrc=s=1920x1080:r=30:d=2",
             "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=30:d=3",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(main_mp4)],
            capture_output=True, timeout=30,
        )

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["episode_render"]["duration_seconds"] = 5.0  # body = the 3s blue hold + 2s motion
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "B_VISUAL_PLAYBACK")
        assert check.status == "fail"
        assert check.evidence.get("freeze_segments")


# ---------------------------------------------------------------------------
# C. THUMBNAIL
# ---------------------------------------------------------------------------

class TestThumbnailCheck:

    def test_passes_on_current_version_real_pixel_thumbnail(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_thumb_pass")
        result = run_production_output_gate("ep_thumb_pass", assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "pass"

    def test_fails_when_headline_metadata_missing(self, tmp_path):
        episode_id = "ep_thumb_no_headline_meta"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"thumbnail_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["candidates"][0]["typography_plan"]["headline"] = ""
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "headline" in check.reason.lower()

    def test_fails_when_headline_not_visible_in_pixels(self, tmp_path):
        """Metadata says there's a headline, but the actual image has no
        burned text in the typography zone -- OCR must catch this."""
        episode_id = "ep_thumb_no_headline_pixels"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        thumb_path = ep_dir / "thumbnails" / "candidate_1.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=1:d=1",
             "-vframes", "1", str(thumb_path)],
            capture_output=True, timeout=30,
        )

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "headline" in check.reason.lower()

    def test_fails_on_wrong_dimensions(self, tmp_path):
        episode_id = "ep_thumb_wrong_dims"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        thumb_path = ep_dir / "thumbnails" / "candidate_1.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1024x1024:r=1:d=1",
             "-vframes", "1", str(thumb_path)],
            capture_output=True, timeout=30,
        )

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "thumbnail_qa_report" in check.evidence or "1024" in json.dumps(check.evidence)

    def test_fails_on_insufficient_candidate_count(self, tmp_path):
        """run_thumbnail_qa's Check E fails only at zero candidates (fewer
        than required but non-zero is a warning, not a failure) -- this
        proves the gate re-runs Check E for real rather than trusting a
        stale persisted report."""
        episode_id = "ep_thumb_few_candidates"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"thumbnail_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["candidates"] = []
        pkg["candidates_generated"] = 0
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"

    def test_fails_on_real_dry_run_placeholder_pixels(self, tmp_path):
        """The exact ep_f3619022 scenario: the published thumbnail measured
        mean=20.5/stddev=9.3, an exact match for the MUAPI dry-run
        placeholder. Re-generates that real signature and confirms the
        gate independently re-inspects the actual pixels rather than
        trusting the persisted thumbnail_qa_report."""
        from hypogeum.muapi import _generate_dry_run_placeholder_clip

        episode_id = "ep_thumb_placeholder"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        thumb_path = ep_dir / "thumbnails" / "candidate_1.jpg"
        # Overwrite with a real placeholder-signature JPEG.
        from PIL import Image
        img = Image.new("RGB", (1280, 720), (20, 20, 20))
        img.save(thumb_path, "JPEG")

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "placeholder" in check.reason.lower()

    def test_fails_when_thumbnail_package_predates_current_version(self, tmp_path):
        episode_id = "ep_thumb_stale"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"thumbnail_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["generation_metadata"]["thumbnail_director_version"] = "1.0"
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "1.0" in check.reason

    def test_fails_when_not_concept_driven(self, tmp_path):
        episode_id = "ep_thumb_legacy"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"thumbnail_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["generation_metadata"]["concept_driven"] = False
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "C_THUMBNAIL")
        assert check.status == "fail"
        assert "concept_driven" in check.reason.lower() or "CONCEPT_DRIVEN" in check.reason


# ---------------------------------------------------------------------------
# D. SHORTS CAPTIONS
# ---------------------------------------------------------------------------

class TestShortsCaptionsCheck:

    def test_passes_when_no_shorts_present(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_no_shorts")
        result = run_production_output_gate("ep_no_shorts", assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "D_SHORTS_CAPTIONS")
        assert check.status == "pass"

    def test_fails_when_short_has_no_burned_in_pixels(self, tmp_path):
        """Reproduces the confirmed ep_f3619022 finding directly: a real
        Short rendered WITHOUT the subtitles filter (plain footage) must
        fail, proven from actual extracted frames, not from whether an
        srt_path parameter was set."""
        episode_id = "ep_shorts_no_burn"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        _make_clip(short_mp4, 1080, 1920, duration=3.0, source="testsrc")

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(
            "1\n00:00:00,100 --> 00:00:02,000\nThere is a passage in these tunnels\n"
        )

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "D_SHORTS_CAPTIONS")
        assert check.status == "fail"

    def test_passes_when_short_has_real_burned_in_captions(self, tmp_path):
        """Real end-to-end proof using the actual canonical caption-burn
        code path (video_renderer._build_caption_ass/_build_subtitles_filter)
        against real local ffmpeg -- if libass isn't installed in this
        environment, the burn silently produces plain video and this test
        is skipped rather than false-failing."""
        from hypogeum.agents.video_renderer import _build_caption_ass, _build_subtitles_filter

        episode_id = "ep_shorts_burned"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(
            "1\n00:00:00,100 --> 00:00:02,500\nThere is a passage in these tunnels\n"
        )
        ass_path = srt_path.with_suffix(".ass")
        assert _build_caption_ass(srt_path, "1080x1920", ass_path)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        short_mp4.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30:d=5",
             "-vf", _build_subtitles_filter(ass_path),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(short_mp4)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not short_mp4.exists():
            pytest.skip("ffmpeg in this environment cannot burn subtitles (no libass)")

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "D_SHORTS_CAPTIONS")
        assert check.status == "pass", check.evidence

    def test_fails_when_ffmpeg_lacks_subtitle_filter(self, tmp_path):
        """Real environment capability check, not just remediation-text
        advice -- when ffmpeg's 'subtitles' filter is unavailable, the gate
        must fail immediately with that specific reason, before ever
        attempting per-Short pixel inspection."""
        episode_id = "ep_shorts_no_libass"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        _make_clip(short_mp4, 1080, 1920, duration=3.0, source="testsrc")
        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text("1\n00:00:00,100 --> 00:00:02,000\nsome text\n")

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        with patch(
            "hypogeum.agents.production_output_gate._check_ffmpeg_subtitle_support",
            return_value="ffmpeg build has no 'subtitles' filter (libass) — caption burn-in cannot work",
        ):
            result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "D_SHORTS_CAPTIONS")
        assert check.status == "fail"
        assert "ffmpeg_capability_check" in check.evidence
        assert "per_short" not in check.evidence  # never reached per-Short inspection

    def test_real_ffmpeg_capability_check_detects_subtitles_filter(self):
        """Direct proof the capability check queries the real ffmpeg -filters
        output rather than assuming support -- this environment has libass
        (confirmed via -buildconf during the investigation this gate is
        based on), so the real check must return None (supported)."""
        from hypogeum.agents.production_output_gate import _check_ffmpeg_subtitle_support

        result = _check_ffmpeg_subtitle_support()
        assert result is None


# ---------------------------------------------------------------------------
# G. SHORTS VISUAL COVERAGE (2026-08-07 — Finding 2 root-cause fix)
#
# Reproduces the real ep_f3619022 Short 001 defect directly: a beat's
# non-last coverage clip faded to black partway through and, because
# video_renderer only trims/stretches the LAST clip in a multi-clip beat,
# played its black tail in full on screen while narration/captions were
# still active. Confirmed against the real files: blackdetect measured
# .hypogeum-assets/episodes/ep_f3619022/video/sh_short_001/
# sc_sh_short_001_s06__extra1.mp4 going black at 4.83s of its 8s duration,
# and the real rendered short_0_ep_f3619022.mp4 measured 99.92% black at
# its required coverage point (0.30s before the last caption ends at
# 85.50s) versus 39.02%/37.93% for the two unaffected Shorts (002/003).
# ---------------------------------------------------------------------------

class TestShortsVisualCoverageCheck:

    def _write_srt(self, path: Path, cues: list[tuple[str, str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, (start, end, text) in enumerate(cues, start=1):
            lines.append(f"{i}\n{start} --> {end}\n{text}\n")
        path.write_text("\n".join(lines))

    def test_passes_when_no_shorts_present(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_novisual_shorts")
        result = run_production_output_gate("ep_novisual_shorts", assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "pass"

    def test_fails_when_black_tail_extends_into_required_caption_window(self, tmp_path):
        """The exact Short 001 tail mismatch: 5s of real content followed
        by 3s of pure black, with the last caption cue still active at
        7.5s (well inside the black region) -- must fail."""
        episode_id = "ep_shorts_black_tail"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        short_mp4.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30:d=5",
             "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=3",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(short_mp4)],
            capture_output=True, timeout=30,
        )

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        self._write_srt(srt_path, [
            ("00:00:00,100", "00:00:02,000", "There is a passage in these tunnels"),
            ("00:00:06,500", "00:00:07,500", "completely silent"),
        ])

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "fail"
        assert check.evidence["per_short"][0]["status"] == "fail"
        assert not result.passed  # single failing capability fails the whole gate

    def test_passes_when_real_content_covers_full_caption_duration(self, tmp_path):
        """Same caption timing, but real (non-black) content runs the full
        8s -- no defect, must pass."""
        episode_id = "ep_shorts_full_coverage"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        _make_clip(short_mp4, 1080, 1920, duration=8.0, source="testsrc")

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        self._write_srt(srt_path, [
            ("00:00:00,100", "00:00:02,000", "There is a passage in these tunnels"),
            ("00:00:06,500", "00:00:07,500", "completely silent"),
        ])

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "pass", check.evidence

    def test_dark_but_not_black_content_is_not_misclassified(self, tmp_path):
        """Guards against false positives on this channel's own legitimately
        dark/moody visual identity: a uniform dark-gray frame (luma ~40,
        well above the near-black threshold) covering the full caption
        window must not be flagged as a black-tail defect."""
        episode_id = "ep_shorts_dark_ok"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        short_mp4.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x282828:s=1080x1920:r=30:d=8",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(short_mp4)],
            capture_output=True, timeout=30,
        )

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        self._write_srt(srt_path, [
            ("00:00:00,100", "00:00:02,000", "There is a passage in these tunnels"),
            ("00:00:06,500", "00:00:07,500", "completely silent"),
        ])

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "pass", check.evidence

    def test_fails_when_short_file_missing(self, tmp_path):
        episode_id = "ep_shorts_visual_missing_file"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        self._write_srt(srt_path, [("00:00:00,100", "00:00:02,000", "text")])

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "fail"

    def test_real_ep_f3619022_short_001_fixture_reproduction(self, tmp_path):
        """Direct proof against the exact confirmed real-world case, using
        the same ffprobe-measured timing found during the investigation:
        a black tail landing exactly where ep_f3619022's own extra1 clip's
        content went black (4.83s into an 8s clip) with a caption still
        active until just before the beat ends."""
        episode_id = "ep_shorts_real_repro"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)

        short_mp4 = ep_dir / f"short_0_{episode_id}.mp4"
        short_mp4.parent.mkdir(parents=True, exist_ok=True)
        # base clip (8s, clean) + extra1-shaped clip (4.83s real, 3.17s black)
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30:d=8",
             "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=4.83",
             "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=3.17",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(short_mp4)],
            capture_output=True, timeout=30,
        )

        srt_path = ep_dir / "captions" / "sh_sh_001.srt"
        # Last cue ends at 15.5s -- inside the black tail (8 + 4.83 = 12.83
        # is where black starts; file ends at 16.0s).
        self._write_srt(srt_path, [
            ("00:00:00,100", "00:00:02,000", "There is a passage in these tunnels"),
            ("00:00:13,300", "00:00:15,500", "completely silent, nobody knows why"),
        ])

        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["shorts_renders"] = [{
            "short_id": "sh_001",
            "video_asset_id": f"episodes/{episode_id}/short_0_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
        }]
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "G_SHORTS_VISUAL_COVERAGE")
        assert check.status == "fail", check.evidence


# ---------------------------------------------------------------------------
# E. END-PAGE CONTINUITY
# ---------------------------------------------------------------------------

class TestEndPageCheck:

    def test_passes_when_no_end_page_configured(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_endpage_none")
        result = run_production_output_gate("ep_endpage_none", assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "E_END_PAGE")
        assert check.status == "pass"
        assert "no end-page configured" in check.reason

    def test_fails_when_renderer_version_stale_even_with_end_page(self, tmp_path):
        """Explicit renderer-version assertion within THIS check, not left
        incidental to B or F failing elsewhere."""
        episode_id = "ep_endpage_stale_version"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        (assets_root / "end-page.png").write_bytes(
            (ep_dir / "thumbnails" / "candidate_1.jpg").read_bytes()
        )
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["package_metadata"]["assembly_agent_version"] = "1.0"
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "E_END_PAGE")
        assert check.status == "fail"
        assert "renderer" in check.reason.lower() or "version" in check.reason.lower()

    def test_passes_with_real_continuous_audio_across_boundary(self, tmp_path):
        """Real end-to-end proof: a continuous tone across the body/end-page
        boundary must pass the real silencedetect-based boundary check."""
        episode_id = "ep_endpage_continuous_audio"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        (assets_root / "end-page.png").write_bytes(
            (ep_dir / "thumbnails" / "candidate_1.jpg").read_bytes()
        )

        main_mp4 = ep_dir / f"main_{episode_id}.mp4"
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1920x1080:r=30:d=5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(main_mp4)],
            capture_output=True, timeout=30,
        )
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["episode_render"]["duration_seconds"] = 2.0
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "E_END_PAGE")
        assert check.status == "pass", check.evidence

    def test_fails_on_real_silence_at_the_boundary(self, tmp_path):
        """Real end-to-end proof of the opposite case: genuine silence
        overlapping the body/end-page boundary must be caught."""
        episode_id = "ep_endpage_boundary_silence"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        (assets_root / "end-page.png").write_bytes(
            (ep_dir / "thumbnails" / "candidate_1.jpg").read_bytes()
        )

        main_mp4 = ep_dir / f"main_{episode_id}.mp4"
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=s=1920x1080:r=30:d=5",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=5",
             "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(main_mp4)],
            capture_output=True, timeout=30,
        )
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["episode_render"]["duration_seconds"] = 2.0
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "E_END_PAGE")
        assert check.status == "fail"
        assert check.evidence.get("boundary_silence")


# ---------------------------------------------------------------------------
# F. ARTIFACT COMPATIBILITY
# ---------------------------------------------------------------------------

class TestArtifactCompatibilityCheck:

    def test_fails_when_render_package_version_is_stale(self, tmp_path):
        episode_id = "ep_compat_stale"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        pkg_path = ep_dir / f"final_render_package_{episode_id}.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["package_metadata"]["assembly_agent_version"] = "1.0"
        pkg_path.write_text(json.dumps(pkg))

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        check = next(c for c in result.checks if c.capability == "F_ARTIFACT_COMPATIBILITY")
        assert check.status == "fail"
        assert result.passed is False


# ---------------------------------------------------------------------------
# Gate aggregation, fail-closed, manifest recording
# ---------------------------------------------------------------------------

class TestGateAggregation:

    def test_passes_overall_only_when_every_capability_passes(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_all_pass")
        result = run_production_output_gate("ep_all_pass", assets_root, MagicMock(), MagicMock())
        assert len(result.checks) == 7  # A-G, including G_SHORTS_VISUAL_COVERAGE (2026-08-07)
        assert result.passed == all(c.status == "pass" for c in result.checks)

    def test_single_failing_capability_fails_the_whole_gate(self, tmp_path):
        """Fail-closed proof: one bad capability (video resolution) is
        enough to fail the entire gate even when every other capability
        passes."""
        episode_id = "ep_one_bad"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        _make_clip(ep_dir / "video" / "sc_s01.mp4", 1280, 720)

        result = run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        statuses = {c.capability: c.status for c in result.checks}
        assert statuses["A_VIDEO"] == "fail"
        assert result.passed is False

    def test_every_check_carries_capability_reason_evidence_remediation(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_fields")
        result = run_production_output_gate("ep_fields", assets_root, MagicMock(), MagicMock())
        for check in result.checks:
            assert check.capability
            assert check.reason
            assert isinstance(check.evidence, dict)
            assert check.status in ("pass", "fail")
            # remediation may be empty on pass, but the field must exist
            assert isinstance(check.remediation, str)

    def test_gate_report_written_to_episode_manifest(self, tmp_path):
        episode_id = "ep_manifest"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        run_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())

        report_path = ep_dir / f"production_output_gate_{episode_id}.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert "passed" in report
        assert len(report["checks"]) == 7  # A-G, including G_SHORTS_VISUAL_COVERAGE (2026-08-07)

        # Also embedded into final_render_package.json.
        render_pkg = json.loads((ep_dir / f"final_render_package_{episode_id}.json").read_text())
        assert "production_output_gate" in render_pkg
        assert render_pkg["production_output_gate"]["episode_id"] == episode_id

    def test_enforce_raises_on_failure_with_full_result_attached(self, tmp_path):
        episode_id = "ep_enforce_fail"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        _make_clip(ep_dir / "video" / "sc_s01.mp4", 1280, 720)

        with pytest.raises(ProductionOutputGateFailure) as exc_info:
            enforce_production_output_gate(episode_id, assets_root, MagicMock(), MagicMock())
        assert "A_VIDEO" in [c.capability for c in exc_info.value.result.checks if c.status == "fail"]

    def test_enforce_does_not_raise_on_pass(self, tmp_path):
        assets_root, _ = _minimal_episode(tmp_path, "ep_enforce_pass")
        result = enforce_production_output_gate("ep_enforce_pass", assets_root, MagicMock(), MagicMock())
        assert result.passed is True


# ---------------------------------------------------------------------------
# Publishing Agent wiring — proves publishing fails closed for real
# ---------------------------------------------------------------------------

class TestProductionOutputGateWiring:

    def test_publishing_halts_when_gate_fails(self, tmp_path):
        from hypogeum.agents.publishing_agent import PublishingAgentError, run_publishing_agent

        episode_id = "ep_publish_blocked"
        assets_root, ep_dir = _minimal_episode(tmp_path, episode_id)
        # Make it fail: a real 720p source clip.
        _make_clip(ep_dir / "video" / "sc_s01.mp4", 1280, 720)

        final_render_package = json.loads(
            (ep_dir / f"final_render_package_{episode_id}.json").read_text()
        )
        # Fill in the fields run_publishing_agent expects beyond what the gate needs.
        final_render_package.setdefault("metadata_source", {
            "ai_disclosure_text": "This video was produced with AI assistance.",
            "episode_title": "Test", "description": "Test",
        })
        final_render_package["episode_render"]["caption_asset_id"] = ""
        final_render_package["thumbnail_references"] = []

        cfg = MagicMock()
        cfg.YOUTUBE_DRY_RUN = False
        (tmp_path / "memory").mkdir(exist_ok=True)

        with pytest.raises(PublishingAgentError, match="Production Output Gate failed"):
            run_publishing_agent(
                final_render_package, cfg, episode_id,
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-08-02T00:00:00+00:00",
            )

    def test_publishing_proceeds_past_gate_when_it_passes(self, tmp_path):
        """Wiring-only proof: with the gate itself mocked to a clean pass
        (each capability check's own correctness is covered by the tests
        above, not re-tested here), run_publishing_agent must reach past
        PRE-FLIGHT 0 without raising ProductionOutputGateFailure."""
        from hypogeum.agents.publishing_agent import run_publishing_agent
        from hypogeum.agents.production_output_gate import ProductionOutputGateResult
        from tests.test_publishing_agent import _make_config

        episode_id = "ep_publish_allowed"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (tmp_path / "memory").mkdir(exist_ok=True)

        pkg = {
            "package_metadata": {},
            "episode_render": {"video_asset_id": "", "caption_asset_id": ""},
            "shorts_renders": [],
            "thumbnail_references": [],
            "metadata_source": {
                "ai_disclosure_text": "This video was produced with AI assistance.",
                "episode_title": "Test", "description": "Test",
            },
        }
        # No real credentials for any platform -- everything skips cleanly
        # after the gate; this test only proves the gate-pass wiring, not
        # any real platform upload path.
        cfg = _make_config(has_youtube=False, has_meta=False, has_tiktok=False)

        passing = ProductionOutputGateResult(
            passed=True, episode_id=episode_id, evaluated_at="now", checks=[],
        )
        with patch(
            "hypogeum.agents.publishing_agent.enforce_production_output_gate",
            return_value=passing,
        ) as mock_gate:
            run_publishing_agent(
                pkg, cfg, episode_id,
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-08-02T00:00:00+00:00",
            )
        mock_gate.assert_called_once()
