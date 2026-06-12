"""
hypogeum/post_run_intelligence.py — Post-run intelligence orchestrator

Owns all Phase 5 data flows that run after the main pipeline completes.
These flows are non-blocking: any exception is caught, logged, and written
to the intelligence report as an error — the pipeline result is never affected.

Data flows:
    run_kg_seed_flow()               — extract kg_seed, insert into DB, update world bible
    run_signal_flow_if_available()   — ingest analytics_snapshot into signal_record + DB
    run_post_run_intelligence()      — top-level entry point; calls all flows; writes report

Report:
    post_run_intelligence_report_{episode_id}.json
    Written to output_dir (defaults to .hypogeum-assets/intelligence/).

Architecture:
    Non-blocking: all sub-flows are wrapped in try/except.
    Caller (pipeline.py) invokes run_post_run_intelligence() once, after
    production_metadata is written, before the pipeline seals its result.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema, insert_kg_seed, insert_signal_record
from hypogeum.identifiers import METADATA_VERSION
from hypogeum.knowledge_graph import extract_kg_seed
from hypogeum.logger import get_logger
from hypogeum.signals import ingest_signal
from hypogeum.world_bible import update_world_bible


log = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_INTELLIGENCE_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "intelligence"
_DEFAULT_KNOWLEDGE_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "knowledge"
_DEFAULT_SIGNALS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "signals"
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_dir(output_dir: Optional[Path], default: Path) -> Path:
    return Path(output_dir) if output_dir is not None else default


# ---------------------------------------------------------------------------
# KG seed flow
# ---------------------------------------------------------------------------

def run_kg_seed_flow(
    episode_id: str,
    production_metadata: Optional[dict] = None,
    source_registry: Optional[list] = None,
    run_id: str = "",
    knowledge_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Extract kg_seed, persist to DB, update world bible.

    Returns a status dict with keys:
        ok (bool), kg_seed_id (str or None), error (str or None).

    Never raises.
    """
    try:
        kg_seed = extract_kg_seed(
            episode_id=episode_id,
            production_metadata=production_metadata,
            source_registry=source_registry,
            output_dir=knowledge_dir,
        )

        # Insert into DB
        _db = _resolve_dir(db_path, _DEFAULT_DB_PATH)
        initialize_schema(db_path=_db)
        conn = get_connection(db_path=_db)
        try:
            summary = kg_seed.get("extraction_summary", {})
            insert_kg_seed(
                conn=conn,
                kg_seed_id=kg_seed["kg_seed_id"],
                episode_id=episode_id,
                run_id=run_id or kg_seed.get("run_id", ""),
                extracted_at=kg_seed.get("extracted_at", _now_iso()),
                entity_count=summary.get("entity_count", 0),
                relationship_count=summary.get("relationship_count", 0),
                source_count=summary.get("source_count", 0),
                full_record=kg_seed,
                metadata_version=METADATA_VERSION,
            )
            conn.commit()
        finally:
            conn.close()

        # Update world bible
        update_world_bible(kg_seed=kg_seed, output_dir=knowledge_dir)

        log.info(
            "post_run_intelligence_kg_seed_ok",
            episode_id=episode_id,
            kg_seed_id=kg_seed["kg_seed_id"],
            entity_count=summary.get("entity_count", 0),
        )
        return {"ok": True, "kg_seed_id": kg_seed["kg_seed_id"], "error": None}

    except Exception as exc:  # noqa: BLE001
        log.error(
            "post_run_intelligence_kg_seed_error",
            episode_id=episode_id,
            error=str(exc),
        )
        return {"ok": False, "kg_seed_id": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Signal flow
# ---------------------------------------------------------------------------

def run_signal_flow_if_available(
    episode_id: str,
    analytics_snapshot: Optional[dict] = None,
    signals_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Ingest analytics_snapshot into signal_record and persist to DB.

    Skips gracefully if analytics_snapshot is None or empty.

    Returns a status dict with keys:
        ok (bool), signal_id (str or None), skipped (bool), error (str or None).

    Never raises.
    """
    if not analytics_snapshot:
        return {"ok": True, "signal_id": None, "skipped": True, "error": None}

    try:
        signal_record = ingest_signal(
            episode_id=episode_id,
            analytics_snapshot=analytics_snapshot,
            output_dir=signals_dir,
        )

        _db = _resolve_dir(db_path, _DEFAULT_DB_PATH)
        initialize_schema(db_path=_db)
        conn = get_connection(db_path=_db)
        try:
            insert_signal_record(
                conn=conn,
                signal_id=signal_record["signal_id"],
                episode_id=episode_id,
                run_id=signal_record.get("run_id", ""),
                ingested_at=signal_record.get("ingested_at", _now_iso()),
                youtube_video_id=signal_record.get("youtube_video_id", ""),
                window_days=signal_record.get("window_days", 0),
                data_quality=signal_record.get("data_quality", "no_data"),
                views=signal_record.get("metrics", {}).get("views"),
                watch_minutes=signal_record.get("metrics", {}).get("estimatedMinutesWatched"),
                full_record=signal_record,
                metadata_version=METADATA_VERSION,
            )
            conn.commit()
        finally:
            conn.close()

        log.info(
            "post_run_intelligence_signal_ok",
            episode_id=episode_id,
            signal_id=signal_record["signal_id"],
            data_quality=signal_record.get("data_quality"),
        )
        return {
            "ok": True,
            "signal_id": signal_record["signal_id"],
            "skipped": False,
            "error": None,
        }

    except Exception as exc:  # noqa: BLE001
        log.error(
            "post_run_intelligence_signal_error",
            episode_id=episode_id,
            error=str(exc),
        )
        return {"ok": False, "signal_id": None, "skipped": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_post_run_intelligence(
    episode_id: str,
    production_metadata: Optional[dict] = None,
    source_registry: Optional[list] = None,
    run_id: str = "",
    analytics_snapshot: Optional[dict] = None,
    output_dir: Optional[Path] = None,
    knowledge_dir: Optional[Path] = None,
    signals_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Run all Phase 5 post-run intelligence flows and write a durable report.

    Called once by pipeline.py after production_metadata is written.
    All sub-flows are non-blocking — failures are recorded in the report
    but never propagated to the caller.

    Args:
        episode_id:           Episode identifier.
        production_metadata:  production_metadata dict from write_production_metadata().
        source_registry:      List of source dicts from Research Director.
        run_id:               Pipeline run UUID4 string.
        analytics_snapshot:   analytics_snapshot dict (may be None for first-run).
        output_dir:           Where to write the intelligence report JSON.
                              Defaults to .hypogeum-assets/intelligence/.
        knowledge_dir:        Override for kg_seed + world bible output.
        signals_dir:          Override for signal_record output.
        db_path:              Override for SQLite DB path.

    Returns:
        Intelligence report dict. Also written to
        post_run_intelligence_report_{episode_id}.json in output_dir.
    """
    report_dir = _resolve_dir(output_dir, _DEFAULT_INTELLIGENCE_DIR)

    kg_result = run_kg_seed_flow(
        episode_id=episode_id,
        production_metadata=production_metadata,
        source_registry=source_registry,
        run_id=run_id,
        knowledge_dir=knowledge_dir,
        db_path=db_path,
    )

    signal_result = run_signal_flow_if_available(
        episode_id=episode_id,
        analytics_snapshot=analytics_snapshot,
        signals_dir=signals_dir,
        db_path=db_path,
    )

    all_ok = kg_result["ok"] and signal_result["ok"]

    report = {
        "schema_version": "v1",
        "metadata_version": METADATA_VERSION,
        "episode_id": episode_id,
        "run_id": run_id,
        "generated_at": _now_iso(),
        "all_flows_ok": all_ok,
        "kg_seed_flow": kg_result,
        "signal_flow": signal_result,
    }

    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"post_run_intelligence_report_{episode_id}.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(
            "post_run_intelligence_report_written",
            episode_id=episode_id,
            path=str(report_path),
            all_flows_ok=all_ok,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "post_run_intelligence_report_write_error",
            episode_id=episode_id,
            error=str(exc),
        )

    return report
