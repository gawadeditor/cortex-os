"""
hypogeum/buffer.py — One-episode publish-ready buffer gate.

Two layers, deliberately separated:
    compute_buffer_state(channel_id, candidates)  — pure, deterministic,
        no I/O. Given a list of already-classified BufferEpisodeCandidate
        objects, decides "enqueue" / "hold" / "excess". Trivially unit-
        testable with fixture candidate lists (0 / 1 / 2+ scenarios) — no
        DB, no disk, no provider call.
    gather_buffer_candidates(channel_id, ...)      — the I/O layer that
        builds that candidate list from real state (episodes table,
        publish_calendar, publishing_outcomes, and
        hypogeum.publishing.readiness.check_publish_readiness()). Used only
        to build compute_buffer_state()'s input for real use.

Wired into run_worker.py's Step 0 discovery-refill path ONLY — gates
whether NEW discovery-driven topic enqueue is allowed for a channel. Every
other worker duty (tick, consume, poll, publishing) is completely
unaffected; this module never stops, pauses, or starts anything on its own.

Buffer definition: the count of a channel's episodes that are ALL of:
  - episodes.status == 'complete'
  - publish-ready per hypogeum.publishing.readiness.check_publish_readiness()
    (no operator metadata override applied by this module — a
    metadata-blocked episode never counts)
  - NOT already published (a publishing_outcomes row with
    publishing_status='success', or a publish_calendar row with
    publish_status='live', for this episode — an already-published episode
    has already been consumed by a slot, it is not "ahead" any more)
  - NOT the current week's episode (a publish_calendar row for this episode
    with target_publish_at inside the next 7 days from `now` — that episode
    is about to air, it is not surplus buffer)

Gate contract:
    buffer == 0  -> "enqueue"  (discovery refill may run)
    buffer == 1  -> "hold"     (sufficient — do not enqueue more)
    buffer >= 2  -> "excess"   (report the surplus — do not enqueue more)

This module never unpauses a schedule, never starts a run, and never
enqueues anything itself — it only classifies state; the caller decides
what "enqueue"/"hold"/"excess" means for its own loop.

Public API:
    BufferEpisodeCandidate, BufferState
    compute_buffer_state(channel_id, candidates)                 -> BufferState
    gather_buffer_candidates(channel_id, *, db_path=None,
                              assets_root, platforms=(), now=None)
        -> list[BufferEpisodeCandidate]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

_CURRENT_WEEK_WINDOW_DAYS = 7


@dataclass(frozen=True)
class BufferEpisodeCandidate:
    episode_id: str
    publish_ready: bool
    already_published: bool
    is_current_week: bool


@dataclass(frozen=True)
class BufferState:
    channel_id: str
    action: str  # "enqueue" | "hold" | "excess"
    buffer_count: int
    counted_episode_ids: List[str] = field(default_factory=list)
    excluded_episode_ids: List[str] = field(default_factory=list)
    reason: str = ""


def compute_buffer_state(channel_id: str, candidates: Sequence[BufferEpisodeCandidate]) -> BufferState:
    """Pure — no I/O, no side effects, deterministic for a given input."""
    counted = [
        c.episode_id for c in candidates
        if c.publish_ready and not c.already_published and not c.is_current_week
    ]
    excluded = [c.episode_id for c in candidates if c.episode_id not in counted]
    n = len(counted)

    if n == 0:
        action = "enqueue"
        reason = "no publish-ready buffer episode ahead — discovery refill may enqueue one replacement episode"
    elif n == 1:
        action = "hold"
        reason = "exactly one publish-ready buffer episode ahead — sufficient, do not enqueue"
    else:
        action = "excess"
        reason = f"{n} publish-ready buffer episodes ahead — excess, do not enqueue"

    return BufferState(
        channel_id=channel_id, action=action, buffer_count=n,
        counted_episode_ids=counted, excluded_episode_ids=excluded, reason=reason,
    )


def gather_buffer_candidates(
    channel_id: str,
    *,
    db_path: Optional[Path] = None,
    assets_root: Path,
    platforms: Sequence[str] = (),
    provider_id: str = "zernio",
    now: Optional[datetime] = None,
) -> List[BufferEpisodeCandidate]:
    """
    Real-state gathering for compute_buffer_state()'s input. Read-only —
    issues SELECTs against episodes/episode_topics/publish_calendar/
    publishing_outcomes and calls check_publish_readiness() (itself
    read-only); never mutates anything.

    episodes has no channel_id column — an episode is scoped to a channel
    via its episode_topics row(s) (same join hypogeum.run_registry.
    _sync_episode_row() already uses), so an episode with no episode_topics
    row at all cannot be attributed to any channel and is excluded.
    """
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415
    from hypogeum.publishing.readiness import check_publish_readiness  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    current_week_cutoff = now + timedelta(days=_CURRENT_WEEK_WINDOW_DAYS)

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        episode_rows = conn.execute(
            """
            SELECT DISTINCT e.episode_id
            FROM episodes e
            JOIN episode_topics t ON t.episode_id = e.episode_id
            WHERE t.channel_id = ? AND e.status = 'complete'
            """,
            (channel_id,),
        ).fetchall()
        episode_ids = [r["episode_id"] for r in episode_rows]

        candidates: List[BufferEpisodeCandidate] = []
        for episode_id in episode_ids:
            published_row = conn.execute(
                """
                SELECT 1 FROM publishing_outcomes
                WHERE episode_id = ? AND publishing_status = 'success'
                LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
            live_calendar_row = conn.execute(
                """
                SELECT 1 FROM publish_calendar
                WHERE episode_id = ? AND publish_status = 'live'
                LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
            already_published = bool(published_row or live_calendar_row)

            current_week_row = conn.execute(
                """
                SELECT 1 FROM publish_calendar
                WHERE episode_id = ? AND target_publish_at IS NOT NULL
                  AND target_publish_at <= ? AND publish_status != 'live'
                LIMIT 1
                """,
                (episode_id, current_week_cutoff.isoformat()),
            ).fetchone()
            is_current_week = bool(current_week_row)

            readiness = check_publish_readiness(
                episode_id, assets_root=assets_root, db_path=db_path,
                channel_id=channel_id, platforms=platforms,
                provider_id=provider_id, metadata_override=None,
            )
            candidates.append(BufferEpisodeCandidate(
                episode_id=episode_id, publish_ready=readiness.passed,
                already_published=already_published, is_current_week=is_current_week,
            ))
        return candidates
    finally:
        conn.close()
