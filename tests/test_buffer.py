"""
tests/test_buffer.py — Channel-scoped, deliverable/bundle-generic buffer gate.

Covers hypogeum.buffer.compute_buffer_state() (pure — target-based
enqueue/hold/excess, and the structural ForeignDemandItemError isolation
guarantee) and hypogeum.buffer.gather_channel_demand_items() /
resolve_complete_item_ids_for_channel() (the I/O layer, exercised against
an isolated tmp_path database and fake episode assets — never the
production DB).

Also proves cross-channel, cross-bundle isolation with two structurally
different channels run through run_worker.py's real Step 0 wiring in the
SAME iteration:
    Channel A: bundle_id='main_episode_bundle' (one long-form + associated
               Shorts as one unit), target_buffer_count=1.
    Channel B: bundle_id='short' (six independent Shorts, no long-form),
               target_buffer_count=2.
Channel A's buffer state must never affect Channel B's gate decision, and
vice versa.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hypogeum.buffer import (
    BufferState,
    ChannelDemandItem,
    DeliverableBundleDefinition,
    ForeignDemandItemError,
    compute_buffer_state,
    gather_channel_demand_items,
    resolve_complete_item_ids_for_channel,
)
from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic


# ---------------------------------------------------------------------------
# Pure function — compute_buffer_state
# ---------------------------------------------------------------------------

CHANNEL_A = DeliverableBundleDefinition(channel_id="channel_a", bundle_id="main_episode_bundle", target_buffer_count=1)
CHANNEL_B = DeliverableBundleDefinition(channel_id="channel_b", bundle_id="short", target_buffer_count=2)


def _item(definition, item_id, ready=True, published=False, current_cycle=False):
    return ChannelDemandItem(
        channel_id=definition.channel_id, bundle_id=definition.bundle_id, item_id=item_id,
        publish_ready=ready, already_published=published, is_current_cycle=current_cycle,
    )


class TestComputeBufferStatePure:
    def test_below_target_enqueues(self):
        state = compute_buffer_state(CHANNEL_A, [])
        assert state.action == "enqueue"
        assert state.buffer_count == 0
        assert state.target_buffer_count == 1

    def test_zero_because_all_are_published_or_current_cycle_or_not_ready(self):
        items = [
            _item(CHANNEL_A, "a", ready=True, published=True),
            _item(CHANNEL_A, "b", ready=True, current_cycle=True),
            _item(CHANNEL_A, "c", ready=False),
        ]
        state = compute_buffer_state(CHANNEL_A, items)
        assert state.action == "enqueue"
        assert state.buffer_count == 0
        assert set(state.excluded_item_ids) == {"a", "b", "c"}

    def test_exactly_at_target_holds(self):
        items = [_item(CHANNEL_A, "a", ready=True), _item(CHANNEL_A, "b", ready=False)]
        state = compute_buffer_state(CHANNEL_A, items)
        assert state.action == "hold"
        assert state.buffer_count == 1
        assert state.counted_item_ids == ["a"]

    def test_above_target_is_excess(self):
        items = [_item(CHANNEL_A, "a"), _item(CHANNEL_A, "b"), _item(CHANNEL_A, "c", ready=False)]
        state = compute_buffer_state(CHANNEL_A, items)
        assert state.action == "excess"
        assert state.buffer_count == 2

    def test_higher_target_bundle_needs_more_to_hold(self):
        # Channel B declares target_buffer_count=2 -- one ready item is
        # still "enqueue", not "hold", proving the target is genuinely
        # config-driven, not a hardcoded "1".
        items = [_item(CHANNEL_B, "s0", ready=True)]
        state = compute_buffer_state(CHANNEL_B, items)
        assert state.action == "enqueue"
        assert state.buffer_count == 1
        assert state.target_buffer_count == 2

        items2 = [_item(CHANNEL_B, "s0"), _item(CHANNEL_B, "s1")]
        assert compute_buffer_state(CHANNEL_B, items2).action == "hold"

    def test_deterministic_for_same_input(self):
        items = [_item(CHANNEL_A, "a"), _item(CHANNEL_A, "b")]
        assert compute_buffer_state(CHANNEL_A, items) == compute_buffer_state(CHANNEL_A, items)


class TestForeignDemandItemIsolation:
    def test_item_from_a_different_channel_raises(self):
        items = [_item(CHANNEL_A, "a"), _item(CHANNEL_B, "s0")]
        with pytest.raises(ForeignDemandItemError):
            compute_buffer_state(CHANNEL_A, items)

    def test_item_from_a_different_bundle_same_channel_raises(self):
        other_bundle = DeliverableBundleDefinition(channel_id="channel_a", bundle_id="other_bundle", target_buffer_count=1)
        items = [_item(CHANNEL_A, "a"), _item(other_bundle, "x")]
        with pytest.raises(ForeignDemandItemError):
            compute_buffer_state(CHANNEL_A, items)

    def test_error_message_never_silently_swallowed(self):
        items = [_item(CHANNEL_B, "s0")]
        with pytest.raises(ForeignDemandItemError, match="channel_b"):
            compute_buffer_state(CHANNEL_A, items)


# ---------------------------------------------------------------------------
# I/O layer — gather_channel_demand_items / resolve_complete_item_ids_for_channel
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


def _seed_complete_item(db_path, assets_root, item_id, *, channel_id):
    conn = get_connection(db_path)
    try:
        # topic matches metadata_source.episode_title below ("T") so
        # A_METADATA sees one consistent candidate, not an artificial
        # conflict this fixture would otherwise introduce.
        insert_episode(conn, episode_id=item_id, created_at=_now_iso(), topic="T", status="complete")
        insert_episode_topic(
            conn, topic_id=f"topic_{item_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now_iso(), updated_at=_now_iso(), episode_id=item_id,
        )
        conn.commit()
    finally:
        conn.close()

    ep_dir = assets_root / "episodes" / item_id
    ep_dir.mkdir(parents=True)
    pkg = {"metadata_source": {"episode_title": "T", "description": "d", "shorts_titles": []}, "shorts_renders": []}
    (ep_dir / f"final_render_package_{item_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{item_id}.mp4").write_bytes(b"x" * 1024)


class TestResolveCompleteItemIds:
    def test_scoped_to_channel_via_episode_topics_join(self, db_path, assets_root):
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        _seed_complete_item(db_path, assets_root, "item_b", channel_id="channel_b")
        assert resolve_complete_item_ids_for_channel("channel_a", db_path=db_path) == ["item_a"]
        assert resolve_complete_item_ids_for_channel("channel_b", db_path=db_path) == ["item_b"]
        assert resolve_complete_item_ids_for_channel("channel_c", db_path=db_path) == []


class TestGatherChannelDemandItems:
    def test_publish_ready_item_is_counted(self, db_path, assets_root):
        from hypogeum.credential_manager import create_secret_profile

        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert len(items) == 1
        assert items[0].channel_id == "channel_a"
        assert items[0].bundle_id == "main_episode_bundle"
        assert items[0].publish_ready is True
        assert items[0].already_published is False
        assert items[0].is_current_cycle is False

    def test_metadata_blocked_item_is_not_publish_ready(self, db_path, assets_root):
        # No secret profile created -> readiness fails -> not publish_ready.
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].publish_ready is False

    def test_current_cycle_item_ids_is_explicit_not_inferred(self, db_path, assets_root):
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset({"item_a"}),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].is_current_cycle is True

    def test_already_published_item_flagged(self, db_path, assets_root):
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publishing_outcomes
                    (outcome_id, episode_id, platform, deliverable_id, publishing_status, recorded_at)
                VALUES ('out_1', 'item_a', 'youtube', 'main_episode', 'success', ?)
                """,
                (_now_iso(),),
            )
            conn.commit()
        finally:
            conn.close()
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is True

    def test_zernio_confirmed_publish_is_recognized_as_already_published(self, db_path, assets_root):
        """Zernio writes to publication_intents/publishing_receipts, never
        to the legacy publishing_outcomes/publish_calendar tables -- this
        proves that path is recognized too, scoped by channel_id."""
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publication_intents
                    (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
                VALUES ('pi_1', 'channel_a', 'item_a', 'main_episode', 'youtube', 'succeeded', ?, ?)
                """,
                (_now_iso(), _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is True

    def test_zernio_confirmed_publish_for_a_different_channel_does_not_leak(self, db_path, assets_root):
        _seed_complete_item(db_path, assets_root, "item_a", channel_id="channel_a")
        conn = get_connection(db_path)
        try:
            # Same episode_id, but attributed to channel_b's intent -- must
            # never satisfy channel_a's already_published check.
            conn.execute(
                """
                INSERT INTO publication_intents
                    (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
                VALUES ('pi_2', 'channel_b', 'item_a', 'main_episode', 'youtube', 'succeeded', ?, ?)
                """,
                (_now_iso(), _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["item_a"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_items_carry_the_definitions_channel_and_bundle_id(self, db_path, assets_root):
        _seed_complete_item(db_path, assets_root, "s0", channel_id="channel_b")
        items = gather_channel_demand_items(
            CHANNEL_B, item_ids=["s0"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].channel_id == "channel_b"
        assert items[0].bundle_id == "short"
        # Feeding straight into compute_buffer_state for the SAME
        # definition must never raise -- proves gather's output is
        # self-consistent with the isolation check.
        compute_buffer_state(CHANNEL_B, items)


# ---------------------------------------------------------------------------
def _seed_complete_bundle_with_shorts(db_path, assets_root, item_id, *, channel_id, n_shorts=3):
    """Same as _seed_complete_item but with n_shorts real (non-placeholder)
    Shorts, so resolve_required_deliverable_ids() returns 'main_episode'
    plus 'short_001'..'short_{n_shorts:03d}' -- a real 4-deliverable bundle
    shape, matching ep_f3619022's own (1 main + 3 Shorts)."""
    conn = get_connection(db_path)
    try:
        insert_episode(conn, episode_id=item_id, created_at=_now_iso(), topic="T", status="complete")
        insert_episode_topic(
            conn, topic_id=f"topic_{item_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now_iso(), updated_at=_now_iso(), episode_id=item_id,
        )
        conn.commit()
    finally:
        conn.close()

    ep_dir = assets_root / "episodes" / item_id
    ep_dir.mkdir(parents=True)
    pkg = {
        "metadata_source": {"episode_title": "T", "description": "d", "shorts_titles": [f"s{i}" for i in range(n_shorts)]},
        "shorts_renders": [{"short_id": f"{i + 1:03d}"} for i in range(n_shorts)],
    }
    (ep_dir / f"final_render_package_{item_id}.json").write_text(json.dumps(pkg), encoding="utf-8")
    (ep_dir / f"main_{item_id}.mp4").write_bytes(b"x" * 1024)


def _insert_succeeded_intent(db_path, *, intent_id, channel_id, episode_id, deliverable_id):
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO publication_intents
                (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'youtube', 'succeeded', ?, ?)
            """,
            (intent_id, channel_id, episode_id, deliverable_id, _now_iso(), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


class TestPartialPublicationBundleSemantics:
    """A bundle (main + 3 Shorts = 4 required deliverables) must only be
    treated as already_published when EVERY required deliverable has its
    own confirmed 'succeeded' publication_intents row -- not when any
    single one does."""

    def test_zero_succeeded_deliverables_bundle_unpublished(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_one_of_four_succeeded_is_partial_not_published(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        _insert_succeeded_intent(db_path, intent_id="pi_1", channel_id="channel_a", episode_id="ep_bundle", deliverable_id="main_episode")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_two_of_four_succeeded_is_partial_not_published(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        _insert_succeeded_intent(db_path, intent_id="pi_1", channel_id="channel_a", episode_id="ep_bundle", deliverable_id="main_episode")
        _insert_succeeded_intent(db_path, intent_id="pi_2", channel_id="channel_a", episode_id="ep_bundle", deliverable_id="short_001")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_three_of_four_succeeded_is_partial_not_published(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        for i, deliverable_id in enumerate(["main_episode", "short_001", "short_002"]):
            _insert_succeeded_intent(db_path, intent_id=f"pi_{i}", channel_id="channel_a", episode_id="ep_bundle", deliverable_id=deliverable_id)
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_all_four_succeeded_marks_bundle_fully_published(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        for i, deliverable_id in enumerate(["main_episode", "short_001", "short_002", "short_003"]):
            _insert_succeeded_intent(db_path, intent_id=f"pi_{i}", channel_id="channel_a", episode_id="ep_bundle", deliverable_id=deliverable_id)
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is True

    def test_failed_or_ambiguous_deliverables_do_not_count(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publication_intents
                    (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
                VALUES ('pi_failed', 'channel_a', 'ep_bundle', 'short_001', 'youtube', 'failed_closed', ?, ?)
                """,
                (_now_iso(), _now_iso()),
            )
            conn.execute(
                """
                INSERT INTO publication_intents
                    (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
                VALUES ('pi_open', 'channel_a', 'ep_bundle', 'short_002', 'youtube', 'open', ?, ?)
                """,
                (_now_iso(), _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        for deliverable_id in ["main_episode", "short_003"]:
            _insert_succeeded_intent(db_path, intent_id=f"pi_ok_{deliverable_id}", channel_id="channel_a", episode_id="ep_bundle", deliverable_id=deliverable_id)
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_duplicate_succeeded_intent_rows_do_not_inflate_completeness(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        # Two duplicate 'succeeded' rows for the SAME deliverable_id --
        # must not be mistaken for two distinct deliverables completing.
        _insert_succeeded_intent(db_path, intent_id="pi_dup_1", channel_id="channel_a", episode_id="ep_bundle", deliverable_id="main_episode")
        _insert_succeeded_intent(db_path, intent_id="pi_dup_2", channel_id="channel_a", episode_id="ep_bundle", deliverable_id="main_episode")
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False

    def test_another_channels_intents_never_count_even_all_four(self, db_path, assets_root):
        _seed_complete_bundle_with_shorts(db_path, assets_root, "ep_bundle", channel_id="channel_a")
        for i, deliverable_id in enumerate(["main_episode", "short_001", "short_002", "short_003"]):
            _insert_succeeded_intent(db_path, intent_id=f"pi_wrong_{i}", channel_id="channel_b", episode_id="ep_bundle", deliverable_id=deliverable_id)
        items = gather_channel_demand_items(
            CHANNEL_A, item_ids=["ep_bundle"], current_cycle_item_ids=frozenset(),
            db_path=db_path, assets_root=assets_root,
        )
        assert items[0].already_published is False


# ---------------------------------------------------------------------------
# End-to-end cross-channel isolation via run_worker.py's real Step 0 wiring
# ---------------------------------------------------------------------------

def _make_channel_profile(db_path, channel_id):
    from hypogeum.channel_config import (
        create_brand_profile, create_channel_profile, create_model_profile, create_publishing_profile,
    )
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )


class TestCrossChannelBundleIsolationViaRunWorker:
    def test_channel_a_full_buffer_does_not_suppress_channel_b_refill_and_vice_versa(
        self, db_path, assets_root, monkeypatch,
    ):
        import run_worker
        from hypogeum.credential_manager import create_secret_profile

        _make_channel_profile(db_path, "channel_a")
        _make_channel_profile(db_path, "channel_b")

        # Channel A: one ready, publish-ready item -> meets target_buffer_count=1 -> "hold".
        _seed_complete_item(db_path, assets_root, "ep_a1", channel_id="channel_a")
        # Channel B: zero ready items -> target_buffer_count=2 unmet -> "enqueue".
        # (No items seeded for channel_b at all.)

        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )

        # Stand in for a real Channel-Package compiler (see
        # run_worker._resolve_bundle_definitions_for_channel's own
        # docstring for why this is normally hardcoded per-channel there;
        # this test exercises both channels through the SAME real
        # gate/loop code by patching the resolver to return each
        # channel's own declared bundle).
        def _resolve(channel_id):
            if channel_id == "channel_a":
                return [DeliverableBundleDefinition(channel_id="channel_a", bundle_id="main_episode_bundle", target_buffer_count=1)]
            if channel_id == "channel_b":
                return [DeliverableBundleDefinition(channel_id="channel_b", bundle_id="short", target_buffer_count=2)]
            return []

        monkeypatch.setattr("run_worker._resolve_bundle_definitions_for_channel", _resolve)

        discovery_calls = []

        def _fake_discovery(channel_id, db_path=None):
            discovery_calls.append(channel_id)
            return {"ok": True, "cycle_id": f"c_{channel_id}", "candidate_count": 0, "selected_candidate_id": None}

        monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)

        result = run_worker._check_and_refill_queue(
            db_path=db_path, threshold=2, auto_approve=False, assets_root=assets_root,
        )

        # Channel A is buffer-gated (hold, target met) -- its discovery is skipped.
        assert "channel_a" in result["buffer_gated"]
        assert result["buffer_states"]["channel_a"]["main_episode_bundle"]["action"] == "hold"
        assert "channel_a" not in discovery_calls

        # Channel B is NOT buffer-gated (enqueue, target unmet) -- its
        # discovery runs, proving Channel A's full buffer never suppressed it.
        assert "channel_b" not in result["buffer_gated"]
        assert result["buffer_states"]["channel_b"]["short"]["action"] == "enqueue"
        assert "channel_b" in discovery_calls

    def test_channel_b_full_buffer_does_not_suppress_channel_a_refill(self, db_path, assets_root, monkeypatch):
        import run_worker
        from hypogeum.credential_manager import create_secret_profile

        _make_channel_profile(db_path, "channel_a")
        _make_channel_profile(db_path, "channel_b")

        # Channel B: two ready items -> meets target_buffer_count=2 -> "hold".
        _seed_complete_item(db_path, assets_root, "sh_b1", channel_id="channel_b")
        _seed_complete_item(db_path, assets_root, "sh_b2", channel_id="channel_b")
        # Channel A: zero ready items -> target_buffer_count=1 unmet -> "enqueue".

        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )

        def _resolve(channel_id):
            if channel_id == "channel_a":
                return [DeliverableBundleDefinition(channel_id="channel_a", bundle_id="main_episode_bundle", target_buffer_count=1)]
            if channel_id == "channel_b":
                return [DeliverableBundleDefinition(channel_id="channel_b", bundle_id="short", target_buffer_count=2)]
            return []

        monkeypatch.setattr("run_worker._resolve_bundle_definitions_for_channel", _resolve)

        discovery_calls = []

        def _fake_discovery(channel_id, db_path=None):
            discovery_calls.append(channel_id)
            return {"ok": True, "cycle_id": f"c_{channel_id}", "candidate_count": 0, "selected_candidate_id": None}

        monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)

        result = run_worker._check_and_refill_queue(
            db_path=db_path, threshold=2, auto_approve=False, assets_root=assets_root,
        )

        assert "channel_b" in result["buffer_gated"]
        assert result["buffer_states"]["channel_b"]["short"]["action"] == "hold"
        assert "channel_b" not in discovery_calls

        assert "channel_a" not in result["buffer_gated"]
        assert result["buffer_states"]["channel_a"]["main_episode_bundle"]["action"] == "enqueue"
        assert "channel_a" in discovery_calls

    def test_unresolvable_channel_is_never_buffer_gated_falls_through_unaffected(
        self, db_path, assets_root, monkeypatch,
    ):
        """A channel with no compiled bundle definition (the real,
        production-honest default for every channel except Hypogeum today)
        must never be buffer-gated -- it falls straight through to the
        existing queue-depth logic, exactly as before this slice."""
        import run_worker

        _make_channel_profile(db_path, "channel_unknown")

        discovery_calls = []

        def _fake_discovery(channel_id, db_path=None):
            discovery_calls.append(channel_id)
            return {"ok": True, "cycle_id": "c1", "candidate_count": 0, "selected_candidate_id": None}

        monkeypatch.setattr("hypogeum.topic_discovery.run_discovery_cycle", _fake_discovery)

        result = run_worker._check_and_refill_queue(
            db_path=db_path, threshold=2, auto_approve=False, assets_root=assets_root,
        )

        assert "channel_unknown" not in result["buffer_gated"]
        assert "channel_unknown" not in result["buffer_states"]
        assert "channel_unknown" in discovery_calls
