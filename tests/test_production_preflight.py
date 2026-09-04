"""
tests/test_production_preflight.py — the two hard pre-spend gates added
2026-09-04 after twinkle produced two unpublishable seedance @496x864
episodes from a batch that also fed one topic to two runs.
"""
import pytest

from hypogeum.artifact_compatibility import qualifies_for_deliverable_after_upscale
from hypogeum.db import get_connection, initialize_schema
from hypogeum.production_preflight import (
    DuplicateTopicError,
    VideoModelNotQualifiedError,
    assert_topic_not_already_produced,
    assert_video_model_qualified,
    evaluate_candidate_video_model,
)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


def _channel(db_path, channel_id, *, shorts_only=True):
    from hypogeum.channel_config import (
        create_brand_profile, create_channel_operations_profile,
        create_channel_profile, create_model_profile, create_publishing_profile,
    )
    brand = create_brand_profile(name=f"{channel_id} b", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} m", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} p", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id,
        model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    cadence = {"videos_per_week": 0, "shorts_per_week": 7} if shorts_only else {"videos_per_week": 1}
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone="UTC",
        operator_enabled=True, production_cadence=cadence, db_path=db_path,
    )
    return model.model_profile_id


def _seed_episode(db_path, episode_id, channel_id, topic, status="complete"):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, topic, status, created_at) VALUES (?,?,?,datetime('now'))",
            (episode_id, topic, status),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (f"run_{episode_id}", episode_id, channel_id, "completed"),
        )
        conn.commit()
    finally:
        conn.close()


# --- pure helper -----------------------------------------------------------

class TestUpscaleQualification:
    def test_wan27_portrait_qualifies_for_shorts(self):
        ok, _ = qualifies_for_deliverable_after_upscale(720, 1280, 1080, 1920)
        assert ok is True

    def test_seedance_496x864_rejected_too_small(self):
        ok, why = qualifies_for_deliverable_after_upscale(496, 864, 1080, 1920)
        assert ok is False and "0.4" in why

    def test_landscape_model_rejected_for_portrait_deliverable(self):
        ok, why = qualifies_for_deliverable_after_upscale(864, 480, 1080, 1920)
        assert ok is False and "orientation" in why

    def test_unknown_resolution_rejected(self):
        ok, why = qualifies_for_deliverable_after_upscale(0, 0, 1080, 1920)
        assert ok is False and "unknown" in why


# --- gate 1: video model --------------------------------------------------

class TestVideoModelGate:
    def test_no_adopted_model_passes(self, db_path):
        _channel(db_path, "ch_none")
        assert_video_model_qualified("ch_none", db_path=db_path)  # no raise

    def test_none_channel_is_noop(self, db_path):
        assert_video_model_qualified(None, db_path=db_path)

    def test_adopted_model_without_evidence_is_denied(self, db_path):
        mp_id = _channel(db_path, "ch_ev")
        from hypogeum.channel_config import update_model_profile
        update_model_profile(mp_id, db_path=db_path, video_model="mystery-model")
        with pytest.raises(VideoModelNotQualifiedError):
            assert_video_model_qualified("ch_ev", db_path=db_path)

    def test_adopted_model_with_good_persisted_resolution_passes(self, db_path):
        mp_id = _channel(db_path, "ch_ok")
        from hypogeum.channel_config import update_model_profile
        update_model_profile(mp_id, db_path=db_path, video_model="wan2.7-text-to-video",
                             video_model_native_width=720, video_model_native_height=1280)
        assert_video_model_qualified("ch_ok", db_path=db_path)

    def test_adopted_subres_model_is_denied(self, db_path):
        mp_id = _channel(db_path, "ch_bad")
        from hypogeum.channel_config import update_model_profile
        update_model_profile(mp_id, db_path=db_path, video_model="seedance-pro-t2v-fast",
                             video_model_native_width=496, video_model_native_height=864)
        with pytest.raises(VideoModelNotQualifiedError):
            assert_video_model_qualified("ch_bad", db_path=db_path)

    def test_candidate_eval_rejects_subres(self, db_path):
        _channel(db_path, "ch_cand")
        ok, why, _ = evaluate_candidate_video_model("ch_cand", "no-evidence-model", db_path=db_path)
        assert ok is False


# --- gate 2: duplicate topic ---------------------------------------------

class TestDuplicateTopicGate:
    def test_new_topic_passes(self, db_path):
        _channel(db_path, "ch_t")
        _seed_episode(db_path, "ep_old", "ch_t", "A Sleepy Fox Counts the Stars")
        assert_topic_not_already_produced(
            "ch_t", "The Brave Little Tugboat", episode_id="ep_new", db_path=db_path
        )

    def test_same_topic_different_episode_is_denied(self, db_path):
        _channel(db_path, "ch_d")
        _seed_episode(db_path, "ep_first", "ch_d", "The Little Hedgehog Who Couldn't Find His Way Home")
        with pytest.raises(DuplicateTopicError):
            assert_topic_not_already_produced(
                "ch_d", "Little Hedgehog Who Couldn't Find His Way Home",  # article/case variant
                episode_id="ep_second", db_path=db_path,
            )

    def test_same_episode_rerun_is_allowed(self, db_path):
        _channel(db_path, "ch_r")
        _seed_episode(db_path, "ep_x", "ch_r", "The Little Hedgehog Who Couldn't Find His Way Home")
        assert_topic_not_already_produced(
            "ch_r", "The Little Hedgehog Who Couldn't Find His Way Home",
            episode_id="ep_x", db_path=db_path,
        )

    def test_superseded_episode_does_not_block(self, db_path):
        _channel(db_path, "ch_s")
        _seed_episode(db_path, "ep_dead", "ch_s", "The Moon Bunny", status="superseded")
        assert_topic_not_already_produced(
            "ch_s", "The Moon Bunny", episode_id="ep_fresh", db_path=db_path
        )

    def test_other_channel_does_not_block(self, db_path):
        _channel(db_path, "ch_a")
        _channel(db_path, "ch_b")
        _seed_episode(db_path, "ep_a", "ch_a", "Shared Title Story")
        assert_topic_not_already_produced(
            "ch_b", "Shared Title Story", episode_id="ep_b", db_path=db_path
        )
