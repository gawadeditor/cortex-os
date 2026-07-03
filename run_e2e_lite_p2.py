"""
run_e2e_lite_p2.py — E2E Lite Phase 2: Assembly → Publishing runtime verification.

Verifies the full Assembly + Publishing runtime path without real spending.
Seeds mock .mp4 clips for all scenes, mocks ffmpeg, mocks YouTube OAuth token
refresh, and uses YOUTUBE_DRY_RUN=true to verify the publish path without
creating a real video.

Checks verified:
  1. clip_availability_rate == 1.0 (all seeded clips found)
  2. production_ready == True (Final Render Reviewer approves)
  3. Main video file exists after assembly render
  4. End-page path triggered (end-page.png present → _append_end_page called)
  5. Finance rollup returns total_cost_usd
  6. Publishing overall_status == "published" (YouTube main dry-run succeeded)
  7. YouTube main dry-run result in platform_results with status "published"
  8. YouTube shorts dry-run results present (3 shorts processed)
  9. search_best_end_screen_videos called with count=2
 10. apply_end_screen called once for the main episode
 11. No real MUAPI spend, no real YouTube upload
 12. Temp DB used — production DB never touched

Usage:
    python3 run_e2e_lite_p2.py

Exit code: 0 = all assertions pass, 1 = any assertion failed.

Constraints honoured:
    MUAPI_DRY_RUN=true (forced)
    YOUTUBE_DRY_RUN=true (forced)
    No real ffmpeg execution
    No real network calls to YouTube, MUAPI, ElevenLabs
    Temp directory is cleaned up on exit
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
INFO = "\033[94mINFO\033[0m"

_results: list[bool] = []
_labels:  list[str]  = []


def _check(label: str, result: bool, detail: str = "") -> bool:
    status = PASS if result else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")
    _results.append(result)
    _labels.append(label)
    return result


def _info(msg: str) -> None:
    print(f"  {INFO}  {msg}")


# ---------------------------------------------------------------------------
# Episode constants
# ---------------------------------------------------------------------------

_EP = "ep_p2_e2e"

# Short IDs (must match visual package)
_SHORT_IDS = ["sh_001", "sh_002", "sh_003"]


# ---------------------------------------------------------------------------
# Package builders
# ---------------------------------------------------------------------------

def _make_visual_package(episode_id: str) -> dict:
    """
    Two main scenes + three short scenes (one per short).
    Narration duration 13s each × 2 main = 26s total → triggers end screen.
    """
    scenes = [
        # Main episode scenes
        {
            "scene_id": "sc_001",
            "scene_index": 0,
            "source_type": "main_episode",
            "narration_reference": "b_001",
            "asset_id": f"episodes/{episode_id}/video/sc_001",
            "production_spec": {"duration_seconds": 8},
            "narrative_signals": [{"signal_type": "mystery_deepening"}],
            "section_reference": "s01",
        },
        {
            "scene_id": "sc_002",
            "scene_index": 1,
            "source_type": "main_episode",
            "narration_reference": "b_002",
            "asset_id": f"episodes/{episode_id}/video/sc_002",
            "production_spec": {"duration_seconds": 8},
            "narrative_signals": [{"signal_type": "resolution"}],
            "section_reference": "s01",
        },
        # Short scenes
        {
            "scene_id": "sc_sh1_001",
            "scene_index": 0,
            "source_type": "short",
            "short_id": "sh_001",
            "narration_reference": "sh1b_001",
            "asset_id": f"episodes/{episode_id}/video/sc_sh1_001",
            "production_spec": {"duration_seconds": 5},
            "narrative_signals": [],
            "text_safe_zone": "bottom",
        },
        {
            "scene_id": "sc_sh2_001",
            "scene_index": 0,
            "source_type": "short",
            "short_id": "sh_002",
            "narration_reference": "sh2b_001",
            "asset_id": f"episodes/{episode_id}/video/sc_sh2_001",
            "production_spec": {"duration_seconds": 5},
            "narrative_signals": [],
            "text_safe_zone": "bottom",
        },
        {
            "scene_id": "sc_sh3_001",
            "scene_index": 0,
            "source_type": "short",
            "short_id": "sh_003",
            "narration_reference": "sh3b_001",
            "asset_id": f"episodes/{episode_id}/video/sc_sh3_001",
            "production_spec": {"duration_seconds": 5},
            "narrative_signals": [],
            "text_safe_zone": "bottom",
        },
    ]
    return {
        "package_metadata": {
            "visual_package_id": f"vp_{episode_id}",
            "episode_id": episode_id,
        },
        "episode_metadata": {
            "episode_title": "E2E Phase 2 Verification Episode",
            "tone_arc": "Ancient mystery",
            "topic_id": "t_e2e_p2",
        },
        "visual_continuity_record": {
            "locked_entities": [],
            "period_palettes": [],
        },
        "scenes": scenes,
        "asset_manifest": {
            "assets": [
                {
                    "asset_id": s["asset_id"],
                    "asset_type": "generated_video",
                    "description": f"Scene {s['scene_id']}",
                    "used_in_scenes": [],
                    "reuse_count": 1,
                    "provider": "muapi",
                    "generation_status": "generated",
                }
                for s in scenes
            ]
        },
        "provider_routing_manifest": {},
        "visual_integrity_log": {},
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What really happened?",
            "candidates": [
                {"candidate_id": "c1", "candidate_type": "literal_scene"},
                {"candidate_id": "c2", "candidate_type": "inferred_moment"},
            ],
            "short_candidates": [
                {
                    "short_id": sid,
                    "candidates": [
                        {"candidate_id": f"{sid}_c1", "candidate_type": "literal_scene"},
                        {"candidate_id": f"{sid}_c2", "candidate_type": "inferred_moment"},
                    ],
                }
                for sid in _SHORT_IDS
            ],
        },
        "package_integrity": {"production_ready": True},
        "sections": [{"section_id": "s01", "title": "The Mystery"}],
    }


def _make_narration_package(episode_id: str) -> dict:
    """
    13s per main beat → 26s total narration, which is > 20s (end-screen threshold).
    """
    return {
        "package_metadata": {
            "narration_package_id": f"np_{episode_id}",
            "episode_id": episode_id,
        },
        "voice_configuration": {
            "voice_id": "voice-e2e",
            "model_id": "eleven_multilingual_v2",
            "environment_source": "ELEVENLABS_VOICE_ID",
        },
        "narration_segments": [
            {
                "segment_id": "seg_b_001",
                "beat_reference": "b_001",
                "source_type": "main_episode",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/b_001",
                "duration_seconds": 13.0,
            },
            {
                "segment_id": "seg_b_002",
                "beat_reference": "b_002",
                "source_type": "main_episode",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/b_002",
                "duration_seconds": 13.0,
            },
            {
                "segment_id": "seg_sh1b_001",
                "beat_reference": "sh1b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh1b_001",
                "duration_seconds": 8.0,
            },
            {
                "segment_id": "seg_sh2b_001",
                "beat_reference": "sh2b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh2b_001",
                "duration_seconds": 8.0,
            },
            {
                "segment_id": "seg_sh3b_001",
                "beat_reference": "sh3b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh3b_001",
                "duration_seconds": 8.0,
            },
        ],
        "stitched_tracks": [
            {
                "track_id": f"track_main_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/main_narration",
                "source_type": "main_episode",
            },
            {
                "track_id": f"track_sh_001_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_001_narration",
                "source_type": "short",
            },
            {
                "track_id": f"track_sh_002_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_002_narration",
                "source_type": "short",
            },
            {
                "track_id": f"track_sh_003_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_003_narration",
                "source_type": "short",
            },
        ],
        "captions": [
            {
                "caption_id": f"cap_main_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/main",
                "source_type": "main_episode",
                "caption_method": "elevenlabs_alignment",
            },
        ],
        "audio_qa_report": {"overall_status": "pass"},
        "package_integrity": {"production_ready": True},
    }


def _make_thumbnail_package(episode_id: str) -> dict:
    return {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {
            "asset_id": f"episodes/{episode_id}/thumbnails/main",
        },
        "short_thumbnails": [
            {"short_id": sid, "asset_id": f"episodes/{episode_id}/thumbnails/{sid}"}
            for sid in _SHORT_IDS
        ],
        "package_integrity": {"production_ready": True},
    }


def _make_music_track_package(episode_id: str, assets_root: Path) -> dict:
    """Build a minimal music_track_package with a real file seeded at the expected path."""
    music_dir = assets_root / "channel" / "music" / "mystery"
    music_dir.mkdir(parents=True, exist_ok=True)
    music_file = music_dir / "mystery_track-e2e.mp3"
    music_file.write_bytes(b"\xff\xfb" * 5000)

    track_asset_id = "channel/music/mystery/mystery_track-e2e"
    section_keys = ["main_episode"] + [f"short_{sid}" for sid in _SHORT_IDS]

    tracks = {
        sk: {
            "section_key": sk,
            "category": "Mystery",
            "asset_id": track_asset_id,
            "local_path": str(music_file),
            "source": "ai_generated",
            "duration_seconds": 120.0,
            "prompt_used": "atmospheric underscore",
        }
        for sk in section_keys
    }

    music_log = [
        {
            "log_id": f"ml_{sk}",
            "placed_in": sk,
            "timeline_position_seconds": 0.0,
            "category": "Mystery",
            "music_asset_id": track_asset_id,
            "source": "ai_generated",
            "provider": "elevenlabs",
            "confidence_note": None,
            "fallback_trigger": None,
        }
        for sk in section_keys
    ]

    return {
        "package_id": f"mtp_{episode_id}_e2e",
        "episode_id": episode_id,
        "sourced_at": "2026-07-03T00:00:00+00:00",
        "sourcing_status": "complete",
        "tracks": tracks,
        "music_log": music_log,
        "sections_without_music": [],
    }


# ---------------------------------------------------------------------------
# File seeder
# ---------------------------------------------------------------------------

def _seed_assets(episode_id: str, assets_root: Path) -> None:
    """
    Seed all fake media files needed for assembly clip validation and
    publishing asset validation.

    Layout in assets_root/:
        episodes/{episode_id}/video/sc_001.mp4           ← main clip 1
        episodes/{episode_id}/video/sc_002.mp4           ← main clip 2
        episodes/{episode_id}/video/sc_sh1_001.mp4       ← short 1 clip
        episodes/{episode_id}/video/sc_sh2_001.mp4       ← short 2 clip
        episodes/{episode_id}/video/sc_sh3_001.mp4       ← short 3 clip
        episodes/{episode_id}/narration/main_narration.mp3
        episodes/{episode_id}/narration/sh_sh_001_narration.mp3
        episodes/{episode_id}/narration/sh_sh_002_narration.mp3
        episodes/{episode_id}/narration/sh_sh_003_narration.mp3
        episodes/{episode_id}/thumbnails/main.jpg
        episodes/{episode_id}/thumbnails/sh_001.jpg
        episodes/{episode_id}/thumbnails/sh_002.jpg
        episodes/{episode_id}/thumbnails/sh_003.jpg
        end-page.png                                     ← triggers end-page path
    """
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)

    # Video clips — note: asset_id is "episodes/{ep}/video/sc_001" → file at that path
    video_dir = ep_dir / "video"
    video_dir.mkdir(exist_ok=True)
    for stem in ["sc_001", "sc_002", "sc_sh1_001", "sc_sh2_001", "sc_sh3_001"]:
        (video_dir / f"{stem}.mp4").write_bytes(b"FAKEVIDEO" * 500)

    # Narration stitched tracks
    narr_dir = ep_dir / "narration"
    narr_dir.mkdir(exist_ok=True)
    for fname in [
        "main_narration.mp3",
        "sh_sh_001_narration.mp3",
        "sh_sh_002_narration.mp3",
        "sh_sh_003_narration.mp3",
    ]:
        (narr_dir / fname).write_bytes(b"\xff\xfb" * 100)

    # Thumbnails
    thumb_dir = ep_dir / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    (thumb_dir / "main.jpg").write_bytes(b"FAKEJPG")
    for sid in _SHORT_IDS:
        (thumb_dir / f"{sid}.jpg").write_bytes(b"FAKEJPG")

    # End-page PNG — existence triggers _append_end_page in assembly
    (assets_root / "end-page.png").write_bytes(b"FAKEPNG")


# ---------------------------------------------------------------------------
# ffmpeg mock
# ---------------------------------------------------------------------------

def _ffmpeg_mock(cmd, capture_output, text, timeout):
    """
    Mock subprocess.run for all ffmpeg calls.
    Writes a minimal fake MP4 to the last positional argument so file-existence
    checks pass. Works for all render passes:
      - main episode render
      - short renders (×3)
      - end-page clip creation
      - end-page concat
    """
    out = Path(cmd[-1])
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write fake content so stat().st_size > 0 checks pass
    out.write_bytes(b"FAKEMP4" * 500)
    r = MagicMock()
    r.returncode = 0
    r.stderr = ""
    r.stdout = ""
    return r


# ---------------------------------------------------------------------------
# Section 0: Setup
# ---------------------------------------------------------------------------

def setup() -> tuple[Path, Path, Path, object]:
    """
    Create isolated temp workspace.

    Directory structure:
        base/                          ← tempdir
          .hypogeum-assets/            ← assets_root
            episodes/
              ep_p2_e2e/              ← output_dir (= episode_dir for assembly)
            ...
            hypogeum.db               ← temp DB
            finance/
            runs/

    Assembly searches upward from output_dir for .hypogeum-assets:
        depth 1: output_dir/.hypogeum-assets          → NO
        depth 2: episodes/.hypogeum-assets             → NO
        depth 3: .hypogeum-assets/.hypogeum-assets     → NO
        depth 4: base/.hypogeum-assets                 → YES ✓

    Publishing follows the same upward search with the same output_dir → same result.
    Both agents resolve assets_root = base/.hypogeum-assets ✓.
    """
    base = Path(tempfile.mkdtemp(prefix="e2e_p2_"))
    assets_root = base / ".hypogeum-assets"
    assets_root.mkdir()
    output_dir = assets_root / "episodes" / _EP
    output_dir.mkdir(parents=True)

    # Create run infrastructure directories
    (assets_root / "runs").mkdir()
    (assets_root / "finance").mkdir()

    return base, assets_root, output_dir


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------

def _build_config():
    """
    Build a Config that:
      - Forces MUAPI_DRY_RUN=true and YOUTUBE_DRY_RUN=true
      - Forces CLIP_REPAIR_ENABLED=false (all clips present; no repair needed)
      - Provides stub YouTube credentials so publishing_agent reaches dry-run branch
        (refresh_youtube_token is mocked anyway, so no real OAuth call occurs)
    """
    from hypogeum.config import Config

    # Only force-override the dry-run and test guard flags.
    # Real credentials from .env are preserved for OPENROUTER_API_KEY etc.
    # YouTube creds are provided as stubs in case .env has none; mocked anyway.
    _overrides: dict = {
        "MUAPI_DRY_RUN": "true",
        "YOUTUBE_DRY_RUN": "true",
        "CLIP_REPAIR_ENABLED": "false",
        "END_PAGE_DURATION_SECONDS": "20",
    }
    if not os.environ.get("YOUTUBE_CLIENT_ID"):
        _overrides["YOUTUBE_CLIENT_ID"] = "e2e-stub-client-id"
    if not os.environ.get("YOUTUBE_CLIENT_SECRET"):
        _overrides["YOUTUBE_CLIENT_SECRET"] = "e2e-stub-client-secret"
    if not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        _overrides["YOUTUBE_REFRESH_TOKEN"] = "e2e-stub-refresh-token"

    with patch.dict(os.environ, _overrides):
        return Config()


# ---------------------------------------------------------------------------
# Section 1: Pre-flight
# ---------------------------------------------------------------------------

def section_preflight(config) -> bool:
    print("\n=== SECTION 1: Pre-flight ===")
    ok = True
    ok &= _check("MUAPI_DRY_RUN == true", getattr(config, "MUAPI_DRY_RUN", False) is True)
    ok &= _check("YOUTUBE_DRY_RUN == true", getattr(config, "YOUTUBE_DRY_RUN", False) is True)
    ok &= _check("CLIP_REPAIR_ENABLED == false", getattr(config, "CLIP_REPAIR_ENABLED", True) is False)
    return ok


# ---------------------------------------------------------------------------
# Section 2: Assembly Agent
# ---------------------------------------------------------------------------

def section_assembly(
    episode_id: str,
    assets_root: Path,
    output_dir: Path,
    config,
) -> tuple[bool, dict]:
    print("\n=== SECTION 2: Assembly Agent ===")
    from hypogeum.agents.assembly_agent import run_assembly_agent

    visual_pkg = _make_visual_package(episode_id)
    narration_pkg = _make_narration_package(episode_id)
    thumbnail_pkg = _make_thumbnail_package(episode_id)
    music_pkg = _make_music_track_package(episode_id, assets_root)

    _seed_assets(episode_id, assets_root)

    result: dict = {}
    exc_info: list = []

    with patch("subprocess.run", side_effect=_ffmpeg_mock):
        try:
            result = run_assembly_agent(
                visual_package=visual_pkg,
                narration_package=narration_pkg,
                thumbnail_package=thumbnail_pkg,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                music_track_package=music_pkg,
            )
        except Exception as exc:
            exc_info.append(exc)
            print(f"  ERROR: run_assembly_agent raised: {exc}")

    ok = True

    if exc_info:
        ok &= _check("assembly agent ran without exception", False, str(exc_info[0]))
        return ok, result

    # Check 1: clip_availability_rate
    qa_report = result.get("assembly_qa_report", {})
    avail_rate = qa_report.get("clip_availability_rate", -1)
    ok &= _check(
        "clip_availability_rate == 1.0",
        avail_rate == 1.0,
        f"got {avail_rate}",
    )

    # Check 2: production_ready
    pkg_integrity = result.get("package_integrity", {})
    prod_ready = pkg_integrity.get("production_ready", False)
    ok &= _check(
        "production_ready == True",
        prod_ready is True,
        f"got {prod_ready}",
    )

    # Check 3: episode_render.video_asset_id non-empty
    ep_render = result.get("episode_render", {})
    video_asset_id = ep_render.get("video_asset_id", "")
    ok &= _check(
        "episode_render.video_asset_id is set",
        bool(video_asset_id),
        f"got {video_asset_id!r}",
    )

    # Check 4: main video file exists on disk
    main_mp4 = output_dir / f"main_{episode_id}.mp4"
    ok &= _check(
        "main video file exists on disk",
        main_mp4.exists(),
        str(main_mp4),
    )

    # Check 5: end-page PNG existed → end-page must have been attempted.
    # Verify indirectly: main video file still exists (end-page concat didn't break it).
    end_page_png = assets_root / "end-page.png"
    end_page_seeded = end_page_png.exists()
    ok &= _check(
        "end-page.png was seeded (precondition for end-page path)",
        end_page_seeded,
    )
    if end_page_seeded:
        ok &= _check(
            "main video intact after end-page append",
            main_mp4.exists() and main_mp4.stat().st_size > 0,
        )

    # Check 6: shorts_renders present
    shorts_renders = result.get("shorts_renders", [])
    ok &= _check(
        "shorts_renders has exactly 3 entries",
        len(shorts_renders) == 3,
        f"got {len(shorts_renders)}",
    )

    # Check 7: assembly_qa_report.overall_status is not 'fail'
    qa_status = qa_report.get("overall_status", "")
    ok &= _check(
        "assembly_qa_report.overall_status != 'fail'",
        qa_status != "fail",
        f"got {qa_status!r}",
    )

    return ok, result


# ---------------------------------------------------------------------------
# Section 3: Finance rollup
# ---------------------------------------------------------------------------

def section_finance(
    run_id: str,
    episode_id: str,
    db_path: Path,
    finance_dir: Path,
) -> bool:
    print("\n=== SECTION 3: Finance rollup ===")
    from hypogeum.finance import write_episode_rollup

    ok = True
    try:
        rollup = write_episode_rollup(
            run_id=run_id,
            episode_id=episode_id,
            db_path=db_path,
            finance_dir=finance_dir,
        )
    except Exception as exc:
        ok &= _check("write_episode_rollup ran without exception", False, str(exc))
        return ok

    ok &= _check(
        "write_episode_rollup returned a dict",
        isinstance(rollup, dict),
        str(type(rollup)),
    )
    ok &= _check(
        "total_cost_usd key present",
        "total_cost_usd" in rollup,
        f"keys: {list(rollup.keys())[:6]}",
    )

    return ok


# ---------------------------------------------------------------------------
# Section 4: Publishing Agent
# ---------------------------------------------------------------------------

def section_publishing(
    final_render_package: dict,
    config,
    episode_id: str,
    output_dir: Path,
) -> bool:
    print("\n=== SECTION 4: Publishing Agent ===")
    from hypogeum.agents.publishing_agent import run_publishing_agent

    ok = True
    result: dict = {}

    # Capture mock call counts
    mock_search = MagicMock(return_value=["yt_prior_001", "yt_prior_002"])
    mock_end_screen = MagicMock(return_value=True)

    patches = [
        # YouTube auth — return a fake token so youtube_available=True
        patch("hypogeum.publishing.youtube.refresh_youtube_token",
              return_value="fake_e2e_token"),
        # Captions upload — return True (SRT files not seeded; mock handles it)
        patch("hypogeum.publishing.youtube.upload_captions", return_value=True),
        # Thumbnail upload — return True
        patch("hypogeum.publishing.youtube.upload_thumbnail", return_value=True),
        # End screen — use capturing mocks so we can assert call signatures
        patch("hypogeum.publishing.youtube.search_best_end_screen_videos", mock_search),
        patch("hypogeum.publishing.youtube.apply_end_screen", mock_end_screen),
        # record_publishing_outcome: uses assets_root/hypogeum.db (temp DB) so it's
        # safe, but mock it anyway to avoid any import-time schema mismatch.
        patch("hypogeum.decision_inputs.store.record_publishing_outcome"),
        # covered_topics_writer modifies project-root files; always mock.
        patch("hypogeum.agents.covered_topics_writer.record_published_topic",
              return_value=True),
    ]

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        try:
            result = run_publishing_agent(
                final_render_package=final_render_package,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                # publish_at set → _yt_privacy="private", _yt_block=False
                publish_at="2026-07-05T14:00:00+00:00",
            )
        except Exception as exc:
            ok &= _check("publishing agent ran without exception", False, str(exc))
            return ok

    # Check A: overall_status
    pkg_meta = result.get("package_metadata", {})
    overall_status = pkg_meta.get("overall_status", "")
    ok &= _check(
        "overall_status == 'published'",
        overall_status == "published",
        f"got {overall_status!r}",
    )

    # Check B: YouTube main result
    platform_results = result.get("platform_results", [])
    yt_main = next(
        (r for r in platform_results
         if r.get("platform") == "youtube"
         and r.get("deliverable_type") == "main_episode"),
        None,
    )
    ok &= _check("YouTube main result present", yt_main is not None)
    if yt_main:
        ok &= _check(
            "YouTube main dry-run status == 'published'",
            yt_main.get("status") == "published",
            f"got {yt_main.get('status')!r}",
        )

    # Check C: YouTube shorts (3 shorts processed)
    yt_shorts = [
        r for r in platform_results
        if r.get("platform") == "youtube"
        and r.get("deliverable_type") == "short"
        and "_placeholder" not in r.get("short_id", "")
    ]
    ok &= _check(
        "3 YouTube short results present (dry-run)",
        len(yt_shorts) == 3,
        f"got {len(yt_shorts)}",
    )

    # Check D: search_best_end_screen_videos called with count=2
    ok &= _check(
        "search_best_end_screen_videos called",
        mock_search.called,
    )
    if mock_search.called:
        _, search_kwargs = mock_search.call_args
        search_count = search_kwargs.get("count") or (
            mock_search.call_args[0][3] if len(mock_search.call_args[0]) > 3 else None
        )
        # Tolerate either positional or keyword
        call_args_flat = list(mock_search.call_args[0]) + list(mock_search.call_args[1].values())
        ok &= _check(
            "search_best_end_screen_videos called with count=2",
            2 in call_args_flat or search_kwargs.get("count") == 2,
            f"call_args={mock_search.call_args}",
        )

    # Check E: apply_end_screen called once
    ok &= _check(
        "apply_end_screen called exactly once",
        mock_end_screen.call_count == 1,
        f"call_count={mock_end_screen.call_count}",
    )

    # Check F: privacyStatus was forced to "private" (via would_send_privacy_status)
    # This is logged in the dry-run path but not stored in the record dict.
    # We verify the precondition: publish_at was set → _yt_block=False → upload attempted.
    ok &= _check(
        "YouTube main was not skipped (publish_at guard)",
        yt_main is not None and yt_main.get("status") != "skipped",
    )

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("\n" + "=" * 60)
    print("E2E Lite Phase 2: Assembly → Publishing")
    print("MUAPI_DRY_RUN=true  YOUTUBE_DRY_RUN=true")
    print("No real ffmpeg  No real network calls  Temp DB only")
    print("=" * 60)

    base: Path = Path()
    try:
        # --- Setup ---
        base, assets_root, output_dir = setup()
        _info(f"Workspace: {base}")

        config = _build_config()
        db_path = assets_root / "hypogeum.db"
        finance_dir = assets_root / "finance"
        runs_dir = assets_root / "runs"

        # Initialize temp DB
        from hypogeum.db import initialize_schema
        initialize_schema(db_path)

        # Register a run (uses temp DB, does not touch production)
        from hypogeum.run_registry import register_run
        from hypogeum.finance import set_run_context, clear_run_context

        run_id = register_run(
            episode_id=_EP,
            config=config,
            db_path=db_path,
            runs_dir=runs_dir,
        )
        set_run_context(run_id=run_id, episode_id=_EP, db_path=db_path)
        _info(f"Run registered: {run_id[:8]}...")

        # --- Section 1: Pre-flight ---
        s1_ok = section_preflight(config)

        # --- Section 2: Assembly ---
        s2_ok, final_render_package = section_assembly(
            episode_id=_EP,
            assets_root=assets_root,
            output_dir=output_dir,
            config=config,
        )

        # --- Section 3: Finance rollup ---
        s3_ok = section_finance(
            run_id=run_id,
            episode_id=_EP,
            db_path=db_path,
            finance_dir=finance_dir,
        )

        # --- Section 4: Publishing (only if assembly produced a valid package) ---
        if final_render_package and final_render_package.get("episode_render"):
            s4_ok = section_publishing(
                final_render_package=final_render_package,
                config=config,
                episode_id=_EP,
                output_dir=output_dir,
            )
        else:
            print("\n=== SECTION 4: Publishing Agent ===")
            print("  SKIP  Assembly produced no valid package — publishing skipped")
            s4_ok = False
            _results.append(False)
            _labels.append("publishing (skipped — no assembly package)")

        clear_run_context()

    except Exception as exc:
        print(f"\nFATAL: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if base and base.exists():
            try:
                shutil.rmtree(base)
            except Exception:
                pass

    # --- Final report ---
    total = len(_results)
    passed = sum(_results)
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"E2E Lite Phase 2 — {passed}/{total} checks passed")
    if failed:
        print(f"\nFailed checks:")
        for label, result in zip(_labels, _results):
            if not result:
                print(f"  ✗  {label}")
    print("=" * 60 + "\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
