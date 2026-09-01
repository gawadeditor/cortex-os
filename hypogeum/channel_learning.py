"""
hypogeum/channel_learning.py — Channel Learning directive store.

The learning engine turns platform analytics into learning_actions. For
`content_volume` an applied action executes directly (nudges the channel's
production_cadence). Every other dimension used to be advisory only — the
operator acknowledged it and nothing changed.

This module closes that loop: an applied action for an advisory dimension
becomes a short, channel-scoped **directive** here, and the relevant agent
reads the channel's active directives on the next episode.

Channel Learning, not Factory Learning: a directive is scoped to exactly
one channel and never crosses to another. (Cross-channel learning —
provider quality, cost, failure patterns — is Factory Learning, a separate
store.)

Public API:
    record_directive(channel_id, dimension, action_type, directive_text, *,
                     source_action_id=None, source_episode=None, db_path=None) -> dict
    get_active_directives(channel_id, *, dimension=None, db_path=None) -> list[dict]
    directive_texts(channel_id, dimension, *, db_path=None) -> list[str]
    deactivate_directive(directive_id, *, db_path=None) -> bool
    list_channel_changelog(channel_id, *, limit=50, db_path=None) -> list[dict]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_ADVISORY_DIMENSIONS = {
    "thumbnail", "hook", "pacing", "topic", "title", "description",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_directive(
    channel_id: str,
    dimension: str,
    action_type: str,
    directive_text: str,
    *,
    source_action_id: Optional[str] = None,
    source_episode: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Store a channel-scoped learning directive. A new directive for the
    same (channel, dimension) supersedes the previous active one — the
    channel carries at most one active directive per dimension."""
    if not channel_id or not directive_text.strip():
        return {"ok": False, "error": "channel_id and directive_text required"}
    initialize_schema(db_path)
    now = _now()
    did = "cld_" + uuid.uuid4().hex[:16]
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE channel_learning_directives SET active = 0, superseded_at = ? "
            "WHERE channel_id = ? AND dimension = ? AND active = 1",
            (now, channel_id, dimension),
        )
        conn.execute(
            "INSERT INTO channel_learning_directives "
            "(directive_id, channel_id, dimension, action_type, directive_text, "
            " source_action_id, source_episode, created_at, active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (did, channel_id, dimension, action_type, directive_text.strip(),
             source_action_id, source_episode, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "directive_id": did, "channel_id": channel_id,
            "dimension": dimension}


def get_active_directives(
    channel_id: str, *, dimension: Optional[str] = None, db_path: Optional[Path] = None,
) -> list[dict]:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        q = ("SELECT * FROM channel_learning_directives "
             "WHERE channel_id = ? AND active = 1")
        params: list = [channel_id]
        if dimension:
            q += " AND dimension = ?"
            params.append(dimension)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in conn.execute(q, params)]
    finally:
        conn.close()


def directive_texts(
    channel_id: str, dimension: str, *, db_path: Optional[Path] = None,
) -> list[str]:
    """Just the instruction strings for an agent to inject. Never raises."""
    try:
        return [d["directive_text"]
                for d in get_active_directives(channel_id, dimension=dimension, db_path=db_path)]
    except Exception:  # noqa: BLE001
        return []


def learning_input_block(
    channel_id: Optional[str], *dimensions: str, db_path: Optional[Path] = None,
) -> dict:
    """A compact block for an agent's input_package:
    {"channel_learning": {dimension: [directive_text, ...], ...}} — only the
    dimensions that actually have an active directive. {} when nothing
    applies. Never raises."""
    if not channel_id:
        return {}
    try:
        block: dict = {}
        for dim in (dimensions or _ADVISORY_DIMENSIONS):
            texts = directive_texts(channel_id, dim, db_path=db_path)
            if texts:
                block[dim] = texts
        return {"channel_learning": block} if block else {}
    except Exception:  # noqa: BLE001
        return {}


def deactivate_directive(directive_id: str, *, db_path: Optional[Path] = None) -> bool:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE channel_learning_directives SET active = 0, superseded_at = ? "
            "WHERE directive_id = ?", (_now(), directive_id))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def list_channel_changelog(
    channel_id: str, *, limit: int = 50, db_path: Optional[Path] = None,
) -> list[dict]:
    """Every directive ever recorded for the channel, newest first — the
    'what Cortex changed and why' feed for the dashboard."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM channel_learning_directives WHERE channel_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (channel_id, int(limit)))]
    finally:
        conn.close()
