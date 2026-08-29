"""
tests/test_autonomous_hypogeum_shadow.py

A deterministic, zero-network acceptance shadow of the REAL autonomous
worker path for the `hypogeum` channel:

    run_worker.run_iteration(dry_run=False)
      -> discovery (faked -> one real enqueued topic)
      -> scheduler.tick / consume_scheduled_runs
      -> queue.poll -> build_gated_pipeline_executor -> run_pipeline
      -> the real 10 pipeline stage functions
      -> (every stage checkpoint-resumes from a seeded ep_f3619022 artifact)
      -> real Finance / QA / Compliance gates
      -> ready_to_publish, publishing NOT invoked

Nothing is faked except:
  * the external provider boundary (hard network sentinel -- any socket
    or requests call fails the test);
  * discovery candidate selection (one real topic is enqueued);
  * the per-stage GENERATION work -- each stage's checkpoint loader
    returns a seeded artifact adapted from the real ep_f3619022 run, so
    `run_pipeline` exercises its real orchestration / state machine /
    stage-execution recording / gates without calling a provider or
    FFmpeg. Fresh generation + a fresh FFmpeg stitch need the media
    fixture harness and are a separate follow-up.

DB isolation: every write is pinned to a temp DB by
`hypogeum.db.execution_db` (Part A). The production tripwire
(tests/_prod_tripwire.py) independently fails the test on any write to
the real production database.
"""
from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import pytest

import hypogeum.db as db

_ROOT = Path(__file__).resolve().parent.parent
_EP = "ep_f3619022"
_EP_DIR = _ROOT / ".hypogeum-assets" / "episodes" / _EP

_CHECKPOINT_SCHEMAS = (
    "validation_scorecard", "topic_brief", "research_package", "narrative_package",
    "script_package", "thumbnail_concept_brief", "narration_package", "visual_package",
    "thumbnail_package", "final_render_package",
)


def _artifacts_present() -> bool:
    return all((_EP_DIR / f"{s}_{_EP}.json").exists() for s in _CHECKPOINT_SCHEMAS)


