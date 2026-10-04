"""Multi-tenant Phase 5 — publishing keys (Zernio / Postiz) by owner, never
"the first active profile"."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hypogeum import workspace_keys as wk
from hypogeum import workspaces as ws
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    ws.assign("channel", "op_ch", "op", db_path=p)
    ws.create_workspace("Acme", workspace_id="acme", db_path=p)
    ws.assign("channel", "acme_ch", "acme", db_path=p)
    return p


def test_each_channel_gets_its_owners_zernio_key(db):
    # client key created FIRST, so a "first active profile" resolver would pick it
    create_secret_profile("zernio", "acme z", "api_key", "z-ACME", workspace_id="acme", db_path=db)
    create_secret_profile("zernio", "op z", "api_key", "z-OP", workspace_id="op", db_path=db)
    from hypogeum.publishing.zernio import _resolve_zernio_credential
    assert _resolve_zernio_credential(db, channel_id="acme_ch") == "z-ACME"
    assert _resolve_zernio_credential(db, channel_id="op_ch") == "z-OP"
    assert _resolve_zernio_credential(db) == "z-OP"                 # operator-level call
    from hypogeum.publishing.postiz import _resolve_postiz_credential
    assert _resolve_postiz_credential(db, channel_id="acme_ch") is None


def test_client_without_key_never_gets_operators(db):
    create_secret_profile("zernio", "op z", "api_key", "z-OP", workspace_id="op", db_path=db)
    from hypogeum.publishing.zernio import _resolve_zernio_credential
    assert _resolve_zernio_credential(db, channel_id="acme_ch") is None


def test_ownerless_channel_publishing_refused(db):
    create_secret_profile("zernio", "op z", "api_key", "z-OP", workspace_id="op", db_path=db)
    with pytest.raises(wk.OwnerlessChannelError):
        wk.publishing_key("zernio", channel_id="nobody", db_path=db)


def test_schedule_sync_reads_each_account_and_keeps_posts_in_their_workspace(db, monkeypatch):
    create_secret_profile("zernio", "op z", "api_key", "z-OP", workspace_id="op", db_path=db)
    create_secret_profile("zernio", "acme z", "api_key", "z-ACME", workspace_id="acme", db_path=db)
    import hypogeum.publishing.zernio_schedule_sync as sync
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    posts_by_key = {
        "Bearer z-OP": [{"_id": "p1", "title": "op ep", "scheduledFor": soon}],
        # a post on the CLIENT's account that would map to the OPERATOR's channel
        "Bearer z-ACME": [{"_id": "p2", "title": "acme ep", "scheduledFor": soon},
                          {"_id": "p3", "title": "op ep 2", "scheduledFor": soon}],
    }
    seen = []

    class T:
        def get(self, *, url, headers, timeout):
            seen.append(headers["Authorization"])
            return SimpleNamespace(status_code=200, json_body={"posts": posts_by_key[headers["Authorization"]]})
    mapping = {"op ep": ("op_ch", "ep_op"), "acme ep": ("acme_ch", "ep_acme"), "op ep 2": ("op_ch", "ep_x")}
    monkeypatch.setattr(sync, "_resolve_channel_and_episode", lambda conn, pid, title, p: mapping[title])
    monkeypatch.setattr(sync, "post_release_time", lambda p: p["scheduledFor"])
    out = sync.sync_zernio_schedule(db_path=db, transport=T())
    assert sorted(seen) == ["Bearer z-ACME", "Bearer z-OP"]
    assert out["ok"] and sorted(out["channels"]) == ["acme_ch", "op_ch"]
    assert out["unattributed"] == 1                   # p3: client account post claiming an operator channel
