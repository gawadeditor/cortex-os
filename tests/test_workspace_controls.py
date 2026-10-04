"""Multi-tenant Phase 3 — production pause and monthly budget per workspace.
One workspace's failures / overspend must never stop another's production."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hypogeum import workspace_controls as wsc
from hypogeum import workspaces as ws
from hypogeum.db import get_connection, get_worker_control, initialize_schema
from hypogeum.factory_entity import create_factory, update_factory
from hypogeum.queue import claim_next, enqueue

NOW = datetime.now(timezone.utc)


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


def _run(db, channel, status, minutes_ago, cost=0.0):
    rid = str(uuid.uuid4())
    started = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    conn = get_connection(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        data = {"run_id": rid, "episode_id": f"ep_{rid[:6]}", "status": status, "started_at": started,
                "channel_id": channel}
        if "created_at" in cols:
            data["created_at"] = started
        conn.execute(f"INSERT INTO runs ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values()))
        if cost:
            conn.execute("INSERT INTO costs (cost_id, run_id, episode_id, provider, model, call_type, "
                         "tokens_or_credits, cost_usd, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (str(uuid.uuid4()), rid, data["episode_id"], "muapi", "m", "scene", 0, cost, started))
        conn.commit()
    finally:
        conn.close()


def test_claim_skips_paused_workspace_and_takes_the_others(db):
    enqueue(db_path=db, channel_id="acme_ch", priority=0)
    enqueue(db_path=db, channel_id="op_ch", priority=5)
    wsc.pause("acme", "test", db_path=db)
    got = claim_next("w1", db_path=db)
    assert got and got["channel_id"] == "op_ch"
    assert claim_next("w1", db_path=db) is None          # acme item stays queued
    wsc.resume("acme", db_path=db)
    assert claim_next("w1", db_path=db)["channel_id"] == "acme_ch"


def test_monthly_budget_is_per_workspace(db):
    update_factory("acme", db_path=db, factory_policies={"monthly_ceiling_usd": 10.0})
    _run(db, "acme_ch", "complete", 30, cost=25.0)        # acme over its own ceiling
    _run(db, "op_ch", "complete", 30, cost=3.0)
    assert wsc.month_spend("acme", db) == 25.0
    assert wsc.month_spend("op", db) == 3.0               # client spend not counted against operator
    assert set(wsc.blocked_channels(db)) == {"acme_ch"}
    enqueue(db_path=db, channel_id="acme_ch")
    enqueue(db_path=db, channel_id="op_ch")
    assert claim_next("w1", db_path=db)["channel_id"] == "op_ch"
    assert claim_next("w1", db_path=db) is None


def test_breaker_pauses_only_the_failing_workspace(db):
    from hypogeum.muapi_spend_breaker import check_muapi_spend_breaker
    for m in (10, 20, 30):
        _run(db, "acme_ch", "failed", m)
    _run(db, "op_ch", "complete", 5)
    cfg = SimpleNamespace(MUAPI_HOURLY_SPEND_LIMIT_USD=0, MUAPI_CONSECUTIVE_FAILURE_LIMIT=3)
    out = check_muapi_spend_breaker(config=cfg, db_path=db, now=NOW)
    assert out["tripped_workspaces"] == ["acme"]
    assert wsc.is_paused("acme", db) and not wsc.is_paused("op", db)
    assert get_worker_control("queue_paused", "0", db_path=db) == "0"   # global switch untouched


def test_operator_resume_never_resumes_a_client(db):
    from hypogeum.muapi_spend_breaker import clear_breaker
    wsc.pause("acme", "client failure", db_path=db)
    wsc.pause("op", "operator failure", db_path=db)
    clear_breaker(db_path=db)
    assert not wsc.is_paused("op", db) and wsc.is_paused("acme", db)
    clear_breaker(db_path=db, workspace_id="acme")
    assert not wsc.is_paused("acme", db)


def test_operator_production_paused_reflects_own_workspace(db):
    assert not wsc.operator_production_paused(db)
    wsc.pause("acme", "x", db_path=db)
    assert not wsc.operator_production_paused(db)
    wsc.pause("op", "x", db_path=db)
    assert wsc.operator_production_paused(db)


def test_single_tenant_budget_stays_in_global_gate(tmp_path, monkeypatch):
    """No client workspaces: blocked_channels never blocks on budget (the
    global gate keeps doing it, with its validation_mode exemption)."""
    import hypogeum.db as hdb
    p = tmp_path / "s.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    update_factory("op", db_path=p, factory_policies={"monthly_ceiling_usd": 1.0})
    ws.assign("channel", "op_ch", "op", db_path=p)
    _run(p, "op_ch", "complete", 10, cost=5.0)
    assert wsc.blocked_channels(p) == {}
    assert not wsc.has_client_workspaces(p)
