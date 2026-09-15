"""
hypogeum/dashboard_live.py — Phase D: Live Operations Data Layer

All reads are non-blocking and safe to call from a Flask SSE generator
or a polling JSON endpoint. No writes. No external network calls.

Public API:
    get_active_runs(db_path)              -> list[dict]
    get_run_live(run_id, db_path)         -> dict | None
    get_queue_snapshot(db_path)           -> dict
    get_live_metrics(db_path)             -> dict
    get_recent_failures(db_path, limit)   -> dict  {runs, stages}
    tail_log(episode_id, n, log_path)     -> list[dict]
    get_factory_pulse(db_path, log_path)  -> dict   what the factory is doing right now
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB      = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_LOG     = _PROJECT_ROOT / ".hypogeum-assets" / "pipeline.log"
_LOG_TAIL_BYTES  = 512 * 1024   # read at most 512 KB from end of log


def _db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_s(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    try:
        ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - ts).total_seconds(), 1)
    except Exception:
        return None


def _row(r) -> dict:
    return dict(r) if r else {}


# ===========================================================================
# Active runs
# ===========================================================================

def get_active_runs(db_path: Optional[Path] = None) -> list[dict]:
    """
    All runs currently executing (status='running') with their active stage.
    Non-blocking: returns [] on error.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            rows = conn.execute("""
                SELECT r.run_id, r.episode_id, r.started_at, r.status,
                       e.title  AS episode_title,
                       e.topic  AS episode_topic,
                       COALESCE(SUM(c.cost_usd), 0.0)          AS cost_so_far,
                       COALESCE(SUM(c.tokens_or_credits), 0.0) AS tokens_so_far
                FROM runs r
                LEFT JOIN episodes e ON e.episode_id = r.episode_id
                LEFT JOIN costs    c ON c.run_id     = r.run_id
                WHERE r.status = 'running'
                GROUP BY r.run_id
                ORDER BY r.started_at DESC
            """).fetchall()

            result = []
            for row in rows:
                d = _row(row)
                d["elapsed_s"] = _elapsed_s(d.get("started_at"))

                # Find currently executing stage for this run
                stage_row = conn.execute("""
                    SELECT stage_name, started_at
                    FROM stage_executions
                    WHERE run_id=? AND status='running'
                    ORDER BY started_at DESC
                    LIMIT 1
                """, [d["run_id"]]).fetchone()
                d["active_stage"]    = stage_row["stage_name"] if stage_row else None
                d["stage_started_at"] = stage_row["started_at"] if stage_row else None

                # Count completed stages
                done = conn.execute(
                    "SELECT COUNT(*) AS n FROM stage_executions WHERE run_id=? AND status='completed'",
                    [d["run_id"]],
                ).fetchone()
                d["stages_done"] = done["n"] if done else 0

                # costs.cost_usd (per run) omits all MUAPI spend — show the
                # full episode bill-to-date instead.
                try:
                    from hypogeum.model_effective_pricing import episode_billed_spend
                    _b = episode_billed_spend(
                        db_path=_db(db_path),
                        episode_ids=[d["episode_id"]] if d.get("episode_id") else [],
                    )
                    if d.get("episode_id") in _b:
                        d["cost_so_far"] = _b[d["episode_id"]]
                except Exception:
                    pass

                result.append(d)
        finally:
            conn.close()
        return result
    except Exception:
        return []


# ===========================================================================
# Single-run live snapshot
# ===========================================================================

