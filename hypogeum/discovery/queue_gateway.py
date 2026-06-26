"""
hypogeum/discovery/queue_gateway.py — Queue Gateway v2

Converts the selected verified opportunity into an episode_topics queue row,
or holds it in pending_approval state, depending on AUTO_DISCOVERY_AUTO_APPROVE.

This is the final step in the Discovery Director v2 pipeline. It is called
after source_verifier.run_source_verifier() has produced a ranked, verified
list and the Discovery Director has selected the winning opportunity.

Routing rules:
  AUTO_DISCOVERY_AUTO_APPROVE=False (default):
    - Do not write to episode_topics.
    - Return {ok: True, routed_to: "pending_approval", topic_id: None}.
    - Operator reviews the opportunity via the dashboard and approves manually.

  AUTO_DISCOVERY_AUTO_APPROVE=True:
    - Call enqueue_topic(validate_channel=True).
    - Priority derived from: priority_score → final_composite → initial_composite.
    - Score [0.0, 1.0] mapped to integer [0, 10] via round(score * 10), clamped.
    - Missing or unparseable score field defaults to priority 5 (medium).
    - Return {ok: True, routed_to: "episode_topics", topic_id: <id>,
              queue_priority: <int>}.

Failure returns (never raise):
  {ok: False, failure_reason: "duplicate"}          — topic already queued
  {ok: False, failure_reason: "channel_not_found"}  — channel_id not in DB
  {ok: False, failure_reason: "route_failed"}       — unexpected exception

Public API:
  route_selected_opportunity(opportunity, config, db_path=None) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.topics import (
    ChannelNotFoundError,
    DuplicateTopicError,
    enqueue_topic,
)


# ---------------------------------------------------------------------------
# Priority derivation
# ---------------------------------------------------------------------------

_PRIORITY_FIELDS = ("priority_score", "final_composite", "initial_composite")
_DEFAULT_PRIORITY = 5
_PRIORITY_MIN = 0
_PRIORITY_MAX = 10


def _derive_priority(opportunity: dict) -> int:
    """
    Derive an integer queue priority [0, 10] from the opportunity's score fields.

    Checks priority_score, then final_composite, then initial_composite.
    Each is treated as a float in [0.0, 1.0] and mapped to [0, 10] via
    round(score * 10). Falls back to 5 (medium) when no usable score is found.
    """
    for key in _PRIORITY_FIELDS:
        val = opportunity.get(key)
        if val is None:
            continue
        try:
            return max(_PRIORITY_MIN, min(_PRIORITY_MAX, round(float(val) * 10)))
        except (TypeError, ValueError):
            continue
    return _DEFAULT_PRIORITY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_selected_opportunity(
    opportunity: dict,
    config: Config,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Route the selected opportunity to the appropriate destination.

    Args:
        opportunity: Enriched opportunity dict (from topic_ideator + verifier).
                     Must contain at minimum: topic_text, channel_id.
                     Optional score fields: priority_score, final_composite,
                     initial_composite — used to derive queue_priority.
        config:      Loaded Config instance. Reads AUTO_DISCOVERY_AUTO_APPROVE.
        db_path:     DB path override (for tests).

    Returns:
        On pending_approval (AUTO_DISCOVERY_AUTO_APPROVE=False):
            {ok: True, routed_to: "pending_approval", topic_id: None}

        On successful enqueue (AUTO_DISCOVERY_AUTO_APPROVE=True):
            {ok: True, routed_to: "episode_topics",
             topic_id: <str>, queue_priority: <int>}

        On duplicate:
            {ok: False, failure_reason: "duplicate"}

        On missing channel:
            {ok: False, failure_reason: "channel_not_found"}

        On any other exception:
            {ok: False, failure_reason: "route_failed"}
    """
    auto_approve = bool(getattr(config, "AUTO_DISCOVERY_AUTO_APPROVE", False))

    if not auto_approve:
        return {
            "ok":        True,
            "routed_to": "pending_approval",
            "topic_id":  None,
        }

    channel_id = (opportunity.get("channel_id") or "").strip()
    topic_text = (opportunity.get("topic_text") or "").strip()
    priority = _derive_priority(opportunity)

    try:
        topic_id = enqueue_topic(
            channel_id=channel_id,
            topic_text=topic_text,
            priority=priority,
            validate_channel=True,
            db_path=db_path,
        )
        return {
            "ok":            True,
            "routed_to":     "episode_topics",
            "topic_id":      topic_id,
            "queue_priority": priority,
        }
    except DuplicateTopicError:
        return {
            "ok":            False,
            "failure_reason": "duplicate",
        }
    except ChannelNotFoundError:
        return {
            "ok":            False,
            "failure_reason": "channel_not_found",
        }
    except Exception:
        return {
            "ok":            False,
            "failure_reason": "route_failed",
        }
