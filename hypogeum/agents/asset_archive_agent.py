"""
hypogeum/agents/asset_archive_agent.py — Asset Catalog Agent (M7.2)

Cross-episode asset catalog. Indexes all pipeline-produced assets from one
episode run into shared, append-only JSONL catalogs that span all episodes.

The catalogs serve as the foundational data layer for:
  - Asset reuse (Reuse Recommendation Engine, M7.3)
  - Cross-episode entity continuity (Continuity Agent, M8.2)
  - Portfolio management (Portfolio Manager Agent, M8.4)

Catalogs written (shared across all episodes):
  asset_catalog.jsonl          — master record for every indexed asset
  entity_catalog.jsonl         — one record per entity (upserted across episodes)
  clip_catalog.jsonl           — detailed video clip records
  thumbnail_catalog.jsonl      — detailed thumbnail records
  music_catalog.jsonl          — detailed music track records

Per-episode output (written alongside other episode artifacts):
  episode_asset_index_{episode_id}.json  — asset_id list for this episode

Default catalog directory: .hypogeum-assets/catalogs/
Default episode output: .hypogeum-assets/episodes/{episode_id}/

Checks (5):
  c_clips_indexed          — PASS/WARN — clips from asset_manifest + renders
  c_thumbnails_indexed     — PASS/WARN — thumbnails from thumbnail_package
  c_music_indexed          — PASS/WARN — music from music_log
  c_entities_indexed       — PASS/WARN — entities from visual_continuity_record
  c_episode_index_written  — PASS/FAIL — episode_asset_index written to disk

Verdicts:
  CATALOGED — all checks pass, no failures or warnings
  WARNING   — no failures, one or more warnings
  FAILED    — c_episode_index_written failed (disk write error)

Non-blocking — verdict is logged and stored in PipelineResult but does not
raise exceptions. Downstream consumers use the query API, not the record.

Query API (module-level functions, read-only):
  query_by_asset_id(asset_id, catalog_dir)           → dict | None
  query_by_entity_id(entity_id, catalog_dir)         → list[dict]
  query_by_source_episode(episode_id, catalog_dir)   → list[dict]
  query_by_asset_type(asset_type, catalog_dir)       → list[dict]
  query_reusable_candidates(asset_type, catalog_dir) → list[dict]

Responsibilities:
  - Index all video clips (visual_package.asset_manifest + final_render_package)
  - Index all thumbnails (thumbnail_package + thumbnail_references)
  - Index all music tracks (final_render_package.music_log)
  - Upsert entity records (visual_continuity_record.locked_entities)
  - Index narration and caption assets (narration_package)
  - Write per-episode asset index file
  - Expose read-only query API for downstream agents

Prohibited responsibilities (owned elsewhere):
  - Content quality evaluation (QA Agent)
  - Budget gate or cost accounting (Finance Agent)
  - Publishing decisions (Publishing Agent)
  - Rights/compliance checks (Compliance Agent)
  - Publication audit records (Publication Archive Recorder / archive_agent)

No Claude calls — pure deterministic cataloging and indexing.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hypogeum.asset_registry as _asset_registry
from hypogeum.db import initialize_schema
from hypogeum.identifiers import METADATA_VERSION
from hypogeum.schemas import validate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CATALOG_SUBDIR = ".hypogeum-assets/catalogs"
_DEFAULT_EPISODES_SUBDIR = ".hypogeum-assets/episodes"


# ---------------------------------------------------------------------------
# Internal helpers — path resolution
# ---------------------------------------------------------------------------

def _default_catalog_dir() -> Path:
    return _PROJECT_ROOT / _DEFAULT_CATALOG_SUBDIR


def _default_output_dir(episode_id: str) -> Path:
    return _PROJECT_ROOT / _DEFAULT_EPISODES_SUBDIR / episode_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Internal helpers — JSONL I/O
# ---------------------------------------------------------------------------

def _read_catalog(catalog_path: Path) -> list[dict]:
    """Read all valid JSON records from a JSONL file. Returns [] if absent."""
    if not catalog_path.exists():
        return []
    records: list[dict] = []
    with catalog_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _append_line(catalog_path: Path, record: dict) -> None:
    """Append one JSON record as a line to a JSONL catalog file."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with catalog_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _rewrite_catalog(catalog_path: Path, records: list[dict]) -> None:
    """Overwrite a JSONL catalog file with the given records (used for upserts)."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with catalog_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _existing_ids(catalog_path: Path, id_field: str) -> set[str]:
    """Return the set of non-empty id_field values already stored in a catalog."""
    return {
        r.get(id_field, "")
        for r in _read_catalog(catalog_path)
        if r.get(id_field)
    }


def _add_check(
    checks: list,
    failures: list,
    warnings: list,
    check_id: str,
    status: str,
    detail: str,
) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
    if status == "fail":
        failures.append(check_id)
    elif status == "warn":
        warnings.append(check_id)


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _index_clips(
    visual_package: dict,
    final_render_package: dict,
    episode_id: str,
    catalog_dir: Path,
    now: str,
    run_id: str = "",
) -> int:
    """
    Index video clips from visual_package.asset_manifest.assets,
    final_render_package.episode_render, and shorts_renders.

    Writes to clip_catalog.jsonl and asset_catalog.jsonl.
    Skips asset_ids already present (idempotent).
    Returns count of newly indexed clips.
    """
    clip_catalog = catalog_dir / "clip_catalog.jsonl"
    asset_catalog = catalog_dir / "asset_catalog.jsonl"

    existing_clips = _existing_ids(clip_catalog, "asset_id")
    existing_assets = _existing_ids(asset_catalog, "asset_id")
    indexed = 0

    def _add(asset_id: str, clip_type: str, scene_id: str, derivation_source: str) -> None:
        nonlocal indexed
        if not asset_id or asset_id in existing_clips:
            return
        _append_line(clip_catalog, {
            "clip_id": f"clip_{uuid.uuid4().hex[:12]}",
            "asset_id": asset_id,
            "run_id": run_id,
            "source_episode_id": episode_id,
            "clip_type": clip_type,
            "scene_id": scene_id,
            "derivation_source": derivation_source,
            "provider": "muapi",
            "model": None,
            "file_path": None,
            "indexed_at": now,
            "reusable": True,
            "metadata_version": METADATA_VERSION,
        })
        existing_clips.add(asset_id)
        if asset_id not in existing_assets:
            _append_line(asset_catalog, {
                "asset_id": asset_id,
                "run_id": run_id,
                "asset_type": "clip",
                "source_episode_id": episode_id,
                "provider": "muapi",
                "model": None,
                "file_path": None,
                "reusable": True,
                "indexed_at": now,
                "metadata": {"clip_type": clip_type, "scene_id": scene_id},
                "metadata_version": METADATA_VERSION,
            })
            existing_assets.add(asset_id)
        indexed += 1

    # From visual_package.asset_manifest.assets
    for asset in visual_package.get("asset_manifest", {}).get("assets", []):
        _add(
            asset_id=asset.get("asset_id", ""),
            clip_type=asset.get("source_type", "main_episode"),
            scene_id=asset.get("scene_id", ""),
            derivation_source=asset.get("derivation_source", ""),
        )

    # Main episode render
    episode_render = final_render_package.get("episode_render", {})
    _add(
        asset_id=episode_render.get("video_asset_id", ""),
        clip_type="main_episode",
        scene_id="",
        derivation_source="final_render",
    )

    # Shorts renders
    for short_render in final_render_package.get("shorts_renders", []):
        short_id = short_render.get("short_id", "")
        _add(
            asset_id=short_render.get("video_asset_id", ""),
            clip_type="short",
            scene_id="",
            derivation_source=f"short_render:{short_id}",
        )

    return indexed


def _index_thumbnails(
    thumbnail_package: dict,
    final_render_package: dict,
    episode_id: str,
    catalog_dir: Path,
    now: str,
    run_id: str = "",
) -> int:
    """
    Index thumbnails from thumbnail_package.main_thumbnail and short_thumbnails.
    Uses thumbnail_references from final_render_package for format metadata.

    Writes to thumbnail_catalog.jsonl and asset_catalog.jsonl.
    Skips asset_ids already present (idempotent).
    Returns count of newly indexed thumbnails.
    """
    thumb_catalog = catalog_dir / "thumbnail_catalog.jsonl"
    asset_catalog = catalog_dir / "asset_catalog.jsonl"

    existing_thumbs = _existing_ids(thumb_catalog, "asset_id")
    existing_assets = _existing_ids(asset_catalog, "asset_id")
    indexed = 0

    # Build format lookup from thumbnail_references
    format_lookup: dict[str, str] = {}
    for ref in final_render_package.get("thumbnail_references", []):
        tid = ref.get("thumbnail_asset_id", "")
        if tid:
            format_lookup[tid] = ref.get("format", "")

    def _add(asset_id: str, thumb_type: str, short_id: Optional[str] = None) -> None:
        nonlocal indexed
        if not asset_id or asset_id in existing_thumbs:
            return
        _append_line(thumb_catalog, {
            "thumbnail_id": f"thumb_{uuid.uuid4().hex[:12]}",
            "asset_id": asset_id,
            "run_id": run_id,
            "source_episode_id": episode_id,
            "thumbnail_type": thumb_type,
            "short_id": short_id,
            "format": format_lookup.get(asset_id, ""),
            "provider": "muapi",
            "model": None,
            "file_path": None,
            "indexed_at": now,
            "reusable": True,
            "metadata_version": METADATA_VERSION,
        })
        existing_thumbs.add(asset_id)
        if asset_id not in existing_assets:
            _append_line(asset_catalog, {
                "asset_id": asset_id,
                "run_id": run_id,
                "asset_type": "thumbnail",
                "source_episode_id": episode_id,
                "provider": "muapi",
                "model": None,
                "file_path": None,
                "reusable": True,
                "indexed_at": now,
                "metadata": {
                    "thumbnail_type": thumb_type,
                    "format": format_lookup.get(asset_id, ""),
                },
                "metadata_version": METADATA_VERSION,
            })
            existing_assets.add(asset_id)
        indexed += 1

    # Main thumbnail
    main_thumb = thumbnail_package.get("main_thumbnail", {})
    _add(asset_id=main_thumb.get("asset_id", ""), thumb_type="main")

    # Short thumbnails
    for short_thumb in thumbnail_package.get("short_thumbnails", []):
        _add(
            asset_id=short_thumb.get("asset_id", ""),
            thumb_type="short",
            short_id=short_thumb.get("short_id") or None,
        )

    return indexed


def _index_music(
    final_render_package: dict,
    episode_id: str,
    catalog_dir: Path,
    now: str,
    run_id: str = "",
) -> int:
    """
    Index music tracks from final_render_package.music_log.

    Writes to music_catalog.jsonl and asset_catalog.jsonl.
    Skips log_ids already present (idempotent).
    Returns count of newly indexed music records.
    """
    music_catalog = catalog_dir / "music_catalog.jsonl"
    asset_catalog = catalog_dir / "asset_catalog.jsonl"

    existing_music = _existing_ids(music_catalog, "log_id")
    existing_assets = _existing_ids(asset_catalog, "asset_id")
    indexed = 0

    for entry in final_render_package.get("music_log", []):
        log_id = entry.get("log_id", "")
        if not log_id or log_id in existing_music:
            continue

        source = entry.get("source", "")
        asset_id = entry.get("asset_id", "") or log_id
        reusable = source == "channel_library"
        provider = "elevenlabs" if source != "channel_library" else "channel_library"

        _append_line(music_catalog, {
            "music_id": f"music_{uuid.uuid4().hex[:12]}",
            "log_id": log_id,
            "asset_id": asset_id,
            "run_id": run_id,
            "source_episode_id": episode_id,
            "source": source,
            "category": entry.get("category", ""),
            "section_key": entry.get("section_key", ""),
            "provider": provider,
            "model": None,
            "file_path": None,
            "indexed_at": now,
            "reusable": reusable,
            "metadata_version": METADATA_VERSION,
        })
        existing_music.add(log_id)

        if asset_id not in existing_assets:
            _append_line(asset_catalog, {
                "asset_id": asset_id,
                "run_id": run_id,
                "asset_type": "music",
                "source_episode_id": episode_id,
                "provider": provider,
                "model": None,
                "file_path": None,
                "reusable": reusable,
                "indexed_at": now,
                "metadata": {"source": source, "category": entry.get("category", "")},
                "metadata_version": METADATA_VERSION,
            })
            existing_assets.add(asset_id)

        indexed += 1

    return indexed


def _index_entities(
    visual_package: dict,
    episode_id: str,
    catalog_dir: Path,
    now: str,
) -> int:
    """
    Upsert entity records from visual_package.visual_continuity_record.locked_entities
    into entity_catalog.jsonl.

    Upsert semantics:
      - New entity: appended with source_episodes=[episode_id], first_seen_at=now.
      - Existing entity: episode_id added to source_episodes, last_seen_at updated,
        lock_state and reference_image_asset_id updated if present.

    Rewrites entity_catalog.jsonl in-place when any entity is upserted.
    Returns count of entity records processed (new + updated).
    """
    entity_catalog = catalog_dir / "entity_catalog.jsonl"

    locked_entities = (
        visual_package
        .get("visual_continuity_record", {})
        .get("locked_entities", [])
    )
    if not locked_entities:
        return 0

    existing_records = _read_catalog(entity_catalog)
    entity_map: dict[str, dict] = {
        r["entity_id"]: r
        for r in existing_records
        if r.get("entity_id")
    }

    processed = 0

    for ent in locked_entities:
        if not isinstance(ent, dict):
            continue
        entity_id = ent.get("entity_id", "")
        if not entity_id:
            continue

        if entity_id in entity_map:
            rec = entity_map[entity_id]
            episodes = rec.get("source_episodes", [])
            if episode_id not in episodes:
                episodes.append(episode_id)
            rec["source_episodes"] = episodes
            rec["last_seen_at"] = now
            rec["lock_state"] = ent.get("lock_state", rec.get("lock_state", "provisional"))
            if ent.get("reference_image_asset_id"):
                rec["reference_image_asset_id"] = ent["reference_image_asset_id"]
        else:
            entity_map[entity_id] = {
                "entity_id": entity_id,
                "source_episodes": [episode_id],
                "lock_state": ent.get("lock_state", "provisional"),
                "reference_image_asset_id": ent.get("reference_image_asset_id"),
                "first_seen_at": now,
                "last_seen_at": now,
            }

        processed += 1

    _rewrite_catalog(entity_catalog, list(entity_map.values()))
    return processed


def _index_narration(
    narration_package: dict,
    episode_id: str,
    catalog_dir: Path,
    now: str,
    run_id: str = "",
) -> int:
    """
    Index narration audio and caption assets into asset_catalog.jsonl only.
    No separate narration_catalog is maintained.
    Narration and captions are marked reusable=False (episode-specific content).
    Returns count of newly indexed narration/caption assets.
    """
    asset_catalog = catalog_dir / "asset_catalog.jsonl"
    existing_assets = _existing_ids(asset_catalog, "asset_id")
    indexed = 0

    for track in narration_package.get("stitched_tracks", []):
        audio_id = track.get("audio_asset_id", "")
        if not audio_id or audio_id in existing_assets:
            continue
        _append_line(asset_catalog, {
            "asset_id": audio_id,
            "run_id": run_id,
            "asset_type": "narration",
            "source_episode_id": episode_id,
            "provider": "elevenlabs",
            "model": None,
            "file_path": None,
            "reusable": False,
            "indexed_at": now,
            "metadata": {
                "track_id": track.get("track_id", ""),
                "source_type": track.get("source_type", ""),
            },
            "metadata_version": METADATA_VERSION,
        })
        existing_assets.add(audio_id)
        indexed += 1

    for cap in narration_package.get("captions", []):
        srt_id = cap.get("srt_asset_id", "")
        if not srt_id or srt_id in existing_assets:
            continue
        _append_line(asset_catalog, {
            "asset_id": srt_id,
            "run_id": run_id,
            "asset_type": "caption",
            "source_episode_id": episode_id,
            "provider": "elevenlabs",
            "model": None,
            "file_path": None,
            "reusable": False,
            "indexed_at": now,
            "metadata": {
                "caption_id": cap.get("caption_id", ""),
                "caption_method": cap.get("caption_method", ""),
            },
            "metadata_version": METADATA_VERSION,
        })
        existing_assets.add(srt_id)
        indexed += 1

    return indexed


# ---------------------------------------------------------------------------
# Public API — run_asset_archive
# ---------------------------------------------------------------------------

def run_asset_archive(
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    final_render_package: dict,
    episode_id: str,
    catalog_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Index all assets from one episode run into shared JSONL catalogs.

    All catalogs are append-only (except entity_catalog which uses upsert).
    Safe to call on resume runs — existing asset_ids are skipped.

    Args:
        visual_package:       Visual Package Director output.
        narration_package:    Audio Agent output.
        thumbnail_package:    Thumbnail Director output.
        final_render_package: Assembly Agent output.
        episode_id:           Episode identifier for asset attribution.
        catalog_dir:          Override for shared catalog directory.
                              Defaults to .hypogeum-assets/catalogs/.
        output_dir:           Override for per-episode output directory.
                              Defaults to .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        asset_archive_record dict (schema-validated, written to disk).
        Never raises — disk errors produce verdict="FAILED" in checks.
    """
    now = _now_iso()
    _run_id = run_id or ""
    _catalog_dir = Path(catalog_dir) if catalog_dir is not None else _default_catalog_dir()
    _output_dir = Path(output_dir) if output_dir is not None else _default_output_dir(episode_id)
    _catalog_dir.mkdir(parents=True, exist_ok=True)
    _output_dir.mkdir(parents=True, exist_ok=True)

    # Record which asset_ids existed before this run — used for DB write diff.
    _existing_before = _existing_ids(_catalog_dir / "asset_catalog.jsonl", "asset_id")

    checks: list = []
    failures: list = []
    warnings: list = []

    # ----------------------------------------------------------------
    # Check 1 — clips
    # ----------------------------------------------------------------
    clips_indexed = _index_clips(
        visual_package, final_render_package, episode_id, _catalog_dir, now,
        run_id=_run_id,
    )
    manifest_assets = visual_package.get("asset_manifest", {}).get("assets", [])
    episode_render_id = final_render_package.get("episode_render", {}).get("video_asset_id", "")
    shorts_count = len(final_render_package.get("shorts_renders", []))
    total_clip_sources = len(manifest_assets) + (1 if episode_render_id else 0) + shorts_count

    if clips_indexed > 0:
        _add_check(checks, failures, warnings, "c_clips_indexed", "pass",
                   f"Indexed {clips_indexed} video clip(s) into clip_catalog.jsonl")
    elif total_clip_sources == 0:
        _add_check(checks, failures, warnings, "c_clips_indexed", "pass",
                   "No video clips available to index for this episode")
    else:
        _add_check(checks, failures, warnings, "c_clips_indexed", "warn",
                   f"All {total_clip_sources} clip source(s) were already in the catalog "
                   "(idempotent re-run)")

    # ----------------------------------------------------------------
    # Check 2 — thumbnails
    # ----------------------------------------------------------------
    thumbs_indexed = _index_thumbnails(
        thumbnail_package, final_render_package, episode_id, _catalog_dir, now,
        run_id=_run_id,
    )
    main_thumb_id = thumbnail_package.get("main_thumbnail", {}).get("asset_id", "")
    short_thumbs_count = len(thumbnail_package.get("short_thumbnails", []))
    total_thumb_sources = (1 if main_thumb_id else 0) + short_thumbs_count

    if thumbs_indexed > 0:
        _add_check(checks, failures, warnings, "c_thumbnails_indexed", "pass",
                   f"Indexed {thumbs_indexed} thumbnail(s) into thumbnail_catalog.jsonl")
    elif total_thumb_sources == 0:
        _add_check(checks, failures, warnings, "c_thumbnails_indexed", "pass",
                   "No thumbnails available to index for this episode")
    else:
        _add_check(checks, failures, warnings, "c_thumbnails_indexed", "warn",
                   f"All {total_thumb_sources} thumbnail(s) were already in the catalog "
                   "(idempotent re-run)")

    # ----------------------------------------------------------------
    # Check 3 — music
    # ----------------------------------------------------------------
    music_indexed = _index_music(
        final_render_package, episode_id, _catalog_dir, now,
        run_id=_run_id,
    )
    music_log = final_render_package.get("music_log", [])

    if music_indexed > 0:
        _add_check(checks, failures, warnings, "c_music_indexed", "pass",
                   f"Indexed {music_indexed} music track(s) into music_catalog.jsonl")
    elif not music_log:
        _add_check(checks, failures, warnings, "c_music_indexed", "pass",
                   "No music_log entries for this episode")
    else:
        _add_check(checks, failures, warnings, "c_music_indexed", "warn",
                   f"All {len(music_log)} music log entry/entries were already in the catalog "
                   "(idempotent re-run)")

    # ----------------------------------------------------------------
    # Check 4 — entities
    # ----------------------------------------------------------------
    entities_indexed = _index_entities(
        visual_package, episode_id, _catalog_dir, now,
    )
    total_entities = len(
        visual_package.get("visual_continuity_record", {}).get("locked_entities", [])
    )

    if entities_indexed > 0:
        _add_check(checks, failures, warnings, "c_entities_indexed", "pass",
                   f"Processed {entities_indexed} entity record(s) in entity_catalog.jsonl")
    elif total_entities == 0:
        _add_check(checks, failures, warnings, "c_entities_indexed", "pass",
                   "No locked entities in visual_continuity_record for this episode")
    else:
        _add_check(checks, failures, warnings, "c_entities_indexed", "warn",
                   f"Entity records for this episode were already up to date "
                   "(idempotent re-run)")

    # Index narration assets (best-effort, no dedicated check)
    _index_narration(narration_package, episode_id, _catalog_dir, now, run_id=_run_id)

    # ----------------------------------------------------------------
    # Check 5 — episode index
    # ----------------------------------------------------------------
    asset_catalog_records = _read_catalog(_catalog_dir / "asset_catalog.jsonl")
    episode_asset_ids = [
        r["asset_id"]
        for r in asset_catalog_records
        if r.get("source_episode_id") == episode_id and r.get("asset_id")
    ]

    try:
        episode_index = {
            "episode_id": episode_id,
            "indexed_at": now,
            "asset_count": len(episode_asset_ids),
            "asset_ids": episode_asset_ids,
        }
        index_path = _output_dir / f"episode_asset_index_{episode_id}.json"
        index_path.write_text(
            json.dumps(episode_index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _add_check(checks, failures, warnings, "c_episode_index_written", "pass",
                   f"episode_asset_index_{episode_id}.json written: "
                   f"{len(episode_asset_ids)} asset(s)")
    except Exception as exc:
        _add_check(checks, failures, warnings, "c_episode_index_written", "fail",
                   f"Failed to write episode_asset_index_{episode_id}.json: {exc}")

    # ----------------------------------------------------------------
    # Verdict
    # ----------------------------------------------------------------
    cataloged = len(failures) == 0

    if not cataloged:
        verdict = "FAILED"
    elif warnings:
        verdict = "WARNING"
    else:
        verdict = "CATALOGED"

    total_assets_indexed = clips_indexed + thumbs_indexed + music_indexed

    record = {
        "asset_archive_id": f"aa_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "run_id": _run_id,
        "cataloged_at": now,
        "verdict": verdict,
        "cataloged": cataloged,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "catalog_summary": {
            "clips_indexed": clips_indexed,
            "thumbnails_indexed": thumbs_indexed,
            "music_tracks_indexed": music_indexed,
            "entities_indexed": entities_indexed,
            "total_assets_indexed": total_assets_indexed,
        },
        "metadata_version": METADATA_VERSION,
    }

    validate("asset_archive_record", record)

    # ----------------------------------------------------------------
    # DB write — non-blocking; write newly indexed assets to assets table
    # ----------------------------------------------------------------
    if _run_id:
        try:
            all_catalog = _read_catalog(_catalog_dir / "asset_catalog.jsonl")
            new_records = [
                r for r in all_catalog
                if r.get("asset_id") and r.get("asset_id") not in _existing_before
            ]
            if new_records:
                initialize_schema(db_path)
                for r in new_records:
                    _asset_registry.register_existing_asset(
                        asset_id=r["asset_id"],
                        asset_type=r.get("asset_type", ""),
                        provider=r.get("provider", ""),
                        source_stage=_asset_registry.infer_source_stage(
                            r.get("asset_type", "")
                        ),
                        run_id=_run_id,
                        episode_id=episode_id,
                        file_path=r.get("file_path"),
                        model=r.get("model"),
                        reuse_eligible=bool(r.get("reusable", True)),
                        db_path=db_path,
                    )
        except Exception:
            pass  # Asset archive DB writes must never halt the pipeline

    out_path = _output_dir / f"asset_archive_record_{episode_id}.json"
    out_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return record


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def _resolve_catalog_dir(catalog_dir: Optional[Path]) -> Path:
    return Path(catalog_dir) if catalog_dir is not None else _default_catalog_dir()


def query_by_asset_id(
    asset_id: str,
    catalog_dir: Optional[Path] = None,
) -> Optional[dict]:
    """
    Return the asset_catalog record for a given asset_id, or None if not found.

    Scans asset_catalog.jsonl linearly. Returns the first matching record.
    asset_ids are unique per episode run; duplicates indicate re-indexing.
    """
    path = _resolve_catalog_dir(catalog_dir) / "asset_catalog.jsonl"
    for record in _read_catalog(path):
        if record.get("asset_id") == asset_id:
            return record
    return None


def query_by_entity_id(
    entity_id: str,
    catalog_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Return all entity_catalog records matching entity_id.

    Under normal upsert semantics, at most one record per entity_id exists.
    Returns a list to support pathological cases (manual edits, corruption).
    Scans entity_catalog.jsonl.
    """
    path = _resolve_catalog_dir(catalog_dir) / "entity_catalog.jsonl"
    return [r for r in _read_catalog(path) if r.get("entity_id") == entity_id]


def query_by_source_episode(
    episode_id: str,
    catalog_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Return all asset_catalog records produced by the given episode.

    Scans asset_catalog.jsonl. Includes all asset types: clip, thumbnail,
    music, narration, caption.
    """
    path = _resolve_catalog_dir(catalog_dir) / "asset_catalog.jsonl"
    return [r for r in _read_catalog(path) if r.get("source_episode_id") == episode_id]


def query_by_asset_type(
    asset_type: str,
    catalog_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Return all asset_catalog records of the given type.

    Valid asset_type values: "clip", "thumbnail", "music", "narration", "caption".
    Scans asset_catalog.jsonl.
    """
    path = _resolve_catalog_dir(catalog_dir) / "asset_catalog.jsonl"
    return [r for r in _read_catalog(path) if r.get("asset_type") == asset_type]


def query_reusable_candidates(
    asset_type: str,
    catalog_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Return all reusable asset_catalog records of the given type.

    Filters asset_catalog.jsonl by asset_type and reusable=True.
    Used by Reuse Recommendation Engine (M7.3) to surface existing assets
    before requesting new generation.

    Reusability by type:
      clip      — True (video clips can be reused across episodes)
      thumbnail — True (thumbnails can be reused across episodes)
      music     — True only for channel_library source; False for ai_generated
      narration — False (episode-specific script content)
      caption   — False (episode-specific timing)
    """
    path = _resolve_catalog_dir(catalog_dir) / "asset_catalog.jsonl"
    return [
        r for r in _read_catalog(path)
        if r.get("asset_type") == asset_type and r.get("reusable") is True
    ]
