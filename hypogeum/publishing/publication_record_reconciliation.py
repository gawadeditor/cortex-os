"""
hypogeum/publishing/publication_record_reconciliation.py — Channel-scoped,
idempotent reconciliation of stale historical upload-attempt records.

Answers the operator-confirmed correction that a prior direct-adapter
upload attempt (recorded in the legacy `publishing_outcomes`/
`publish_calendar` tables, or a sealed `publishing_record_*.json`
artifact) no longer represents current platform state — e.g. the operator
deleted the uploads from the platform directly. Once confirmed, those
records must never be treated as proof of a current live publish by any
other part of this system (hypogeum.buffer's already_published check,
hypogeum.publishing.readiness, a future operator reviewing the episode) —
but they also must never be silently erased; they remain real historical
evidence that an upload attempt happened.

Mirrors hypogeum.topics.reconcile_stale_topics_for_episode()'s exact
contract shape and channel-isolation mechanism (mandatory channel_id
argument, no default, verified against the ONE real channel_id
episode_topics attributes episode_id to — the only real channel
attribution mechanism this schema supports; publishing_outcomes/
publish_calendar have no channel_id column of their own).

Never deletes a row. Only moves `publishing_status`/`publish_status` to
the existing, already-valid enum value 'skipped' (no CHECK constraint or
schema change required) — leaving the row itself, its platform_video_id,
and its timestamps fully intact as historical evidence. A row already
recorded as 'success' (publishing_outcomes) or 'live' (publish_calendar)
is NEVER touched by this function — reconciling a CONFIRMED live publish
into "stale" is a materially different, higher-stakes decision this
function does not make; only unconfirmed ('pending'/'unknown'/'scheduled')
attempts are eligible.

Public API:
    StaleRecordRow, PublicationRecordReconciliationResult
    reconcile_stale_publishing_records_for_episode(episode_id, channel_id,
        db_path=None, dry_run=True) -> PublicationRecordReconciliationResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_RECONCILIATION_MARKER = "operator_confirmed_stale_reconciliation"

_OUTCOME_ELIGIBLE_STATUSES = ("pending", "unknown")
_CALENDAR_ELIGIBLE_STATUSES = ("pending", "scheduled")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StaleRecordRow:
    """One publishing_outcomes or publish_calendar row a reconciliation
    call found or acted on."""
    table: str  # 'publishing_outcomes' | 'publish_calendar'
    row_id: str  # outcome_id or calendar_id
    platform: str
    platform_video_id: Optional[str]
    previous_status: str


@dataclass
class PublicationRecordReconciliationResult:
    """
    Auditable outcome of one reconcile_stale_publishing_records_for_episode()
    call. Same eligible/dry_run/applied_count semantics as
    hypogeum.topics.TopicReconciliationResult.
    """
    episode_id: str
    channel_id: str
    dry_run: bool
    eligible: bool
    reason: str
    rows: List[StaleRecordRow] = field(default_factory=list)
    applied_count: int = 0


def reconcile_stale_publishing_records_for_episode(
    episode_id: str,
    channel_id: str,
    db_path: Optional[Path] = None,
    dry_run: bool = True,
) -> PublicationRecordReconciliationResult:
    """
    Idempotent, scoped-to-one-(channel_id, episode_id). channel_id is
    required — no default, no global fallback. Verified against the ONE
    channel episode_topics actually attributes episode_id to; a missing or
    mismatched attribution reconciles nothing (eligible=False), exactly
    like hypogeum.topics.reconcile_stale_topics_for_episode().

    dry_run=True (default): read-only preview — returns exactly the rows
    that WOULD be reconciled, without writing anything.
    dry_run=False: moves every eligible row's status to 'skipped' with a
    reconciliation marker. Idempotent — a second call finds zero eligible
    rows left and is a safe no-op.
    """
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not channel_id:
                conn.rollback()
                return PublicationRecordReconciliationResult(
                    episode_id=episode_id, channel_id=channel_id, dry_run=dry_run, eligible=False,
                    reason="channel_id is required and was not supplied — channel scope cannot be proven",
                )

            attributed_rows = conn.execute(
                "SELECT DISTINCT channel_id FROM episode_topics WHERE episode_id = ?", (episode_id,),
            ).fetchall()
            attributed_channel_ids = {r["channel_id"] for r in attributed_rows}
            if attributed_channel_ids and attributed_channel_ids != {channel_id}:
                conn.rollback()
                return PublicationRecordReconciliationResult(
                    episode_id=episode_id, channel_id=channel_id, dry_run=dry_run, eligible=False,
                    reason=(
                        f"episode_id {episode_id!r} is attributed to channel(s) "
                        f"{sorted(attributed_channel_ids)}, not the asserted channel_id={channel_id!r} — "
                        f"refusing to reconcile under the wrong channel"
                    ),
                )

            outcome_candidates = conn.execute(
                f"""
                SELECT outcome_id, platform, platform_video_id, publishing_status
                FROM publishing_outcomes
                WHERE episode_id = ? AND publishing_status IN ({",".join("?" * len(_OUTCOME_ELIGIBLE_STATUSES))})
                ORDER BY recorded_at ASC
                """,
                (episode_id, *_OUTCOME_ELIGIBLE_STATUSES),
            ).fetchall()
            calendar_candidates = conn.execute(
                f"""
                SELECT calendar_id, platform, platform_video_id, publish_status
                FROM publish_calendar
                WHERE episode_id = ? AND publish_status IN ({",".join("?" * len(_CALENDAR_ELIGIBLE_STATUSES))})
                ORDER BY created_at ASC
                """,
                (episode_id, *_CALENDAR_ELIGIBLE_STATUSES),
            ).fetchall()

            rows = [
                StaleRecordRow(
                    table="publishing_outcomes", row_id=r["outcome_id"], platform=r["platform"],
                    platform_video_id=r["platform_video_id"], previous_status=r["publishing_status"],
                )
                for r in outcome_candidates
            ] + [
                StaleRecordRow(
                    table="publish_calendar", row_id=r["calendar_id"], platform=r["platform"],
                    platform_video_id=r["platform_video_id"], previous_status=r["publish_status"],
                )
                for r in calendar_candidates
            ]

            if not rows:
                conn.rollback()
                return PublicationRecordReconciliationResult(
                    episode_id=episode_id, channel_id=channel_id, dry_run=dry_run, eligible=True,
                    reason="no unconfirmed publishing_outcomes/publish_calendar rows remain for this episode — nothing to reconcile",
                )

            if dry_run:
                conn.rollback()
                return PublicationRecordReconciliationResult(
                    episode_id=episode_id, channel_id=channel_id, dry_run=True, eligible=True,
                    reason=f"preview only — {len(rows)} unconfirmed record(s) would be reconciled; no mutation performed",
                    rows=rows,
                )

            now = _now_iso()
            applied = 0
            for row in rows:
                if row.table == "publishing_outcomes":
                    cursor = conn.execute(
                        f"""
                        UPDATE publishing_outcomes
                        SET publishing_status = 'skipped',
                            failure_reason = ? || COALESCE(failure_reason, '')
                        WHERE outcome_id = ? AND publishing_status IN ({",".join("?" * len(_OUTCOME_ELIGIBLE_STATUSES))})
                        """,
                        (f"{_RECONCILIATION_MARKER}_{now}: ", row.row_id, *_OUTCOME_ELIGIBLE_STATUSES),
                    )
                else:
                    cursor = conn.execute(
                        f"""
                        UPDATE publish_calendar
                        SET publish_status = 'skipped', updated_at = ?
                        WHERE calendar_id = ? AND publish_status IN ({",".join("?" * len(_CALENDAR_ELIGIBLE_STATUSES))})
                        """,
                        (now, row.row_id, *_CALENDAR_ELIGIBLE_STATUSES),
                    )
                applied += cursor.rowcount
            conn.commit()
            return PublicationRecordReconciliationResult(
                episode_id=episode_id, channel_id=channel_id, dry_run=False, eligible=True,
                reason=f"reconciled {applied} stale historical upload-attempt record(s)",
                rows=rows, applied_count=applied,
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
