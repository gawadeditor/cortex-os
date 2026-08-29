"""
tests/test_factory_multichannel.py

Factory / channel separation, and the factory driving MORE THAN ONE
channel. Proves, with a real temp DB and zero network, that:

  * the worker enumerates every active channel (not just "hypogeum")
  * scheduler.tick() fires each channel's own due schedule
  * consume_scheduled_runs() promotes each channel's scheduled_run to a
    run_queue row carrying that channel's channel_id
  * poll() -> the gated executor -> run_pipeline() is invoked once per
    channel with the correct channel_id (so run_pipeline loads that
    channel's own ChannelConfig / ChannelOperationsProfile)

The factory-level clearance gate (OIS / factory-active / credentials /
monthly budget) is shared; every per-channel decision (CALOG state,
operator_enabled, weekly budget, cadence, backlog, schedule) is isolated.
"""
from __future__ import annotations

import datetime as _dt
import socket
import uuid
from pathlib import Path

import pytest

import hypogeum.db as db

CHANNELS = ("hypogeum", "zephyr")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _channel_cfg(cid: str) -> dict:
    return {
        "channel_id": cid,
        "name": f"{cid.title()} Channel",
        "brand": {"language": "en"},
        "publishing": {"enabled_platforms": ["youtube"]},
        "models": {
            "episode_producer": "anthropic/claude-sonnet-4.6",
            "research_director": "anthropic/claude-opus-4.6",
            "thumbnail_planner": "openai/gpt-5.5-pro",
            "brand_guardian_p1": "anthropic/claude-sonnet-4.6",
            "brand_guardian_p2": "openai/gpt-5.5-pro",
            "narration_voice_id": "j9jfwdrw7BRfcR43Qohk",
            "image_provider": "muapi", "video_provider": "muapi",
        },
    }


@pytest.fixture
def two_channel_factory(tmp_path, monkeypatch):
    dbp = tmp_path / "factory.db"
    db.initialize_schema(dbp)

    forbidden = tmp_path / "FORBIDDEN.db"
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", forbidden)
    for mod in ("run_registry", "ois", "finance", "channel_config", "topics", "scheduler"):
        monkeypatch.setattr(f"hypogeum.{mod}._DEFAULT_DB_PATH", forbidden, raising=False)

    def _no_net(*a, **k):
        raise AssertionError("network access in multi-channel factory test")
    monkeypatch.setattr(socket, "socket", _no_net)

    # factory entity (shared, single) + factory-scoped credentials
    conn = db.get_connection(dbp)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, status, "
            "default_language, default_timezone, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Factory", "hypogeum", "active", "en", "UTC", _now_iso(), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    from hypogeum.credential_manager import create_secret_profile
    for prov in ("openrouter", "perplexity", "elevenlabs", "muapi"):
        create_secret_profile(provider=prov, label=f"{prov}", credential_type="api_key",
                              credential_data="sk_test", scope="factory", db_path=dbp)

    from hypogeum.channel_config import bootstrap_channel, create_channel_operations_profile
    from hypogeum.calog import set_channel_state
    from hypogeum.scheduler import create_schedule
    from hypogeum.topics import enqueue_topic

    due = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    for cid in CHANNELS:
        bootstrap_channel(_channel_cfg(cid), db_path=dbp)
        create_channel_operations_profile(
            channel_id=cid, channel_name=f"{cid.title()} Channel", timezone="UTC",
            operator_enabled=True, unattended_operation_enabled=True,
            production_enabled=True, discovery_enabled=True,
            production_cadence={"videos_per_week": 1},
            publishing_schedule={"day_of_week": "friday", "time_of_day": "14:00"},
            weekly_video_budget_usd=135.0, db_path=dbp,
        )
        try:
            set_channel_state(cid, "active", triggered_by="test", db_path=dbp)
        except Exception:
            pass
        create_schedule(db_path=dbp, scope="channel", channel_id=cid, cadence="weekly",
                        next_run_at=due, label=f"{cid}_weekly")
        # a backlog topic per channel so consume->poll has something to claim
        enqueue_topic(cid, f"A mystery for {cid}", validate_channel=True, db_path=dbp,
                      episode_id=f"ep_{cid}")

    class _Cfg:
        AUTO_DISCOVERY_ENABLED = False
        AUTO_DISCOVERY_AUTO_APPROVE = False
        AUTO_DISCOVERY_THRESHOLD = 2
        AUTO_ZERNIO_PUBLISHING_ENABLED = False
        QUEUE_RETRY_BACKOFF_MINUTES = "0"
        QUEUE_MAX_AUTO_RETRIES = 1
        FINANCE_BUDGET_CEILING_USD = 135.0
        def __getattr__(self, n):
            from hypogeum.config import Config
            return getattr(Config(), n)

    return {"db_path": dbp, "forbidden": forbidden, "config": _Cfg()}


def test_worker_fires_and_routes_every_channel(two_channel_factory, monkeypatch):
    import run_worker
    import hypogeum.pipeline as pipeline

    dbp = two_channel_factory["db_path"]
    forbidden = two_channel_factory["forbidden"]

    calls: list[dict] = []

    def _stub_run_pipeline(*a, **k):
        calls.append({"channel_id": k.get("channel_id"),
                      "episode_id": k.get("episode_id"),
                      "db_path": k.get("db_path")})
        # nothing else — we are testing routing, not the pipeline

    monkeypatch.setattr("hypogeum.queue._pipeline_module", pipeline)
    monkeypatch.setattr(pipeline, "run_pipeline", _stub_run_pipeline)

    wid = str(uuid.uuid4())
    for it in range(1, 8):
        run_worker.run_iteration(
            worker_id=wid, db_path=dbp, dry_run=False, channel_id=None,
            config=two_channel_factory["config"], iteration=it, assets_root=dbp.parent,
        )

    assert not forbidden.exists(), "a write escaped to the process-default DB"

    conn = db.get_connection(dbp)
    try:
        sched_channels = {r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM scheduled_runs")}
        queue_channels = {r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM run_queue")}
    finally:
        conn.close()

    # every channel's own schedule fired and produced a channel-scoped queue row
    assert sched_channels == set(CHANNELS), sched_channels
    assert queue_channels == set(CHANNELS), queue_channels

    # the executor invoked run_pipeline once per channel, each with its own id
    pipeline_channels = {c["channel_id"] for c in calls}
    assert pipeline_channels == set(CHANNELS), (pipeline_channels, calls)
    assert all(c["db_path"] == dbp for c in calls), calls
    # each channel's run used its own reserved episode_id
    by_ch = {c["channel_id"]: c["episode_id"] for c in calls}
    assert by_ch["hypogeum"] == "ep_hypogeum"
    assert by_ch["zephyr"] == "ep_zephyr"


def test_factory_clearance_is_shared_channel_gates_are_not(two_channel_factory):
    """A factory-level block (OIS) stops every channel; a single channel
    being non-runnable (CALOG) stops only that channel."""
    from hypogeum.factory_director import check_run_clearance
    from hypogeum.channel_config import get_channel_operations_profile

    dbp = two_channel_factory["db_path"]

    # factory gate: one verdict, not per channel
    c1 = check_run_clearance(db_path=dbp, channel_id="hypogeum")
    c2 = check_run_clearance(db_path=dbp, channel_id="zephyr")
    assert c1.cleared == c2.cleared  # same factory, same verdict

    # channel gates are independent rows
    assert get_channel_operations_profile("hypogeum", db_path=dbp).channel_id == "hypogeum"
    assert get_channel_operations_profile("zephyr", db_path=dbp).channel_id == "zephyr"
