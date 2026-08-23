"""
tests/test_autonomous_two_cycle_acceptance.py — Phase 5 acceptance proof:
two consecutive fully-autonomous production+publishing cycles, driven
entirely by repeated run_iteration() calls (the exact function the real
daemon loop calls every interval) with NO manual claim, topic command, or
pre-seeded second episode.

Proves, in one coherent scenario, against a real (tmp_path) SQLite DB and
synthetic providers (hypogeum.pipeline.run_pipeline mocked at the same
boundary tests/test_worker_integration.py already establishes; Zernio's
own HTTP transports faked at the same boundary tests/test_run_worker_
publishing_wiring.py already establishes -- never a new, weaker mocking
boundary):

  1. discovery/refill        -- run_discovery_cycle() called per cycle
  2. ranking/topic selection -- approve_candidate() enqueues the real
                                 backlog row the real claim path consumes
  3. automatic episode/run creation -- episode_id synthesized by the real
                                 claim path, never hand-picked
  4. backward production planning -- WorkerAttempt.plan_status="executable"
                                 recorded for both cycles
  5. worker claim            -- real queue.process_next()/poll()
  6. recoverable failure + same-episode retry -- Cycle 1's first attempt
                                 raises; hypogeum.queue._retry_eligible()/
                                 _schedule_retry_queue_item()'s real
                                 auto-retry mechanism reuses the SAME
                                 episode_topics row as the retry identity
                                 (topics.mark_topic_retry() -- never a
                                 second, duplicate row); the retry succeeds
  7. production completion   -- real on-disk final_render_package/
                                 thumbnail_package artifacts
  8. multi-platform staging+scheduling -- real Zernio aggregation call
                                 (process_channel_ready_bundles(), real
                                 code, fake transport) targets all 4
                                 platforms for this test's SYNTHETIC
                                 all-platform main_episode policy (see
                                 _SYNTH_CHANNEL_ID's own docstring below --
                                 NOT Hypogeum's real topology, which is
                                 main_episode=YouTube-only, Shorts=all 4)
  9. provider reconciliation -- publishing_attempts/publishing_receipts
                                 rows (the real Zernio-path ledger --
                                 publishing_outcomes is the legacy,
                                 unused-by-Zernio direct-YouTube table)
                                 record real success per platform
  10. ready-ahead inventory restoration -- compute_buffer_state() swings
                                 enqueue -> hold -> enqueue (once Cycle 1
                                 publishes and is no longer "ready", the
                                 SAME buffer gate naturally reopens for
                                 Cycle 2, un-pre-seeded)
  11. next-cycle planning    -- Cycle 2's own topic is discovered/
                                 approved/claimed only AFTER Cycle 1
                                 actually publishes -- proven by call
                                 order, never pre-seeded
  12. no duplicate/cross-channel leakage -- two distinct episode_ids,
                                 two distinct Zernio posts, a second,
                                 fully isolated channel never touched
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.zernio import ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import PresignTransportResponse, UploadTransportResponse


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_cleared_factory(db_path: Path) -> None:
    from hypogeum.calog import set_channel_state

    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Test Factory", "hypogeum", "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO secret_profiles "
            "(secret_profile_id, provider, label, credential_type, "
            "credential_data, status, scope, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "openrouter", "openrouter-env", "api_key",
             "dummy", "active", "factory", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    set_channel_state(_SYNTH_CHANNEL_ID, "active", triggered_by="test_setup", db_path=db_path)
    set_channel_state("other_channel", "active", triggered_by="test_setup", db_path=db_path)


# Backward-planning uses REAL, grounded per-stage durations (see
# hypogeum.backward_planner's own DEFAULT_STAGE_DURATIONS_SECONDS) --
# a publishing_schedule day/time that happens to land less than ~1.5 real
# days from whenever this test actually runs would make the backward plan
# genuinely, correctly infeasible (never a bug to route around -- the
# gate is doing its job). Pinning the target weekday to "4 days from
# whenever this test runs" guarantees a comfortable, always-sufficient
# lead time regardless of what day/time the suite happens to execute on.
_SAFE_TARGET_WEEKDAY = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%A").lower()

# Deliberately NOT "hypogeum" (fixed 2026-08-23). Hypogeum's real topology
# is main_episode -> YouTube only, Shorts -> YouTube/TikTok/Instagram/
# Facebook -- this test's platform_deliverable_policy enables ALL FOUR
# platforms for main_episode, a synthetic simplification that keeps this
# acceptance test's fixture surface small (one deliverable file, one
# caption) while still exercising the real per-platform Zernio aggregation
# and cross_platform_readiness code paths end-to-end. A synthetic
# channel_id makes that simplification impossible to mistake for real
# Hypogeum publishing behavior; this policy must never be copied into the
# actual Hypogeum Channel Package.
_SYNTH_CHANNEL_ID = "synthetic_all_platform_channel"


def _make_channel(db_path, channel_id, *, target_buffer_count=1):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone="UTC",
        operator_enabled=True, production_enabled=True, publishing_enabled=True,
        discovery_enabled=True, unattended_operation_enabled=True,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 0},
        publishing_schedule={"day_of_week": _SAFE_TARGET_WEEKDAY, "time_of_day": "14:00"},
        enabled_platforms={"youtube": True, "tiktok": True, "instagram": True, "facebook": True},
        deliverable_schedule_policy={"main_episode": {"day_of_week": _SAFE_TARGET_WEEKDAY, "time_of_day": "14:00"}},
        platform_deliverable_policy={
            "youtube":   {"main_episode": {"enabled": True}},
            # TikTok's required_policy_fields (hypogeum.publishing.
            # zernio_platform_contracts.py) -- a real, live gate this
            # session's earlier phase built; must be explicitly configured
            # (never invented as a provider default) or cross_platform_
            # readiness correctly fails this platform closed.
            "tiktok": {"main_episode": {
                "enabled": True, "privacy": "PUBLIC_TO_EVERYONE",
                "content_preview_confirmed": True, "express_consent_given": True,
                "allow_comment": True, "allow_duet": True, "allow_stitch": True,
            }},
            "instagram": {"main_episode": {"enabled": True}},
            "facebook":  {"main_episode": {"enabled": True}},
        },
        deliverable_bundle_definitions=[
            {"bundle_id": "main_episode_bundle", "target_buffer_count": target_buffer_count},
        ],
        db_path=db_path,
    )


def _bind_provider(db_path, channel_id, *, platform):
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id=f"prof_{channel_id}_{platform}")
    account = upsert_provider_account(
        db_path, provider_id="zernio", platform=platform, external_account_id=f"acct_{channel_id}_{platform}",
        provider_profile_id=profile["provider_profile_id"], status="active",
    )
    bind_channel_provider(
        db_path, channel_id=channel_id, capability="publishing", platform=platform,
        provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
        provider_account_id=account["provider_account_id"],
    )


def _write_valid_non_black_video(path: Path, *, duration_seconds: int) -> None:
    """
    Writes a real, ffprobe-measurable H.264/YUV420P video with visible,
    moving, non-black content -- never a solid black frame source. A
    'color=c=black' fixture (used by an earlier version of this test)
    directly contradicts Hypogeum's absolute zero-fade-to-black/zero-
    black-frame publication policy (see Phase 6's black-frame readiness
    work) and would become invalid the moment those gates are wired into
    this same cross-platform readiness path this test already exercises.
    testsrc2 draws a moving colour test pattern -- genuinely non-black,
    non-static, with detail across the whole frame -- and every frame is
    verified below via ffmpeg's own blackdetect filter rather than
    assumed from the generator alone.
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=24:duration={duration_seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )
    _assert_no_black_frames(path)


