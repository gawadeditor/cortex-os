"""hypogeum.workspace_keys — multi-tenant Phase 2: spend with the owning
workspace's keys, never another workspace's."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hypogeum import workspace_keys as wk
from hypogeum import workspaces as ws
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory

ROOT = Path(__file__).resolve().parent.parent


def _cfg():
    return SimpleNamespace(OPENROUTER_API_KEY="env-or", MUAPI_API_KEY="env-mu", ELEVENLABS_API_KEY="env-el",
                           PERPLEXITY_API_KEY="env-px", ANTHROPIC_API_KEY="env-an", OTHER="keep")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    return p


def test_single_tenant_ownerless_channel_unchanged(db):
    cfg = _cfg()
    assert wk.config_for_channel(cfg, "nobody", db) is cfg


def test_platform_channel_env_key_wins_over_vault(db):
    """2026-10-04: the platform vault held older keys than .env — .env must
    stay the operator's live source; the vault only fills an empty field."""
    ws.assign("channel", "mine", "op", db_path=db)
    out = wk.config_for_channel(_cfg(), "mine", db)
    assert out.OPENROUTER_API_KEY == "env-or" and out.OTHER == "keep"
    create_secret_profile("muapi", "stale op muapi", "api_key", "vault-mu", workspace_id="op", db_path=db)
    assert wk.config_for_channel(_cfg(), "mine", db).MUAPI_API_KEY == "env-mu"
    cfg = _cfg()
    cfg.MUAPI_API_KEY = None
    assert wk.config_for_channel(cfg, "mine", db).MUAPI_API_KEY == "vault-mu"


def test_client_channel_gets_only_its_own_keys(db):
    ws.create_workspace("Acme", workspace_id="acme", db_path=db)
    ws.assign("channel", "acme_ch", "acme", db_path=db)
    create_secret_profile("openrouter", "acme or", "api_key", "acme-or", workspace_id="acme", db_path=db)
    cfg = _cfg()
    out = wk.config_for_channel(cfg, "acme_ch", db)
    assert out.OPENROUTER_API_KEY == "acme-or"
    for f in ("MUAPI_API_KEY", "ELEVENLABS_API_KEY", "PERPLEXITY_API_KEY", "ANTHROPIC_API_KEY"):
        assert getattr(out, f) is None, f          # missing -> fail closed, never env-*
    assert cfg.OPENROUTER_API_KEY == "env-or"      # caller's config untouched (copy)


def test_another_workspaces_key_is_never_used(db):
    ws.create_workspace("Acme", workspace_id="acme", db_path=db)
    ws.create_workspace("Beta", workspace_id="beta", db_path=db)
    ws.assign("channel", "beta_ch", "beta", db_path=db)
    create_secret_profile("openrouter", "acme or", "api_key", "acme-or", workspace_id="acme", db_path=db)
    assert wk.config_for_channel(_cfg(), "beta_ch", db).OPENROUTER_API_KEY is None


def test_ownerless_channel_refused_once_clients_exist(db):
    ws.create_workspace("Acme", workspace_id="acme", db_path=db)
    with pytest.raises(wk.OwnerlessChannelError):
        wk.config_for_channel(_cfg(), "nobody", db)
    with pytest.raises(wk.OwnerlessChannelError):
        wk.config_for_channel(_cfg(), None, db)


def test_every_gate_adapter_resolves_keys_through_the_envelope_channel():
    """Guard: a provider adapter that builds a fresh Config() must wrap it in
    config_for_channel(..., ctx.envelope.channel_id, ...) — otherwise it
    would spend the .env (operator) keys for every workspace."""
    offenders = []
    for p in sorted((ROOT / "hypogeum" / "providers").glob("*_adapter.py")):
        src = p.read_text()
        bare = [m.start() for m in re.finditer(r"Config\(\)", src)
                if "config_for_channel(Config(), ctx.envelope.channel_id" not in src[max(0, m.start() - 40):m.start() + 60]]
        if bare:
            offenders.append(p.name)
    assert not offenders, offenders
