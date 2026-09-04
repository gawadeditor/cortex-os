"""
hypogeum/command_state.py — Phase F0: Real Operational State Layer

Aggregates the minimum viable operational snapshot for the Command Center.

get_command_state(db_path=None) -> dict

Returns:
    checked_at          — ISO timestamp of this snapshot
    factory_status      — level (HEALTHY/DEGRADED/BLOCKED/UNKNOWN), reason, score
    providers           — connectivity status per required provider (no live API calls)
    run_state           — active, queued, last completed, recent failures, OIS blockers
    episode_readiness   — can_launch, next episode, blockers, missing inputs
    attention_queue     — ordered items requiring operator action (real DB items only)

Rules:
    - No external API calls. Provider status derived from secret_profiles DB + env vars.
    - No mock data. Every attention_queue item comes from the real DB.
    - No publishing. No episode launch. Read-only.
    - Provider validation is "untested" unless last_tested_at is within 30 days.
      Rationale: live validation costs money; operator must trigger it explicitly.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_RECENT_TEST_DAYS = 30

_REQUIRED_PROVIDERS = [
    {"name": "openrouter", "label": "OpenRouter",    "env_var": "OPENROUTER_API_KEY", "required": True},
    {"name": "elevenlabs", "label": "ElevenLabs",    "env_var": "ELEVENLABS_API_KEY", "required": False},
    {"name": "muapi",      "label": "MUAPI (Video)", "env_var": "MUAPI_API_KEY",      "required": False},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Factory Status
# ---------------------------------------------------------------------------

def _get_factory_status(db_path: Path) -> dict:
    """
    Derive factory status from the Factory Director report.
    Never raises — returns UNKNOWN on any error.
    """
    try:
        from hypogeum.factory_director import assess_factory
        report = assess_factory(db_path=db_path)
        level = {
            "READY":    "HEALTHY",
            "DEGRADED": "DEGRADED",
            "BLOCKED":  "BLOCKED",
        }.get(report.readiness_level, "UNKNOWN")

        reason = None
        if report.blocking_reasons:
            reason = report.blocking_reasons[0]
        elif report.warnings:
            reason = report.warnings[0]

        return {
            "level":           level,
            "reason":          reason,
            "director_score":  report.readiness_score,
            "director_level":  report.readiness_level,
            "blocking_reasons": list(report.blocking_reasons),
            "warnings":        list(report.warnings[:3]),
        }
    except Exception as exc:
        return {
            "level":           "UNKNOWN",
            "reason":          f"Director assessment failed: {exc}",
            "director_score":  0,
            "director_level":  "UNKNOWN",
            "blocking_reasons": [],
            "warnings":        [],
        }


# ---------------------------------------------------------------------------
# Provider Status
# ---------------------------------------------------------------------------

def _provider_status_from_profiles(name: str, profiles: list) -> Optional[dict]:
    """
    Return status dict if a secret_profile exists for this provider, else None.
    Profiles are SecretProfile dataclass instances.
    """
    matching = [p for p in profiles if (p.provider or "").lower() == name.lower()]
    if not matching:
        return None

    # Prefer active profiles; otherwise use the first one found
    active = [p for p in matching if p.status == "active"]
    profile = active[0] if active else matching[0]

    if profile.status == "expired":
        return {"status": "expired", "note": f"Credential expired — update secret profile '{profile.label}'"}
    if profile.status == "revoked":
        return {"status": "revoked", "note": f"Credential revoked — create a new secret profile"}
    if profile.status != "active":
        return {"status": "unknown", "note": f"Credential status: {profile.status}"}

    # Active — check recency of last test
    if profile.last_tested_at:
        try:
            tested_at = datetime.fromisoformat(profile.last_tested_at)
            if tested_at.tzinfo is None:
                tested_at = tested_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - tested_at
            if age <= timedelta(days=_RECENT_TEST_DAYS):
                return {
                    "status": "configured",
                    "note":   f"Tested {age.days}d ago — credential verified",
                    "last_tested_at": profile.last_tested_at,
                }
        except (ValueError, TypeError):
            pass

    return {
        "status": "untested",
        "note":   "Credential present but not recently tested — live validation not performed (avoids API cost)",
        "last_tested_at": profile.last_tested_at,
    }


def _get_provider_status(db_path: Path) -> list[dict]:
    """
    Return connectivity status for each required provider.
    No live API calls — status is derived from secret_profiles + env vars only.
    """
    try:
        from hypogeum.credential_manager import list_secret_profiles
        all_profiles = list_secret_profiles(db_path=db_path)
    except Exception:
        all_profiles = []

    result = []
    for spec in _REQUIRED_PROVIDERS:
        name    = spec["name"]
        label   = spec["label"]
        env_var = spec["env_var"]
        req     = spec["required"]

        profile_status = _provider_status_from_profiles(name, all_profiles)
        if profile_status:
            entry = {"name": name, "label": label, "required": req, **profile_status}
        else:
            # Fall back to env var presence (no live call)
            env_val = os.environ.get(env_var, "").strip()
            if env_val:
                entry = {
                    "name":     name,
                    "label":    label,
                    "required": req,
                    "status":   "untested",
                    "note":     f"{env_var} found in environment — not registered as secret profile; live validation not performed",
                }
            else:
                entry = {
                    "name":     name,
                    "label":    label,
                    "required": req,
                    "status":   "missing",
                    "note":     f"No secret profile and {env_var} not set — operator action required",
                }

        result.append(entry)

    return result


# ---------------------------------------------------------------------------
# Run State
# ---------------------------------------------------------------------------

def _get_run_state(db_path: Path) -> dict:
    """
    Return current run state from DB + blocking OIS signals.
    Never raises — returns zeroed state on any error.
    """
    empty = {
        "active_runs":    [],
        "queued_runs":    [],
        "last_completed": None,
        "recent_failures": [],
        "blockers":       [],
    }

    try:
        conn = get_connection(db_path)
        try:
            active = conn.execute("""
                SELECT run_id, episode_id, started_at, status, channel_id
                FROM runs
                WHERE status = 'running'
                ORDER BY started_at DESC
            """).fetchall()

            queued = conn.execute("""
                SELECT run_id, episode_id, started_at, status, channel_id
                FROM runs
                WHERE status IN ('queued','paused','awaiting_review','awaiting_publish_approval','publishing')
                ORDER BY started_at DESC
            """).fetchall()

            last_completed = conn.execute("""
                SELECT run_id, episode_id, started_at, completed_at, status, channel_id
                FROM runs
                WHERE status IN ('completed','complete')
                ORDER BY completed_at DESC
                LIMIT 1
            """).fetchone()

            # Only *actionable* failures: the episode still exists and wasn't
            # superseded, nothing later succeeded for it, and its topic wasn't
            # abandoned. Phantom / retried-and-recovered failures don't count.
            failures = conn.execute("""
                SELECT r.run_id, r.episode_id, r.started_at, r.completed_at,
                       r.error_message, r.channel_id
                FROM runs r
                WHERE LOWER(r.status) IN ('failed', 'error')
                  AND EXISTS (SELECT 1 FROM episodes e WHERE e.episode_id = r.episode_id
                              AND COALESCE(e.status,'') != 'superseded')
                  AND NOT EXISTS (SELECT 1 FROM runs r2 WHERE r2.episode_id = r.episode_id
                              AND LOWER(r2.status) IN ('complete','completed','published'))
                  AND NOT EXISTS (SELECT 1 FROM episode_topics t WHERE t.episode_id = r.episode_id
                              AND t.status IN ('cancelled','skipped'))
                ORDER BY COALESCE(r.completed_at, r.started_at) DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        blockers = _get_ois_blockers(db_path)

        return {
            "active_runs":    [dict(r) for r in active],
            "queued_runs":    [dict(r) for r in queued],
            "last_completed": dict(last_completed) if last_completed else None,
            "recent_failures": [dict(r) for r in failures],
            "blockers":       blockers,
        }
    except Exception:
        return empty


