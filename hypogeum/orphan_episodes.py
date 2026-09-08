"""
hypogeum/orphan_episodes.py — Orphaned episode + topic reconciliation

Sibling to hypogeum/orphan_runs.py. That module reconciles the `runs`,
`run_queue` and `stage_executions` tables when a worker dies mid-pipeline
(a `kill -9`, a launchd bootout, a crash). It does NOT touch two rows that
the same crash also strands:

  1. the `episodes` row, left at status='in_progress' forever — it keeps
     showing as live work and its channel-weekly budget hold is never
     reaped (the weekly_reservation_reaper's terminal set is
     failed/error/cancelled/superseded, never in_progress).

  2. the `episode_topics` row, left at status='claimed' pointing at that
     dead episode_id — claim_next_topic()/claim_next_episode_topic() only
     ever move a 'claimed' row, so the topic is never re-served and the
     content silently drops out of the queue.

queue.process_next() DOES supersede the episode + release the topic, but
only from its own in-line permanent-skip branches (EP/QA/Compliance/
finance reject, failure ceiling, retry budget) — a process killed before
reaching them never runs that cleanup. This was done by hand in commit
3bf6413 for five stranded episodes; this module does it automatically at
worker startup and on the periodic self-healing sweep.

Ownership safety mirrors orphan_runs.py: an episode is only reconciled
when its run is in a terminal-dead state AND the run has no live lease,
so a legitimate out-of-band run_pipeline() still executing is never
clobbered.

Public API:
    reconcile_orphaned_episodes(db_path) -> dict
        {"superseded_episode_ids": [...], "released_topic_ids": [...]}
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Run states that mean "this run is over and it did not succeed". A run
# orphaned by orphan_runs.py is marked 'failed'; an operator cancel is
# 'cancelled'; 'error'/'blocked' are legacy/terminal too. 'completed' and
# the legacy 'complete' are deliberately excluded — a done run's episode
# is finalised by the pipeline itself, not here.
_TERMINAL_DEAD_RUN_STATES = ("failed", "error", "cancelled", "blocked")

# Episode states this module is allowed to move to 'superseded'. Never a
# 'complete' or already-'superseded' episode.
_RECONCILABLE_EPISODE_STATES = ("in_progress", "queued")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_orphaned_episodes(db_path: Optional[Path] = None) -> dict:
    """
    Supersede episodes stranded 'in_progress' by a crashed worker and
    release the 'claimed' episode_topics rows that point at them.

    An episode is reconciled when EITHER:
      - its run_id is set and that run is in a terminal-dead state
        (_TERMINAL_DEAD_RUN_STATES) with no live lease, OR
      - its run_id is NULL / points at no runs row at all (the worker
        died before register_run()).

    A topic is released (status -> 'queued', episode_id -> NULL,
    claimed_at -> NULL) when it is 'claimed', its episode_id is set, and
    that episode is now 'superseded' (by this pass or an earlier one) or
    has no `episodes` row at all.

    Best-effort and idempotent: a clean database is a no-op. Never raises.

    Returns {"superseded_episode_ids": [...], "released_topic_ids": [...]}.
    """
    result: dict = {"superseded_episode_ids": [], "released_topic_ids": []}

    from hypogeum.db import get_connection, initialize_schema

    try:
        initialize_schema(db_path)
    except Exception:  # noqa: BLE001
        return result

    now = _now_iso()

    try:
        from hypogeum.run_registry import run_lease_is_live
    except Exception:  # noqa: BLE001
        run_lease_is_live = None  # type: ignore

    try:
        conn = get_connection(db_path)
    except Exception:  # noqa: BLE001
        return result

    try:
        placeholders = ",".join("?" for _ in _RECONCILABLE_EPISODE_STATES)
        rows = conn.execute(
            f"""
            SELECT e.episode_id, e.run_id, r.status AS run_status
            FROM episodes e
            LEFT JOIN runs r ON r.run_id = e.run_id
            WHERE e.status IN ({placeholders})
            """,
            tuple(_RECONCILABLE_EPISODE_STATES),
        ).fetchall()

        to_supersede: list[str] = []
        for row in rows:
            episode_id = row["episode_id"]
            run_id = row["run_id"]
            run_status = row["run_status"]

            if run_id:
                # A run row exists: only reconcile if it is terminal-dead.
                if run_status not in _TERMINAL_DEAD_RUN_STATES:
                    continue
                # And only if nothing legitimately owns it right now.
                if run_lease_is_live is not None and run_lease_is_live(
                    run_id, at=now, db_path=db_path
                ):
                    continue
            # else: run_id is NULL or dangling — the worker died before the
            # run was ever registered. Nothing can be executing it.

            to_supersede.append(episode_id)

        for episode_id in to_supersede:
            conn.execute(
                "UPDATE episodes SET status = 'superseded' "
                "WHERE episode_id = ? AND status NOT IN ('complete', 'superseded')",
                (episode_id,),
            )

        # Release every 'claimed' topic that points at a concrete episode_id
        # which is now superseded or has no episodes row at all — both the
        # ones we just superseded and any stranded by an earlier pass or a
        # manual repair that missed the topic side. A 'claimed' topic whose
        # episode_id is still NULL is left alone: that is the brief window
        # inside queue.process_next() between the claim and episode
        # creation, and the singleton worker owns it.
        claimed = conn.execute(
            """
            SELECT t.topic_id
            FROM episode_topics t
            LEFT JOIN episodes e ON e.episode_id = t.episode_id
            WHERE t.status = 'claimed'
              AND t.episode_id IS NOT NULL
              AND (e.episode_id IS NULL OR e.status = 'superseded')
            """
        ).fetchall()
        released = [r["topic_id"] for r in claimed]
        for topic_id in released:
            conn.execute(
                """
                UPDATE episode_topics
                SET status = 'queued', episode_id = NULL,
                    claimed_at = NULL, updated_at = ?
                WHERE topic_id = ? AND status = 'claimed'
                """,
                (now, topic_id),
            )

        conn.commit()
        result["superseded_episode_ids"] = to_supersede
        result["released_topic_ids"] = released
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()

    return result
