"""
hypogeum/workspace_keys.py — every spending call uses the keys (and so the
balance) of the Workspace that owns the channel. Multi-tenant plan, Phase 2
(docs/plans/MULTI_TENANT_PLAN.md).

    config_for_channel(config, channel_id, db_path=None) -> Config
    config_for_workspace(config, workspace_id, db_path=None) -> Config
    config_for_client(config, client_id, db_path=None) -> Config   # Cortex Advisory
    publishing_key(provider, *, channel_id=None, workspace_id=None, db_path=None)  # Phase 5
    workspaces_with_key(provider, db_path=None) -> list[(workspace_id, key)]
    workspace_key(provider, workspace_id, db_path=None) -> Optional[str]

Both return a shallow COPY of `config` with the provider-key fields replaced;
everything else (models, flags, tiers) is untouched, and the copy is passed
down the existing call chain exactly as `config` already is. The gate
adapters (hypogeum/providers/*_adapter.py) apply config_for_channel() with
the channel of the VERIFIED envelope they are invoked for, so the key used
on the wire is bound to the signed authorization, not to caller input.

Resolution, per provider key field:
  * platform-owner workspace: the .env value already on `config` whenever it
    is set (today's behaviour, unchanged), its vault key only for a field
    .env leaves empty. Deliberately .env-first: on 2026-10-04 the platform's
    vault held OLDER openrouter/muapi/elevenlabs keys than .env (openrouter
    …755a vs the funded …0567) — vault-first would have silently switched
    production to them. Moving the operator to vault keys is a separate,
    explicit step.
  * client workspace: its vault key ONLY. Missing -> the field is None and
    the provider call fails ("key not configured"). Never the operator's.
  * channel with no owner: if the install has no client workspaces it is a
    single-tenant install and `config` is returned unchanged; once any client
    workspace exists it raises OwnerlessChannelError (fail closed).

Vault keys are secret_profiles rows (provider = 'openrouter' | 'muapi' |
'elevenlabs' | 'perplexity' | 'anthropic', status 'active') assigned to the
workspace in workspace_assignments (hypogeum.workspaces).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="workspace_keys")

# provider name in secret_profiles -> Config attribute it feeds
PROVIDER_FIELDS = {
    "openrouter": "OPENROUTER_API_KEY",
    "muapi": "MUAPI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class OwnerlessChannelError(RuntimeError):
    """A spending call for a channel no workspace owns, on an install that
    has client workspaces — refusing rather than guessing whose balance."""


def _p(db_path):
    """One explicit DB for every lookup here: credential_manager resolves
    db_path=None to its own import-time constant while workspaces follows
    hypogeum.db._DEFAULT_DB_PATH — mixing the two could match one DB's
    ownership against another DB's keys."""
    if db_path:
        return Path(db_path)
    import hypogeum.db as _db
    return Path(_db._DEFAULT_DB_PATH)


def workspace_key(provider: str, workspace_id: str, db_path=None) -> Optional[str]:
    """The workspace's own active vault key for `provider`, else None."""
    from hypogeum.credential_manager import list_secret_profiles, resolve_credential
    from hypogeum.workspaces import resources_of
    owned = set(resources_of(workspace_id, "secret_profile", db_path=_p(db_path)))
    if not owned:
        return None
    for prof in list_secret_profiles(provider=provider, db_path=_p(db_path)):
        if prof.status == "active" and prof.secret_profile_id in owned:
            return resolve_credential(prof.secret_profile_id, db_path=_p(db_path))
    return None


def _has_client_workspaces(db_path) -> bool:
    from hypogeum.workspaces import list_workspaces
    try:
        return any(w["kind"] == "client" for w in list_workspaces(_p(db_path)))
    except Exception:  # noqa: BLE001 — no factories table: single-tenant/test install
        return False


