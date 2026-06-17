"""
hypogeum/decision_inputs/state.py

Aggregates all decision input sections into a single state snapshot.

get_decision_inputs_state(db_path=None) -> dict

Returns:
    checked_at  — ISO timestamp
    discovery   — from episode_topics
    episodes    — from episodes + runs
    publishing  — from publishing_outcomes
    costs       — from costs
    audience    — from platform_analytics (real data only; mock labeled)
    readiness   — {has_real_inputs, missing_inputs, ready_for_decision_engine}

No recommendation engine. No mock data as truth. No external calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.decision_inputs.discovery  import get_discovery_facts
from hypogeum.decision_inputs.episodes   import get_episode_facts
from hypogeum.decision_inputs.publishing import get_publishing_facts
from hypogeum.decision_inputs.costs      import get_cost_facts
from hypogeum.decision_inputs.audience   import get_audience_facts


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_readiness(
    discovery:  dict,
    episodes:   dict,
    publishing: dict,
    costs:      dict,
    audience:   dict,
) -> dict:
    """
    Derive readiness state from all input sections.

    Minimum required for ready_for_decision_engine:
        - Discovery: any topics exist
        - Episodes:  any completed episodes
        - Costs:     any cost records

    Publishing and audience are enriching inputs but not blockers.
    """
    missing: list[str] = []

    discovery_ok  = discovery.get("has_data", False)
    episodes_ok   = episodes.get("completed_episodes", 0) > 0
    costs_ok      = costs.get("has_data", False)
    publishing_ok = publishing.get("has_data", False)
    audience_ok   = audience.get("has_real_data", False)

    if not discovery_ok:
        missing.append("discovery")
    if not episodes_ok:
        missing.append("episodes")
    if not costs_ok:
        missing.append("costs")
    if not publishing_ok:
        missing.append("publishing")
    if not audience_ok:
        missing.append("audience")

    has_real = discovery_ok or episodes_ok or costs_ok or publishing_ok or audience_ok
    ready    = discovery_ok and episodes_ok and costs_ok

    return {
        "has_real_inputs":            has_real,
        "missing_inputs":             missing,
        "ready_for_decision_engine":  ready,
    }


def get_decision_inputs_state(db_path: Optional[Path] = None) -> dict:
    """
    Assemble a complete decision inputs snapshot.

    All sub-queries are non-blocking. Partial failures return empty sections.
    No external API calls. No mock data treated as truth. No writes.

    Returns a dict with keys:
        checked_at, discovery, episodes, publishing, costs, audience, readiness
    """
    discovery  = get_discovery_facts(db_path)
    episodes   = get_episode_facts(db_path)
    publishing = get_publishing_facts(db_path)
    costs      = get_cost_facts(db_path)
    audience   = get_audience_facts(db_path)
    readiness  = _compute_readiness(discovery, episodes, publishing, costs, audience)

    return {
        "checked_at": _now_iso(),
        "discovery":  discovery,
        "episodes":   episodes,
        "publishing": publishing,
        "costs":      costs,
        "audience":   audience,
        "readiness":  readiness,
    }
