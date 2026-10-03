"""
hypogeum/publishing/postiz_cutover.py — move a channel's platforms between
Zernio and Postiz, one platform at a time, reversibly.

Routing is decided only by channel_provider_bindings (see
zernio_publish_runtime._route_platforms_by_binding), so a cutover is:

  register_postiz_accounts()   provider_profile + one provider_account per
                               Postiz integration, matched to a channel by
                               its handle. Routes nothing on its own.
  switch_platform(.., "postiz") disable the active Zernio binding and bind
                               the channel's Postiz account instead.
  switch_platform(.., "zernio") the reverse: re-bind the Zernio account the
                               channel used most recently for that platform.

Every switch is all-or-nothing per platform and refuses when the target
account is missing — never leaves a platform with no enabled binding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    disable_binding,
    list_bindings,
    resolve_active_binding,
    upsert_provider_account,
    upsert_provider_profile,
)

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")

# Postiz integration -> Cortex channel, matched on the integration's own
# handle (`profile`), or its display name where Postiz has no handle
# (Facebook Pages). Verified against GET /integrations on 2026-10-03.
CHANNEL_HANDLES: Dict[str, Dict[str, str]] = {
    "hypogeum": {
        "youtube": "@thehypogeum", "instagram": "thehypogeum",
        "tiktok": "thehypogeum1", "facebook": "The Hypogeum",
    },
    "twinkle_mini_tales": {
        "youtube": "@twinkle_mini_tales", "instagram": "twinkle_mini_tales",
        "tiktok": "twinkle_mini_tale", "facebook": "twinkleminitales",
    },
}


class CutoverError(RuntimeError):
    pass


def match_integrations(integrations: List[dict]) -> Dict[tuple, dict]:
    """{(channel_id, platform): integration} for every handle in
    CHANNEL_HANDLES. Raises if a handle matches zero or several integrations,
    so a renamed/duplicated account can never be bound silently."""
    out: Dict[tuple, dict] = {}
    for channel_id, handles in CHANNEL_HANDLES.items():
        for platform, handle in handles.items():
            hits = [i for i in integrations
                    if i.get("identifier") == platform and not i.get("disabled")
                    and handle in (i.get("profile"), i.get("name"))]
            if len(hits) != 1:
                raise CutoverError(
                    f"{channel_id}/{platform}: expected exactly one Postiz integration with "
                    f"handle {handle!r}, found {len(hits)}"
                )
            out[(channel_id, platform)] = hits[0]
    return out


def register_postiz_accounts(integrations: List[dict], *, db_path: Optional[Path] = None,
                             organization_id: str = "postiz") -> Dict[tuple, dict]:
    """Upsert one postiz provider_profile and a provider_account per matched
    integration (external_account_id = Postiz integration id). Idempotent.
    Creates no binding — routing is unchanged afterwards."""
    matched = match_integrations(integrations)
    profile = upsert_provider_profile(db_path, provider_id="postiz", external_profile_id=organization_id,
                                      display_label="Postiz (self-hosted)")
    accounts = {}
    for (channel_id, platform), integ in matched.items():
        accounts[(channel_id, platform)] = upsert_provider_account(
            db_path, provider_id="postiz", platform=platform, external_account_id=integ["id"],
            provider_profile_id=profile["provider_profile_id"],
            display_label=f"{channel_id} {integ.get('profile') or integ.get('name')}",
            status="active", needs_reconnection=bool(integ.get("refreshNeeded")),
            metadata={"channel_id": channel_id},
        )
    return accounts


def _accounts_for(db_path, *, provider_id: str, platform: str) -> List[dict]:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM provider_accounts WHERE provider_id = ? AND platform = ?",
            (provider_id, platform),
        )]
    finally:
        conn.close()


def _target_account(db_path, channel_id: str, platform: str, to_provider: str) -> dict:
    if to_provider == "postiz":
        import json
        for acc in _accounts_for(db_path, provider_id="postiz", platform=platform):
            meta = json.loads(acc.get("metadata_json") or "{}") if acc.get("metadata_json") else {}
            if meta.get("channel_id") == channel_id:
                return acc
        raise CutoverError(f"no registered Postiz account for {channel_id}/{platform} — run register first")
    # back to zernio: the account this channel's most recent zernio binding used
    history = [b for b in list_bindings(db_path, channel_id=channel_id, capability="publishing", platform=platform)
               if b["provider_id"] == "zernio"]
    if not history:
        raise CutoverError(f"{channel_id}/{platform} has never had a Zernio binding to return to")
    latest = max(history, key=lambda b: b["updated_at"])
    return {"provider_account_id": latest["provider_account_id"],
            "provider_profile_id": latest["provider_profile_id"]}


def switch_platform(channel_id: str, platform: str, to_provider: str, *,
                    db_path: Optional[Path] = None, logical_alias: str = "default") -> dict:
    """Move one (channel, platform) publishing binding to `to_provider`.
    No-op if already there. Returns {"from", "to", "binding"}."""
    if to_provider not in ("postiz", "zernio"):
        raise CutoverError("to_provider must be 'postiz' or 'zernio'")
    if platform not in PLATFORMS:
        raise CutoverError(f"unknown platform {platform!r}")
    current = resolve_active_binding(db_path, channel_id=channel_id, capability="publishing",
                                     platform=platform, logical_alias=logical_alias)
    if current and current["provider_id"] == to_provider:
        return {"from": to_provider, "to": to_provider, "binding": current}
    target = _target_account(db_path, channel_id, platform, to_provider)  # raises before any change
    if current:
        disable_binding(db_path, binding_id=current["binding_id"])
    try:
        binding = bind_channel_provider(
            db_path, channel_id=channel_id, capability="publishing", platform=platform,
            provider_id=to_provider, provider_profile_id=target["provider_profile_id"],
            provider_account_id=target["provider_account_id"], logical_alias=logical_alias,
        )
    except Exception:
        # never leave the platform unbound: restore what was there
        if current:
            _reenable(db_path, current["binding_id"])
        raise
    return {"from": current["provider_id"] if current else None, "to": to_provider, "binding": binding}


def _reenable(db_path, binding_id: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE channel_provider_bindings SET enabled = 1 WHERE binding_id = ?", (binding_id,))
        conn.commit()
    finally:
        conn.close()


def status(db_path: Optional[Path] = None) -> Dict[str, Dict[str, Optional[str]]]:
    """{channel_id: {platform: provider_id or None}} for every channel in
    CHANNEL_HANDLES — what the publishing runtime will route to right now."""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for channel_id in CHANNEL_HANDLES:
        out[channel_id] = {}
        for platform in PLATFORMS:
            b = resolve_active_binding(db_path, channel_id=channel_id, capability="publishing",
                                       platform=platform)
            out[channel_id][platform] = b["provider_id"] if b else None
    return out