def _get_ois_blockers(db_path: Path) -> list[dict]:
    """Return active blocking OIS signals. Never raises."""
    try:
        import json as _json
        conn = get_connection(db_path)
        try:
            rows = conn.execute("""
                SELECT signal_id, signal_type, severity, full_record,
                       channel_id, source_system, ingested_at
                FROM signal_records
                WHERE lifecycle_status = 'open' AND blocking = 1
                ORDER BY ingested_at DESC
            """).fetchall()
        finally:
            conn.close()

        result = []
        for r in rows:
            d = dict(r)
            try:
                full = _json.loads(d.pop("full_record") or "{}")
                d["message"] = full.get("message", "")
            except Exception:
                d.pop("full_record", None)
                d["message"] = ""
            result.append(d)
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Episode Readiness
# ---------------------------------------------------------------------------

def _get_episode_readiness(db_path: Path, factory_status: dict) -> dict:
    """
    Determine whether the next episode can be launched and what blocks it.
    Never raises — returns can_launch=False with explanation on error.
    """
    blockers: list[str] = []
    missing_inputs: list[str] = []
    required_approvals: list[str] = []

    # 1. Run clearance from Factory Director
    run_clearance_cleared = True
    try:
        from hypogeum.factory_director import check_run_clearance
        clearance = check_run_clearance(db_path=db_path)
        run_clearance_cleared = clearance.cleared
        if not clearance.cleared:
            blockers.append(clearance.reason)
    except Exception as exc:
        run_clearance_cleared = False
        blockers.append(f"Run clearance check failed: {exc}")

    # 2. Acceptance audit
    audit_ready = True
    audit_blockers: list[str] = []
    try:
        from hypogeum.acceptance_audit import run_acceptance_audit
        report = run_acceptance_audit(db_path=db_path)
        audit_ready = report.ready
        for c in (report.blockers or []):
            for issue in (c.issues or []):
                audit_blockers.append(f"{c.name}: {issue}")
        if audit_blockers:
            blockers.extend(audit_blockers[:3])
    except Exception as exc:
        audit_ready = False
        blockers.append(f"Acceptance audit failed: {exc}")

    # 3. Next queued topic
    next_topic = _get_next_queued_topic(db_path)

    # 4. Pending approvals
    pending_approvals: list[dict] = []
    try:
        from hypogeum.approvals import list_pending_approvals
        pending_approvals = [
            {"approval_id": a.approval_id, "approval_type": a.approval_type,
             "run_id": a.run_id, "episode_id": a.episode_id}
            for a in list_pending_approvals(db_path=db_path)
        ]
        for ap in pending_approvals:
            required_approvals.append(
                f"{ap['approval_type']} approval pending for {ap.get('episode_id','?')}"
            )
    except Exception:
        pass

    # 5. Publishing readiness (basic: is there a channel with publishing profile?)
    publishing_ready = _check_publishing_readiness(db_path)

    can_launch = (
        run_clearance_cleared
        and audit_ready
        and not factory_status.get("blocking_reasons")
        and not missing_inputs
    )

    # Summarise what the next episode_id would be
    next_episode_id = next_topic.get("channel_id") and "next queued topic available" or None

    return {
        "can_launch":          can_launch,
        "next_topic":          next_topic,
        "next_episode_id":     next_episode_id,
        "blockers":            blockers,
        "missing_inputs":      missing_inputs,
        "required_approvals":  required_approvals,
        "publishing_ready":    publishing_ready,
        "pending_approval_count": len(pending_approvals),
    }