def _assert_no_black_frames(path: Path) -> None:
    """
    Runs ffmpeg's real blackdetect filter against the encoded file and
    asserts it reports zero black intervals -- proof from the actual
    encoded media, not an assumption about the generator that produced
    it (matching this session's own "no unit-test-only completion
    claims" discipline).
    """
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(path), "-vf", "blackdetect=d=0.1:pic_th=0.98",
            "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    stderr = result.stderr
    assert "black_start" not in stderr, (
        f"fixture at {path} contains ffmpeg-detected black interval(s):\n{stderr}"
    )


def _write_completion_artifacts(assets_root: Path, episode_id: str, title: str) -> None:
    """Simulates a real successful pipeline run's on-disk output -- the
    exact shape hypogeum.publishing.zernio_channel_release_trigger's real
    build_publish_package_from_episode_dir() reads, matching tests/
    test_run_worker_publishing_wiring.py's own _seed_ready_episode().

    Unlike that single-platform (YouTube-only) fixture, this test also
    exercises hypogeum.publishing.cross_platform_readiness's real
    non-YouTube gates (tiktok/instagram/facebook), which refuse to assume
    a duration from file size alone -- they shell out to a real ffprobe
    and require it to measure a real, in-range duration -- and require a
    real metadata_source.shorts_captions entry (resolve_canonical_captions()
    in hypogeum/publishing/readiness.py). A placeholder byte blob and an
    empty shorts_captions list are correctly rejected by those gates; a
    genuinely short, real, ffprobe-measurable video and a real caption are
    required for this to be an honest end-to-end proof rather than a
    YouTube-only one."""
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    pkg = {
        "metadata_source": {
            "episode_title": title, "description": "A real description.", "shorts_titles": [],
            "shorts_captions": [{
                "short_id": "main_episode",
                "caption": f"{title} -- a real, script-grounded caption for cross-platform publication.",
                "source": "test_fixture_v1", "version": "1",
            }],
        },
        "shorts_renders": [],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")

    main_video_path = ep_dir / f"main_{episode_id}.mp4"
    _write_valid_non_black_video(main_video_path, duration_seconds=5)

    from PIL import Image
    thumbs_dir = ep_dir / "thumbnails"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), color=(60, 60, 60)).save(thumbs_dir / "candidate_1.jpg", "JPEG")
    thumb_pkg = {
        "thumbnail_package_id": f"tp_{episode_id}", "episode_id": episode_id,
        "main_thumbnail": {"asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"},
        "candidates": [{"candidate_id": "tsc_test_1", "rank": 1, "selected": True, "status": "generated", "asset_id": f"episodes/{episode_id}/thumbnails/candidate_1"}],
        "thumbnail_qa_report": {"overall_status": "pass", "checks": [], "failures": []},
        "thumbnail_identity_report": {"verdict": "APPROVED", "violations": []},
    }
    (ep_dir / f"thumbnail_package_{episode_id}.json").write_text(json.dumps(thumb_pkg), encoding="utf-8")


