"""
hypogeum/topics.py — Topic Backlog / Episode Queue

Manages the ordered backlog of topics awaiting production. The Queue Consumer
claims the next topic for a channel before calling run_pipeline(). On successful
pipeline completion the topic is consumed.

Topic lifecycle:
    queued   → claimed   (claim_next_topic — atomic conditional UPDATE)
    claimed  → consumed  (consume_topic — called after successful pipeline run)
    claimed  → queued    (release_topic — pipeline failed; topic retry on next pass)
    claimed  → skipped   (skip_topic w/ reason="ep_rejected" — EP scored REJECT)
    queued   → skipped   (skip_topic — operator skips a specific topic)
    queued   → cancelled (cancel_topic — operator removes before claim)
    claimed  → cancelled (cancel_topic — operator force-cancels in-flight)

Atomic claim guarantee:
    claim_next_topic() uses the same conditional UPDATE pattern as the queue
    consumer: SELECT candidate then UPDATE WHERE status='queued'. Exactly one
    concurrent caller claims any given topic row.

Local-safe design:
  - No pipeline calls.
  - No provider calls.
  - No publishing.
  - No content generation.
  - Pure DB state transitions only.

Topic Intake:
    enqueue_topic() validates channel existence and prevents exact duplicate
    queued/claimed topics per channel (case-insensitive, whitespace-normalised).
    import_topics_from_csv() bulk-loads topics from a CSV/text file, reporting
    counts of added, skipped, and errored rows.

Public API:
    enqueue_topic(channel_id, topic_text, priority=0,
                  validate_channel=True, db_path=None)  -> str
    import_topics_from_csv(channel_id, csv_path,
                           priority=0, db_path=None)    -> ImportResult
    claim_next_topic(channel_id, db_path=None)          -> dict | None
    consume_topic(topic_id, db_path=None)               -> None
    release_topic(topic_id, db_path=None)               -> None
    skip_topic(topic_id, reason=None, db_path=None)     -> None
    cancel_topic(topic_id, db_path=None)                -> None
    get_topic(topic_id, db_path=None)                   -> dict | None
    list_topics(channel_id=None, status=None, db_path=None) -> list[dict]
    make_topic_resolver(db_path=None)                   -> Callable
    set_topic_target_publish_at(topic_id, target_publish_at,
                                 db_path=None)            -> bool
    set_topic_episode_id(topic_id, episode_id,
                          db_path=None)                  -> bool
    reconcile_stale_topics_for_episode(episode_id, db_path=None,
                                        dry_run=True)     -> TopicReconciliationResult

Exceptions:
    DuplicateTopicError — raised when a queued/claimed topic with the same
                          normalised text already exists for the channel.
    ChannelNotFoundError — raised when validate_channel=True and the channel
                           does not exist in channel_profiles.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from hypogeum.db import (
    claim_next_episode_topic,
    get_connection,
    get_episode_topic,
    initialize_schema,
    insert_episode_topic,
    list_episode_topics,
    update_episode_topic_status,
)

_RECONCILIATION_REASON = "terminal_state_reconciliation"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _resolve_db_path(db_path: Optional[Path]) -> Optional[Path]:
    return db_path  # pass-through; get_connection handles the default


def _normalise(text: str) -> str:
    """Lower-case, collapse whitespace — used for duplicate detection."""
    return re.sub(r"\s+", " ", text.strip()).lower()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DuplicateTopicError(ValueError):
    """Raised when an identical queued/claimed topic already exists for the channel."""

    def __init__(self, channel_id: str, topic_text: str) -> None:
        self.channel_id = channel_id
        self.topic_text = topic_text
        super().__init__(
            f"Channel '{channel_id}' already has a queued or claimed topic "
            f"matching '{topic_text}'. Cancel or consume the existing entry first."
        )


class ChannelNotFoundError(ValueError):
    """Raised by enqueue_topic when validate_channel=True and channel does not exist."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        super().__init__(
            f"Channel '{channel_id}' not found in channel_profiles. "
            f"Create the channel profile before enqueuing topics."
        )


# ---------------------------------------------------------------------------
# Import result
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    """Summary of a bulk topic import operation."""
    channel_id: str
    added: int = 0
    skipped_duplicates: int = 0
    skipped_empty: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.added + self.skipped_duplicates + self.skipped_empty + len(self.errors)

    def __str__(self) -> str:
        parts = [
            f"channel={self.channel_id}",
            f"added={self.added}",
            f"skipped_duplicates={self.skipped_duplicates}",
            f"skipped_empty={self.skipped_empty}",
            f"errors={len(self.errors)}",
        ]
        return "ImportResult(" + ", ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Internal duplicate check
