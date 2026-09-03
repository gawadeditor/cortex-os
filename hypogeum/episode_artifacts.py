"""
hypogeum/episode_artifacts.py — Disk-first episode artifact reader.

Reads artifacts from .hypogeum-assets/episodes/<episode_id>/ without
requiring a DB entry.  Used by the dashboard to surface generated content
(renders, narration, thumbnails, captions, JSON packages) as browsable links.

Public API:
    get_episode_artifacts(episode_id, assets_root=None) -> dict
    list_disk_episodes(assets_root=None, limit=20) -> list[dict]
    safe_episode_asset_path(episode_id, kind, filename, assets_root=None) -> Path | None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

# JSON package stems that the pipeline emits, in logical display order
_JSON_PACKAGES = [
    "topic_brief",
    "research_package",
    "narrative_package",
    "script_package",
    "visual_package",
    "narration_package",
    "thumbnail_package",
    "qa_approval_report",
    "final_render_package",
    "asset_archive_record",
    "validation_scorecard",
    "finance_authorization",
    "finance_post_vpd_authorization",
]

_JSON_DISPLAY_NAMES = {
    "topic_brief":                  "Topic Brief",
    "research_package":             "Research",
    "narrative_package":            "Narrative",
    "script_package":               "Script",
    "visual_package":               "Visual Plan",
    "narration_package":            "Narration",
    "thumbnail_package":            "Thumbnails",
    "qa_approval_report":           "QA Report",
    "final_render_package":         "Render Package",
    "asset_archive_record":         "Asset Archive",
    "validation_scorecard":         "Validation",
    "finance_authorization":        "Budget Auth",
    "finance_post_vpd_authorization": "Post-VPD Budget",
}


def _assets_root(override: Optional[Path]) -> Path:
    return Path(override) if override is not None else _DEFAULT_ASSETS_ROOT


def get_episode_artifacts(
    episode_id: str,
    assets_root: Optional[Path] = None,
) -> dict:
    """
    Return a structured summary of on-disk artifacts for one episode.

    Always returns a dict (never raises). The `exists` key is False when the
    episode directory is absent.
    """
    root = _assets_root(assets_root)
    ep_dir = root / "episodes" / episode_id

    if not ep_dir.is_dir():
        return {
            "episode_id": episode_id,
            "exists": False,
            "has_render": False,
            "has_thumbnail": False,
            "has_narration": False,
            "has_captions": False,
            "clip_count": 0,
            "json_artifacts": [],
            "video_clips": [],
        }

    # ── Render (main episode MP4) ──────────────────────────────────────────
    render_path = ep_dir / f"main_{episode_id}.mp4"
    has_render  = render_path.exists() and render_path.stat().st_size > 0
    render_size_mb = round(render_path.stat().st_size / 1_048_576, 1) if has_render else 0.0

    # Real finished-video length (seconds) from the render package — NOT the
    # production wall-clock time. Falls back silently.
    render_duration_seconds = None
    _frp = ep_dir / f"final_render_package_{episode_id}.json"
    if _frp.exists():
        try:
            _frp_data = json.loads(_frp.read_text(encoding="utf-8"))
            for _path in (("episode_render", "duration_seconds"),
                          ("package_metadata", "render_duration_seconds")):
                _v = _frp_data
                for _k in _path:
                    _v = _v.get(_k) if isinstance(_v, dict) else None
                if isinstance(_v, (int, float)) and _v > 0:
                    render_duration_seconds = round(float(_v), 1)
                    break
        except Exception:  # noqa: BLE001
            pass
    if render_duration_seconds:
        _m, _s = divmod(int(round(render_duration_seconds)), 60)
        render_duration_fmt = f"{_m}:{_s:02d}" if _m else f"{_s}s"
    else:
        render_duration_fmt = None

    # ── Thumbnail ──────────────────────────────────────────────────────────
    thumb_dir = ep_dir / "thumbnails"
    thumb_path = None
    thumb_ext  = None
    if thumb_dir.is_dir():
        for ext in ("jpg", "jpeg", "png", "webp"):
            candidate = thumb_dir / f"main.{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                thumb_path = candidate
                thumb_ext  = ext
                break
    has_thumbnail = thumb_path is not None

    # ── Narration (stitched main track) ───────────────────────────────────
    narration_path = ep_dir / "narration" / "main_narration.mp3"
    has_narration  = narration_path.exists() and narration_path.stat().st_size > 0
    narration_size_mb = round(narration_path.stat().st_size / 1_048_576, 1) if has_narration else 0.0

    # ── Captions (SRT) ────────────────────────────────────────────────────
    captions_path = ep_dir / "captions" / "main.srt"
    has_captions  = captions_path.exists() and captions_path.stat().st_size > 0

    # ── Video clips ───────────────────────────────────────────────────────
    video_dir = ep_dir / "video"
    video_clips: list[dict] = []
    if video_dir.is_dir():
        for f in sorted(video_dir.glob("sc_*.mp4")):
            if f.stat().st_size > 0:
                video_clips.append({
                    "scene_id": f.stem,
                    "filename": f.name,
                    "size_mb":  round(f.stat().st_size / 1_048_576, 1),
                })

    # ── JSON packages ─────────────────────────────────────────────────────
    json_artifacts: list[dict] = []
    for stem in _JSON_PACKAGES:
        candidate = ep_dir / f"{stem}_{episode_id}.json"
        if candidate.exists():
            try:
                size_kb = candidate.stat().st_size // 1024
            except OSError:
                size_kb = 0
            # Quick title extraction from known fields
            title = _JSON_DISPLAY_NAMES.get(stem, stem.replace("_", " ").title())
            json_artifacts.append({
                "stem":     stem,
                "filename": candidate.name,
                "size_kb":  size_kb,
                "label":    title,
            })

    # QA verdict from qa_approval_report if present
    qa_verdict = None
    qa_approved = None
    qa_path = ep_dir / f"qa_approval_report_{episode_id}.json"
    if qa_path.exists():
        try:
            qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
            qa_verdict  = qa_data.get("verdict")
            qa_approved = qa_data.get("approved")
        except Exception:
            pass

    # Episode title from narration_package or script_package
    display_title = None
    for stem in ("narration_package", "script_package", "topic_brief"):
        pkg_path = ep_dir / f"{stem}_{episode_id}.json"
        if pkg_path.exists():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                if stem == "narration_package":
                    display_title = (
                        pkg.get("package_metadata", {}).get("episode_title")
                        or pkg.get("voice_configuration", {}).get("episode_title")
                    )
                elif stem == "script_package":
                    display_title = pkg.get("episode_metadata", {}).get("topic")
                elif stem == "topic_brief":
                    display_title = pkg.get("episode_title") or pkg.get("topic_id")
                if display_title:
                    break
            except Exception:
                pass

    return {
        "episode_id":       episode_id,
        "exists":           True,
        "display_title":    display_title,
        "has_render":       has_render,
        "render_size_mb":   render_size_mb,
        "render_duration_seconds": render_duration_seconds,
        "render_duration_fmt":     render_duration_fmt,
        "has_thumbnail":    has_thumbnail,
        "thumb_ext":        thumb_ext,
        "has_narration":    has_narration,
        "narration_size_mb": narration_size_mb,
        "has_captions":     has_captions,
        "clip_count":       len(video_clips),
        "video_clips":      video_clips,
        "json_artifacts":   json_artifacts,
        "qa_verdict":       qa_verdict,
        "qa_approved":      qa_approved,
    }


def list_disk_episodes(
    assets_root: Optional[Path] = None,
    limit: int = 20,
) -> list[dict]:
    """
    List episodes that have an on-disk directory under .hypogeum-assets/episodes/.

    Returns a lightweight summary dict per episode, newest-modified first.
    Used to supplement the DB episode list.
    """
    root = _assets_root(assets_root)
    ep_base = root / "episodes"
    if not ep_base.is_dir():
        return []

    summaries: list[dict] = []
    for ep_dir in sorted(ep_base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not ep_dir.is_dir():
            continue
        episode_id = ep_dir.name
        if episode_id.startswith("."):
            continue

        render_path = ep_dir / f"main_{episode_id}.mp4"
        qa_path     = ep_dir / f"qa_approval_report_{episode_id}.json"
        thumb_dir   = ep_dir / "thumbnails"
        has_render  = render_path.exists() and render_path.stat().st_size > 0

        qa_verdict = None
        if qa_path.exists():
            try:
                qa_data    = json.loads(qa_path.read_text(encoding="utf-8"))
                qa_verdict = qa_data.get("verdict")
            except Exception:
                pass

        # Brief title extraction
        title = None
        for stem in ("topic_brief", "script_package"):
            p = ep_dir / f"{stem}_{episode_id}.json"
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    title = d.get("episode_title") or d.get("topic_id") or (
                        d.get("episode_metadata", {}).get("topic") if stem == "script_package" else None
                    )
                    if title:
                        break
                except Exception:
                    pass

        summaries.append({
            "episode_id":  episode_id,
            "title":       title,
            "has_render":  has_render,
            "qa_verdict":  qa_verdict,
            "mtime":       ep_dir.stat().st_mtime,
        })

        if len(summaries) >= limit:
            break

    return summaries


def safe_episode_asset_path(
    episode_id: str,
    kind: str,
    filename: str,
    assets_root: Optional[Path] = None,
) -> Optional[Path]:
    """
    Return the absolute path of an episode asset if it exists inside the
    episode directory and is safe (no path traversal).

    kind: "thumbnail" | "video" | "narration" | "captions" | "artifact"
    filename: the specific file name (e.g. "main.jpg", "sc_s01.mp4")

    Returns None if:
    - The path would escape the episode directory (traversal guard)
    - The file does not exist
    - The file is empty
    """
    root    = _assets_root(assets_root)
    ep_dir  = root / "episodes" / episode_id

    subdir_map = {
        "thumbnail": ep_dir / "thumbnails",
        "video":     ep_dir / "video",
        "narration": ep_dir / "narration",
        "captions":  ep_dir / "captions",
        "artifact":  ep_dir,            # JSON packages in episode root
    }

    subdir = subdir_map.get(kind)
    if subdir is None:
        return None

    try:
        candidate = (subdir / filename).resolve()
    except Exception:
        return None

    # Traversal guard — must stay inside ep_dir
    try:
        candidate.relative_to(ep_dir.resolve())
    except ValueError:
        return None

    if not candidate.exists() or candidate.stat().st_size == 0:
        return None

    return candidate
