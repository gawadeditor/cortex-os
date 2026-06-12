"""
hypogeum/asset_registry.py — Asset Registry v1

Live factory infrastructure for tracking every asset generation attempt across
the entire pipeline. Writes to assets (v2 columns), asset_versions, and
archive_index in the operational database.

Architecture:
    Module-level run context (mirrors hypogeum/finance.py pattern).
    Pipeline calls set_run_context() at run start and clear_run_context() at
    run end. Generation agents call the register_* functions without threading
    run_id through every signature.

    All write functions are non-blocking: any exception is silently swallowed.
    Registry operations must never halt the pipeline.

Lifecycle:
    1. register_asset_attempt()  — write asset_versions row, status="attempted"
    2. register_asset_success()  — update asset_versions to "succeeded" +
                                   write assets (v2) + write archive_index
    3. register_asset_failure()  — update asset_versions to "failed"

    register_existing_asset()    — for assets discovered at archive time
                                   (not during generation); writes assets (v2)
                                   and archive_index if reuse_eligible.

Utility:
    compute_asset_slot_id()      — deterministic slot ID per logical generation slot
    compute_content_hash()       — SHA-256 of a local file
    normalize_asset_type()       — canonical type string
    infer_source_stage()         — which pipeline stage produced an asset type

Public API:
    set_run_context(run_id, episode_id, db_path=None)   -> None
    clear_run_context()                                  -> None
    get_run_context()                                    -> dict

    compute_asset_slot_id(episode_id, asset_type, provider,
                          variant="", scene_index=0)     -> str (16-char hex)
    compute_content_hash(file_path)                      -> str (SHA-256 hex)
    normalize_asset_type(raw_type)                       -> str
    infer_source_stage(asset_type, provider="")          -> str

    register_asset_attempt(asset_slot_id, asset_type, provider,
                           attempt_number, version_index, attempted_at,
                           run_id=None, episode_id=None,
                           prompt_hash=None, model=None,
                           db_path=None)                 -> str  (version_id)

    register_asset_success(asset_slot_id, version_id, asset_id,
                           asset_type, provider, attempted_at,
                           run_id=None, episode_id=None,
                           duration_ms=None, model=None,
                           file_path=None, prompt_hash=None,
                           cost_id=None, cost_usd=None,
                           source_stage=None, reuse_eligible=True,
                           db_path=None)                 -> None

    register_asset_failure(asset_slot_id, version_id,
                           asset_type, provider,
                           error_type, error_message, attempted_at,
                           run_id=None, episode_id=None,
                           duration_ms=None, cost_usd=None,
                           cost_id=None, model=None,
                           db_path=None)                 -> None

    register_existing_asset(asset_id, asset_type, provider, source_stage,
                            run_id=None, episode_id=None,
                            file_path=None, model=None,
                            prompt_hash=None, reuse_eligible=True,
                            db_path=None)                -> None
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import (
    get_connection,
    insert_archive_index_entry,
    insert_asset_version,
    update_asset_version_outcome,
)
from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# Module-level run context
# ---------------------------------------------------------------------------

_active_run: dict = {}


def set_run_context(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Set the active run context.

    Must be called after run_registry.register_run() and before any generation
    agent runs. Mirrors the pattern in hypogeum/finance.py.

    Args:
        run_id:     UUID4 string from run_registry.register_run().
        episode_id: Episode identifier (e.g. "ep_000").
        db_path:    Override SQLite path (tests use tmp_path).
    """
    _active_run.clear()
    _active_run["run_id"] = run_id
    _active_run["episode_id"] = episode_id
    _active_run["db_path"] = db_path


def clear_run_context() -> None:
    """Clear the active run context. Call in the pipeline's finally block."""
    _active_run.clear()


def get_run_context() -> dict:
    """Return a copy of the current run context. Empty dict if not set."""
    return dict(_active_run)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    if db_path is not None:
        return db_path
    return _active_run.get("db_path") or _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Canonical asset type mapping
# ---------------------------------------------------------------------------

_ASSET_TYPE_MAP: dict[str, str] = {
    "clip":               "video_clip",
    "video":              "video_clip",
    "video_clip":         "video_clip",
    "scene_clip":         "video_clip",
    "thumbnail_image":    "thumbnail",
    "thumbnail":          "thumbnail",
    "reference_image":    "reference_image",
    "ref_image":          "reference_image",
    "narration":          "narration",
    "narration_segment":  "narration",
    "audio":              "narration",
    "music":              "music",
    "music_track":        "music",
    "caption":            "caption",
    "srt":                "caption",
    "final_video":        "final_video",
    "main_render":        "final_video",
}