def config_for_workspace(config, workspace_id: str, db_path=None):
    from hypogeum.workspaces import platform_workspace_id
    is_platform = workspace_id == platform_workspace_id(_p(db_path))
    out = copy.copy(config)
    for provider, field in PROVIDER_FIELDS.items():
        if is_platform and getattr(config, field, None):
            continue   # operator: the live .env key wins (see module docstring)
        own = workspace_key(provider, workspace_id, db_path)
        # client: its own key or None (fail closed); platform with an empty
        # .env field: its vault key if it has one
        setattr(out, field, own if (own or not is_platform) else getattr(config, field, None))
    return out


def config_for_channel(config, channel_id: Optional[str], db_path=None):
    from hypogeum.workspaces import workspace_of
    if not channel_id:
        if _has_client_workspaces(db_path):
            raise OwnerlessChannelError("spending call with no channel on a multi-workspace install")
        return config
    ws = workspace_of(channel_id, db_path=_p(db_path))
    if ws is None:
        if _has_client_workspaces(db_path):
            log.error("ownerless_channel_spend_refused", channel_id=channel_id)
            raise OwnerlessChannelError(f"channel {channel_id!r} belongs to no workspace")
        return config
    return config_for_workspace(config, ws, db_path)


def config_for_client(config, client_id: Optional[str], db_path=None):
    """Cortex Advisory spend (content analysis, brand strategy/drafts) for a
    client uses the keys of the workspace that owns that client — same rules
    as config_for_channel()."""
    from hypogeum.workspaces import owner_of
    ws = owner_of("client", client_id, db_path=_p(db_path)) if client_id else None
    if ws is None:
        if _has_client_workspaces(db_path):
            log.error("ownerless_client_spend_refused", client_id=client_id)
            raise OwnerlessChannelError(f"client {client_id!r} belongs to no workspace")
        return config
    return config_for_workspace(config, ws, db_path)


# ---------------------------------------------------------------------------
# Phase 5: publishing-provider keys (zernio / postiz) — vault only, by owner
# ---------------------------------------------------------------------------

def publishing_key(provider: str, *, channel_id: Optional[str] = None, workspace_id: Optional[str] = None,
                   db_path=None) -> Optional[str]:
    """The active `provider` key (secret_profiles) owned by the workspace
    that owns `channel_id` — or `workspace_id` — or, with neither, the
    platform owner (operator-level calls: content grid, analytics, cover
    hold). Never another workspace's key: before 2026-10-05 the resolver
    took the first active profile, so a client's Zernio key could have been
    used for the operator's posts or vice versa. Ownerless channel on a
    multi-workspace install -> OwnerlessChannelError (fail closed)."""
    from hypogeum.workspaces import platform_workspace_id, workspace_of
    ws = workspace_id
    if ws is None and channel_id:
        ws = workspace_of(channel_id, db_path=_p(db_path))
        if ws is None and _has_client_workspaces(db_path):
            raise OwnerlessChannelError(f"channel {channel_id!r} belongs to no workspace")
    if ws is None:
        ws = platform_workspace_id(_p(db_path))
    if ws is None:   # install without workspaces at all: the single active key, as before
        from hypogeum.credential_manager import list_secret_profiles, resolve_credential
        active = [p for p in list_secret_profiles(provider=provider, scope="factory", db_path=_p(db_path))
                  if p.status == "active"]
        return resolve_credential(active[0].secret_profile_id, db_path=_p(db_path)) if active else None
    return workspace_key(provider, ws, db_path)


def workspaces_with_key(provider: str, db_path=None) -> list:
    """[(workspace_id, key)] for every workspace holding an active `provider`
    key — for account-wide syncs that must read each workspace's own account."""
    from hypogeum.workspaces import list_workspaces
    out = []
    try:
        workspaces = list_workspaces(_p(db_path))
    except Exception:  # noqa: BLE001
        workspaces = []
    for w in workspaces:
        k = workspace_key(provider, w["workspace_id"], db_path)
        if k:
            out.append((w["workspace_id"], k))
    if not workspaces:
        k = publishing_key(provider, db_path=db_path)
        if k:
            out.append((None, k))
    return out
