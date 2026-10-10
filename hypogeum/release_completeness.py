"""
hypogeum/release_completeness.py — is an episode's release to ITS target platforms complete?

An episode can be booked on some platforms and not others (a provider outage, a
rejected post, missing metadata ...). Two things need to know:

  * the archive relocation must not move an episode's files to the NAS while
    platforms are still to be released -- the release needs those files
    (2026-10-10: ep_4c324f61 and ep_014876fc each lost their local folder after the
    first platform succeeded, and every remaining leg sat in blocked_metadata);
  * the operator must be told when a release stays partial.

    incomplete_release(episode_id, channel_id, *, db_path=None, now=None) -> dict | None

returns None when the release is complete or nothing was released yet; otherwise
{"succeeded": [...], "missing": [...], "first_succeeded_at": datetime, "age_s": float,
 "last_errors": {platform: message}}.

Targets are the channel's targetable platforms (zernio_channel_release_trigger
._resolve_target_platforms) narrowed by the channel's platform_deliverable_policy for
the deliverables this episode actually has. Fail-open: any lookup problem returns None
(never blocks anything because of a bug here).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def incomplete_release(
    episode_id: str, channel_id: str, *, db_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> Optional[dict]:
    now = now or datetime.now(timezone.utc)
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
        from hypogeum.publishing.deliverable_ids import classify_deliverable_class  # noqa: PLC0415
        from hypogeum.publishing.zernio_channel_release_trigger import (  # noqa: PLC0415
            _resolve_platform_deliverable_policy, _resolve_target_platforms,
        )
        dbp = Path(db_path) if db_path else None
        conn = get_connection(dbp)
        try:
            intents = conn.execute(
                "SELECT platform, deliverable_id, status, updated_at FROM publication_intents "
                "WHERE episode_id = ? AND channel_id = ?", (episode_id, channel_id)).fetchall()
            errors = {}
            for r in conn.execute(
                    "SELECT platform, last_error_message FROM publishing_attempts WHERE episode_id = ? AND channel_id = ? "
                    "ORDER BY created_at ASC", (episode_id, channel_id)).fetchall():
                errors[r[0]] = r[1]
        finally:
            conn.close()
        succeeded = {r["platform"] for r in intents if r["status"] == "succeeded"}
        if not succeeded:
            return None                         # nothing released yet: not a partial release
        first = min((t for t in (_parse(r["updated_at"]) for r in intents if r["status"] == "succeeded") if t), default=None)
        if first is None:
            return None
        classes = {classify_deliverable_class(r["deliverable_id"]) for r in intents} or {"main_episode"}
        policy = _resolve_platform_deliverable_policy(channel_id, dbp) or {}
        missing = []
        for platform in _resolve_target_platforms(channel_id, dbp):
            if platform in succeeded:
                continue
            if policy and not any(bool(((policy.get(platform) or {}).get(c) or {}).get("enabled")) for c in classes):
                continue                         # this platform is not meant to carry this deliverable
            missing.append(platform)
        if not missing:
            return None
        return {
            "succeeded": sorted(succeeded), "missing": sorted(missing), "first_succeeded_at": first,
            "age_s": (now - first).total_seconds(),
            "last_errors": {p: (errors.get(p) or "") for p in missing},
        }
    except Exception:  # noqa: BLE001 — fail open
        return None
