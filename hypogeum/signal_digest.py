"""
hypogeum/signal_digest.py — operator-readable presentation for OIS signals.

The raw signal feed speaks in internal identifiers ("Run '72e49c5a-…'
transitioned to 'completed'"). A channel owner reading the dashboard cannot
tell noise from a real problem, nor what a "Resolve" click will do.

This module turns each open signal into three plain-language fields —
a title, a one-sentence explanation (with episode *titles*, not ids), and
an optional call-to-action — and classifies it:

    kind = "notice"     purely informational; safe to clear, no decision
    kind = "attention"  a real condition the operator should act on
    kind = "stale"      tied to an episode that is already published — the
                        condition no longer matters and is auto-resolved

`auto_resolve_stale_signals()` clears the notice/stale buckets server-side
so the panel only ever shows things that need a human.

Public API:
    present_signal(sig, *, db_path=None)      -> dict (adds title/explain/kind/cta)
    digest_signals(signals, *, db_path=None)  -> {"attention": [...], "notices": [...]}
    auto_resolve_stale_signals(db_path=None)  -> int  (count resolved)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection as _get_connection
from hypogeum.logger import get_logger

log = get_logger()


def get_connection(db_path):
    """get_connection that tolerates a str / None db_path."""
    if isinstance(db_path, str):
        db_path = Path(db_path)
    return _get_connection(db_path)


# signal_type → how to say it to a human.
#   cta: (label, url) or None
_PRESENTATION: dict[str, dict] = {
    "run_completed": {
        "title": "Production run finished",
        "plain": "An episode run completed. This is a status note — nothing to do.",
        "kind": "notice",
    },
    "approval_granted": {
        "title": "Approval recorded",
        "plain": "A run that was waiting for sign-off has been released. Status note only.",
        "kind": "notice",
    },
    "run_started": {
        "title": "Production run started",
        "plain": "A new episode run began. Status note only.",
        "kind": "notice",
    },
    "approval_pending": {
        "title": "Waiting for your review",
        "plain": "A run is paused until someone approves or rejects it.",
        "kind": "attention",
        "cta": ("Review", "/approvals"),
    },
    "run_failed": {
        "title": "A production run failed",
        "plain": "A run stopped before finishing. Open it to see why and decide whether to retry.",
        "kind": "attention",
        "cta": ("Open failed runs", "/runs?status=failed"),
    },
    "retry_exhausted": {
        "title": "A run stopped retrying",
        "plain": "A queued episode used up its automatic retries and will not retry on its own.",
        "kind": "attention",
        "cta": ("Open failed runs", "/runs?status=failed"),
    },
    "startup_blocked": {
        "title": "Worker started with an unresolved check",
        "plain": (
            "When the worker last started, a preflight check was still failing. "
            "If the factory is producing normally now, this is safe to clear."
        ),
        "kind": "attention",
        "cta": ("Open checklist", "/preflight"),
    },
    "credential_failure": {
        "title": "A service connection failed",
        "plain": (
            "A required service (an AI model, a content provider, or a platform) "
            "could not be reached with the current credentials."
        ),
        "kind": "attention",
        "cta": ("Open providers", "/providers"),
    },
    "provider_failure": {
        "title": "A content provider is not responding",
        "plain": "Video or image generation calls to a provider are failing.",
        "kind": "attention",
        "cta": ("Open providers", "/providers"),
    },
    "provider_failure_spike": {
        "title": "A content provider is failing often",
        "plain": "A provider is returning errors at a high rate — production may stall.",
        "kind": "attention",
        "cta": ("Open providers", "/providers"),
    },
    "backup_unhealthy": {
        "title": "Database backup has not run",
        "plain": (
            "No healthy backup of the factory database exists yet. Set one up so "
            "work can be restored if something breaks."
        ),
        "kind": "attention",
        "cta": ("Backup settings", "/infrastructure"),
    },
    "budget_exhaustion": {
        "title": "Monthly AI budget used up",
        "plain": "Production is paused until the budget resets or the ceiling is raised.",
        "kind": "attention",
        "cta": ("Open budget", "/advanced"),
    },
    "budget_blocked": {
        "title": "Monthly AI budget used up",
        "plain": "Production is paused until the budget resets or the ceiling is raised.",
        "kind": "attention",
        "cta": ("Open budget", "/advanced"),
    },
    "low_budget_warning": {
        "title": "AI budget running low",
        "plain": "The monthly budget is close to its ceiling.",
        "kind": "attention",
        "cta": ("Open budget", "/advanced"),
    },
    "cost_spike": {
        "title": "Unusual cost spike",
        "plain": "Recent spend is well above the normal rate for an episode.",
        "kind": "attention",
        "cta": ("Open budget", "/advanced"),
    },
    "archive_staleness": {
        "title": "Content archive stopped growing",
        "plain": "No new scenes have been catalogued recently — production may have stalled.",
        "kind": "attention",
        "cta": ("Open runs", "/runs"),
    },
    "acceptance_audit_failed": {
        "title": "Factory not cleared for production",
        "plain": "The readiness audit is failing. New runs will be blocked until it passes.",
        "kind": "attention",
        "cta": ("Open checklist", "/preflight"),
    },
    "gate_failure": {
        "title": "A brand-compliance check failed",
        "plain": "A run was held back by a quality or brand gate.",
        "kind": "attention",
        "cta": ("Open failed runs", "/runs?status=failed"),
    },
}

_FALLBACK = {
    "title": None,          # filled from signal_type
    "plain": "",
    "kind": "attention",
    "cta": None,
}

# Signal types that are informational by nature regardless of severity.
_NOTICE_TYPES = {"run_completed", "run_started", "approval_granted"}


def _episode_label(conn, episode_id: Optional[str]) -> Optional[str]:
    """A human title for an episode id, or None."""
    if not episode_id:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(NULLIF(title,''), NULLIF(topic,'')) AS label "
            "FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if row and row["label"]:
            return str(row["label"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _published_episode_ids(conn) -> set:
    """Episode ids that have reached a published/succeeded state — signals
    about these describe history, not a live problem."""
    ids: set = set()
    for sql in (
        "SELECT episode_id FROM episodes "
        "WHERE status IN ('complete','completed','published')",
        "SELECT DISTINCT episode_id FROM publication_intents WHERE status = 'succeeded'",
    ):
        try:
            ids.update(r[0] for r in conn.execute(sql) if r[0])
        except Exception:  # noqa: BLE001
            continue
    return ids


def present_signal(sig: dict, *, db_path: Optional[Path] = None, _conn=None,
                   _published: Optional[set] = None) -> dict:
    """Return `sig` enriched with title / explain / kind / cta / episode_label."""
    own_conn = _conn is None
    conn = _conn or get_connection(db_path)
    try:
        published = _published if _published is not None else _published_episode_ids(conn)
        stype = sig.get("signal_type") or ""
        spec = _PRESENTATION.get(stype, _FALLBACK)
        ep_id = sig.get("episode_id")
        ep_label = _episode_label(conn, ep_id)

        kind = spec["kind"]
        if stype in _NOTICE_TYPES or (sig.get("severity") or "").upper() == "INFO":
            kind = "notice"
        if ep_id and ep_id in published and kind != "notice":
            kind = "stale"

        title = spec["title"] or stype.replace("_", " ").capitalize()
        plain = spec["plain"]
        if kind == "stale":
            subject = f"“{ep_label}”" if ep_label else "an episode"
            plain = f"This was about {subject}, which is already published. Safe to clear."
        elif ep_label:
            plain = f"{plain} (episode: “{ep_label}”)".strip()

        cta = spec.get("cta")
        out = dict(sig)
        out.update({
            "title": title,
            "explain": plain,
            "kind": kind,
            "cta_label": cta[0] if cta else None,
            "cta_url": cta[1] if cta else None,
            "episode_label": ep_label,
        })
        return out
    finally:
        if own_conn:
            conn.close()


def digest_signals(signals: list, *, db_path: Optional[Path] = None) -> dict:
    """Split a list of raw open signals into {"attention": [...], "notices": [...]}.

    "notices" holds informational + stale items (a single "clear all" in the UI).
    "attention" holds everything that needs a human decision, most severe first.
    """
    conn = get_connection(db_path)
    try:
        published = _published_episode_ids(conn)
        presented = [
            present_signal(s, _conn=conn, _published=published) for s in (signals or [])
        ]
    finally:
        conn.close()

    sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    attention = [p for p in presented if p["kind"] == "attention"]
    attention.sort(key=lambda p: sev_rank.get((p.get("severity") or "").upper(), 3))
    notices = [p for p in presented if p["kind"] in ("notice", "stale")]
    return {"attention": attention, "notices": notices}


def auto_resolve_stale_signals(db_path: Optional[Path] = None) -> int:
    """Auto-resolve open signals that no longer need a human:
      - informational types (run_completed / run_started / approval_granted)
      - any INFO-severity signal
      - any signal tied to an already-published episode

    Returns the number resolved. Never raises.
    """
    from hypogeum.ois import auto_resolve_signal
    try:
        conn = get_connection(db_path)
        try:
            published = _published_episode_ids(conn)
            rows = conn.execute(
                "SELECT signal_id, signal_type, severity, episode_id "
                "FROM signal_records WHERE lifecycle_status = 'open'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0

    # A stuck_run signal is stale the moment the stuck-run check goes
    # healthy again — otherwise every 15-minute health sweep piles a fresh
    # identical one on top of the last.
    _stuck_cleared = False
    if any((dict(r).get("signal_type") or "") == "stuck_run" for r in rows):
        try:
            from hypogeum.observability import check_stuck_runs
            _stuck_cleared = bool(getattr(check_stuck_runs(db_path=db_path), "healthy", False))
        except Exception:  # noqa: BLE001
            _stuck_cleared = False

    resolved = 0
    for r in rows:
        d = dict(r)
        stype = d.get("signal_type") or ""
        sev = (d.get("severity") or "").upper()
        ep = d.get("episode_id")
        reason = None
        if stype in _NOTICE_TYPES or sev == "INFO":
            reason = "informational"
        elif ep and ep in published:
            reason = "episode_published"
        elif stype == "stuck_run" and _stuck_cleared:
            reason = "stuck_run_cleared"
        if reason and auto_resolve_signal(d["signal_id"], reason, db_path=db_path):
            resolved += 1
    if resolved:
        log.info("signals_auto_resolved", count=resolved)
    return resolved
