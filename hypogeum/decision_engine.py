"""
hypogeum/decision_engine.py — F3: Real Decision Engine v0

Conservative, rule-based decision engine. Every decision is traceable to
real inputs from F2 (decision_inputs) and the operational command state.

If evidence is insufficient the engine returns an insufficient_data entry
instead of inventing a recommendation.

No mock data as truth. No synthetic recommendations. No automatic execution.
No publishing. No episode launch. No LLM-generated strategy.

Public API:
    get_decision_state(db_path=None) -> dict

Decision types:
    launch_readiness   — can the next episode be launched?
    failure_review     — recent run failures need inspection
    cost_review        — cost per episode relative to static threshold
    publishing_gap     — completed episodes with no publishing outcome
    audience_review    — real audience data availability
    discovery_review   — topic queue state

Return shape:
    {
        "checked_at":       str,
        "engine_status":    "not_ready|partial|ready",
        "summary":          str,
        "decisions":        [DecisionCard, ...],
        "insufficient_data":[InsufficientEntry, ...],
    }

DecisionCard shape:
    decision_id, type, priority, title, recommendation, evidence,
    expected_impact, confidence, status, action_label, reject_label,
    investigate_url
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configurable constants
# ---------------------------------------------------------------------------

COST_THRESHOLD_USD_PER_EPISODE: float = 1.00
MIN_FAILURES_FOR_REVIEW:        int   = 2
MIN_AUDIENCE_SNAPSHOTS:         int   = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _did(dtype: str, key: str = "") -> str:
    """Deterministic 16-char hex ID from type + key."""
    raw = f"{dtype}:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _card(
    dtype:           str,
    priority:        int,
    title:           str,
    recommendation:  str,
    evidence:        list[str],
    expected_impact: str,
    confidence:      float,
    action_label:    str,
    investigate_url: str,
    id_key:          str = "",
) -> dict:
    return {
        "decision_id":     _did(dtype, id_key),
        "type":            dtype,
        "priority":        priority,
        "title":           title,
        "recommendation":  recommendation,
        "evidence":        evidence,
        "expected_impact": expected_impact,
        "confidence":      round(confidence, 2),
        "status":          "proposed",
        "action_label":    action_label,
        "reject_label":    "Reject",
        "investigate_url": investigate_url,
    }


def _insuff(dtype: str, reason: str) -> dict:
    return {"type": dtype, "reason": reason}


# ---------------------------------------------------------------------------
# Rule: launch_readiness
# ---------------------------------------------------------------------------

def _rule_launch_readiness(cmd: dict, di: dict) -> tuple[list, list]:
    """
    If can_launch=False → critical decision per blocker.
    If can_launch=True + queued topic → ready-to-launch decision (informational).
    """
    decisions: list[dict] = []
    er   = cmd.get("episode_readiness", {})
    disc = di.get("discovery", {})

    if not er.get("can_launch", False):
        blockers = er.get("blockers", [])
        if blockers:
            for b in blockers[:2]:
                decisions.append(_card(
                    dtype="launch_readiness", priority=1,
                    title="Launch blocked — resolve before next production",
                    recommendation="Resolve this issue to unblock the production pipeline.",
                    evidence=[b],
                    expected_impact="Unblocks next episode run.",
                    confidence=1.0,
                    action_label="View preflight",
                    investigate_url="/preflight",
                    id_key=b[:40],
                ))
        else:
            decisions.append(_card(
                dtype="launch_readiness", priority=1,
                title="Launch not ready",
                recommendation="Factory is not cleared for a new run. Check preflight audit.",
                evidence=["command_state.episode_readiness.can_launch = False"],
                expected_impact="Unblocks next episode run once resolved.",
                confidence=1.0,
                action_label="View preflight",
                investigate_url="/preflight",
                id_key="no_launch",
            ))
    else:
        queued = disc.get("by_status", {}).get("queued", 0)
        if queued > 0:
            decisions.append(_card(
                dtype="launch_readiness", priority=1,
                title=f"Ready to launch — {queued} topic(s) queued",
                recommendation=(
                    "Factory is cleared and topic queue has work. "
                    "Operator may initiate the next production run when ready."
                ),
                evidence=[
                    "command_state.episode_readiness.can_launch = True",
                    f"episode_topics.queued = {queued}",
                ],
                expected_impact="Next episode can be produced from the queued topic.",
                confidence=1.0,
                action_label="View topic queue",
                investigate_url="/queue",
                id_key="can_launch",
            ))

    return decisions, []


# ---------------------------------------------------------------------------
# Rule: failure_review
# ---------------------------------------------------------------------------

def _rule_failure_review(cmd: dict) -> tuple[list, list]:
    """2+ recent failed runs → decision to inspect before launching another."""
    failures = cmd.get("run_state", {}).get("recent_failures", [])
    if len(failures) < MIN_FAILURES_FOR_REVIEW:
        return [], []

    evidence = [
        f"Run {r.get('run_id','?')[:8]}… — episode {r.get('episode_id','?')}"
        + (f": {(r.get('error_message') or '')[:60]}" if r.get("error_message") else "")
        for r in failures[:3]
    ]
    decisions = [_card(
        dtype="failure_review", priority=2,
        title=f"{len(failures)} recent run failure(s) require inspection",
        recommendation=(
            "Review failure causes before launching another run. "
            "Repeating without investigation risks wasting production budget."
        ),
        evidence=evidence,
        expected_impact="Prevents budget waste from repeated failures.",
        confidence=1.0,
        action_label="View failures",
        investigate_url="/failures",
        id_key=str(len(failures)),
    )]
    return decisions, []


# ---------------------------------------------------------------------------
# Rule: cost_review
# ---------------------------------------------------------------------------

def _rule_cost_review(di: dict) -> tuple[list, list]:
    """
    If avg cost per episode > threshold → decision to review cost drivers.
    If no cost data → insufficient_data.
    """
    costs = di.get("costs", {})

    if not costs.get("has_data"):
        return [], [_insuff(
            "cost_review",
            "No cost records found. Cost review requires at least one completed episode run."
        )]

    avg = costs.get("avg_cost_per_episode")
    if avg is None:
        return [], [_insuff("cost_review", "Average cost per episode could not be computed.")]

    if avg > COST_THRESHOLD_USD_PER_EPISODE:
        top_provider = max(costs.get("by_provider", {}).items(),
                           key=lambda kv: kv[1], default=("unknown", 0))
        decisions = [_card(
            dtype="cost_review", priority=3,
            title=f"Cost per episode ${avg:.4f} exceeds threshold ${COST_THRESHOLD_USD_PER_EPISODE:.2f}",
            recommendation=(
                f"Review cost drivers. Largest provider: {top_provider[0]} "
                f"(${top_provider[1]:.4f} total). "
                "Check model assignments and retry rates."
            ),
            evidence=[
                f"avg_cost_per_episode = ${avg:.4f}",
                f"threshold = ${COST_THRESHOLD_USD_PER_EPISODE:.2f}",
                f"total_cost_usd = ${costs.get('total_cost_usd', 0):.4f}",
                f"episodes_costed = {costs.get('total_episodes_costed', 0)}",
            ],
            expected_impact="Reduces cost per episode toward threshold.",
            confidence=0.8,
            action_label="View cost breakdown",
            investigate_url="/analytics",
            id_key=f"{avg:.2f}",
        )]
        return decisions, []

    return [], []


# ---------------------------------------------------------------------------
# Rule: publishing_gap
# ---------------------------------------------------------------------------

def _rule_publishing_gap(di: dict) -> tuple[list, list]:
    """Completed episodes with no publishing outcome recorded → gap decision."""
    episodes   = di.get("episodes", {})
    publishing = di.get("publishing", {})

    completed  = episodes.get("completed_episodes", 0)
    pub_total  = publishing.get("total_outcomes", 0)

    if completed > 0 and pub_total == 0:
        decisions = [_card(
            dtype="publishing_gap", priority=3,
            title=f"{completed} completed episode(s) with no publishing outcome recorded",
            recommendation=(
                "Record publishing outcomes for completed episodes to enable "
                "future decision-making around content distribution."
            ),
            evidence=[
                f"episodes.completed = {completed}",
                "publishing_outcomes.total = 0",
            ],
            expected_impact="Enables publishing outcome tracking and future gap analysis.",
            confidence=0.9,
            action_label="View decision inputs",
            investigate_url="/decision-inputs",
            id_key=f"{completed}_completed",
        )]
        return decisions, []

    return [], []


# ---------------------------------------------------------------------------
# Rule: audience_review
# ---------------------------------------------------------------------------

def _rule_audience_review(di: dict) -> tuple[list, list]:
    """
    Summarise audience data availability. Does NOT recommend content direction.
    Requires MIN_AUDIENCE_SNAPSHOTS real snapshots before an audience_review
    decision is produced.
    Mock-only data → insufficient_data.
    """
    audience = di.get("audience", {})
    real  = audience.get("real_snapshots", 0)
    mock  = audience.get("mock_snapshots", 0)

    if real == 0 and mock == 0:
        return [], [_insuff(
            "audience_review",
            "No audience data imported. Import real performance data after publishing."
        )]

    if real == 0 and mock > 0:
        return [], [_insuff(
            "audience_review",
            f"All {mock} audience snapshot(s) are mock/demo data. "
            "Import real data from platform dashboards to enable audience review."
        )]

    if real < MIN_AUDIENCE_SNAPSHOTS:
        decisions = [_card(
            dtype="audience_review", priority=4,
            title=f"{real} real audience snapshot(s) available — more needed for trends",
            recommendation=(
                f"Audience data is present but limited ({real} snapshot(s)). "
                f"Collect at least {MIN_AUDIENCE_SNAPSHOTS} real snapshots before drawing conclusions."
            ),
            evidence=[
                f"real_snapshots = {real}",
                f"mock_snapshots = {mock}",
                f"minimum_for_trends = {MIN_AUDIENCE_SNAPSHOTS}",
            ],
            expected_impact="More audience data enables reliable trend analysis.",
            confidence=0.3,
            action_label="View audience data",
            investigate_url="/decision-inputs",
            id_key=f"real_{real}",
        )]
        return decisions, []

    # 3+ real snapshots
    by_platform = audience.get("by_platform", {})
    platform_summary = "; ".join(
        f"{p}: {v.get('real', 0)} real"
        for p, v in by_platform.items()
        if v.get("real", 0) > 0
    ) or "see /decision-inputs"

    decisions = [_card(
        dtype="audience_review", priority=4,
        title=f"{real} real audience snapshot(s) available for investigation",
        recommendation=(
            "Sufficient audience data is available for initial analysis. "
            "Review in Analytics or Decision Inputs. "
            "Content direction recommendations require additional evidence."
        ),
        evidence=[
            f"real_snapshots = {real}",
            f"platforms: {platform_summary}",
        ],
        expected_impact="Enables evidence-based content direction review.",
        confidence=0.7,
        action_label="View analytics",
        investigate_url="/analytics",
        id_key=f"real_{real}",
    )]
    return decisions, []


# ---------------------------------------------------------------------------
# Rule: discovery_review
# ---------------------------------------------------------------------------

def _rule_discovery_review(cmd: dict, di: dict) -> tuple[list, list]:
    """Topic queue state — always produces a decision (queue full or empty)."""
    disc   = di.get("discovery", {})
    queued = disc.get("by_status", {}).get("queued", 0)
    total  = disc.get("total_topics", 0)

    if queued > 0:
        # launch_readiness already covered the launch-ready case;
        # discovery_review is informational here
        decisions = [_card(
            dtype="discovery_review", priority=4,
            title=f"Topic queue: {queued} topic(s) ready",
            recommendation="Topic queue has work available for the next production run.",
            evidence=[f"episode_topics.queued = {queued}"],
            expected_impact="Next run can consume a queued topic.",
            confidence=1.0,
            action_label="View queue",
            investigate_url="/queue",
            id_key=f"queued_{queued}",
        )]
        return decisions, []

    if total == 0:
        decisions = [_card(
            dtype="discovery_review", priority=3,
            title="No topics in queue — add a topic before production",
            recommendation=(
                "The episode topic queue is empty. Add or select a topic "
                "before the next production run can start."
            ),
            evidence=["episode_topics.total = 0"],
            expected_impact="A queued topic is required to start a production run.",
            confidence=1.0,
            action_label="View episodes",
            investigate_url="/episodes",
            id_key="empty_queue",
        )]
        return decisions, []

    # Topics exist but none queued (all consumed/skipped/cancelled)
    consumed  = disc.get("by_status", {}).get("consumed", 0)
    decisions = [_card(
        dtype="discovery_review", priority=3,
        title="Topic queue empty — all topics consumed or skipped",
        recommendation=(
            "All topics have been processed. Add new topics to keep production running."
        ),
        evidence=[
            f"episode_topics.total = {total}",
            f"episode_topics.consumed = {consumed}",
            "episode_topics.queued = 0",
        ],
        expected_impact="New topics enable continuous production.",
        confidence=1.0,
        action_label="View episodes",
        investigate_url="/episodes",
        id_key="all_consumed",
    )]
    return decisions, []


# ---------------------------------------------------------------------------
# Engine status + summary
# ---------------------------------------------------------------------------

def _compute_engine_status(di: dict) -> str:
    r = di.get("readiness", {})
    if r.get("ready_for_decision_engine"):
        return "ready"
    if r.get("has_real_inputs"):
        return "partial"
    return "not_ready"


def _compute_summary(status: str, decisions: list, insufficient: list) -> str:
    if status == "not_ready":
        return (
            "Insufficient data for decision-making. "
            "Produce at least one episode to generate real decisions."
        )
    if status == "partial":
        return (
            f"{len(decisions)} decision(s) available based on partial data. "
            f"{len(insufficient)} area(s) require more data before decisions can be made."
        )
    return (
        f"{len(decisions)} proposed decision(s) based on real production data. "
        f"{len(insufficient)} area(s) flagged for data collection."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_decision_state(db_path: Optional[Path] = None) -> dict:
    """
    Compute decision state from real operational and factual inputs.

    Sources:
        get_command_state()        — operational state (F0)
        get_decision_inputs_state() — factual inputs (F2)

    All rules are applied non-blocking. Any rule failure is silently skipped.
    Returns a safe empty state on complete failure.
    """
    try:
        from hypogeum.command_state import get_command_state
        cmd = get_command_state(db_path)
    except Exception:
        cmd = {
            "episode_readiness": {"can_launch": False, "blockers": [], "next_topic": {},
                                  "pending_approval_count": 0},
            "run_state": {"recent_failures": [], "active_runs": [], "queued_runs": [],
                          "last_completed": None, "blockers": []},
        }

    try:
        from hypogeum.decision_inputs import get_decision_inputs_state
        di = get_decision_inputs_state(db_path)
    except Exception:
        di = {
            "discovery":  {"has_data": False, "by_status": {}, "total_topics": 0},
            "episodes":   {"has_data": False, "completed_episodes": 0},
            "costs":      {"has_data": False},
            "audience":   {"has_real_data": False, "real_snapshots": 0, "mock_snapshots": 0},
            "publishing": {"has_data": False, "total_outcomes": 0},
            "readiness":  {"has_real_inputs": False, "ready_for_decision_engine": False},
        }

    _RULES = [
        lambda: _rule_launch_readiness(cmd, di),
        lambda: _rule_failure_review(cmd),
        lambda: _rule_cost_review(di),
        lambda: _rule_publishing_gap(di),
        lambda: _rule_audience_review(di),
        lambda: _rule_discovery_review(cmd, di),
    ]

    all_decisions:   list[dict] = []
    all_insufficient: list[dict] = []

    for rule in _RULES:
        try:
            decisions, insufficient = rule()
            all_decisions.extend(decisions)
            all_insufficient.extend(insufficient)
        except Exception:
            pass

    all_decisions.sort(key=lambda x: x["priority"])

    engine_status = _compute_engine_status(di)
    summary       = _compute_summary(engine_status, all_decisions, all_insufficient)

    return {
        "checked_at":       _now_iso(),
        "engine_status":    engine_status,
        "summary":          summary,
        "decisions":        all_decisions,
        "insufficient_data": all_insufficient,
    }