# ---------------------------------------------------------------------------

def _has_active_duplicate(conn, channel_id: str, topic_text: str) -> bool:
    """
    Return True if a queued or claimed topic with the same normalised text
    already exists for channel_id.
    """
    norm = _normalise(topic_text)
    rows = conn.execute(
        """
        SELECT topic_text FROM episode_topics
        WHERE channel_id = ? AND status IN ('queued', 'claimed')
        """,
        (channel_id,),
    ).fetchall()
    return any(_normalise(r[0]) == norm for r in rows)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue_topic(
    channel_id: str,
    topic_text: str,
    priority: int = 0,
    validate_channel: bool = False,
    db_path: Optional[Path] = None,
    source: str = "manual",
    discovery_score: Optional[float] = None,
    candidate_id: Optional[str] = None,
    episode_id: Optional[str] = None,
) -> str:
    """
    Add a topic to the episode backlog for a channel.

    Args:
        channel_id:       Channel that will produce this topic.
        topic_text:       The topic idea string (passed to run_pipeline as topic_input).
        priority:         Claim ordering — lower integer = higher priority. Default 0.
        validate_channel: If True, verify the channel exists in DB before inserting.
                          Raises ChannelNotFoundError if not found. Default False —
                          the CLI and import helpers pass True explicitly.
        db_path:          Override DB path (for tests).
        episode_id:       Optional episode_id to preserve for reruns. When set, the
                          queue worker reuses this episode_id so pipeline checkpoints
                          on disk are picked up. Reruns bypass the duplicate check.

    Returns:
        New topic_id (UUID4 string).

    Raises:
        ValueError:          If topic_text or channel_id is empty.
        ChannelNotFoundError: If validate_channel=True and the channel does not exist.
        DuplicateTopicError: If an identical queued/claimed topic already exists
                             for this channel (case-insensitive, whitespace-normalised).
                             NOT raised when source='rerun' or 'auto_retry'
                             (both always proceed).
    """
    if not topic_text or not topic_text.strip():
        raise ValueError("topic_text must be a non-empty string.")
    if not channel_id or not channel_id.strip():
        raise ValueError("channel_id must be a non-empty string.")

    clean_text = topic_text.strip()

    if validate_channel:
        from hypogeum.channel_config import get_channel_profile
        if get_channel_profile(channel_id, db_path=db_path) is None:
            raise ChannelNotFoundError(channel_id)

    initialize_schema(db_path)
    topic_id = _new_id()
    now = _now_iso()

    conn = get_connection(db_path)
    try:
        # Reruns and auto-retries bypass dedup — same topic text is expected
        # and intentional (auto-retry recreates the exact topic the failed
        # attempt was about; see hypogeum.queue._maybe_schedule_retry()).
        if source not in ("rerun", "auto_retry") and _has_active_duplicate(
            conn, channel_id, clean_text,
        ):
            raise DuplicateTopicError(channel_id, clean_text)

        insert_episode_topic(
            conn,
            topic_id=topic_id,
            channel_id=channel_id,
            topic_text=clean_text,
            priority=priority,
            source=source,
            discovery_score=discovery_score,
            candidate_id=candidate_id,
            created_at=now,
            updated_at=now,
            episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()

    return topic_id


def import_topics_from_csv(
    channel_id: str,
    csv_path: Path,
    priority: int = 0,
    validate_channel: bool = False,
    db_path: Optional[Path] = None,
) -> ImportResult:
    """
    Bulk-import topics from a CSV or plain-text file into the episode backlog.

    File format:
        - One topic per line.
        - Lines that are empty or whitespace-only are skipped.
        - If the first non-empty line matches a known header name
          ('topic', 'topic_text', 'text', 'title'), it is treated as a header
          and skipped.
        - A single-column CSV is also accepted (first column is the topic text).
        - Lines with only a comma-separated header row are skipped.

    Each topic is validated and deduplicated the same way as enqueue_topic():
        - Empty text → skipped (counted as skipped_empty).
        - Duplicate queued/claimed for channel → skipped (skipped_duplicates).
        - ChannelNotFoundError on first topic if validate_channel=True.

    Args:
        channel_id:       Target channel.
        csv_path:         Path to the CSV or plain-text file.
        priority:         Priority assigned to every imported topic. Default 0.
        validate_channel: Verify channel exists before processing. Default True.
        db_path:          Override DB path (for tests).

    Returns:
        ImportResult with counts: added, skipped_duplicates, skipped_empty, errors.

    Raises:
        ChannelNotFoundError: If validate_channel=True and the channel does not exist.
        FileNotFoundError:    If csv_path does not exist.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    if validate_channel:
        from hypogeum.channel_config import get_channel_profile
        if get_channel_profile(channel_id, db_path=db_path) is None:
            raise ChannelNotFoundError(channel_id)

    result = ImportResult(channel_id=channel_id)

    raw = csv_path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))

    _HEADER_NAMES = {"topic", "topic_text", "text", "title"}
    first_data_row = True

    for row in reader:
        if not row:
            result.skipped_empty += 1
            continue

        # Take only the first column; strip surrounding whitespace
        cell = row[0].strip()

        if not cell:
            result.skipped_empty += 1
            continue

        # Skip header row if present
        if first_data_row:
            first_data_row = False
            if cell.lower() in _HEADER_NAMES:
                continue

        try:
            enqueue_topic(
                channel_id,
                cell,
                priority=priority,
                validate_channel=False,  # already validated above
                db_path=db_path,
            )
            result.added += 1
        except DuplicateTopicError:
            result.skipped_duplicates += 1
        except ValueError:
            result.skipped_empty += 1
        except Exception as exc:
            result.errors.append(f"{cell!r}: {exc}")

    return result


def claim_next_topic(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Atomically claim the highest-priority queued topic for a channel.

    Uses a conditional UPDATE to prevent double-claiming. The first caller
    to execute the UPDATE wins; concurrent callers receive None.

    Args:
        channel_id: Channel to claim a topic for.
        db_path:    Override DB path (for tests).

    Returns:
        Dict of the claimed episode_topics row (topic_id, channel_id,
        topic_text, status, priority, claimed_at, …), or None if no
        queued topic exists for this channel.
    """
    conn = get_connection(db_path)
    try:
        result = claim_next_episode_topic(conn, channel_id, _now_iso())
        conn.commit()
        return result
    finally:
        conn.close()


def consume_topic(
    topic_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Mark a claimed topic as consumed after successful pipeline execution.

    Only transitions 'claimed' topics. Already-consumed or terminal topics
    are unchanged (no-op, no error raised).

    Args:
        topic_id: Topic to mark consumed.
        db_path:  Override DB path (for tests).
    """
    now = _now_iso()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE episode_topics
            SET status = 'consumed', consumed_at = ?, updated_at = ?
            WHERE topic_id = ? AND status = 'claimed'
            """,
            (now, now, topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def release_topic(
    topic_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Release a claimed topic back to 'queued' for retry.

    Called when a pipeline run fails for reasons unrelated to the topic content
    (provider error, model unavailable, budget exceeded, etc.). The topic text
    remains valid and will be retried on the next poll pass.

    Only transitions 'claimed' topics. No-op for topics in any other state.
    Never raises — cleanup errors are silently swallowed so the caller can
    always re-raise the original pipeline exception.

    Args:
        topic_id: Topic to release.
        db_path:  Override DB path (for tests).
    """
    now = _now_iso()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE episode_topics
            SET status = 'queued', claimed_at = NULL, updated_at = ?
            WHERE topic_id = ? AND status = 'claimed'
            """,
            (now, topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def skip_topic(
    topic_id: str,
    reason: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """
    Skip a topic that should not be retried.

    Handles both 'queued' topics (operator decision) and 'claimed' topics
    (EP rejection path — the pipeline claimed the topic, ran the EP, and the
    EP scored it below the REJECT threshold).

    A skipped topic will never be claimed or retried.

    Args:
        topic_id: Topic to skip.
        reason:   Optional explanation (e.g. "ep_rejected").
        db_path:  Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE episode_topics
            SET status = 'skipped', claimed_at = NULL,
                reason = COALESCE(?, reason), updated_at = ?
            WHERE topic_id = ? AND status IN ('queued', 'claimed')
            """,
            (reason, _now_iso(), topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_topic(
    topic_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Cancel a topic. Valid from 'queued' or 'claimed' status.

    Already-consumed or skipped topics are unchanged (no-op).

    Args:
        topic_id: Topic to cancel.
        db_path:  Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE episode_topics
            SET status = 'cancelled', updated_at = ?
            WHERE topic_id = ? AND status IN ('queued', 'claimed')
            """,
            (_now_iso(), topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_topic_target_publish_at(
    topic_id: str,
    target_publish_at: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Fill target_publish_at on a specific topic, only if it is currently unset.

    Used by queue.process_next() immediately after it atomically claims a
    topic for a scheduler-linked run_queue item (see
    hypogeum.scheduler.is_scheduled_invocation()) — the deterministic
    publish time is computed for and written onto the exact topic that was
    just claimed, never a topic merely observed earlier. Unlike a "next
    queued" lookup, there is no ordering/positional assumption here: the
    caller already holds the specific topic_id of a row it owns the claim
    for, so this is a single unambiguous conditional UPDATE by primary key.
    Never overwrites an explicit/operator-set value.

    Args:
        topic_id:            Topic to update (already claimed by the caller).
        target_publish_at:  ISO 8601 UTC datetime string.
        db_path:             Override DB path (for tests).

    Returns:
        True if the row was updated, False if the topic does not exist or
        already had an explicit target_publish_at.
    """
    now = _now_iso()
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            UPDATE episode_topics
            SET target_publish_at = ?, updated_at = ?
            WHERE topic_id = ? AND target_publish_at IS NULL
            """,
            (target_publish_at, now, topic_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def set_topic_episode_id(
    topic_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Fill episode_id on a specific topic, only if it is currently unset.

    Structural twin of set_topic_target_publish_at() — same conditional
    UPDATE-by-primary-key shape, same idempotency guarantee, never
    overwrites an existing value. Part of the identity contract documented
    in queue.py::process_next(), Step 5: this is what makes
    lookup_episode_target_publish_at(episode_id) (keyed by episode_id) able
    to find the row that set_topic_target_publish_at(topic_id, ...) (keyed
    by topic_id) just wrote, for a topic that had no pre-assigned
    episode_id at claim time.

    Args:
        topic_id:   Topic to update (already claimed by the caller).
        episode_id: The episode_id assigned to this claim.
        db_path:    Override DB path (for tests).

    Returns:
        True if the row was updated, False if the topic does not exist or
        already had an episode_id.
    """
    now = _now_iso()
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            UPDATE episode_topics
            SET episode_id = ?, updated_at = ?
            WHERE topic_id = ? AND episode_id IS NULL
            """,
            (episode_id, now, topic_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_topic(
    topic_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return a single episode_topics row as a dict, or None if not found."""
    conn = get_connection(db_path)
    try:
        return get_episode_topic(conn, topic_id)
    finally:
        conn.close()


def list_topics(
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return topics from the backlog, optionally filtered by channel and/or status.

    Args:
        channel_id: Filter to a specific channel. None returns all channels.
        status:     Filter by status ('queued', 'claimed', 'consumed', etc.).
        db_path:    Override DB path (for tests).

    Returns:
        List of dicts, ordered by channel_id, priority ASC, created_at ASC.
    """
    conn = get_connection(db_path)
    try:
        rows = list_episode_topics(conn, channel_id=channel_id, status=status)
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queue Consumer integration
# ---------------------------------------------------------------------------

def make_topic_resolver(
    db_path: Optional[Path] = None,
) -> Callable[[dict], Optional[str]]:
    """
    Return a topic_resolver function for use with queue.process_next().

    The returned callable:
      1. Reads channel_id from the queue item dict.
      2. Calls claim_next_topic(channel_id) to atomically claim the next topic.
      3. Returns the topic_text string, or None if no queued topic exists.

    When process_next() receives None from this resolver it releases the queue
    item back to 'queued' and returns ProcessResult(status="no_topic").

    The caller is responsible for calling consume_topic(topic_id) after
    successful pipeline execution when using this resolver directly. When
    process_next() uses its built-in backlog path (topic_resolver=None),
    it calls consume_topic() automatically.

    Args:
        db_path: Override DB path passed to claim_next_topic (for tests).

    Returns:
        Callable[[dict], Optional[str]] — returns topic_text or None.
    """
    def _resolver(item: dict) -> Optional[str]:
        channel_id = item.get("channel_id")
        if not channel_id:
            return None
        topic_row = claim_next_topic(channel_id, db_path=db_path)
        if topic_row is None:
            return None
        return topic_row["topic_text"]

    return _resolver


# ---------------------------------------------------------------------------
# Stale-topic reconciliation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TopicReconciliationRow:
    """One episode_topics row a reconciliation call found or acted on."""
    topic_id: str
    source: str
    priority: int
    created_at: str


@dataclass
class TopicReconciliationResult:
    """
    Auditable outcome of one reconcile_stale_topics_for_episode() call.

    eligible=False means episode_id is unknown or not yet in a terminal
    'complete' state — no row is ever touched in that case, regardless of
    dry_run. When eligible=True and dry_run=True, `rows` lists exactly the
    queued rows that WOULD be reconciled and applied_count is always 0 (no
    mutation occurred). When eligible=True and dry_run=False, `rows` lists
    the rows that WERE reconciled and applied_count == len(rows).
    """
    episode_id: str
    dry_run: bool
    eligible: bool
    episode_status: Optional[str]
    reason: str
    rows: List[TopicReconciliationRow] = field(default_factory=list)
    applied_count: int = 0


def reconcile_stale_topics_for_episode(
    episode_id: str,
    db_path: Optional[Path] = None,
    dry_run: bool = True,
) -> TopicReconciliationResult:
    """
    Idempotent, scoped-to-one-episode closure of obsolete queued
    episode_topics rows, once episodes.status='complete' for that episode —
    the terminal state confirming no further retry of THIS episode_id is
    ever needed again. Never touches 'claimed' rows (those are owned by an
    active worker — same boundary cancel_queued_topics_for_episode() in
    hypogeum.db already respects) and never touches any other episode_id's
    rows.

    Two callers:
      - An operator, explicitly, with dry_run=True first (preview) then
        dry_run=False (apply) — the manual path for pre-existing stale rows
        that predate this reconciliation mechanism (e.g. ep_f3619022's 8
        rows).
      - hypogeum.run_registry._sync_episode_row(), automatically, with
        dry_run=False, immediately after it sets episodes.status='complete'
        — the root-cause fix so this staleness never recurs for any
        episode completed from this point forward. Best-effort there
        (wrapped in try/except); never allowed to affect run completion.

    Args:
        episode_id: Exactly one episode to reconcile. No broad/multi-episode
                    cleanup is performed by this function.
        db_path:    Override DB path (for tests / isolated reconciliation DBs).
        dry_run:    True (default) previews without mutating. False applies.

    Returns:
        TopicReconciliationResult — see its own docstring for field meaning.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            ep_row = conn.execute(
                "SELECT status FROM episodes WHERE episode_id = ?", (episode_id,),
            ).fetchone()
            episode_status = ep_row["status"] if ep_row is not None else None

            if episode_status != "complete":
                conn.rollback()
                return TopicReconciliationResult(
                    episode_id=episode_id, dry_run=dry_run, eligible=False,
                    episode_status=episode_status,
                    reason=(
                        f"episode_id {episode_id!r} is not in a terminal 'complete' state "
                        f"(status={episode_status!r}) — no queued topics reconciled"
                    ),
                )

            candidates = conn.execute(
                """
                SELECT topic_id, source, priority, created_at FROM episode_topics
                WHERE episode_id = ? AND status = 'queued'
                ORDER BY created_at ASC
                """,
                (episode_id,),
            ).fetchall()
            rows = [
                TopicReconciliationRow(
                    topic_id=r["topic_id"], source=r["source"],
                    priority=r["priority"], created_at=r["created_at"],
                )
                for r in candidates
            ]

            if not rows:
                conn.rollback()
                return TopicReconciliationResult(
                    episode_id=episode_id, dry_run=dry_run, eligible=True,
                    episode_status=episode_status,
                    reason="no queued topics remain for this episode — nothing to reconcile",
                )

            if dry_run:
                conn.rollback()
                return TopicReconciliationResult(
                    episode_id=episode_id, dry_run=True, eligible=True,
                    episode_status=episode_status,
                    reason=f"preview only — {len(rows)} queued topic(s) would be reconciled; no mutation performed",
                    rows=rows,
                )

            now = _now_iso()
            applied = 0
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE episode_topics
                    SET status = 'cancelled', reason = ?, updated_at = ?
                    WHERE topic_id = ? AND status = 'queued'
                    """,
                    (_RECONCILIATION_REASON, now, row.topic_id),
                )
                applied += cursor.rowcount
            conn.commit()
            return TopicReconciliationResult(
                episode_id=episode_id, dry_run=False, eligible=True,
                episode_status=episode_status,
                reason=f"reconciled {applied} stale queued topic(s)",
                rows=rows, applied_count=applied,
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