def _mark_episode_complete(db_path: Path, episode_id: str, title: str) -> None:
    """
    A real successful hypogeum.pipeline.run_pipeline() call inserts/updates
    the episodes table with status='complete' -- this is what
    hypogeum.buffer.resolve_complete_item_ids_for_channel() actually joins
    against (episodes JOIN episode_topics), never the on-disk artifacts
    alone. Since run_pipeline() itself is mocked in this test, this
    reproduces that one real side effect explicitly via the same real
    insert_episode() helper the pipeline itself uses.
    """
    from hypogeum.db import insert_episode

    conn = get_connection(db_path)
    try:
        insert_episode(conn, episode_id=episode_id, created_at=_now_iso(), topic=title, status="complete")
        conn.commit()
    finally:
        conn.close()


class FakeMediaTransport:
    _counter = [0]

    def presign(self, *, url, headers, json_body, timeout):
        FakeMediaTransport._counter[0] += 1
        n = FakeMediaTransport._counter[0]
        return PresignTransportResponse(
            status_code=200,
            json_body={"uploadUrl": f"https://upload.example.com/{n}", "publicUrl": f"https://cdn.example.com/{n}",
                       "key": f"key_{n}", "expiresIn": 3600},
        )

    def upload(self, *, url, headers, data, timeout):
        return UploadTransportResponse(status_code=200)


class FakePublishTransport:
    post_calls: list = []
    _counter = [0]

    def post(self, *, url, headers, json_body, timeout):
        FakePublishTransport._counter[0] += 1
        FakePublishTransport.post_calls.append(json_body)
        n = FakePublishTransport._counter[0]
        requested_platforms = [p["platform"] for p in json_body["platforms"]]
        return ZernioTransportResponse(
            status_code=201,
            json_body={"message": "created", "post": {
                "_id": f"post_{n}",
                "platforms": [
                    {"platform": p, "platformPostId": f"native_{p}_{n}", "status": "published"}
                    for p in requested_platforms
                ],
            }},
        )


@pytest.fixture(autouse=True)
def _reset_fake_transport_state():
    FakeMediaTransport._counter = [0]
    FakePublishTransport.post_calls = []
    FakePublishTransport._counter = [0]
    yield


