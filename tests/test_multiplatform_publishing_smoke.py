"""
tests/test_multiplatform_publishing_smoke.py — Unit tests for the non-public
multi-platform publishing smoke test operational utility
(scripts/multiplatform_publishing_smoke.py).

The module under test lives outside the hypogeum/ package (it's a
scripts/ operational utility), so it is loaded here via importlib from its
file path rather than a normal package import.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "multiplatform_publishing_smoke.py"
_spec = importlib.util.spec_from_file_location("multiplatform_publishing_smoke", _SCRIPT_PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["multiplatform_publishing_smoke"] = smoke
_spec.loader.exec_module(smoke)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(youtube_dry_run: bool = False) -> MagicMock:
    cfg = MagicMock()
    cfg.YOUTUBE_DRY_RUN = youtube_dry_run
    cfg.YOUTUBE_CLIENT_ID = "yt_client_id"
    cfg.YOUTUBE_CLIENT_SECRET = "yt_client_secret"
    cfg.YOUTUBE_REFRESH_TOKEN = "yt_refresh_token"
    cfg.YOUTUBE_DEFAULT_CATEGORY_ID = 27
    cfg.TIKTOK_ACCESS_TOKEN = "tt_access_token"
    cfg.TIKTOK_OPEN_ID = "tt_open_id"
    cfg.META_APP_ID = "meta_app_id"
    cfg.META_APP_SECRET = "meta_app_secret"
    cfg.META_ACCESS_TOKEN = "meta_access_token"
    cfg.META_INSTAGRAM_ACCOUNT_ID = "ig_account"
    cfg.META_STAGING_BASE_URL = None
    return cfg


def _build_episode(tmp_path: Path, episode_id: str = "ep_test", num_shorts: int = 0):
    """Write a minimal but realistic final_render_package + asset files under tmp_path."""
    assets_root = tmp_path / ".hypogeum-assets"
    ep_dir = assets_root / "episodes" / episode_id
    (ep_dir / "captions").mkdir(parents=True)
    (ep_dir / "thumbnails").mkdir(parents=True)

    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"FAKEMP4MAIN")
    (ep_dir / "captions" / "main.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    (ep_dir / "thumbnails" / "candidate_1.jpg").write_bytes(b"FAKEJPGMAIN")

    shorts_renders = []
    thumbnail_refs = [
        {"short_id": None, "format": "16:9", "thumbnail_asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"},
    ]
    for i in range(1, num_shorts + 1):
        sid = f"short_{i:03d}"
        (ep_dir / f"short_{i - 1}_{episode_id}.mp4").write_bytes(b"FAKEMP4SHORT")
        (ep_dir / "thumbnails" / f"{sid}.jpg").write_bytes(b"FAKEJPGSHORT")
        shorts_renders.append({
            "short_id": sid,
            "video_asset_id": f"episodes/{episode_id}/short_{i - 1}_{episode_id}",
        })
        thumbnail_refs.append({
            "short_id": sid, "format": "9:16",
            "thumbnail_asset_id": f"episodes/{episode_id}/thumbnails/{sid}",
        })

    final_render_package = {
        "episode_render": {
            "video_asset_id": f"episodes/{episode_id}/main_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions/main",
            "duration_seconds": 120.0,
        },
        "shorts_renders": shorts_renders,
        "thumbnail_references": thumbnail_refs,
        "metadata_source": {
            "episode_title": "Test Episode",
            "logline": "A test logline.",
            "description": "desc",
            "section_titles": [],
            "shorts_titles": {sr["short_id"]: "Short Title" for sr in shorts_renders},
            "sources": [],
            "ai_disclosure_text": "This video uses AI-generated narration and visuals.",
        },
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(final_render_package))
    return ep_dir, final_render_package


_CREDMGR_PATCH = patch("hypogeum.credential_manager.list_secret_profiles", return_value=[])


# ---------------------------------------------------------------------------
# 1. Plan-only mode makes no publishing calls
# ---------------------------------------------------------------------------

def test_plan_only_mode_makes_no_publishing_calls(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=1)
    with patch.object(smoke, "Config", return_value=_make_config()), \
         patch.object(smoke.yt_client, "upload_video") as m_yt, \
         patch.object(smoke.meta_client, "upload_facebook_video") as m_fb, \
         patch.object(smoke.tt_client, "init_tiktok_upload") as m_tt, \
         patch.object(smoke.meta_client, "create_instagram_container") as m_ig:
        rc = smoke.main(["--episode-id", "ep_test"], project_root=tmp_path)

    assert rc == 0
    m_yt.assert_not_called()
    m_fb.assert_not_called()
    m_tt.assert_not_called()
    m_ig.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Execute without confirmation is refused
# ---------------------------------------------------------------------------

def test_execute_without_confirmation_is_refused(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    with patch.object(smoke, "Config", return_value=_make_config()), \
         patch.object(smoke.yt_client, "upload_video") as m_yt:
        rc = smoke.main(["--episode-id", "ep_test", "--execute"], project_root=tmp_path)

    assert rc == 2
    m_yt.assert_not_called()


# ---------------------------------------------------------------------------
# 3 / 8 / 9. YouTube main uses private, applies the selected thumbnail, and
# submits captions (language "en" is proven against the real low-level source).
# ---------------------------------------------------------------------------

def test_youtube_main_uses_private_privacy_status(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "youtube_main", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_123") as m_upload, \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True):
        smoke._run_youtube_main(cfg, "token123", assets_root, "ep_test", frp, step, ledger)

    assert m_upload.call_args.kwargs["metadata"]["privacyStatus"] == "private"


def test_youtube_main_applies_selected_thumbnail(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "youtube_main", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_123"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True) as m_thumb, \
         patch.object(smoke.yt_client, "upload_captions", return_value=True):
        result = smoke._run_youtube_main(cfg, "token123", assets_root, "ep_test", frp, step, ledger)

    assert m_thumb.called
    thumb_kwargs = m_thumb.call_args.kwargs
    assert thumb_kwargs["video_id"] == "yt_vid_123"
    assert thumb_kwargs["thumb_path"].name == "candidate_1.jpg"
    assert result["thumbnail_applied"] is True
    assert result["outcome"] == "success"


def test_youtube_main_thumbnail_failure_halts_checkpoint(tmp_path):
    """Per spec: the main checkpoint is not successful until video AND thumbnail succeed."""
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "youtube_main", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_123"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=False):
        try:
            smoke._run_youtube_main(cfg, "token123", assets_root, "ep_test", frp, step, ledger)
            assert False, "expected RuntimeError when thumbnail application fails"
        except RuntimeError as exc:
            assert "thumbnail" in str(exc)

    # video_id must be preserved in the ledger so a resume never re-uploads.
    assert ledger.entry("youtube_main")["video_id"] == "yt_vid_123"


def test_youtube_main_submits_captions_and_records_result(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "youtube_main", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_123"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True) as m_caps:
        result = smoke._run_youtube_main(cfg, "token123", assets_root, "ep_test", frp, step, ledger)

    assert m_caps.called
    caps_kwargs = m_caps.call_args.kwargs
    assert caps_kwargs["video_id"] == "yt_vid_123"
    assert caps_kwargs["srt_path"].name == "main.srt"
    assert result["captions_submitted"] is True
    assert result["outcome"] == "success"
    assert result["result_entry"]["metadata_applied"]["captions_submitted"] is True


def test_youtube_main_caption_failure_is_review_required_not_a_hard_failure(tmp_path):
    """Caption failure after video+thumbnail success halts for review, distinct from a hard failure."""
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "youtube_main", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_123"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=False):
        result = smoke._run_youtube_main(cfg, "token123", assets_root, "ep_test", frp, step, ledger)

    assert result["outcome"] == "review_required"
    assert result["captions_submitted"] is False
    assert result["captions_skipped_reason"] == "captions_api_rejected"
    # video + thumbnail work must not be lost — status is still "published".
    assert result["result_entry"]["status"] == "published"


def test_upload_captions_low_level_function_uses_language_en():
    """Proves against the real source (not our mock) that captions are submitted as English."""
    from hypogeum.publishing import youtube as real_yt
    src = inspect.getsource(real_yt.upload_captions)
    assert '"language": "en"' in src


# ---------------------------------------------------------------------------
# 4. Facebook uses published=False
# ---------------------------------------------------------------------------

def test_facebook_uses_published_false(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    step = {
        "step_id": "facebook_main", "platform": "facebook", "deliverable_type": "main_episode",
        "short_id": None, "video_asset_id": frp["episode_render"]["video_asset_id"],
    }

    with patch.object(smoke.meta_client, "upload_facebook_video", return_value="fb_vid_1") as m_fb:
        result = smoke._run_facebook_main(cfg, "token", assets_root, "ep_test", frp, step, ledger)

    assert m_fb.call_args.kwargs["published"] is False
    assert result["visibility"] == "unpublished"


# ---------------------------------------------------------------------------
# 5. TikTok uses SELF_ONLY
# ---------------------------------------------------------------------------

def test_tiktok_uses_self_only_privacy_level(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=1)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    short = frp["shorts_renders"][0]
    step = {
        "step_id": "tiktok_short_first", "platform": "tiktok", "deliverable_type": "short",
        "short_id": short["short_id"], "video_asset_id": short["video_asset_id"],
    }

    with patch.object(smoke.tt_client, "init_tiktok_upload", return_value=("pub1", "https://upload.example")) as m_init, \
         patch.object(smoke.tt_client, "upload_tiktok_chunks"), \
         patch.object(smoke.tt_client, "poll_tiktok_publish", return_value="post_1"):
        result = smoke._run_tiktok_short(cfg, assets_root, "ep_test", frp, step, ledger)

    assert m_init.call_args.kwargs["privacy_level"] == "SELF_ONLY"
    assert result["visibility"] == "self_only"


# ---------------------------------------------------------------------------
# 6. Instagram never calls publish_instagram_container
# ---------------------------------------------------------------------------

def test_publish_instagram_container_never_imported_or_called_in_source():
    tree = ast.parse(_SCRIPT_PATH.read_text())
    imported_names = set()
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name:
                called_names.add(name)

    assert "publish_instagram_container" not in imported_names
    assert "publish_instagram_container" not in called_names
    assert not hasattr(smoke, "publish_instagram_container")


def test_instagram_short_stops_at_container_poll(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=1)
    assets_root = tmp_path / ".hypogeum-assets"
    ledger = smoke.CheckpointLedger(ep_dir / "ledger.json", "ep_test")
    cfg = _make_config()
    short = frp["shorts_renders"][0]
    step = {
        "step_id": "instagram_short_first", "platform": "instagram", "deliverable_type": "short",
        "short_id": short["short_id"], "video_asset_id": short["video_asset_id"],
    }

    fake_staging = MagicMock()
    fake_staging.stage_file.return_value = "https://staged.example.com/video.mp4"

    with patch.object(smoke, "get_staging_provider", return_value=fake_staging), \
         patch.object(smoke.meta_client, "create_instagram_container", return_value="creation_1"), \
         patch.object(smoke.meta_client, "poll_instagram_container", return_value="FINISHED"), \
         patch.object(smoke.meta_client, "publish_instagram_container") as m_publish:
        result = smoke._run_instagram_short(cfg, "token", assets_root, "ep_test", frp, step, ledger)

    m_publish.assert_not_called()
    assert result["visibility"] == "container_only"
    assert result["result_entry"]["status"] == "skipped"
    assert result["result_entry"]["platform_video_id"] is None


# ---------------------------------------------------------------------------
# 7. Successful ledger checkpoints are not rerun
# ---------------------------------------------------------------------------

def test_successful_checkpoint_is_not_rerun(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1") as m_upload, \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(smoke.meta_client, "upload_facebook_video", return_value="fb_vid_1") as m_fb:
        rc1 = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )
        assert rc1 == 0
        assert m_upload.call_count == 1
        assert m_fb.call_count == 1

        rc2 = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )
        assert rc2 == 0
        # Neither upload call is repeated on the second (resumed) run.
        assert m_upload.call_count == 1
        assert m_fb.call_count == 1


# ---------------------------------------------------------------------------
# 10. Resolved dry-run=true refuses real execution
# ---------------------------------------------------------------------------

def test_execute_refused_when_youtube_dry_run_is_true(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    with patch.object(smoke, "Config", return_value=_make_config(youtube_dry_run=True)), \
         patch.object(smoke.yt_client, "upload_video") as m_upload:
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 3
    m_upload.assert_not_called()


def test_upload_video_low_level_function_ignores_youtube_dry_run():
    """Proves against the real source that the low-level path has no dry-run branch."""
    from hypogeum.publishing import youtube as real_yt
    src = (
        inspect.getsource(real_yt.upload_video)
        + inspect.getsource(real_yt._resumable_upload)
        + inspect.getsource(real_yt._upload_chunks)
    )
    assert "DRY_RUN" not in src
    assert "dry_run" not in src.lower()


# ---------------------------------------------------------------------------
# 11. No secret values are printed
# ---------------------------------------------------------------------------

def test_no_secret_values_printed(tmp_path, capsys):
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()
    yt_secret = "SUPER-SECRET-YT-TOKEN-abc123"
    meta_secret = "SUPER-SECRET-META-TOKEN-xyz789"

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value=yt_secret), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value=meta_secret), \
         patch.object(smoke.meta_client, "upload_facebook_video", return_value="fb_vid_1"):
        smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    captured = capsys.readouterr()
    assert yt_secret not in captured.out
    assert yt_secret not in captured.err
    assert meta_secret not in captured.out
    assert meta_secret not in captured.err


# ---------------------------------------------------------------------------
# Phase 3: provider-verification truth — HTTP 400 content/request rejections
# must not falsely mark OAuth credentials invalid; only 401/403 (or a
# credential-resolution-phase failure) does. Successes record verified.
# ---------------------------------------------------------------------------

def test_content_validation_400_does_not_mark_credentials_failed(tmp_path):
    """The real 2026-07-20 Facebook failure shape: a valid token, HTTP 400 on upload."""
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(
             smoke.meta_client, "upload_facebook_video",
             side_effect=smoke.HardFailureError("HTTP 400 — hard failure (no retry) (message='x', type='OAuthException', code=200)", status_code=400),
         ):
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 1  # halted on facebook failure
    facebook_calls = [c for c in m_record_pv.call_args_list if c.kwargs.get("provider") == "facebook"]
    assert all(c.kwargs.get("ok") is not False for c in facebook_calls), (
        "an HTTP 400 content/request rejection must not record facebook credentials as failed"
    )


def test_credential_401_marks_provider_failed(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(
             smoke.meta_client, "upload_facebook_video",
             side_effect=smoke.HardFailureError("HTTP 401 — hard failure (no retry)", status_code=401),
         ):
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 1
    facebook_failed_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "facebook" and c.kwargs.get("ok") is False
    ]
    assert facebook_failed_calls, "a real 401 must record the credential as failed"


def test_credential_resolution_failure_records_provider_failed(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(
             smoke.meta_client, "refresh_meta_token",
             side_effect=smoke.HardFailureError("meta token refresh failed", status_code=401),
         ):
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 1
    facebook_failed_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "facebook" and c.kwargs.get("ok") is False
    ]
    assert facebook_failed_calls, "a credential-resolution-phase failure is always a credential failure"


def test_successful_checkpoint_records_provider_verified(tmp_path):
    _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(smoke.meta_client, "upload_facebook_video", return_value="fb_vid_1"):
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 0
    youtube_verified_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "youtube" and c.kwargs.get("ok") is True
    ]
    facebook_verified_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "facebook" and c.kwargs.get("ok") is True
    ]
    assert youtube_verified_calls, "a successful youtube checkpoint must record verified"
    assert facebook_verified_calls, "a successful facebook checkpoint must record verified"


def test_operation_failure_adds_pdr_to_publishing_record(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification"), \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(
             smoke.meta_client, "upload_facebook_video",
             side_effect=smoke.HardFailureError("HTTP 400 — hard failure (no retry)", status_code=400),
         ):
        smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    record_path = ep_dir / "publishing_record_ep_test.json"
    record = json.loads(record_path.read_text())
    assert record["failures"], "an operational failure must add a PDR to the publishing record"
    assert any("facebook_main" in f.get("deficiency_items", [{}])[0].get("item_id", "") for f in record["failures"])


# ---------------------------------------------------------------------------
# Replaying an already-successful ledger checkpoint must record provider
# verified (from the stored evidence, not a new API call) and must never
# re-upload. A failed/incomplete checkpoint must never be treated this way.
# ---------------------------------------------------------------------------

def _seed_ledger(ep_dir: Path, episode_id: str, step_id: str, status: str, **fields) -> None:
    ledger_path = ep_dir / f"smoke_publishing_checkpoints_{episode_id}.json"
    data = {"episode_id": episode_id, "steps": {}}
    if ledger_path.exists():
        data = json.loads(ledger_path.read_text())
    data["steps"][step_id] = {"status": status, "updated_at": "2026-07-20T09:25:11+00:00", **fields}
    ledger_path.write_text(json.dumps(data, indent=2))


def test_replaying_successful_checkpoint_records_verified_without_reupload(tmp_path):
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    stored_result_entry = {
        "result_id": "res_prior", "platform": "youtube", "deliverable_type": "main_episode",
        "short_id": None, "status": "published", "skipped_reason": None,
        "platform_video_id": "jzRhnPapGNo",
        "published_url": "https://www.youtube.com/watch?v=jzRhnPapGNo",
        "published_at": "2026-07-20T09:25:11+00:00", "attempts": 1, "visibility": "private",
        "metadata_applied": {
            "title": "T", "description_length_chars": 10, "tags_count": 0,
            "thumbnail_applied": True, "captions_submitted": True,
            "captions_skipped_reason": None, "ai_disclosure_included": True,
            "end_screen_applied": None,
        },
    }
    _seed_ledger(
        ep_dir, "ep_test", "youtube_main", "success",
        video_id="jzRhnPapGNo", platform_video_id="jzRhnPapGNo",
        thumbnail_applied=True, visibility="private", result_entry=stored_result_entry,
    )
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video") as m_upload, \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(smoke.meta_client, "upload_facebook_video", return_value="fb_vid_1"):
        rc = smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    assert rc == 0
    m_upload.assert_not_called()  # never re-uploaded

    youtube_verified_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "youtube" and c.kwargs.get("ok") is True
    ]
    assert youtube_verified_calls, "replaying a stored success must record youtube as verified"

    record_path = ep_dir / "publishing_record_ep_test.json"
    record = json.loads(record_path.read_text())
    replayed = next(r for r in record["platform_results"] if r["result_id"] == "res_prior")
    assert replayed["platform_video_id"] == "jzRhnPapGNo"


def test_failed_checkpoint_replay_never_marks_provider_verified(tmp_path):
    """A failed (not "success") ledger entry must never take the replay path
    at all — it is retried as a real attempt, and only a real outcome may
    mark the provider's verification state."""
    ep_dir, frp = _build_episode(tmp_path, num_shorts=0)
    _seed_ledger(ep_dir, "ep_test", "facebook_main", "failed", error="HTTP 400 — hard failure (no retry)")
    cfg = _make_config()

    with _CREDMGR_PATCH, \
         patch.object(smoke, "Config", return_value=cfg), \
         patch("hypogeum.provider_verifier.record_provider_verification") as m_record_pv, \
         patch.object(smoke.yt_client, "refresh_youtube_token", return_value="yt_token"), \
         patch.object(smoke.yt_client, "upload_video", return_value="yt_vid_1"), \
         patch.object(smoke.yt_client, "upload_thumbnail", return_value=True), \
         patch.object(smoke.yt_client, "upload_captions", return_value=True), \
         patch.object(smoke.meta_client, "refresh_meta_token", return_value="meta_token"), \
         patch.object(
             smoke.meta_client, "upload_facebook_video",
             side_effect=smoke.HardFailureError("HTTP 400 — hard failure (no retry)", status_code=400),
         ):
        smoke.main(
            ["--episode-id", "ep_test", "--execute", "--confirm-nonpublic-smoke"],
            project_root=tmp_path,
        )

    facebook_verified_calls = [
        c for c in m_record_pv.call_args_list
        if c.kwargs.get("provider") == "facebook" and c.kwargs.get("ok") is True
    ]
    assert not facebook_verified_calls, "a stale failed checkpoint must never be replayed as verified"