def get_run_live(
    run_id:  str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Live snapshot for one run: current stage, completed stages, costs, tokens.
    Returns None if run not found.
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
                "SELECT title, topic FROM episodes WHERE episode_id=?",
                [run_row["episode_id"]],
            ).fetchone()

            stages = conn.execute("""
                SELECT stage_name, started_at, completed_at, status,
                       duration_ms, skipped, error_message, model_slug, cost_usd
                FROM stage_executions
                WHERE run_id=?
                ORDER BY started_at ASC
            """, [run_id]).fetchall()

            cost_row = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0.0)          AS total_cost,
                       COALESCE(SUM(tokens_or_credits), 0.0) AS total_tokens
                FROM costs WHERE run_id=?
            """, [run_id]).fetchone()

            latest_gate = conn.execute("""
                SELECT gate_name, verdict, evaluated_at
                FROM gate_verdicts
                WHERE run_id=?
                ORDER BY evaluated_at DESC
                LIMIT 1
            """, [run_id]).fetchone()

        finally:
            conn.close()

        stages_list = [_row(s) for s in stages]
        active = next((s for s in stages_list if s["status"] == "running"), None)
        done   = [s for s in stages_list if s["status"] in ("completed", "skipped")]
        failed = [s for s in stages_list if s["status"] == "failed"]

        return {
            "run":           _row(run_row),
            "episode_title": ep_row["title"] if ep_row else None,
            "episode_topic": ep_row["topic"] if ep_row else None,
            "stages":        stages_list,
            "stage_active":  active,
            "stages_done":   done,
            "stages_failed": failed,
            "cost_so_far":   round(cost_row["total_cost"]   or 0.0, 6) if cost_row else 0.0,
            "tokens_so_far": round(cost_row["total_tokens"] or 0.0, 0) if cost_row else 0,
            "elapsed_s":     _elapsed_s(run_row["started_at"] if run_row else None),
            "latest_gate":   _row(latest_gate) if latest_gate else None,
            "checked_at":    _now_iso(),
        }
    except Exception:
        return None


# ===========================================================================
# Queue snapshot
# ===========================================================================

def get_queue_snapshot(db_path: Optional[Path] = None) -> dict:
    """
    run_queue counts by status and the current claimed items.
    Non-blocking: returns zeroed snapshot on error.
    """
    _empty = {"queued": 0, "claimed": 0, "completed": 0, "failed": 0,
              "cancelled": 0, "total": 0, "pending": [], "topic_backlog": 0}
    try:
        conn = get_connection(_db(db_path))
        try:
            count_rows = conn.execute("""
                SELECT status, COUNT(*) AS n FROM run_queue GROUP BY status
            """).fetchall()
            items = conn.execute("""
                SELECT queue_id, channel_id, episode_id, status, priority,
                       worker_id, queued_at, claimed_at
                FROM run_queue
                WHERE status IN ('queued','claimed')
                ORDER BY priority ASC, queued_at ASC
                LIMIT 50
            """).fetchall()
            # The real "up next" number an operator cares about: the topic
            # backlog, not run_queue rows (which are only briefly non-empty).
            backlog = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE status = 'queued'"
            ).fetchone()[0]
        finally:
            conn.close()

        counts = {s: 0 for s in ("queued", "claimed", "completed", "failed", "cancelled")}
        for r in count_rows:
            counts[r["status"]] = r["n"]
        counts["total"]   = sum(counts.values())
        counts["pending"] = [_row(r) for r in items]
        counts["topic_backlog"] = backlog
        return counts
    except Exception:
        return _empty


# ===========================================================================
# Factory-wide live metrics
# ===========================================================================

def get_live_metrics(db_path: Optional[Path] = None) -> dict:
    """
    Factory-wide KPIs suitable for a live header bar.
    Non-blocking: returns zeroed metrics on error.
    """
    _empty = {
        "active_runs": 0, "queue_depth": 0,
        "cost_today_usd": 0.0, "tokens_today": 0,
        "failed_24h": 0, "completed_24h": 0,
        "checked_at": _now_iso(),
    }
    try:
        conn = get_connection(_db(db_path))
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            active = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status='running'"
            ).fetchone()["n"]

            # "Waiting" = the topic backlog an operator sees, not run_queue
            # rows (those are only briefly non-empty during a hand-off).
            queue_depth = conn.execute(
                "SELECT COUNT(*) AS n FROM episode_topics WHERE status='queued'"
            ).fetchone()["n"]

            # costs.cost_usd holds no MUAPI (video/upscale) spend — ~70% of
            # the real daily bill. Pull the complete figure from the
            # grant-consumption ledger; keep the costs tokens column.
            cost_row = conn.execute("""
                SELECT COALESCE(SUM(tokens_or_credits),0) AS tokens
                FROM costs WHERE called_at >= ?
            """, [today]).fetchone()
            try:
                from hypogeum.model_effective_pricing import spend_since
                cost_today = spend_since(today, db_path=_db(db_path))
            except Exception:
                cost_today = 0.0

            failed_24h = conn.execute("""
                SELECT COUNT(*) AS n FROM runs
                WHERE status='failed' AND started_at >= ?
            """, [today]).fetchone()["n"]

            completed_24h = conn.execute("""
                SELECT COUNT(*) AS n FROM runs
                WHERE status IN ('complete','completed') AND started_at >= ?
            """, [today]).fetchone()["n"]

        finally:
            conn.close()

        return {
            "active_runs":    active,
            "queue_depth":    queue_depth,
            "cost_today_usd": round(cost_today or 0.0, 4),
            "tokens_today":   int(cost_row["tokens"]   or 0)      if cost_row else 0,
            "failed_24h":     failed_24h,
            "completed_24h":  completed_24h,
            "checked_at":     _now_iso(),
        }
    except Exception:
        return _empty


# ===========================================================================
# Recent failures
# ===========================================================================

def get_recent_failures(
    db_path: Optional[Path] = None,
    limit:   int             = 10,
) -> dict:
    """
    Most recent failed runs and failed stage executions.
    Returns {"runs": [...], "stages": [...]}.
    Non-blocking.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            # Only ACTIONABLE failures: skip a failed run when the episode was
            # later produced successfully (recovered), when the episode is
            # superseded, or when its topic was abandoned (cancelled/skipped).
            # Archaeology belongs in the run history, not on "what went wrong".
            failed_runs = conn.execute("""
                SELECT r.run_id, r.episode_id, r.started_at,
                       r.error_message, e.title, e.topic
                FROM runs r
                LEFT JOIN episodes e ON e.episode_id = r.episode_id
                WHERE r.status = 'failed'
                  AND e.episode_id IS NOT NULL           -- has a real episode row, not a pure orphan
                  AND COALESCE(e.status, '') != 'superseded'
                  AND NOT EXISTS (
                      SELECT 1 FROM runs r2
                      WHERE r2.episode_id = r.episode_id
                        AND r2.status IN ('complete', 'completed')
                        AND r2.started_at >= r.started_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM episode_topics t
                      WHERE t.episode_id = r.episode_id
                        AND t.status IN ('cancelled', 'skipped')
                  )
                ORDER BY r.started_at DESC
                LIMIT ?
            """, [limit]).fetchall()

            # Same actionable-only gate as failed_runs: a failed stage is
            # only worth showing if its episode still exists, isn't
            # superseded, was never recovered by a later successful run, and
            # its topic wasn't abandoned.
            failed_stages = conn.execute("""
                SELECT se.stage_name, se.run_id, se.episode_id,
                       se.started_at, se.error_message, se.duration_ms
                FROM stage_executions se
                JOIN episodes e ON e.episode_id = se.episode_id
                WHERE se.status = 'failed'
                  AND COALESCE(e.status, '') != 'superseded'
                  AND NOT EXISTS (
                      SELECT 1 FROM runs r2
                      WHERE r2.episode_id = se.episode_id
                        AND r2.status IN ('complete', 'completed')
                        AND r2.started_at >= se.started_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM episode_topics t
                      WHERE t.episode_id = se.episode_id
                        AND t.status IN ('cancelled', 'skipped')
                  )
                ORDER BY se.started_at DESC
                LIMIT ?
            """, [limit]).fetchall()
        finally:
            conn.close()

        return {
            "runs":   [_row(r) for r in failed_runs],
            "stages": [_row(s) for s in failed_stages],
        }
    except Exception:
        return {"runs": [], "stages": []}


