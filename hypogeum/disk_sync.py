"""
hypogeum/disk_sync.py — Sync disk episode artifacts into SQLite.

SQLite is the canonical source of truth for the dashboard.  Disk artifacts
produced by pipeline scripts (ep_003, ep_000, etc.) may not have been recorded
to the DB, causing /episodes to show 0 rows and /episode/<id> to show blanks.

This module reads the JSON packages from .hypogeum-assets/episodes/<id>/ and
upserts minimal DB rows so every disk episode becomes visible in the dashboard.

Idempotent: running multiple times never creates duplicate rows.

Public API
----------
    sync_disk_episode_to_db(episode_id, assets_root=None, db_path=None) -> dict
    sync_all_disk_episodes_to_db(assets_root=None, db_path=None)        -> list[dict]

    infer_episode_status(qa_pkg, bga_pkgs, artifacts)    -> str
    load_disk_pkg(ep_dir, stem, episode_id)               -> dict
    episode_title_from_disk(ep_dir, episode_id)           -> str | None
    extract_total_cost(ep_dir, episode_id)                -> float | None
    extract_render_duration_ms(ep_dir, episode_id)        -> int | None
    extract_scene_count(ep_dir, episode_id)               -> int
    extract_created_at(ep_dir, episode_id)                -> str
    SYNTHETIC_STAGES                                      -> list[(stage_name, group_label, pkg_stem)]

Status semantics
----------------
    COMPLETE      render + QA approved + BGA approved + no blocking violations
    QA_WARNING    render + QA approved-with-warnings + BGA approved
    NEEDS_REVIEW  BGA BLOCKED, QA failed, or missing publish metadata
    PARTIAL       some artifacts exist but render/QA not complete
    FAILED        pipeline explicitly failed
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_ASSETS  = _PROJECT_ROOT / ".hypogeum-assets"
_DEFAULT_DB      = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# Stages we synthesise from disk packages so the production tab isn't empty.
# Each entry maps (stage_name, group_label, package_stem) — package_stem is
# used only for a cost comment; we can't recover per-stage costs from disk.
SYNTHETIC_STAGES = [
    ("research_director",       "Research",    "research_package"),
    ("narrative_architect",     "Script",      "narrative_package"),
    ("script_engine",           "Script",      "script_package"),
    ("audio_agent",             "Voiceover",   "narration_package"),
    ("visual_package_director", "Visuals",     "visual_package"),
    ("thumbnail_director",      "Visuals",     "thumbnail_package"),
    ("assembly_agent",          "Assembly",    "final_render_package"),
    ("qa_agent",                "Final Video", "qa_approval_report"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_disk_pkg(ep_dir: Path, stem: str, episode_id: str) -> dict:
    """Read <stem>_<episode_id>.json; also tries <stem>.json. Returns {} on error."""
    for name in (f"{stem}_{episode_id}.json", f"{stem}.json"):
        p = ep_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


# Private alias kept for internal use within this module
_load = load_disk_pkg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def episode_title_from_disk(ep_dir: Path, episode_id: str) -> Optional[str]:
    """Best available episode title from disk packages."""
    for stem in ("topic_brief", "narrative_package", "script_package"):
        pkg = _load(ep_dir, stem, episode_id)
        for key in ("episode_title", "title", "topic"):
            val = pkg.get(key)
            if val and isinstance(val, str) and len(val) > 3:
                return val
    return None


def infer_episode_status(
    qa_pkg: dict,
    bga_pkgs: list[dict],
    artifacts: dict,
) -> str:
    """
    Derive canonical episode status from QA + BGA + artifact flags.

    COMPLETE     = render present + QA approved + all BGA passes approved
    QA_WARNING   = render present + QA approved but has warnings + BGA approved
    NEEDS_REVIEW = BGA BLOCKED, QA failed, or render present but unverified
    PARTIAL      = some files but render or QA missing
    FAILED       = explicit failed state
    """
    has_render = bool(artifacts.get("has_render"))

    if not has_render:
        return "partial"

    # Check BGA verdict (any BLOCKED pass → NEEDS_REVIEW)
    for bga in bga_pkgs:
        outcome = (bga.get("outcome") or "").upper()
        blocking = (bga.get("summary") or {}).get("blocking_count", 0)
        violations = bga.get("violations") or []
        has_blocking_violation = any(
            (v.get("severity") or "").upper() == "BLOCKING"
            for v in violations
        )
        if outcome == "BLOCKED" or blocking > 0 or has_blocking_violation:
            return "needs_review"

    # Check QA verdict — guard must come before any attribute access
    if not qa_pkg:
        return "needs_review"

    qa_verdict  = (qa_pkg.get("verdict") or "").upper()
    qa_approved = bool(qa_pkg.get("approved"))

    if not qa_approved:
        return "needs_review"

    if qa_verdict == "WARNING":
        return "qa_warning"

    return "complete"


def extract_total_cost(ep_dir: Path, episode_id: str) -> Optional[float]:
    """
    Sum estimated spend from finance authorization packages.
    Falls back to 0.0 if no finance package found.
    """
    total = 0.0
    found = False
    for stem in ("finance_authorization", "finance_post_vpd_authorization"):
        pkg = _load(ep_dir, stem, episode_id)
        val = pkg.get("estimated_spend_usd")
        if val is not None:
            total += float(val)
            found = True
    return round(total, 4) if found else None


def extract_render_duration_ms(ep_dir: Path, episode_id: str) -> Optional[int]:
    """Extract episode video duration in milliseconds from final_render_package."""
    pkg = _load(ep_dir, "final_render_package", episode_id)
    ep_render = pkg.get("episode_render") or {}
    dur_s = ep_render.get("duration_seconds")
    if dur_s is not None:
        return int(float(dur_s) * 1000)
    # Fallback: sum narration segments
    nar = _load(ep_dir, "narration_package", episode_id)
    segs = nar.get("narration_segments") or []
    if segs:
        total_s = sum(s.get("duration_seconds", 0) for s in segs)
        if total_s > 0:
            return int(total_s * 1000)
    return None


def extract_scene_count(ep_dir: Path, episode_id: str) -> int:
    """Count scenes from final_render_package or script_package."""
    pkg = _load(ep_dir, "final_render_package", episode_id)
    ep_render = pkg.get("episode_render") or {}
    count = ep_render.get("scene_count")
    if count:
        return int(count)
    nar = _load(ep_dir, "narration_package", episode_id)
    segs = nar.get("narration_segments") or []
    return len(segs)


def extract_created_at(ep_dir: Path, episode_id: str) -> str:
    """
    Best available creation timestamp. Falls back to directory mtime.
    """
    for stem, key in [
        ("topic_brief",              "issued_at"),
        ("finance_authorization",    "recorded_at"),
        ("qa_approval_report",       "evaluated_at"),
        ("final_render_package",     "rendered_at"),
    ]:
        pkg = _load(ep_dir, stem, episode_id)
        val = pkg.get(key)
        if val and isinstance(val, str):
            return val[:19] + "Z"
    # Directory mtime
    try:
        mtime = ep_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()[:19] + "Z"
    except Exception:
        return _now_iso()[:19] + "Z"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_disk_episode_to_db(
    episode_id: str,
    assets_root: Optional[Path] = None,
    db_path: Optional[Path]     = None,
) -> dict:
    """
    Read disk artifacts for episode_id and upsert into DB.

    Returns a summary dict:
        {ok: bool, episode_id: str, status: str, action: str, error: str|None}
    """
    assets = Path(assets_root) if assets_root else _DEFAULT_ASSETS
    db     = Path(db_path)     if db_path     else _DEFAULT_DB

    ep_dir = assets / "episodes" / episode_id

    if not ep_dir.is_dir():
        return {"ok": False, "episode_id": episode_id,
                "error": f"episode directory not found: {ep_dir}",
                "action": "skipped", "status": None}

    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db)

    # ── Read packages ────────────────────────────────────────────────────────
    qa_pkg   = _load(ep_dir, "qa_approval_report", episode_id)
    bga_pkgs = []
    for pass_num in (1, 2):
        pkg = _load(ep_dir, f"bga_compliance_report_p{pass_num}", episode_id)
        if pkg:
            bga_pkgs.append(pkg)

    # Detect disk artifact presence
    has_render    = (ep_dir / f"main_{episode_id}.mp4").exists()
    has_thumbnail = any(
        (ep_dir / "thumbnails" / f"main.{ext}").exists()
        for ext in ("jpg", "jpeg", "png", "webp")
    )
    has_narration = (ep_dir / "narration").is_dir() and any(
        True for _ in (ep_dir / "narration").iterdir()
    ) if (ep_dir / "narration").is_dir() else False

    artifacts = {
        "has_render":    has_render,
        "has_thumbnail": has_thumbnail,
        "has_narration": has_narration,
    }

    status     = infer_episode_status(qa_pkg, bga_pkgs, artifacts)
    title      = episode_title_from_disk(ep_dir, episode_id)
    created_at = extract_created_at(ep_dir, episode_id)
    cost_usd   = extract_total_cost(ep_dir, episode_id)
    dur_ms     = extract_render_duration_ms(ep_dir, episode_id)
    scene_cnt  = extract_scene_count(ep_dir, episode_id)

    # ── Synthetic run_id ─────────────────────────────────────────────────────
    # If a real run was never recorded, create a deterministic synthetic id
    # so foreign keys don't break.
    syn_run_id = f"disk_run_{episode_id}"

    conn = get_connection(db)
    try:
        conn.execute("BEGIN")

        # ── Upsert run row ───────────────────────────────────────────────────
        existing_run = conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?", [syn_run_id]
        ).fetchone()

        if not existing_run:
            conn.execute(
                """
                INSERT INTO runs (run_id, episode_id, status, started_at, completed_at)
                VALUES (?, ?, 'complete', ?, ?)
                """,
                [syn_run_id, episode_id, created_at, created_at],
            )

        # ── Upsert episode row ───────────────────────────────────────────────
        existing_ep = conn.execute(
            "SELECT episode_id FROM episodes WHERE episode_id = ?", [episode_id]
        ).fetchone()

        if existing_ep:
            # Only update status and title if currently a synthetic row
            # (preserve real DB data — only patch if status is stale)
            conn.execute(
                """
                UPDATE episodes
                SET status = ?, title = COALESCE(title, ?)
                WHERE episode_id = ?
                  AND run_id = ?
                """,
                [status, title, episode_id, syn_run_id],
            )
            action = "updated"
        else:
            conn.execute(
                """
                INSERT INTO episodes (episode_id, title, topic, status, created_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [episode_id, title, title, status, created_at, syn_run_id],
            )
            action = "inserted"

        # ── Upsert synthetic stage rows ──────────────────────────────────────
        # Only insert stages if none exist for this run yet (idempotent).
        existing_stage_count = conn.execute(
            "SELECT COUNT(*) FROM stage_executions WHERE run_id = ?", [syn_run_id]
        ).fetchone()[0]

        if existing_stage_count == 0:
            for stage_name, _group, pkg_stem in SYNTHETIC_STAGES:
                pkg = _load(ep_dir, pkg_stem, episode_id)
                stage_exists = bool(pkg)
                if not stage_exists:
                    continue
                exec_id = f"disk_stage_{episode_id}_{stage_name}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO stage_executions
                        (stage_exec_id, run_id, episode_id, stage_name,
                         started_at, completed_at, duration_ms, status, skipped)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 0)
                    """,
                    [exec_id, syn_run_id, episode_id,
                     stage_name, created_at, created_at, dur_ms],
                )

        # ── Upsert cost row (single aggregate) ───────────────────────────────
        if cost_usd is not None:
            cost_id = f"disk_cost_{episode_id}"
            existing_cost = conn.execute(
                "SELECT cost_id FROM costs WHERE cost_id = ?", [cost_id]
            ).fetchone()
            if not existing_cost:
                conn.execute(
                    """
                    INSERT INTO costs
                        (cost_id, run_id, episode_id, provider, call_type,
                         cost_usd, called_at)
                    VALUES (?, ?, ?, 'disk_sync', 'estimated', ?, ?)
                    """,
                    [cost_id, syn_run_id, episode_id, cost_usd, created_at],
                )

        # ── Upsert asset rows ────────────────────────────────────────────────
        if has_render:
            asset_id = f"disk_asset_{episode_id}_render"
            conn.execute(
                """
                INSERT OR IGNORE INTO assets
                    (asset_id, run_id, episode_id, asset_type, provider,
                     file_path, created_at)
                VALUES (?, ?, ?, 'render', 'disk_sync', ?, ?)
                """,
                [asset_id, syn_run_id, episode_id,
                 f"episodes/{episode_id}/main_{episode_id}.mp4", created_at],
            )
        if has_thumbnail:
            asset_id = f"disk_asset_{episode_id}_thumbnail"
            for ext in ("jpg", "jpeg", "png", "webp"):
                fp = ep_dir / "thumbnails" / f"main.{ext}"
                if fp.exists():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO assets
                            (asset_id, run_id, episode_id, asset_type, provider,
                             file_path, created_at)
                        VALUES (?, ?, ?, 'thumbnail', 'disk_sync', ?, ?)
                        """,
                        [asset_id, syn_run_id, episode_id,
                         f"episodes/{episode_id}/thumbnails/main.{ext}", created_at],
                    )
                    break

        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        return {"ok": False, "episode_id": episode_id,
                "error": str(exc), "action": "error", "status": None}

    conn.close()
    return {
        "ok":         True,
        "episode_id": episode_id,
        "status":     status,
        "action":     action,
        "error":      None,
        "title":      title,
        "cost_usd":   cost_usd,
        "dur_ms":     dur_ms,
        "scene_count": scene_cnt,
        "has_render": has_render,
        "has_thumbnail": has_thumbnail,
        "bga_passes": len(bga_pkgs),
        "qa_approved": bool(qa_pkg.get("approved")),
    }


def sync_all_disk_episodes_to_db(
    assets_root: Optional[Path] = None,
    db_path:     Optional[Path] = None,
) -> list[dict]:
    """
    Scan all episode directories and sync each one.

    Skips directories that don't look like episode IDs (no ep_ prefix)
    or that have no meaningful JSON artifacts.

    Returns a list of sync result dicts.
    """
    assets = Path(assets_root) if assets_root else _DEFAULT_ASSETS
    episodes_dir = assets / "episodes"

    if not episodes_dir.is_dir():
        return []

    results = []
    for ep_dir in sorted(episodes_dir.iterdir()):
        if not ep_dir.is_dir():
            continue
        ep_id = ep_dir.name
        # Only sync directories that look like episode IDs
        if not (ep_id.startswith("ep_") or ep_id.startswith("episode_")):
            continue
        # Skip if no JSON artifacts present
        if not any(ep_dir.glob("*.json")):
            continue
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db_path)
        results.append(result)

    return results
