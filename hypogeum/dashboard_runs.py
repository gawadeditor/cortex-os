"""
hypogeum/dashboard_runs.py — Phase C: Runs & Episode Operations Data Layer

Read-only queries for all operational surfaces:
    list_runs(db_path, limit, status)          -> list[dict]
    get_run_detail(run_id, db_path)            -> dict | None
    list_episodes(db_path, limit)              -> list[dict]
    list_episode_topics(channel_id, status, db_path, limit) -> list[dict]
    list_approvals_history(db_path, status, limit)          -> list[dict]
    list_signals_history(db_path, status, limit)            -> list[dict]
    get_preflight_status(db_path)              -> dict
    fmt_duration(ms)                           -> str   (pure, no I/O)

All functions are non-blocking: return [] / None / zeroed dict on any error.
No writes. No network calls.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def fmt_duration(ms) -> str:
    """Format milliseconds to a human-readable string."""
    if ms is None:
        return "—"
    ms = float(ms)
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


# ===========================================================================
# Runs
# ===========================================================================

def list_runs(
    db_path: Optional[Path] = None,
    limit:   int             = 100,
    status:  Optional[str]  = None,
    channel_id: Optional[str] = None,
) -> list[dict]:
    """
    Return runs ordered newest-first.
    Joins episode title/topic where available.
    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            clauses = []
            params: list = []
            if status:
                clauses.append("r.status = ?")
                params.append(status)
            if channel_id:
                clauses.append("r.channel_id = ?")
                params.append(channel_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = conn.execute(f"""
                SELECT r.run_id, r.episode_id, r.started_at, r.completed_at,
                       r.status, r.exit_code, r.error_message,
                       e.title  AS episode_title,
                       e.topic  AS episode_topic,
                       COALESCE(SUM(c.cost_usd), 0.0) AS total_cost_usd,
                       COUNT(DISTINCT se.stage_exec_id) AS stage_count
                FROM runs r
                LEFT JOIN episodes e      ON e.episode_id = r.episode_id
                LEFT JOIN costs c         ON c.run_id     = r.run_id
                LEFT JOIN stage_executions se ON se.run_id = r.run_id
                {where}
                GROUP BY r.run_id
                ORDER BY r.started_at DESC
                LIMIT ?
            """, params).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def get_run_detail(
    run_id:  str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Full detail for one run: run row, stages, gate verdicts, costs, transitions.
    Returns None if not found.
    Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            run_row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", [run_id]
            ).fetchone()
            if run_row is None:
                return None

            ep_row = conn.execute(
                "SELECT * FROM episodes WHERE episode_id=?",
                [run_row["episode_id"]],
            ).fetchone()

            stages = conn.execute("""
                SELECT stage_exec_id, stage_name, started_at, completed_at,
                       duration_ms, status, skipped, error_message,
                       model_slug, cost_usd
                FROM stage_executions
                WHERE run_id=?
                ORDER BY started_at ASC
            """, [run_id]).fetchall()

            gates = conn.execute("""
                SELECT verdict_id, gate_name, verdict, evaluated_at,
                       flag_count, blocking_count, warning_count, model_slug
                FROM gate_verdicts
                WHERE run_id=?
                ORDER BY evaluated_at ASC
            """, [run_id]).fetchall()

            costs = conn.execute("""
                SELECT cost_id, provider, model, call_type,
                       cost_usd, called_at
                FROM costs
                WHERE run_id=?
                ORDER BY called_at ASC
            """, [run_id]).fetchall()

            transitions = conn.execute("""
                SELECT transition_id, from_state, to_state,
                       triggered_by, trigger_source, reason,
                       transitioned_at
                FROM run_state_transitions
                WHERE run_id=?
                ORDER BY transitioned_at ASC
            """, [run_id]).fetchall()

            assets = conn.execute("""
                SELECT asset_id, asset_type, provider, file_path, created_at
                FROM assets
                WHERE run_id=?
                ORDER BY created_at ASC
            """, [run_id]).fetchall()

            # Publish calendar — keyed by episode_id (may be empty until Publishing Agent runs)
            episode_id = run_row["episode_id"] if run_row else None
            pub_cal_rows = []
            if episode_id:
                try:
                    pub_cal_rows = conn.execute("""
                        SELECT calendar_id, episode_id, short_id, platform,
                               target_publish_at, upload_status, publish_status,
                               platform_video_id, platform_supports_scheduled_publish,
                               publish_method, created_at, updated_at
                        FROM publish_calendar
                        WHERE episode_id = ?
                        ORDER BY platform, short_id
                    """, [episode_id]).fetchall()
                except Exception:
                    pub_cal_rows = []

        finally:
            conn.close()

        stages_list = [_row_to_dict(s) for s in stages]
        for s in stages_list:
            s["duration_fmt"] = fmt_duration(s.get("duration_ms"))

        total_cost = sum(c["cost_usd"] or 0.0 for c in costs)
        failed_stages = [s["stage_name"] for s in stages_list
                         if s.get("status") == "failed"]

        # Minimum Operational Dashboard Phase 2 — this run's own gate
        # outcome (why it was blocked/deferred, if it was), not just the
        # most-recent-overall one shown on /infrastructure/workers.
        # Read-only; passed through as the original WorkerAttempt object.
        worker_attempt = None
        if episode_id:
            try:
                from hypogeum.worker_integration import load_latest_worker_attempt
                worker_attempt = load_latest_worker_attempt(episode_id)
            except Exception:
                pass

        return {
            "run":              _row_to_dict(run_row),
            "episode":          _row_to_dict(ep_row) if ep_row else {},
            "stages":           stages_list,
            "gates":            [_row_to_dict(g) for g in gates],
            "costs":            [_row_to_dict(c) for c in costs],
            "transitions":      [_row_to_dict(t) for t in transitions],
            "assets":           [_row_to_dict(a) for a in assets],
            "publish_calendar": [_row_to_dict(c) for c in pub_cal_rows],
            "total_cost_usd":   round(total_cost, 6),
            "stage_count":      len(stages_list),
            "failed_stages":    failed_stages,
            "worker_attempt":   worker_attempt,
        }
    except Exception:
        return None


# ===========================================================================
# Episodes
# ===========================================================================

def list_episodes(
    db_path: Optional[Path] = None,
    limit:   int             = 100,
    channel_id: Optional[str] = None,
) -> list[dict]:
    """
    Episodes ordered newest-first, joined with most-recent run status and cost.
    When channel_id is given, only episodes whose run OR queued topic belongs
    to that channel are returned. Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            clause = ""
            params: list = []
            if channel_id:
                clause = ("WHERE r.channel_id = ? OR e.episode_id IN "
                          "(SELECT episode_id FROM episode_topics WHERE channel_id = ? "
                          " AND episode_id IS NOT NULL)")
                params = [channel_id, channel_id]
            params.append(limit)
            rows = conn.execute(f"""
                SELECT e.episode_id, e.title, e.topic, e.status, e.created_at,
                       e.run_id,
                       r.status  AS run_status,
                       r.started_at AS run_started_at,
                       r.channel_id AS channel_id,
                       COALESCE(SUM(c.cost_usd), 0.0) AS total_cost_usd
                FROM episodes e
                LEFT JOIN runs  r ON r.run_id = e.run_id
                LEFT JOIN costs c ON c.episode_id = e.episode_id
                {clause}
                GROUP BY e.episode_id
                ORDER BY e.created_at DESC
                LIMIT ?
            """, params).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def list_episode_topics(
    channel_id: Optional[str] = None,
    status:     Optional[str] = None,
    db_path:    Optional[Path] = None,
    limit:      int            = 200,
) -> list[dict]:
    """
    Topic queue rows. Filters by channel_id and/or status if provided.
    Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            clauses, params = [], []
            if channel_id:
                clauses.append("channel_id = ?")
                params.append(channel_id)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = conn.execute(f"""
                SELECT topic_id, channel_id, topic_text, status,
                       priority, created_at, claimed_at, consumed_at,
                       source, discovery_score, candidate_id,
                       target_publish_at, production_deadline_at,
                       episode_id
                FROM episode_topics
                {where}
                ORDER BY priority ASC, created_at ASC
                LIMIT ?
            """, params).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# Approvals history
# ===========================================================================

def list_approvals_history(
    db_path: Optional[Path] = None,
    status:  Optional[str]  = None,
    limit:   int             = 200,
) -> list[dict]:
    """
    All approvals, newest-first, with run/episode context.
    Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            clauses, params = [], []
            if status:
                clauses.append("a.status = ?")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = conn.execute(f"""
                SELECT a.approval_id, a.run_id, a.episode_id, a.approval_type,
                       a.requested_by, a.status, a.requested_at,
                       a.decided_by, a.decided_at, a.notes,
                       e.title AS episode_title, e.topic AS episode_topic
                FROM approvals a
                LEFT JOIN episodes e ON e.episode_id = a.episode_id
                {where}
                ORDER BY a.requested_at DESC
                LIMIT ?
            """, params).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# Signal history
# ===========================================================================

def list_signals_history(
    db_path: Optional[Path] = None,
    status:  Optional[str]  = None,
    limit:   int             = 200,
) -> list[dict]:
    """
    All signal records, newest-first.
    Parses message from full_record JSON.
    Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            clauses, params = [], []
            if status:
                clauses.append("lifecycle_status = ?")
                params.append(status)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = conn.execute(f"""
                SELECT signal_id, signal_type, severity, source_system,
                       channel_id, blocking, lifecycle_status,
                       full_record, ingested_at,
                       resolved_at, resolved_by
                FROM signal_records
                {where}
                ORDER BY ingested_at DESC
                LIMIT ?
            """, params).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            d = _row_to_dict(r)
            try:
                full = _json.loads(d.pop("full_record") or "{}")
                d["message"] = full.get("message", "")
            except Exception:
                d.pop("full_record", None)
                d["message"] = ""
            result.append(d)
        return result
    except Exception:
        return []


# ===========================================================================
# Preflight
# ===========================================================================

def get_preflight_status(db_path: Optional[Path] = None) -> dict:
    """
    Run the acceptance audit and return a summary suitable for the UI.
    Non-blocking.
    """
    try:
        from hypogeum.acceptance_audit import run_acceptance_audit
        report = run_acceptance_audit(db_path=db_path)
        checks = []
        for c in report.passed_checks + report.failed_checks:
            checks.append({
                "name":    c.name,
                "tier":    c.tier,
                "passed":  c.passed,
                "message": c.detail,
                "details": c.issues or [],
            })
        return {
            "ready":         report.ready,
            "checked_at":    report.checked_at,
            "blocker_count": len(report.blockers),
            "warning_count": len(report.warnings),
            "checks":        checks,
            "blockers":      [c.name for c in report.blockers],
            "warnings":      [c.name for c in report.warnings],
        }
    except Exception as exc:
        return {
            "ready":         False,
            "checked_at":    None,
            "blocker_count": 1,
            "warning_count": 0,
            "checks":        [],
            "blockers":      [f"audit error: {exc}"],
            "warnings":      [],
        }