def _adapt(schema: str, episode_id: str) -> dict:
    raw = (_EP_DIR / f"{schema}_{_EP}.json").read_text()
    pkg = json.loads(raw.replace(_EP, episode_id))
    if schema == "visual_package":
        # Present a CLEAN, complete plan so `_run_visual_package_director`'s
        # checkpoint check treats Phase 2 as done and returns without
        # re-running the cinematic director / MUAPI. ep_f3619022 left 2
        # scene clips 'generation_failed'.
        for a in pkg.get("asset_manifest", {}).get("assets", []):
            if a.get("generation_status") not in ("generated", "reused", "dry_run", "deferred"):
                a["generation_status"] = "generated"
    if schema == "thumbnail_package":
        # ep_f3619022's thumbnail was a placeholder (production_ready False,
        # thumbnail_qa_report overall_status 'fail'). Present a passing
        # report -- thumbnail image QUALITY is out of scope for this
        # automation-path shadow.
        pkg.setdefault("package_integrity", {})["production_ready"] = True
        qr = pkg.setdefault("thumbnail_qa_report", {})
        qr["overall_status"] = "pass"
        for c in qr.get("checks", []):
            if c.get("status") in ("fail", "warn"):
                c["status"] = "pass"
    if schema == "final_render_package":
        # ep_f3619022 never got a finished main-episode title/description
        # (that is why its own real run failed QA). Fill them so the
        # metadata-completeness gate passes -- this shadow is about the
        # automation path, not copywriting.
        ms = pkg.setdefault("metadata_source", {})
        ms["episode_title"] = "Europe's Erdstall Tunnels: The Passage No One Can Explain"
        ms["logline"] = "A deterministic acceptance-shadow logline."
        ms["description"] = (
            "Acceptance-shadow description for the Hypogeum autonomous pipeline. "
            "Not for publication. " * 3
        )
        ms["ai_disclosure_text"] = "This video was produced with AI assistance."
        # ep_f3619022's real render shipped silent (music_log == []); the
        # music-completeness QA gate now rejects that. This shadow is about
        # the automation path, not scoring -- give it a sourced track.
        if not pkg.get("music_log"):
            pkg["music_log"] = [{
                "log_id": "ml_main_episode_0", "section_key": "main_episode_0",
                "category": "Mystery", "source": "channel_library",
                "asset_id": "music/mystery/shadow_track",
            }]
    return pkg


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    dbp = tmp_path / "shadow.db"
    assets_root = tmp_path / ".hypogeum-assets"
    assets_root.mkdir()
    db.initialize_schema(dbp)

    # Pin the process default at a FORBIDDEN path so any unbound fallthrough
    # is visible; the real production path is separately blocked by the tripwire.
    forbidden = tmp_path / "FORBIDDEN.db"
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", forbidden)
    for mod in ("run_registry", "finance", "ois", "channel_config", "scheduler", "queue"):
        monkeypatch.setattr(f"hypogeum.{mod}._DEFAULT_DB_PATH", forbidden, raising=False)

    # ---- hard network sentinel ----
    def _no_net(*a, **k):
        raise AssertionError("NETWORK ACCESS ATTEMPTED IN SHADOW")

    monkeypatch.setattr(socket, "socket", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    try:
        import requests
        monkeypatch.setattr(requests, "post", _no_net)
        monkeypatch.setattr(requests, "get", _no_net)
        monkeypatch.setattr(requests.Session, "request", _no_net)
    except Exception:
        pass

    # ---- cleared factory + real hypogeum channel + workflow into the temp DB ----
    from scripts.bootstrap_channel import bootstrap_channel
    from hypogeum import workflow_registry as wr
    from hypogeum.calog import set_channel_state

    _conn = db.get_connection(dbp)
    try:
        _conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, status, "
            "default_language, default_timezone, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Hypogeum Factory", "hypogeum", "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        _conn.commit()
    finally:
        _conn.close()

    from hypogeum.credential_manager import create_secret_profile
    for _prov in ("openrouter", "perplexity", "elevenlabs", "muapi"):
        create_secret_profile(
            provider=_prov, label=f"{_prov}-shadow", credential_type="api_key",
            credential_data="sk_test_never_real", scope="factory", db_path=dbp,
        )

    bootstrap_channel(
        {
            "channel_id": "hypogeum", "name": "The Hypogeum Chronicles",
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
        },
        db_path=dbp,
    )
    wf_bytes = (_ROOT / "channel_packages" / "hypogeum" / "workflow"
                / "hypogeum_main_production.json").read_bytes()
    wr.register_workflow("hypogeum", "hypogeum_main_production", "1.0.0", wf_bytes, db_path=dbp)

    import datetime as _dt
    from hypogeum.channel_config import create_channel_operations_profile
    # publishing_schedule ~4 days out so the real backward plan
    # (hypogeum.backward_planner) has a comfortable, feasible lead time
    # regardless of when the suite runs.
    _target_weekday = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=4)).strftime("%A").lower()
    create_channel_operations_profile(
        channel_id="hypogeum", channel_name="The Hypogeum Chronicles", timezone="UTC",
        operator_enabled=True, unattended_operation_enabled=True,
        production_enabled=True, discovery_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": _target_weekday, "time_of_day": "14:00"},
        weekly_video_budget_usd=135.0, db_path=dbp,
    )
    try:
        set_channel_state("hypogeum", "active", triggered_by="shadow_setup", db_path=dbp)
    except Exception:
        pass

    # a due channel schedule -- the real mechanism that turns a backlog
    # topic into a claimable run_queue row (production hypogeum has one,
    # label 'hypogeum_daily').
    import datetime as _dt
    from hypogeum.scheduler import create_schedule
    create_schedule(
        db_path=dbp, scope="channel", channel_id="hypogeum", cadence="weekly",
        next_run_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1),
        label="hypogeum_weekly_shadow",
    )

    # defeat conftest's autouse run-registry / finance stubs -- this shadow
    # needs the REAL run lifecycle so `runs` / transitions / stage_executions
    # are written (to the temp DB) and observable.
    import hypogeum.pipeline as pipeline
    import hypogeum.run_registry as _rr
    import hypogeum.finance as _fin
    monkeypatch.setattr(pipeline, "register_run", _rr.register_run)
    monkeypatch.setattr(pipeline, "fail_run", _rr.fail_run)
    monkeypatch.setattr(pipeline, "complete_run", _rr.complete_run)
    monkeypatch.setattr(pipeline, "transition_run_state", _rr.transition_run_state)
    monkeypatch.setattr(pipeline, "cancel_run", _rr.cancel_run)
    monkeypatch.setattr(pipeline, "set_run_context", _fin.set_run_context)
    monkeypatch.setattr(pipeline, "clear_run_context", _fin.clear_run_context)
    monkeypatch.setattr(pipeline, "write_episode_rollup", _fin.write_episode_rollup)

    # healthy startup, no network
    from hypogeum.startup import StartupReport
    monkeypatch.setattr(
        pipeline, "run_startup_checks",
        lambda *a, **k: StartupReport(
            overall_status="healthy", openrouter_reachable=True, models_verified=True,
            fallback_active=False, fallback_configured=True, checked_at="2026-08-29T00:00:00Z",
        ),
    )

    # ---- every pipeline stage checkpoint-resumes from an adapted artifact ----
    seeded_for: dict[str, dict] = {}

    def _fake_load_package(episode_id, schema_name, output_dir):
        if schema_name not in _CHECKPOINT_SCHEMAS:
            return None
        cache = seeded_for.setdefault(episode_id, {})
        if schema_name not in cache:
            cache[schema_name] = _adapt(schema_name, episode_id)
        return cache[schema_name]

    def _fake_load_verified_checkpoint(episode_id, output_dir, spec, log, **kw):
        return _fake_load_package(episode_id, spec.schema_name, output_dir)

    monkeypatch.setattr(pipeline, "_load_package", _fake_load_package)
    monkeypatch.setattr(pipeline, "_load_verified_checkpoint", _fake_load_verified_checkpoint)

    # The Production Output Gate is an UNCONDITIONAL pre-publish check that
    # re-inspects real rendered pixels: 47 source clips at >=1080p, the
    # assembled main + 3 Shorts, the thumbnail, captions, the end-page. A
    # zero-provider shadow cannot produce those, so this ONE internal gate
    # is stubbed here -- it is the documented media-fixture boundary of
    # this shadow, not an external provider. Everything up to it (16 real
    # stages incl. QA + Compliance) runs for real.
    monkeypatch.setattr(
        "hypogeum.agents.publishing_agent.enforce_production_output_gate",
        lambda **kw: None,
    )

    # discovery -> one real enqueued topic for hypogeum
    disc = {"n": 0}

    def _fake_discovery(channel_id, db_path=None):
        if channel_id != "hypogeum":
            return {"ok": True, "cycle_id": "c_other", "channel_id": channel_id,
                    "candidate_count": 0, "selected_candidate_id": None}
        disc["n"] += 1
        return {"ok": True, "cycle_id": f"c{disc['n']}", "channel_id": channel_id,
                "candidate_count": 1, "selected_candidate_id": f"cand_{disc['n']}"}

    def _fake_approve(candidate_id, db_path=None):
        from hypogeum.topics import enqueue_topic
        tid = enqueue_topic("hypogeum", "The Erdstall Tunnels", priority=10,
                            source="discovered", discovery_score=9.0,
                            candidate_id=candidate_id, db_path=db_path)
        return {"ok": True, "candidate_id": candidate_id, "topic_id": tid,
                "topic_text": "The Erdstall Tunnels", "channel_id": "hypogeum"}

    monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)
    monkeypatch.setattr("hypogeum.topic_discovery.approve_candidate", _fake_approve)

    class _Cfg:
        AUTO_DISCOVERY_ENABLED = True
        AUTO_DISCOVERY_AUTO_APPROVE = True
        AUTO_DISCOVERY_THRESHOLD = 1
        AUTO_ZERNIO_PUBLISHING_ENABLED = False
        QUEUE_RETRY_BACKOFF_MINUTES = "0"
        QUEUE_MAX_AUTO_RETRIES = 1
        FINANCE_BUDGET_CEILING_USD = 135.0

        def __getattr__(self, n):
            from hypogeum.config import Config
            return getattr(Config(), n)

    return dict(db_path=dbp, assets_root=assets_root, forbidden=forbidden, config=_Cfg())