# ===========================================================================
# Log tail
# ===========================================================================

def tail_log(
    episode_id: Optional[str] = None,
    n:          int            = 100,
    log_path:   Optional[Path] = None,
) -> list[dict]:
    """
    Return the last `n` JSONL log entries from pipeline.log,
    optionally filtered to one episode_id.
    Pure read — never raises. Returns [] if log file absent.
    """
    path = Path(log_path) if log_path else _DEFAULT_LOG
    if not path.exists():
        return []
    try:
        fsize = path.stat().st_size
        if fsize == 0:
            return []
        chunk = min(fsize, _LOG_TAIL_BYTES)
        with path.open("rb") as fh:
            fh.seek(-chunk, 2)
            raw = fh.read().decode("utf-8", errors="replace")

        entries: list[dict] = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except Exception:
                continue
            if episode_id and entry.get("episode_id") != episode_id:
                continue
            entries.append(entry)
        return entries[-n:]
    except Exception:
        return []


# ===========================================================================
# Factory pulse — "what is the factory doing right now"
# ===========================================================================

_WORKER_ALIVE_SECONDS = 180   # a heartbeat older than this = worker not running

# log agent/event -> short human phrase for the "current activity" line
_ACTIVITY_PHRASES = {
    "topic_ideator":     "discovering new topics",
    "source_verifier":   "fact-checking topic candidates",
    "discovery_director": "running a discovery cycle",
    "executive_producer": "scoring a topic",
    "research_director": "researching the episode",
    "narrative_architect": "shaping the story",
    "script_engine":     "writing the script",
    "visual_package_director": "planning & generating video",
    "scene_planner":     "planning scenes",
    "source_enhancement": "upscaling clips",
    "clip_repair_agent": "regenerating missing clips",
    "audio_agent":       "recording narration & music",
    "assembly_agent":    "assembling the final cut",
    "thumbnail_director": "designing the thumbnail",
    "publishing_agent":  "publishing",
    "zernio_channel_release_trigger": "checking episodes for release",
    "canonical_metadata_agent": "writing titles & descriptions",
    "archive_agent":     "archiving a finished episode",
}


def get_factory_pulse(
    db_path:  Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> dict:
    """One call the dashboard uses to show what's happening right now:
    worker liveness, the current activity, in-flight production runs,
    recent discovery cycles, and what's queued. Pure read, never raises."""
    now = datetime.now(timezone.utc)
    out: dict = {
        "checked_at": now.isoformat(),
        "worker": {"status": "unknown", "alive": False, "heartbeat_age_s": None, "paused": False},
        "activity": None,          # {phrase, agent, event, channel_id, episode_id, at, age_s}
        "active_runs": [],
        "discovery": [],           # recent cycles
        "queued": {"count": 0, "items": []},
        "in_production": {"count": 0, "items": []},
    }

    # ── worker heartbeat / control ──────────────────────────────────────
    try:
        conn = get_connection(_db(db_path))
        try:
            wc = {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM worker_control"
            ).fetchall()}
            hb = wc.get("worker_heartbeat_at")
            age = _elapsed_s(hb)
            # A worker running a pipeline does not heartbeat during that run
            # (single process, blocking) — a live run is itself proof of life,
            # so a stale heartbeat alone must not read as "worker down".
            _has_live_run = bool(conn.execute(
                "SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"
            ).fetchone())
            out["worker"] = {
                "status": wc.get("worker_status") or "unknown",
                "alive": bool(_has_live_run or (age is not None and age <= _WORKER_ALIVE_SECONDS)),
                "heartbeat_age_s": round(age) if age is not None else None,
                "paused": str(wc.get("queue_paused", "0")) == "1",
                "stop_requested": str(wc.get("stop_requested", "0")) == "1",
            }

            # ── recent discovery cycles (last 45 min) ───────────────────
            try:
                rows = conn.execute("""
                    SELECT cycle_id, channel_id, status, selected_canonical_subject,
                           created_at, completed_at
                    FROM discovery_cycles
                    ORDER BY created_at DESC LIMIT 6
                """).fetchall()
                for r in rows:
                    started = _elapsed_s(r["created_at"])
                    if started is not None and started > 2700:
                        continue
                    out["discovery"].append({
                        "cycle_id": r["cycle_id"],
                        "channel_id": r["channel_id"],
                        "status": r["status"],
                        "subject": (r["selected_canonical_subject"] or "").replace("_", " "),
                        "age_s": round(started) if started is not None else None,
                        "running": r["status"] not in ("complete", "failed", "error"),
                    })
            except Exception:
                pass

            # ── queued / in-production topics ───────────────────────────
            try:
                for state, key in (("queued", "queued"), ("claimed", "in_production")):
                    # count is a real COUNT(*), not len(rows) -- the item
                    # preview below is capped at 20 fetched, so a `queued`
                    # backlog past 20 used to silently under-report the
                    # count itself instead of just truncating the preview.
                    total = conn.execute(
                        "SELECT COUNT(*) FROM episode_topics WHERE status = ?", [state]
                    ).fetchone()[0]
                    rows = conn.execute("""
                        SELECT topic_id, channel_id, topic_text, discovery_score
                        FROM episode_topics WHERE status = ?
                        ORDER BY priority ASC, created_at ASC LIMIT 20
                    """, [state]).fetchall()
                    out[key] = {
                        "count": total,
                        "items": [{
                            "channel_id": r["channel_id"],
                            "topic": r["topic_text"],
                            "score": r["discovery_score"],
                        } for r in rows[:5]],
                    }
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        pass

    # ── in-flight production runs (reuse the existing query) ────────────
    try:
        out["active_runs"] = get_active_runs(db_path)
        if out["active_runs"] and not out["in_production"]["items"]:
            out["in_production"] = {
                "count": len(out["active_runs"]),
                "items": [{
                    "channel_id": None,
                    "topic": r.get("episode_title") or r.get("episode_topic") or r.get("episode_id"),
                    "score": None,
                } for r in out["active_runs"][:5]],
            }
    except Exception:
        pass

    # ── current activity, inferred from the last meaningful log line ────
    try:
        recent = tail_log(n=60, log_path=log_path)
        for entry in reversed(recent):
            agent = entry.get("agent") or ""
            phrase = _ACTIVITY_PHRASES.get(agent)
            if not phrase:
                continue
            at = entry.get("timestamp")
            age = _elapsed_s(at)
            if age is not None and age > 600:   # nothing in 10 min = quiet
                break
            out["activity"] = {
                "phrase": phrase,
                "agent": agent,
                "event": entry.get("event"),
                "channel_id": entry.get("channel_id"),
                "episode_id": entry.get("field_episode_id") or entry.get("episode_id"),
                "at": at,
                "age_s": round(age) if age is not None else None,
            }
            break
    except Exception:
        pass

    return out
