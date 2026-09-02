#!/usr/bin/env python3
"""
scripts/burnin_monitor.py — one-off operational monitor for the first
autonomous Burn-in. Not part of the application; not imported by
anything; safe to delete once the burn-in observation is complete.

Polls .hypogeum-assets/hypogeum.db every POLL_INTERVAL seconds, diffs
state against the previous poll, and appends a chronological, timestamped
entry to docs/operations/BURN_IN_LOG.md for every real transition:
scheduler fires, queue changes, topic selection, run start/finish, stage
entered/completed/failed (with duration), publishing outcomes, worker
status changes.

Also incrementally tails .hypogeum-assets/pipeline.log for warn/error
lines only, starting from the byte offset captured at monitor start
(START_LOG_OFFSET) — the existing ~29GB of prior history is never
re-scanned, only newly appended bytes are read on each poll.

Runs until killed. Never modifies application state — read-only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / ".hypogeum-assets" / "hypogeum.db"
PIPELINE_LOG = REPO / ".hypogeum-assets" / "pipeline.log"
BURNIN_LOG = REPO / "docs" / "operations" / "BURN_IN_LOG.md"

POLL_INTERVAL = 30          # seconds
HEARTBEAT_INTERVAL = 1800   # seconds — only while nothing else changed

# Captured once at import time: never read pipeline.log before this offset.
START_LOG_OFFSET = PIPELINE_LOG.stat().st_size if PIPELINE_LOG.exists() else 0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def append_log(lines: list) -> None:
    if not lines:
        return
    with open(BURNIN_LOG, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
            print(line, flush=True)


def snapshot() -> dict:
    conn = get_conn()
    try:
        runs = {
            r["run_id"]: dict(r) for r in conn.execute(
                "SELECT run_id, episode_id, status, started_at, completed_at "
                "FROM runs"
            ).fetchall()
        }
        stages = {
            r["stage_exec_id"]: dict(r) for r in conn.execute(
                "SELECT stage_exec_id, run_id, episode_id, stage_name, status, "
                "started_at, completed_at, duration_ms, error_message "
                "FROM stage_executions"
            ).fetchall()
        }
        queue = {
            r["queue_id"]: dict(r) for r in conn.execute(
                "SELECT queue_id, channel_id, episode_id, status, worker_id, "
                "source, retry_count FROM run_queue"
            ).fetchall()
        }
        topics_claimed = {
            r["topic_id"]: dict(r) for r in conn.execute(
                "SELECT topic_id, topic_text, status, episode_id "
                "FROM episode_topics WHERE status='claimed'"
            ).fetchall()
        }
        pub = {
            r["outcome_id"]: dict(r) for r in conn.execute(
                "SELECT outcome_id, episode_id, platform, deliverable_id, "
                "publishing_status, platform_video_id FROM publishing_outcomes"
            ).fetchall()
        }
        worker = {
            r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM worker_control"
            ).fetchall()
        }
        schedules = {
            r["schedule_id"]: dict(r) for r in conn.execute(
                "SELECT schedule_id, status, next_run_at, last_run_at "
                "FROM schedules"
            ).fetchall()
        }
        scheduled_runs = {
            r["scheduled_run_id"]: dict(r) for r in conn.execute(
                "SELECT scheduled_run_id, channel_id, episode_id, status, "
                "scheduled_for, triggered_at FROM scheduled_runs"
            ).fetchall()
        }
    finally:
        conn.close()
    return dict(
        runs=runs, stages=stages, queue=queue, topics_claimed=topics_claimed,
        pub=pub, worker=worker, schedules=schedules,
        scheduled_runs=scheduled_runs,
    )


def diff_and_log(prev: dict, curr: dict) -> list:
    lines = []
    ts = now()

    for sid, s in curr["schedules"].items():
        p = prev["schedules"].get(sid)
        if p and p["last_run_at"] != s["last_run_at"]:
            lines.append(
                f"- `{ts}` **SCHEDULER FIRED** — schedule `{sid}` "
                f"(status={s['status']}) last_run_at {p['last_run_at']} -> "
                f"{s['last_run_at']}; next_run_at now {s['next_run_at']}"
            )

    for srid, sr in curr["scheduled_runs"].items():
        p = prev["scheduled_runs"].get(srid)
        if p is None:
            lines.append(
                f"- `{ts}` scheduled_runs: new row `{srid}` "
                f"channel={sr['channel_id']} status={sr['status']} "
                f"scheduled_for={sr['scheduled_for']}"
            )
        elif p["status"] != sr["status"]:
            lines.append(
                f"- `{ts}` scheduled_runs `{srid}`: "
                f"{p['status']} -> {sr['status']}"
            )

    for qid, q in curr["queue"].items():
        p = prev["queue"].get(qid)
        if p is None:
            lines.append(
                f"- `{ts}` **QUEUE**: new item `{qid}` "
                f"channel={q['channel_id']} episode_id={q['episode_id']} "
                f"source={q['source']} status={q['status']}"
            )
        elif p["status"] != q["status"] or p["worker_id"] != q["worker_id"]:
            lines.append(
                f"- `{ts}` QUEUE `{qid}`: status {p['status']}->{q['status']}, "
                f"worker {p['worker_id']}->{q['worker_id']}, "
                f"retry_count={q.get('retry_count')}"
            )

    for tid, t in curr["topics_claimed"].items():
        if tid not in prev["topics_claimed"]:
            lines.append(
                f"- `{ts}` **TOPIC SELECTED**: \"{t['topic_text']}\" "
                f"(topic_id={tid}, episode_id={t['episode_id']})"
            )

    for rid, r in curr["runs"].items():
        p = prev["runs"].get(rid)
        if p is None:
            lines.append(
                f"- `{ts}` **RUN STARTED**: run_id=`{rid}` "
                f"episode_id=`{r['episode_id']}` started_at={r['started_at']}"
            )
        elif p["status"] != r["status"]:
            lines.append(
                f"- `{ts}` **RUN {r['status'].upper()}**: run_id=`{rid}` "
                f"episode_id=`{r['episode_id']}` (was {p['status']})"
            )

    for sid, s in curr["stages"].items():
        p = prev["stages"].get(sid)
        if p is None:
            lines.append(
                f"- `{ts}` STAGE ENTERED: `{s['stage_name']}` "
                f"run_id=`{s['run_id']}` episode_id=`{s['episode_id']}` "
                f"started_at={s['started_at']}"
            )
        elif p["status"] != s["status"]:
            dur = f", duration_ms={s['duration_ms']}" if s.get("duration_ms") else ""
            err = f", error={str(s['error_message'])[:200]}" if s.get("error_message") else ""
            lines.append(
                f"- `{ts}` STAGE {s['status'].upper()}: `{s['stage_name']}` "
                f"run_id=`{s['run_id']}`{dur}{err}"
            )

    for oid, o in curr["pub"].items():
        if oid not in prev["pub"]:
            lines.append(
                f"- `{ts}` **PUBLISHED**: platform={o['platform']} "
                f"deliverable={o['deliverable_id']} episode_id={o['episode_id']} "
                f"video_id={o['platform_video_id']} status={o['publishing_status']}"
            )

    if prev["worker"].get("worker_status") != curr["worker"].get("worker_status"):
        lines.append(
            f"- `{ts}` WORKER STATUS: "
            f"{prev['worker'].get('worker_status')} -> "
            f"{curr['worker'].get('worker_status')} "
            f"(active_queue_id={curr['worker'].get('active_queue_id')})"
        )

    return lines


def tail_warnings(last_offset: int) -> tuple:
    """Read only newly-appended bytes since last_offset. Never seeks
    before START_LOG_OFFSET. Returns (log_lines, new_offset)."""
    if not PIPELINE_LOG.exists():
        return [], last_offset
    size = PIPELINE_LOG.stat().st_size
    if size <= last_offset:
        return [], last_offset  # log rotated/truncated or unchanged
    lines = []
    with open(PIPELINE_LOG, "r", encoding="utf-8", errors="replace") as f:
        f.seek(last_offset)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("level") in ("warn", "warning", "error"):
                lines.append(
                    f"- `{now()}` LOG {rec.get('level', '').upper()}: "
                    f"episode_id={rec.get('episode_id')} "
                    f"agent={rec.get('agent')} event={rec.get('event')}"
                )
        new_offset = f.tell()
    return lines, new_offset


def main() -> None:
    prev = snapshot()
    last_heartbeat = time.monotonic()
    log_offset = START_LOG_OFFSET
    append_log([
        f"\n## Monitoring session started `{now()}` "
        f"(pipeline.log tail starts at byte offset {START_LOG_OFFSET})\n"
    ])
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            curr = snapshot()
            lines = diff_and_log(prev, curr)
            warn_lines, log_offset = tail_warnings(log_offset)
            lines.extend(warn_lines)
            if lines:
                append_log(lines)
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL:
                active_next = [
                    s["next_run_at"] for s in curr["schedules"].values()
                    if s["status"] == "active"
                ]
                nxt = min(active_next) if active_next else "n/a"
                append_log([
                    f"- `{now()}` heartbeat: worker="
                    f"{curr['worker'].get('worker_status')}, "
                    f"queue_depth={len(curr['queue'])}, "
                    f"next_schedule_fires={nxt}"
                ])
                last_heartbeat = time.monotonic()
            prev = curr
        except Exception as exc:
            append_log([f"- `{now()}` MONITOR ERROR (non-fatal, continuing): {exc}"])


if __name__ == "__main__":
    main()