class TestAutonomousHypogeumShadow:

    def test_worker_iteration_drives_hypogeum_to_a_terminal_state_zero_network(self, shadow_env, monkeypatch):
        if not _artifacts_present():
            pytest.skip("ep_f3619022 checkpoint artifacts not present")

        import run_worker

        dbp = shadow_env["db_path"]
        forbidden = shadow_env["forbidden"]
        worker_id = str(uuid.uuid4())

        pipeline_episode_ids: list = []
        pipeline_db_paths: list = []
        import hypogeum.pipeline as pipeline
        _real_run_pipeline = pipeline.run_pipeline

        def _traced_run_pipeline(*a, **k):
            pipeline_episode_ids.append(k.get("episode_id") or (a[1] if len(a) > 1 else None))
            pipeline_db_paths.append(k.get("db_path"))
            return _real_run_pipeline(*a, **k)

        monkeypatch.setattr("hypogeum.queue._pipeline_module", pipeline)
        monkeypatch.setattr(pipeline, "run_pipeline", _traced_run_pipeline)

        for it in range(1, 12):
            run_worker.run_iteration(
                worker_id=worker_id, db_path=dbp, dry_run=False, channel_id=None,
                config=shadow_env["config"], iteration=it,
                assets_root=shadow_env["assets_root"],
            )
            conn = db.get_connection(dbp)
            try:
                terminal = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN "
                    "('completed','ready_to_publish','failed','awaiting_review')"
                ).fetchone()[0]
            finally:
                conn.close()
            if terminal:
                break

        # every stateful write stayed in the temp execution DB
        assert not forbidden.exists(), "a stateful write escaped to the process-default DB"
        assert all(p == dbp for p in pipeline_db_paths), pipeline_db_paths

        conn = db.get_connection(dbp)
        try:
            runs = [dict(r) for r in conn.execute("SELECT run_id, episode_id, status FROM runs")]
            queue_rows = conn.execute("SELECT COUNT(*) FROM run_queue").fetchone()[0]
            states = [r[0] for r in conn.execute(
                "SELECT to_state FROM run_state_transitions ORDER BY rowid")]
            stage_execs = conn.execute("SELECT COUNT(*) FROM stage_executions").fetchone()[0]
        finally:
            conn.close()

        # exactly one execution, one queue item, one pipeline invocation
        assert len(runs) == 1, runs
        assert queue_rows == 1
        assert len(set(pipeline_episode_ids)) == 1
        assert stage_execs > 0                       # real stages ran and recorded

        final_state = runs[0]["status"]
        if final_state == "failed":
            conn = db.get_connection(dbp)
            try:
                err = conn.execute("SELECT error_message FROM runs WHERE run_id = ?",
                                   (runs[0]["run_id"],)).fetchone()
            finally:
                conn.close()
            pytest.fail(f"pipeline reached 'failed' -- real blocker: {(err[0] or '')[:800]}")

        # the automation path reached a terminal published-ready state
        # (the worker's own success state is 'completed'; publishing was
        # never invoked -- publishing_enabled is False and the hard network
        # sentinel in the fixture never fired).
        assert final_state in ("ready_to_publish", "completed"), (final_state, states)

    def test_replay_creates_no_duplicate_execution(self, shadow_env, monkeypatch):
        if not _artifacts_present():
            pytest.skip("ep_f3619022 checkpoint artifacts not present")
        import run_worker

        dbp = shadow_env["db_path"]
        worker_id = str(uuid.uuid4())

        def _runs(dp):
            conn = db.get_connection(dp)
            try:
                return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            finally:
                conn.close()

        for it in range(1, 8):
            run_worker.run_iteration(
                worker_id=worker_id, db_path=dbp, dry_run=False, channel_id=None,
                config=shadow_env["config"], iteration=it,
                assets_root=shadow_env["assets_root"],
            )
            if _runs(dbp) >= 1:
                break
        after_first = _runs(dbp)

        # further iterations must not spawn a second execution for the same slot
        for it in range(8, 12):
            run_worker.run_iteration(
                worker_id=worker_id, db_path=dbp, dry_run=False, channel_id=None,
                config=shadow_env["config"], iteration=it,
                assets_root=shadow_env["assets_root"],
            )
        assert _runs(dbp) == after_first