# Stage inference: which pipeline stage owns each asset type
_STAGE_MAP: dict[str, str] = {
    "video_clip":      "visual_package_director",
    "reference_image": "visual_package_director",
    "thumbnail":       "thumbnail_director",
    "narration":       "audio_agent",
    "music":           "audio_agent",
    "caption":         "caption_agent",
    "final_video":     "assembly_agent",
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def compute_asset_slot_id(
    episode_id: str,
    asset_type: str,
    provider: str,
    variant: str = "",
    scene_index: int = 0,
) -> str:
    """
    Return a deterministic 16-character hex ID for a logical asset slot.

    The slot ID is stable across retries — it identifies the logical
    "what we are trying to generate" (episode + type + provider + variant),
    not the specific attempt. Multiple asset_versions rows share the same
    asset_slot_id.

    Args:
        episode_id:  Episode identifier.
        asset_type:  Raw or canonical asset type string.
        provider:    Provider slug ("muapi", "elevenlabs", etc.).
        variant:     Disambiguates slots within the same type: entity_id,
                     scene_id, candidate_id, or deliverable_id. Default "".
        scene_index: 0-based scene number. Default 0.

    Returns:
        16-character lowercase hexadecimal string.
    """
    normalized = "|".join([
        episode_id.strip().lower(),
        normalize_asset_type(asset_type),
        provider.strip().lower(),
        str(variant).strip().lower(),
        str(scene_index),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def compute_content_hash(file_path) -> str:
    """
    Return the SHA-256 hex digest of a local file's content.

    Returns empty string if the file does not exist or cannot be read.
    Reads in 64 KiB chunks to handle large files without loading into memory.

    Args:
        file_path: Path-like or string pointing to a local file.

    Returns:
        SHA-256 hex string (64 chars), or "" on any error.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return ""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def normalize_asset_type(raw_type: str) -> str:
    """
    Return the canonical asset type string for a raw type label.

    Unknown types are returned lowercased and stripped (no error raised).
    """
    return _ASSET_TYPE_MAP.get(str(raw_type).lower().strip(),
                               str(raw_type).lower().strip())


def infer_source_stage(asset_type: str, provider: str = "") -> str:
    """
    Infer which pipeline stage produced an asset based on its type.

    Returns "unknown" for unrecognized types.
    """
    return _STAGE_MAP.get(normalize_asset_type(asset_type), "unknown")


# ---------------------------------------------------------------------------
# register_asset_attempt
# ---------------------------------------------------------------------------

def register_asset_attempt(
    asset_slot_id: str,
    asset_type: str,
    provider: str,
    attempt_number: int,
    version_index: int,
    attempted_at: str,
    run_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    model: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> str:
    """
    Record the start of one asset generation attempt.

    Writes one asset_versions row with status="attempted". Returns the
    version_id so the caller can pass it to register_asset_success() or
    register_asset_failure() when the attempt resolves.

    Non-blocking: exceptions are silently ignored. The version_id is
    generated before the DB write so it is always available to the caller
    even on write failure.

    Args:
        asset_slot_id:  Deterministic slot ID from compute_asset_slot_id().
        asset_type:     Raw or canonical asset type.
        provider:       Provider slug ("muapi", "elevenlabs", etc.).
        attempt_number: 1-indexed human-readable attempt counter.
        version_index:  0-indexed global version counter across all attempts
                        for this slot in this run.
        attempted_at:   ISO 8601 UTC timestamp of attempt start.
        run_id:         Override run_id. Falls back to active context.
        episode_id:     Override episode_id. Falls back to active context.
        prompt_hash:    First 64 chars of SHA-256 of the generation prompt.
        model:          Model slug if known at attempt time.
        db_path:        Override SQLite path.

    Returns:
        version_id — UUID4 string. Pass to register_asset_success / failure.
    """
    version_id = str(uuid.uuid4())
    try:
        eff_run_id = run_id or _active_run.get("run_id")
        eff_episode_id = episode_id or _active_run.get("episode_id")
        if not eff_run_id or not eff_episode_id:
            return version_id

        conn = get_connection(_resolve_db_path(db_path))
        try:
            insert_asset_version(
                conn,
                version_id=version_id,
                asset_slot_id=asset_slot_id,
                run_id=eff_run_id,
                episode_id=eff_episode_id,
                asset_type=normalize_asset_type(asset_type),
                provider=provider,
                attempt_number=attempt_number,
                version_index=version_index,
                status="attempted",
                attempted_at=attempted_at,
                model=model,
                prompt_hash=prompt_hash,
                metadata_version="v2",
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking
    return version_id


# ---------------------------------------------------------------------------
# register_asset_success
# ---------------------------------------------------------------------------

def register_asset_success(
    asset_slot_id: str,
    version_id: str,
    asset_id: str,
    asset_type: str,
    provider: str,
    attempted_at: str,
    run_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    model: Optional[str] = None,
    file_path=None,
    prompt_hash: Optional[str] = None,
    cost_id: Optional[str] = None,
    cost_usd: Optional[float] = None,
    source_stage: Optional[str] = None,
    reuse_eligible: bool = True,
    db_path: Optional[Path] = None,
) -> None:
    """
    Record a successful asset generation.

    Performs three writes:
    1. UPDATE asset_versions → status="succeeded" + asset_id + metrics.
    2. INSERT OR IGNORE assets (v2 columns) — canonical asset record.
    3. INSERT archive_index — if reuse_eligible=True.

    Non-blocking: all exceptions silently ignored.

    Args:
        asset_slot_id:  From compute_asset_slot_id().
        version_id:     From register_asset_attempt().
        asset_id:       The concrete asset identifier (from MUAPI/provider result).
        asset_type:     Raw or canonical asset type.
        provider:       Provider slug.
        attempted_at:   ISO 8601 UTC timestamp of the generation attempt.
        run_id:         Override. Falls back to active context.
        episode_id:     Override. Falls back to active context.
        duration_ms:    Generation wall-clock time in milliseconds.
        model:          Model slug used (e.g. "gpt-image-2-text-to-image").
        file_path:      Absolute local path to the downloaded asset file.
                        Used to compute content_hash and file_size_bytes.
        prompt_hash:    First 64 chars of SHA-256 of the generation prompt.
        cost_id:        FK to costs table row (if available).
        cost_usd:       Actual cost of this successful generation.
        source_stage:   Pipeline stage that produced this asset. If None,
                        inferred from asset_type via infer_source_stage().
        reuse_eligible: Whether this asset can be reused across episodes.
                        Default True. Narration/captions should pass False.
        db_path:        Override SQLite path.
    """
    try:
        eff_run_id = run_id or _active_run.get("run_id")
        eff_episode_id = episode_id or _active_run.get("episode_id")
        if not eff_run_id or not eff_episode_id:
            return

        normalized_type = normalize_asset_type(asset_type)
        eff_stage = source_stage or infer_source_stage(normalized_type, provider)

        # Compute file metrics from local path when available
        content_hash: Optional[str] = None
        file_size_bytes: Optional[int] = None
        resolved_file_path: Optional[str] = None
        if file_path is not None:
            resolved_file_path = str(file_path)
            raw_hash = compute_content_hash(file_path)
            content_hash = raw_hash if raw_hash else None
            try:
                file_size_bytes = Path(file_path).stat().st_size
            except Exception:
                pass

        resolved_db = _resolve_db_path(db_path)
        conn = get_connection(resolved_db)
        try:
            # 1. Close the asset_versions attempt row
            update_asset_version_outcome(
                conn,
                version_id=version_id,
                status="succeeded",
                asset_id=asset_id,
                cost_usd=cost_usd,
                cost_id=cost_id,
                duration_ms=duration_ms,
            )

            # 2. Write assets table (v2 columns via direct SQL to include all fields)
            conn.execute(
                """
                INSERT OR IGNORE INTO assets
                    (asset_id, run_id, episode_id, asset_type, provider,
                     prompt_hash, file_path, created_at,
                     stage_name, generation_attempt, quality_status,
                     content_hash, file_size_bytes, metadata_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'unvalidated', ?, ?, ?)
                """,
                (
                    asset_id, eff_run_id, eff_episode_id,
                    normalized_type, provider,
                    prompt_hash, resolved_file_path, attempted_at,
                    eff_stage,
                    content_hash, file_size_bytes,
                    "v2",
                ),
            )

            # 3. Write archive_index for reusable assets
            if reuse_eligible and asset_id:
                insert_archive_index_entry(
                    conn,
                    archive_id=str(uuid.uuid4()),
                    asset_id=asset_id,
                    episode_id=eff_episode_id,
                    run_id=eff_run_id,
                    asset_type=normalized_type,
                    provider=provider,
                    model=model,
                    file_path=resolved_file_path,
                    prompt_hash=prompt_hash,
                    file_size_bytes=file_size_bytes,
                    content_hash=content_hash,
                    source_stage=eff_stage,
                    reuse_eligible=True,
                    quality_verdict="unvalidated",
                    archived_at=attempted_at,
                    metadata_version="v2",
                )

            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking


# ---------------------------------------------------------------------------
# register_asset_failure
# ---------------------------------------------------------------------------

def register_asset_failure(
    asset_slot_id: str,
    version_id: str,
    asset_type: str,
    provider: str,
    error_type: str,
    error_message: str,
    attempted_at: str,
    run_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    cost_id: Optional[str] = None,
    model: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """
    Record a failed asset generation attempt.

    Updates the asset_versions row to status="failed" with error details.
    Does NOT write to assets or archive_index (no successful asset exists).

    Non-blocking: exceptions silently ignored.

    Args:
        asset_slot_id:  From compute_asset_slot_id() (informational only;
                        the version_id uniquely identifies the row to update).
        version_id:     From register_asset_attempt().
        asset_type:     Raw or canonical asset type.
        provider:       Provider slug.
        error_type:     Exception class name (e.g. "MUAPIQuotaExhausted").
        error_message:  Error description or exception str().
        attempted_at:   ISO 8601 UTC timestamp (informational).
        run_id:         Override. Falls back to active context.
        episode_id:     Override. Falls back to active context.
        duration_ms:    Time spent before failure (if measurable).
        cost_usd:       Cost incurred for this failed attempt (may be non-zero).
        cost_id:        FK to costs table row (if a cost was recorded).
        model:          Model slug attempted.
        db_path:        Override SQLite path.
    """
    try:
        eff_run_id = run_id or _active_run.get("run_id")
        eff_episode_id = episode_id or _active_run.get("episode_id")
        if not eff_run_id or not eff_episode_id:
            return

        conn = get_connection(_resolve_db_path(db_path))
        try:
            update_asset_version_outcome(
                conn,
                version_id=version_id,
                status="failed",
                error_type=error_type,
                error_message=error_message,
                cost_usd=cost_usd,
                cost_id=cost_id,
                duration_ms=duration_ms,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking


# ---------------------------------------------------------------------------
# register_existing_asset
# ---------------------------------------------------------------------------

def register_existing_asset(
    asset_id: str,
    asset_type: str,
    provider: str,
    source_stage: str,
    run_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    file_path=None,
    model: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    reuse_eligible: bool = True,
    db_path: Optional[Path] = None,
) -> None:
    """
    Register an asset discovered at archive time rather than during generation.

    Called by asset_archive_agent after JSONL indexing is complete, for each
    newly cataloged asset that was not recorded during live generation.

    Writes:
    1. INSERT OR IGNORE assets (v2) — idempotent; skips if already present.
    2. INSERT archive_index — only if reuse_eligible=True.

    Non-blocking: exceptions silently ignored.

    Args:
        asset_id:       The asset identifier.
        asset_type:     Raw or canonical asset type.
        provider:       Provider slug.
        source_stage:   Which pipeline stage produced this asset.
        run_id:         Override. Falls back to active context.
        episode_id:     Override. Falls back to active context.
        file_path:      Local file path (for content_hash + file_size).
        model:          Model slug (if known from catalog record).
        prompt_hash:    Prompt hash (if known from catalog record).
        reuse_eligible: Whether this asset can be reused. Default True.
        db_path:        Override SQLite path.
    """
    try:
        eff_run_id = run_id or _active_run.get("run_id")
        eff_episode_id = episode_id or _active_run.get("episode_id")
        if not eff_run_id or not eff_episode_id:
            return

        normalized_type = normalize_asset_type(asset_type)
        now = _now_iso()

        content_hash: Optional[str] = None
        file_size_bytes: Optional[int] = None
        resolved_file_path: Optional[str] = None
        if file_path is not None:
            resolved_file_path = str(file_path)
            raw_hash = compute_content_hash(file_path)
            content_hash = raw_hash if raw_hash else None
            try:
                file_size_bytes = Path(file_path).stat().st_size
            except Exception:
                pass

        resolved_db = _resolve_db_path(db_path)
        conn = get_connection(resolved_db)
        try:
            # assets table
            conn.execute(
                """
                INSERT OR IGNORE INTO assets
                    (asset_id, run_id, episode_id, asset_type, provider,
                     prompt_hash, file_path, created_at,
                     stage_name, quality_status, content_hash,
                     file_size_bytes, metadata_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unvalidated', ?, ?, ?)
                """,
                (
                    asset_id, eff_run_id, eff_episode_id,
                    normalized_type, provider,
                    prompt_hash, resolved_file_path, now,
                    source_stage,
                    content_hash, file_size_bytes,
                    "v2",
                ),
            )

            # archive_index for reusable assets
            if reuse_eligible and asset_id:
                insert_archive_index_entry(
                    conn,
                    archive_id=str(uuid.uuid4()),
                    asset_id=asset_id,
                    episode_id=eff_episode_id,
                    run_id=eff_run_id,
                    asset_type=normalized_type,
                    provider=provider,
                    model=model,
                    file_path=resolved_file_path,
                    prompt_hash=prompt_hash,
                    file_size_bytes=file_size_bytes,
                    content_hash=content_hash,
                    source_stage=source_stage,
                    reuse_eligible=True,
                    quality_verdict="unvalidated",
                    archived_at=now,
                    metadata_version="v2",
                )

            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking
