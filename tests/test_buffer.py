"""
tests/test_buffer.py — One-episode publish-ready buffer gate.

Covers hypogeum.buffer.compute_buffer_state() (pure — 0/1/2+ publish-ready
buffer episodes) and hypogeum.buffer.gather_buffer_candidates() (the I/O
layer, exercised against an isolated tmp_path database and fake episode
assets — never the production DB). Also covers run_worker.py's Step 0
wiring: the buffer gate skips discovery-refill for a channel without
touching tick()/consume_scheduled_runs()/poll().
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.buffer import BufferEpisodeCandidate, BufferState, compute_buffer_state, gather_buffer_candidates
from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic


# ---------------------------------------------------------------------------
# Pure function — compute_buffer_state
# ---------------------------------------------------------------------------

def _cand(episode_id, ready=True, published=False, current_week=False):
    return BufferEpisodeCandidate(
        episode_id=episode_id, publish_ready=ready,
        already_published=published, is_current_week=current_week,
    )


class TestComputeBufferStatePure:
    def test_zero_publish_ready_episodes_enqueues(self):
        state = compute_buffer_state("hypogeum", [])
        assert state.action == "enqueue"
        assert state.buffer_count == 0

    def test_zero_because_all_are_published_or_current_week(self):
        candidates = [
            _cand("ep_a", ready=True, published=True),
            _cand("ep_b", ready=True, current_week=True),
            _cand("ep_c", ready=False),
        ]
        state = compute_buffer_state("hypogeum", candidates)
        assert state.action == "enqueue"
        assert state.buffer_count == 0
        assert set(state.excluded_episode_ids) == {"ep_a", "ep_b", "ep_c"}

    def test_exactly_one_holds(self):
        candidates = [_cand("ep_a", ready=True), _cand("ep_b", ready=False)]
        state = compute_buffer_state("hypogeum", candidates)
        assert state.action == "hold"
        assert state.buffer_count == 1
        assert state.counted_episode_ids == ["ep_a"]

    def test_two_or_more_is_excess(self):
        candidates = [_cand("ep_a"), _cand("ep_b"), _cand("ep_c", ready=False)]
        state = compute_buffer_state("hypogeum", candidates)
        assert state.action == "excess"
        assert state.buffer_count == 2
        assert sorted(state.counted_episode_ids) == ["ep_a", "ep_b"]

    def test_deterministic_for_same_input(self):
        candidates = [_cand("ep_a"), _cand("ep_b")]
        s1 = compute_buffer_state("hypogeum", candidates)
        s2 = compute_buffer_state("hypogeum", candidates)
        assert s1 == s2


# ---------------------------------------------------------------------------
# I/O layer — gather_buffer_candidates
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "buffer_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def assets_root(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    return root


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _seed_complete_episode(db_path, assets_root, episode_id, *, channel_id="hypogeum"):
    conn = get_connection(db_path)
    try:
        # topic matches the render package's metadata_source.episode_title
        # below ("T") so the A_METADATA title-candidate check sees a single
        # consistent value rather than an artificial conflict this fixture
        # would otherwise introduce.
        insert_episode(conn, episode_id=episode_id, created_at=_now_iso(), topic="T", status="complete")
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now_iso(), updated_at=_now_iso(), episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()

    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True)
    pkg = {
        "metadata_source": {"episode_title": "T", "description": "d", "shorts_titles": []},
        "shorts_renders": [],
    }
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"x" * 1024)


class TestGatherBufferCandidates:
    def test_no_complete_episodes_returns_empty(self, db_path, assets_root):
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert candidates == []

    def test_complete_and_metadata_only_ready_episode_is_counted(self, db_path, assets_root):
        from hypogeum.credential_manager import create_secret_profile

        _seed_complete_episode(db_path, assets_root, "ep_ready")
        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert len(candidates) == 1
        assert candidates[0].episode_id == "ep_ready"
        assert candidates[0].publish_ready is True
        assert candidates[0].already_published is False
        assert candidates[0].is_current_week is False

    def test_metadata_blocked_episode_is_not_publish_ready(self, db_path, assets_root):
        # No secret profile created -> E_MEDIA_STAGING_READY fails -> not publish_ready.
        _seed_complete_episode(db_path, assets_root, "ep_blocked")
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert len(candidates) == 1
        assert candidates[0].publish_ready is False

    def test_already_published_episode_flagged(self, db_path, assets_root):
        _seed_complete_episode(db_path, assets_root, "ep_live")
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publishing_outcomes
                    (outcome_id, episode_id, platform, deliverable_id, publishing_status, recorded_at)
                VALUES ('out_1', 'ep_live', 'youtube', 'main_episode', 'success', ?)
                """,
                (_now_iso(),),
            )
            conn.commit()
        finally:
            conn.close()
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert candidates[0].already_published is True

    def test_current_week_episode_flagged(self, db_path, assets_root):
        _seed_complete_episode(db_path, assets_root, "ep_soon")
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publish_calendar
                    (calendar_id, episode_id, platform, target_publish_at, created_at, updated_at)
                VALUES ('cal_1', 'ep_soon', 'youtube', ?, ?, ?)
                """,
                (soon, _now_iso(), _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert candidates[0].is_current_week is True

    def test_other_channel_episode_excluded(self, db_path, assets_root):
        _seed_complete_episode(db_path, assets_root, "ep_other_channel", channel_id="some_other_channel")
        candidates = gather_buffer_candidates("hypogeum", db_path=db_path, assets_root=assets_root)
        assert candidates == []


# ---------------------------------------------------------------------------
# run_worker.py wiring — Step 0 gate only
# ---------------------------------------------------------------------------

class TestRunWorkerBufferWiring:
    def test_buffer_gated_channel_skips_discovery_but_result_reports_it(self, db_path, assets_root, monkeypatch):
        import run_worker
        from hypogeum.channel_config import create_channel_profile, create_brand_profile, create_model_profile, create_publishing_profile

        brand = create_brand_profile(name="Hypogeum Brand", db_path=db_path)
        model = create_model_profile(name="Hypogeum Model", db_path=db_path)
        pub = create_publishing_profile(name="Hypogeum Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="hypogeum", name="Hypogeum",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )

        monkeypatch.setattr(
            "hypogeum.buffer.compute_buffer_state",
            lambda channel_id, candidates: BufferState(
                channel_id=channel_id, action="hold", buffer_count=1, counted_episode_ids=["ep_x"], reason="test-forced-hold",
            ),
        )
        monkeypatch.setattr("hypogeum.buffer.gather_buffer_candidates", lambda *a, **k: [])

        called = {"discovery": False}
        monkeypatch.setattr(
            "hypogeum.topic_discovery.run_discovery_cycle",
            lambda *a, **k: called.__setitem__("discovery", True) or {"ok": True, "cycle_id": "c1", "candidate_count": 0},
        )

        result = run_worker._check_and_refill_queue(db_path=db_path, threshold=2, auto_approve=False, assets_root=assets_root)

        assert called["discovery"] is False
        assert "hypogeum" in result["buffer_gated"]
        assert result["buffer_states"]["hypogeum"]["action"] == "hold"

    def test_buffer_enqueue_state_falls_through_to_existing_queue_depth_logic(self, db_path, assets_root, monkeypatch):
        import run_worker
        from hypogeum.channel_config import create_channel_profile, create_brand_profile, create_model_profile, create_publishing_profile

        brand = create_brand_profile(name="Hypogeum Brand", db_path=db_path)
        model = create_model_profile(name="Hypogeum Model", db_path=db_path)
        pub = create_publishing_profile(name="Hypogeum Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="hypogeum", name="Hypogeum",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )

        monkeypatch.setattr("hypogeum.buffer.gather_buffer_candidates", lambda *a, **k: [])

        called = {"discovery": False}

        def _fake_discovery(channel_id, db_path=None):
            called["discovery"] = True
            return {"ok": True, "cycle_id": "c1", "candidate_count": 0, "selected_candidate_id": None}

        monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)

        result = run_worker._check_and_refill_queue(db_path=db_path, threshold=2, auto_approve=False, assets_root=assets_root)

        assert "hypogeum" not in result["buffer_gated"]
        assert result["buffer_states"]["hypogeum"]["action"] == "enqueue"
        assert called["discovery"] is True