def _get_next_queued_topic(db_path: Path) -> dict:
    """Return the highest-priority queued topic, or {} if none exist."""
    try:
        conn = get_connection(db_path)
        try:
            row = conn.execute("""
                SELECT topic_id, channel_id, topic_text, priority, created_at
                FROM episode_topics
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """).fetchone()
        finally:
            conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def _check_publishing_readiness(db_path: Path) -> bool:
    """True if at least one channel has an active publishing profile."""
    try:
        conn = get_connection(db_path)
        try:
            row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM channel_publishing_profiles
                WHERE enabled_platforms IS NOT NULL
            """).fetchone()
        finally:
            conn.close()
        return (row["cnt"] > 0) if row else False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Attention Queue
# ---------------------------------------------------------------------------

_ALLOWED_CATEGORIES = frozenset({
    "blocking_signal",
    "director_blocker",
    "launch_blocker",
    "pending_approval",
    "missing_credential",
    "untested_credential",
    "recent_failures",
})


def _item(
    priority: int,
    level: str,
    category: str,
    title: str,
    why: str,
    evidence: str,
    action_url: str,
    action_label: str,
    investigate_url: str,
    investigate_label: str,
) -> dict:
    """Construct a decision card item. All fields are required strings."""
    assert category in _ALLOWED_CATEGORIES, f"unknown category: {category}"
    return {
        "priority":          priority,
        "level":             level,
        "category":          category,
        "title":             title,
        "why":               why,
        "evidence":          evidence,
        "action_url":        action_url,
        "action_label":      action_label,
        "investigate_url":   investigate_url,
        "investigate_label": investigate_label,
    }


def _build_attention_queue(
    factory_status:    dict,
    providers:         list[dict],
    run_state:         dict,
    episode_readiness: dict,
) -> list[dict]:
    """
    Build an ordered list of decision card items requiring operator action.
    Items come exclusively from real DB state — no mock items, no analytics.
    Priority: 1=critical, 2=high, 3=medium, 4=low.

    Each item shape:
        priority, level, category, title, why, evidence,
        action_url, action_label, investigate_url, investigate_label
    """
    items: list[dict] = []
    seen_evidence: set[str] = set()

    def _add(item: dict) -> None:
        key = f"{item['category']}:{item['evidence']}"
        if key not in seen_evidence:
            seen_evidence.add(key)
            items.append(item)

    # 1. Blocking OIS signals (critical)
    for sig in run_state.get("blockers", []):
        sig_type = sig.get("signal_type", "unknown signal")
        msg = sig.get("message", "") or sig_type
        _add(_item(
            priority=1, level="critical", category="blocking_signal",
            title=f"Factory blocked — {sig_type}",
            why="This OIS signal prevents new episode runs from starting until resolved.",
            evidence=msg[:200],
            action_url="/signals", action_label="Resolve signal",
            investigate_url="/signals", investigate_label="View all signals",
        ))

    # 2. Factory Director blockers (critical)
    for reason in factory_status.get("blocking_reasons", [])[:3]:
        _add(_item(
            priority=1, level="critical", category="director_blocker",
            title="Factory Director: run blocked",
            why="The Factory Director has identified a configuration issue preventing new runs.",
            evidence=reason[:200],
            action_url="/preflight", action_label="Run preflight audit",
            investigate_url="/signals", investigate_label="View OIS signals",
        ))

    # 3. Launch blockers not already shown (critical)
    existing_evidence = {i["evidence"] for i in items}
    if not episode_readiness.get("can_launch"):
        for b in episode_readiness.get("blockers", [])[:3]:
            if b[:200] not in existing_evidence:
                _add(_item(
                    priority=1, level="critical", category="launch_blocker",
                    title="Launch blocked",
                    why="The next episode cannot be launched until this is resolved.",
                    evidence=b[:200],
                    action_url="/preflight", action_label="View preflight",
                    investigate_url="/runs", investigate_label="View runs",
                ))

    # 4. Recent run failures (high — if ≥2 of the last 5 failed)
    failures = run_state.get("recent_failures", [])
    if len(failures) >= 2:
        sample = ", ".join(r.get("episode_id", "?") for r in failures[:3])
        _add(_item(
            priority=2, level="high", category="recent_failures",
            title=f"{len(failures)} recent run failures",
            why="Multiple recent failures may indicate a systemic provider or config issue.",
            evidence=f"Failed episodes: {sample}",
            action_url="/failures", action_label="View failures",
            investigate_url="/signals", investigate_label="Check OIS signals",
        ))

    # 5. Pending approvals (high)
    approval_count = episode_readiness.get("pending_approval_count", 0)
    if approval_count > 0:
        noun = "approval" if approval_count == 1 else "approvals"
        _add(_item(
            priority=2, level="high", category="pending_approval",
            title=f"{approval_count} {noun} pending",
            why="Runs are paused waiting for operator review before they can proceed.",
            evidence=f"{approval_count} run(s) in awaiting_review or awaiting_publish_approval state",
            action_url="/approvals", action_label="Review approvals",
            investigate_url="/runs", investigate_label="View runs",
        ))

    # 6. Missing required credentials (medium)
    for p in providers:
        if p.get("status") == "missing" and p.get("required"):
            _add(_item(
                priority=3, level="medium", category="missing_credential",
                title=f"Required credential missing: {p['label']}",
                why=f"{p['label']} is required for episode production. Without it, runs will fail at the first API call.",
                evidence=p.get("note", "No secret profile found"),
                action_url="/preflight", action_label="Configure credential",
                investigate_url="/preflight", investigate_label="View preflight audit",
            ))

    # 7. Missing optional credentials (low)
    for p in providers:
        if p.get("status") == "missing" and not p.get("required"):
            _add(_item(
                priority=4, level="low", category="missing_credential",
                title=f"Optional credential not configured: {p['label']}",
                why=f"{p['label']} is optional but enables additional pipeline capabilities.",
                evidence=p.get("note", "No secret profile found"),
                action_url="/preflight", action_label="Configure credential",
                investigate_url="/preflight", investigate_label="View preflight audit",
            ))

    # 8. Untested credentials (low)
    for p in providers:
        if p.get("status") == "untested":
            _add(_item(
                priority=4, level="low", category="untested_credential",
                title=f"Credential untested: {p['label']}",
                why="Untested credentials may fail during a live run. Verify before launching.",
                evidence=p.get("note", "Last test date unknown"),
                action_url="/preflight", action_label="View preflight",
                investigate_url="/preflight", investigate_label="Preflight audit",
            ))

    items.sort(key=lambda x: x["priority"])
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_command_state(db_path: Optional[Path] = None) -> dict:
    """
    Assemble a real operational state snapshot for the Command Center.

    All sub-queries are non-blocking. A partial failure in any section
    returns an empty/safe section rather than aborting the snapshot.

    No external API calls. No mock data. No publishing. Read-only.

    Returns a dict with keys:
        checked_at, factory_status, providers, run_state,
        episode_readiness, attention_queue
    """
    db = _resolve_db(db_path)

    # Ensure schema exists (idempotent)
    try:
        initialize_schema(db)
    except Exception:
        pass

    factory_status    = _get_factory_status(db)
    providers         = _get_provider_status(db)
    run_state         = _get_run_state(db)
    episode_readiness = _get_episode_readiness(db, factory_status)
    attention_queue   = _build_attention_queue(
        factory_status, providers, run_state, episode_readiness
    )

    return {
        "checked_at":        _now_iso(),
        "factory_status":    factory_status,
        "providers":         providers,
        "run_state":         run_state,
        "episode_readiness": episode_readiness,
        "attention_queue":   attention_queue,
    }