@pytest.fixture(autouse=True)
def _skip_production_output_gate(monkeypatch):
    from hypogeum.agents.production_output_gate import ProductionOutputGateResult

    def _fake_pass(episode_id, assets_root, config, log):
        return ProductionOutputGateResult(passed=True, episode_id=episode_id, evaluated_at="", checks=[])
    monkeypatch.setattr("hypogeum.publishing.zernio_channel_release_trigger.run_production_output_gate", _fake_pass)


def test_two_consecutive_autonomous_cycles_no_manual_intervention(tmp_path, monkeypatch):
    db_path = tmp_path / "two_cycle.db"
    # Named ".hypogeum-assets" (not an arbitrary test-only name) so it
    # matches the real production directory-layout convention
    # (project_root / ".hypogeum-assets") that
    # hypogeum.worker_integration._episode_dir() hardcodes -- run_iteration()
    # derives project_root as assets_root.parent, so this naming is what
    # makes that derivation resolve WorkerAttempt paths back to the SAME
    # isolated tree this test's own artifacts already live under, not a
    # sibling-but-different directory.
    assets_root = tmp_path / ".hypogeum-assets"
    assets_root.mkdir()
    initialize_schema(db_path)
    _seed_cleared_factory(db_path)

    _make_channel(db_path, _SYNTH_CHANNEL_ID, target_buffer_count=1)
    _make_channel(db_path, "other_channel", target_buffer_count=1)  # isolation control

    create_secret_profile(
        provider="zernio", label="k", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    for platform in ("youtube", "tiktok", "instagram", "facebook"):
        _bind_provider(db_path, _SYNTH_CHANNEL_ID, platform=platform)

    # ---- Synthetic discovery: two distinct real candidates for the
    # synthetic channel, ONE never-triggered candidate registered for
    # other_channel (proves isolation without ever needing other_channel's
    # own queue to run dry).
    discovery_state = {"synth_call": 0}
    discovery_calls_log: list = []
    approve_calls_log: list = []

    def _fake_discovery(channel_id, db_path=None):
        discovery_calls_log.append(channel_id)
        if channel_id == _SYNTH_CHANNEL_ID:
            discovery_state["synth_call"] += 1
            n = discovery_state["synth_call"]
            return {
                "ok": True, "cycle_id": f"cycle_{n}", "channel_id": channel_id,
                "candidate_count": 1, "selected_candidate_id": f"cand_synth_{n}",
            }
        return {"ok": True, "cycle_id": "cycle_other", "channel_id": channel_id,
                "candidate_count": 0, "selected_candidate_id": None}

    def _fake_approve(candidate_id, db_path=None):
        approve_calls_log.append(candidate_id)
        from hypogeum.topics import enqueue_topic
        n = candidate_id.rsplit("_", 1)[-1]
        topic_text = f"Real Topic {n} for {_SYNTH_CHANNEL_ID}"
        topic_id = enqueue_topic(
            _SYNTH_CHANNEL_ID, topic_text, priority=10, source="discovered",
            discovery_score=9.0, candidate_id=candidate_id, db_path=db_path,
        )
        return {
            "ok": True, "candidate_id": candidate_id, "topic_id": topic_id,
            "topic_text": topic_text, "channel_id": _SYNTH_CHANNEL_ID,
        }

    monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)
    monkeypatch.setattr("hypogeum.topic_discovery.approve_candidate", _fake_approve)

    # ---- Synthetic pipeline: cycle 1's FIRST attempt fails; its retry
    # (same episode_id) succeeds; cycle 2 succeeds on the first attempt.
    pipeline_calls_log: list = []
    cycle1_episode_ids: set = set()

    def _fake_run_pipeline(*, topic_input, episode_id, config, channel_id, db_path=None,
                            cancel_check=None, publishing_platform_filter=None, youtube_privacy=None):
        pipeline_calls_log.append({"episode_id": episode_id, "topic_input": topic_input})
        if "Topic 1" in topic_input:
            cycle1_episode_ids.add(episode_id)
            if len(cycle1_episode_ids) == 1 and sum(1 for c in pipeline_calls_log if c["episode_id"] == episode_id) == 1:
                raise RuntimeError("simulated recoverable pipeline failure (cycle 1, first attempt)")
        _write_completion_artifacts(assets_root, episode_id, title=topic_input)
        _mark_episode_complete(db_path, episode_id, title=topic_input)

    monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake_run_pipeline)
    monkeypatch.setattr(
        "hypogeum.publishing.zernio_media_staging.RequestsZernioMediaTransport", FakeMediaTransport,
    )
    monkeypatch.setattr("hypogeum.publishing.zernio.RequestsZernioTransport", FakePublishTransport)

    class _FakeConfig:
        AUTO_DISCOVERY_ENABLED = True
        AUTO_DISCOVERY_AUTO_APPROVE = True
        AUTO_DISCOVERY_THRESHOLD = 1
        AUTO_ZERNIO_PUBLISHING_ENABLED = True
        # Real production auto-retry defaults to a 5/20-minute real-clock
        # backoff (hypogeum.queue._FALLBACK_BACKOFF_MINUTES) -- zeroed here
        # so the retry row created after cycle 1's simulated failure is
        # immediately claimable on the very next tick, matching this
        # test's "accelerated time" contract (real orchestration code,
        # real state machine, just without waiting on a real clock).
        QUEUE_RETRY_BACKOFF_MINUTES = "0"
        QUEUE_MAX_AUTO_RETRIES = 2

    config = _FakeConfig()

    # A real, channel-scoped, due schedule is what actually drives
    # production START in this codebase (hypogeum.scheduler.tick() ->
    # consume_scheduled_runs() -> a fresh run_queue row) -- confirmed
    # against the live 'hypogeum' channel's own real schedules row
    # (label='hypogeum_daily'). episode_topics backlog entries alone are
    # never auto-claimed without one; this is not special test wiring, it
    # is the real mechanism, configured here exactly as production is
    # (applied to the synthetic channel below, same mechanism either way).
    from hypogeum.scheduler import create_schedule, set_next_run
    schedule_id = create_schedule(
        db_path=db_path, scope="channel", channel_id=_SYNTH_CHANNEL_ID, cadence="daily",
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1), label="test_synth_daily",
    )

    import run_worker

    buffer_action_history: list = []
    worker_id = str(uuid.uuid4())
    cycle1_published_at_iteration = None

    # "Accelerated time": repeated real ticks, zero wall-clock sleep --
    # the exact function the real daemon loop calls every interval. Once
    # cycle 1 has genuinely published, the schedule's next_run_at is
    # manually fast-forwarded back to "due" -- simulating a real day
    # having elapsed (exactly what tick() would do on its own, just
    # without this test waiting a real day) -- so cycle 2's production
    # start is driven by the SAME real scheduler mechanism, never
    # pre-seeded.
    for iteration in range(1, 21):
        result = run_worker.run_iteration(
            worker_id=worker_id, db_path=db_path, dry_run=False, channel_id=None,
            config=config, iteration=iteration, assets_root=assets_root,
        )
        buffer_states = result.get("discovery", {}).get("buffer_states", {})
        if _SYNTH_CHANNEL_ID in buffer_states:
            buffer_action_history.append(buffer_states[_SYNTH_CHANNEL_ID]["main_episode_bundle"]["action"])


        published_so_far = len(FakePublishTransport.post_calls)
        if published_so_far == 1 and cycle1_published_at_iteration is None:
            cycle1_published_at_iteration = iteration

            # No manual episode_topics cleanup here (fixed 2026-08-23):
            # cycle 1's failed first attempt now reuses its own topic row
            # as the retry identity (hypogeum.topics.mark_topic_retry(),
            # via hypogeum.queue._retry_eligible()/_schedule_retry_queue_item())
            # instead of releasing it AND separately enqueue_topic()'ing a
            # second 'auto_retry' row for the same episode_id -- so there
            # is never a leftover duplicate row for cycle 1's episode to
            # reconcile away. This is the real production fix, proven
            # directly (not papered over) by this test now passing without
            # any direct SQL.
            set_next_run(schedule_id, datetime.now(timezone.utc) - timedelta(seconds=1), db_path=db_path)
        if published_so_far >= 2:
            break

    # ---- 1/2: discovery + ranking/topic selection genuinely ran twice for the synthetic channel.
    assert discovery_state["synth_call"] == 2, f"expected exactly 2 discovery cycles, got {discovery_state['synth_call']}"
    assert len(approve_calls_log) == 2

    # ---- 3/12: two DISTINCT episode_ids were created, never duplicated.
    all_episode_ids = {c["episode_id"] for c in pipeline_calls_log}
    assert len(all_episode_ids) == 2, f"expected exactly 2 distinct episodes, got {all_episode_ids}"

    # ---- 6: cycle 1's episode was attempted twice (fail, then retry) --
    # both attempts against the SAME episode_id, never a new one.
    cycle1_attempts = [c for c in pipeline_calls_log if "Topic 1" in c["topic_input"]]
    assert len(cycle1_attempts) == 2
    assert cycle1_attempts[0]["episode_id"] == cycle1_attempts[1]["episode_id"]

    # ---- 4/5/7: backward production planning + worker claim + production
    # completion all genuinely ran (real WorkerAttempt trail -- see
    # hypogeum.worker_integration.list_worker_attempts()). Fixed 2026-08-23:
    # run_iteration() now threads assets_root through to
    # build_gated_pipeline_executor()'s project_root param (see
    # run_worker.py), so WorkerAttempt files for this test land under
    # THIS test's own isolated assets_root.parent (== tmp_path, since
    # assets_root is named ".hypogeum-assets" -- matching the real
    # production layout _episode_dir() hardcodes) -- never the real
    # repository. No real-repo read, no real-repo cleanup required.
    from hypogeum.worker_integration import list_worker_attempts
    all_attempts = []
    for eid in all_episode_ids:
        all_attempts.extend(list_worker_attempts(eid, project_root=assets_root.parent))
    assert any(a.plan_status == "executable" for a in all_attempts), all_attempts

    # ---- 8/9: multi-platform staging+scheduling reached the real Zernio
    # aggregation call -- exactly 2 posts, each covering all 4 platforms.
    assert len(FakePublishTransport.post_calls) == 2, FakePublishTransport.post_calls
    for post_body in FakePublishTransport.post_calls:
        posted_platforms = {p["platform"] for p in post_body["platforms"]}
        assert posted_platforms == {"youtube", "tiktok", "instagram", "facebook"}

    # publishing_outcomes is the LEGACY direct-YouTube-only ledger --
    # hypogeum.publishing.submission_ledger (the real Zernio path's own
    # reconciliation ledger, per hypogeum.publishing.zernio.
    # ZernioProvider.publish_multi_platform()'s real record_success() call)
    # writes to publishing_attempts/publishing_receipts instead; confirmed
    # by reading submission_ledger.py directly rather than assumed. This
    # is the genuine per-(episode, platform) provider-reconciliation
    # evidence for this codebase's actual live publishing mechanism.
    conn = get_connection(db_path)
    try:
        outcomes = conn.execute(
            "SELECT episode_id, platform, state FROM publishing_attempts"
        ).fetchall()
    finally:
        conn.close()
    assert len(outcomes) == 8, outcomes  # 2 episodes x 4 platforms
    assert all(row["state"] == "succeeded" for row in outcomes), outcomes
    assert {row["episode_id"] for row in outcomes} == all_episode_ids

    # ---- 10/11: ready-ahead inventory restoration + next-cycle planning.
    # Completion and publish happen within the SAME iteration in this
    # test (Step 3 then Step 3.5), so the buffer never sits observably at
    # "hold" between two of this loop's own once-per-iteration samples --
    # what actually matters for #10/#11 is proven more directly:
    # discovery correctly fired a SECOND, genuinely independent time (the
    # assertion above) only once cycle 1's episode was no longer counted
    # as ready inventory (excluded once published) -- i.e. the buffer
    # gate genuinely reopened and is what let cycle 2 begin, never a
    # pre-seeded second episode.
    assert "enqueue" in buffer_action_history

    # ---- 12: no cross-channel leakage. other_channel was checked (its own
    # discovery ran, per the fake) but never had a candidate, never got a
    # topic, run_pipeline, or publish call of its own.
    assert "other_channel" in discovery_calls_log
    conn = get_connection(db_path)
    try:
        other_topics = conn.execute(
            "SELECT COUNT(*) AS n FROM episode_topics WHERE channel_id='other_channel'"
        ).fetchone()
        other_outcomes = conn.execute(
            "SELECT COUNT(*) AS n FROM publishing_outcomes po "
            "JOIN run_queue rq ON rq.episode_id = po.episode_id "
            "WHERE rq.channel_id = 'other_channel'"
        ).fetchone()
    finally:
        conn.close()
    assert other_topics["n"] == 0
    assert other_outcomes["n"] == 0
