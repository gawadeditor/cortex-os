"""
tests/test_bounded_runs.py — Durable bounded autonomous run.

Coverage for hypogeum.db.{provision_bounded_run, get_bounded_run,
count_bounded_run_claims, record_bounded_run_claim, mark_bounded_run_exhausted,
BoundedRunError}, hypogeum.queue.{provision_bounded_run, claim_next_bounded,
process_next(bounded_run_id=), poll(bounded_run_id=)}, and
hypogeum.worker_integration.build_gated_pipeline_executor(bounded_run_id=).

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
— never the production .hypogeum-assets/hypogeum.db. No provider, network,
or publishing platform call is made anywhere in this file; hypogeum.pipeline
.run_pipeline is monkeypatched wherever the gated executor's boundary is
under test.
"""

from __future__ import annotations

import threading

import pytest

from hypogeum.db import (
    BoundedRunError,
    count_bounded_run_claims,
    get_bounded_run,
    get_connection,
    initialize_schema,
)
from hypogeum.queue import (
    claim_next,
    claim_next_bounded,
    complete as queue_complete,
    enqueue,
    fail as queue_fail,
    get_item,
    poll,
    process_next,
    provision_bounded_run,
    release,
)
from hypogeum.topics import enqueue_topic


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "bounded_runs_test.db"
    initialize_schema(p)
    return p


def _enqueue_with_episode(db_path, episode_id, channel_id="hypogeum", priority=0):
    return enqueue(
        db_path=db_path, channel_id=channel_id, episode_id=episode_id,
        priority=priority, source="manual",
    )


def _run_queue_row(db_path, queue_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM run_queue WHERE queue_id = ?", (queue_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _bounded_run_row(db_path, bounded_run_id):
    conn = get_connection(db_path)
    try:
        return get_bounded_run(conn, bounded_run_id)
    finally:
        conn.close()


def _claim_count(db_path, bounded_run_id):
    conn = get_connection(db_path)
    try:
        return count_bounded_run_claims(conn, bounded_run_id)
    finally:
        conn.close()


# ===========================================================================
# 1/17. Provisioning
# ===========================================================================

class TestProvisioning:
    def test_explicit_provisioning_succeeds(self, db_path):
        row = provision_bounded_run("br_1", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        assert row["bounded_run_id"] == "br_1"
        assert row["max_episode_claims"] == 1
        assert row["publishing_allowed"] == 0
        assert row["status"] == "active"
        assert row["completed_at"] is None

    def test_restart_reuses_same_bounded_run_id_idempotently(self, db_path):
        first = provision_bounded_run("br_restart", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        second = provision_bounded_run("br_restart", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        assert first == second

    def test_reprovisioning_with_different_params_fails_closed(self, db_path):
        provision_bounded_run("br_conflict", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        with pytest.raises(BoundedRunError):
            provision_bounded_run("br_conflict", max_episode_claims=2, publishing_allowed=False, db_path=db_path)

    def test_invalid_max_episode_claims_fails_closed(self, db_path):
        with pytest.raises(BoundedRunError):
            provision_bounded_run("br_bad", max_episode_claims=0, publishing_allowed=False, db_path=db_path)
        with pytest.raises(BoundedRunError):
            provision_bounded_run("br_bad2", max_episode_claims=-1, publishing_allowed=False, db_path=db_path)

    def test_a_new_bounded_run_id_is_a_separate_explicitly_provisioned_run(self, db_path):
        provision_bounded_run("br_a", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        provision_bounded_run("br_b", max_episode_claims=1, publishing_allowed=False, db_path=db_path)

        _enqueue_with_episode(db_path, "ep_a")
        _enqueue_with_episode(db_path, "ep_b")

        claim_a = claim_next_bounded(worker_id="w1", bounded_run_id="br_a", db_path=db_path)
        assert claim_a is not None
        # br_a is now exhausted -- br_b is untouched and still has its own allowance.
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_a", db_path=db_path)

        claim_b = claim_next_bounded(worker_id="w1", bounded_run_id="br_b", db_path=db_path)
        assert claim_b is not None
        assert claim_b["episode_id"] != claim_a["episode_id"]


# ===========================================================================
# 2/3/4/5/6/7/8/9/10/11/12/13/14/15. claim_next_bounded()
# ===========================================================================

class TestClaimNextBounded:
    def test_unknown_bounded_run_id_fails_closed(self, db_path):
        _enqueue_with_episode(db_path, "ep_1")
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="does_not_exist", db_path=db_path)
        # Nothing was claimed.
        assert claim_next(worker_id="w2", db_path=db_path) is not None  # queue item still 'queued'

    def test_missing_bounded_run_id_fails_closed(self, db_path):
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id=None, db_path=db_path)
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="", db_path=db_path)

    def test_one_authorized_claim_succeeds(self, db_path):
        provision_bounded_run("br_ok", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = _enqueue_with_episode(db_path, "ep_ok")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_ok", db_path=db_path)

        assert claimed is not None
        assert claimed["queue_id"] == queue_id
        assert claimed["episode_id"] == "ep_ok"
        assert claimed["status"] == "claimed"

    def test_ledger_row_and_queue_claim_commit_together(self, db_path):
        provision_bounded_run("br_atomic", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = _enqueue_with_episode(db_path, "ep_atomic")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_atomic", db_path=db_path)

        assert claimed is not None
        row = _run_queue_row(db_path, queue_id)
        assert row["status"] == "claimed"

        conn = get_connection(db_path)
        try:
            ledger = conn.execute(
                "SELECT * FROM bounded_run_claims WHERE bounded_run_id = ?", ("br_atomic",)
            ).fetchone()
        finally:
            conn.close()
        assert ledger is not None
        assert ledger["queue_id"] == queue_id
        assert ledger["episode_id"] == "ep_atomic"

    def test_second_claim_blocked_at_max_episode_claims_1(self, db_path):
        provision_bounded_run("br_cap1", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_first")
        _enqueue_with_episode(db_path, "ep_second")

        first = claim_next_bounded(worker_id="w1", bounded_run_id="br_cap1", db_path=db_path)
        assert first is not None

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_cap1", db_path=db_path)

        # The second item was never touched.
        assert _claim_count(db_path, "br_cap1") == 1
        run_row = _bounded_run_row(db_path, "br_cap1")
        assert run_row["status"] == "exhausted"

    def test_same_bounded_run_id_remains_blocked_after_process_restart(self, db_path):
        provision_bounded_run("br_restart2", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")
        _enqueue_with_episode(db_path, "ep_2")

        claim_next_bounded(worker_id="w1", bounded_run_id="br_restart2", db_path=db_path)

        # "Restart" == a brand-new call against the same on-disk db_path with
        # no in-memory state carried over -- every claim_next_bounded() call
        # already opens a fresh connection, so this is exactly that.
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w2-after-restart", bounded_run_id="br_restart2", db_path=db_path)

    def test_a_changed_worker_id_cannot_bypass_the_cap(self, db_path):
        provision_bounded_run("br_worker", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")
        _enqueue_with_episode(db_path, "ep_2")

        claim_next_bounded(worker_id="worker-A", bounded_run_id="br_worker", db_path=db_path)

        for other_worker in ("worker-B", "worker-C", "worker-A"):
            with pytest.raises(BoundedRunError):
                claim_next_bounded(worker_id=other_worker, bounded_run_id="br_worker", db_path=db_path)

        assert _claim_count(db_path, "br_worker") == 1

    def test_concurrent_claims_cannot_exceed_the_cap(self, db_path):
        provision_bounded_run("br_conc", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_a")
        _enqueue_with_episode(db_path, "ep_b")
        _enqueue_with_episode(db_path, "ep_c")

        results = []
        lock = threading.Lock()

        def _attempt(worker_id):
            try:
                r = claim_next_bounded(worker_id=worker_id, bounded_run_id="br_conc", db_path=db_path)
            except BoundedRunError:
                r = None
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_attempt, args=(f"w{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r is not None]
        assert len(successes) == 1
        assert _claim_count(db_path, "br_conc") == 1

    def test_failure_before_commit_consumes_no_allowance(self, db_path, monkeypatch):
        provision_bounded_run("br_fail", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = _enqueue_with_episode(db_path, "ep_fail")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure before commit")

        monkeypatch.setattr("hypogeum.queue.record_bounded_run_claim", _boom)

        with pytest.raises(RuntimeError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_fail", db_path=db_path)

        # Nothing persisted: queue item still queued, no ledger row, run still active.
        row = _run_queue_row(db_path, queue_id)
        assert row["status"] == "queued"
        assert _claim_count(db_path, "br_fail") == 0
        run_row = _bounded_run_row(db_path, "br_fail")
        assert run_row["status"] == "active"

    def test_release_after_committed_claim_does_not_restore_allowance(self, db_path):
        provision_bounded_run("br_release", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_release", db_path=db_path)
        release(claimed["queue_id"], "w1", db_path=db_path)

        row = _run_queue_row(db_path, claimed["queue_id"])
        assert row["status"] == "queued"  # released back
        assert _claim_count(db_path, "br_release") == 1  # ledger untouched

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_release", db_path=db_path)

    def test_downstream_processing_failure_does_not_restore_allowance(self, db_path):
        provision_bounded_run("br_downstream", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_downstream", db_path=db_path)
        queue_fail(claimed["queue_id"], "w1", error_message="pipeline blew up", db_path=db_path)

        row = _run_queue_row(db_path, claimed["queue_id"])
        assert row["status"] == "failed"
        assert _claim_count(db_path, "br_downstream") == 1

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_downstream", db_path=db_path)

    def test_worker_crash_restart_does_not_restore_allowance(self, db_path):
        provision_bounded_run("br_crash", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")
        _enqueue_with_episode(db_path, "ep_2")

        claim_next_bounded(worker_id="w1-crashed", bounded_run_id="br_crash", db_path=db_path)
        # Simulate a fresh worker process after a crash: new worker_id, new
        # connection (claim_next_bounded always opens one), no shared state.
        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w2-fresh-process", bounded_run_id="br_crash", db_path=db_path)
        assert _claim_count(db_path, "br_crash") == 1

    def test_cancelled_bounded_run_blocks_claims(self, db_path):
        provision_bounded_run("br_cancel", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")

        conn = get_connection(db_path)
        try:
            conn.execute("UPDATE bounded_runs SET status = 'cancelled' WHERE bounded_run_id = ?", ("br_cancel",))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_cancel", db_path=db_path)
        assert _claim_count(db_path, "br_cancel") == 0

    def test_exhausted_bounded_run_blocks_claims(self, db_path):
        provision_bounded_run("br_exh", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        _enqueue_with_episode(db_path, "ep_1")
        _enqueue_with_episode(db_path, "ep_2")

        claim_next_bounded(worker_id="w1", bounded_run_id="br_exh", db_path=db_path)
        run_row = _bounded_run_row(db_path, "br_exh")
        assert run_row["status"] == "exhausted"

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_exh", db_path=db_path)

    def test_null_episode_id_scheduler_row_is_bounded_claimed(self, db_path):
        """CORE CORRECTION: a fresh scheduler-originated row (episode_id
        starts NULL, exactly what tick()/consume_scheduled_runs() produce
        for a genuine autonomous discovery) must be a valid bounded
        candidate -- not excluded in favor of only pre-existing-episode_id
        items."""
        provision_bounded_run("br_epid", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        null_ep_queue_id = enqueue(
            db_path=db_path, channel_id="hypogeum", episode_id=None, priority=-100, source="schedule",
        )

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_epid", db_path=db_path)

        assert claimed is not None
        assert claimed["queue_id"] == null_ep_queue_id
        assert claimed["status"] == "claimed"
        # Synthesized using the exact existing fallback formula -- no new ID scheme.
        assert claimed["episode_id"] == f"ep_{null_ep_queue_id[:8]}"

    def test_synthesized_episode_id_and_all_four_effects_are_one_atomic_commit(self, db_path):
        """Queue claim, episode_id assignment, ledger insert, and the
        exhausted transition (cap=1) all land in a single commit."""
        provision_bounded_run("br_atomic4", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_atomic4", db_path=db_path)
        expected_episode_id = f"ep_{queue_id[:8]}"

        assert claimed["episode_id"] == expected_episode_id
        row = _run_queue_row(db_path, queue_id)
        assert row["status"] == "claimed"
        assert row["episode_id"] == expected_episode_id  # (1) queue claim + (2) episode_id assignment

        conn = get_connection(db_path)
        try:
            ledger = conn.execute(
                "SELECT * FROM bounded_run_claims WHERE bounded_run_id = ?", ("br_atomic4",)
            ).fetchone()
        finally:
            conn.close()
        assert ledger["episode_id"] == expected_episode_id  # (3) ledger insert

        run_row = _bounded_run_row(db_path, "br_atomic4")
        assert run_row["status"] == "exhausted"  # (4) exhausted transition

    def test_forced_failure_at_ledger_insert_leaves_all_four_effects_uncommitted(self, db_path, monkeypatch):
        provision_bounded_run("br_fail_ledger", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        monkeypatch.setattr(
            "hypogeum.queue.record_bounded_run_claim",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom at ledger insert")),
        )

        with pytest.raises(RuntimeError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_fail_ledger", db_path=db_path)

        row = _run_queue_row(db_path, queue_id)
        assert row["status"] == "queued"
        assert row["episode_id"] is None  # synthesis rolled back too
        assert _claim_count(db_path, "br_fail_ledger") == 0
        run_row = _bounded_run_row(db_path, "br_fail_ledger")
        assert run_row["status"] == "active"

    def test_forced_failure_at_exhausted_transition_leaves_all_four_effects_uncommitted(self, db_path, monkeypatch):
        provision_bounded_run("br_fail_exh", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        monkeypatch.setattr(
            "hypogeum.queue.mark_bounded_run_exhausted",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom at exhausted transition")),
        )

        with pytest.raises(RuntimeError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_fail_exh", db_path=db_path)

        # The ledger insert already executed inside the same transaction as
        # this forced failure -- but since nothing committed, it must be
        # rolled back too, along with the queue claim and episode_id write.
        row = _run_queue_row(db_path, queue_id)
        assert row["status"] == "queued"
        assert row["episode_id"] is None
        assert _claim_count(db_path, "br_fail_exh") == 0
        run_row = _bounded_run_row(db_path, "br_fail_exh")
        assert run_row["status"] == "active"

    def test_existing_episode_id_reused_not_resynthesized(self, db_path):
        provision_bounded_run("br_reuse", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        queue_id = _enqueue_with_episode(db_path, "ep_preexisting_rerun")

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_reuse", db_path=db_path)

        assert claimed["queue_id"] == queue_id
        assert claimed["episode_id"] == "ep_preexisting_rerun"
        assert claimed["episode_id"] != f"ep_{queue_id[:8]}"

    def test_bounded_ordering_matches_legacy_priority_fifo_ordering(self, db_path, tmp_path):
        """Bounded claiming now uses the exact same candidate SELECT as
        claim_next() (no episode_id filter) -- draining the same mixed
        priority/episode_id-known/NULL queue via each path must claim items
        in the same enqueue-index sequence."""
        legacy_db = tmp_path / "legacy_ordering.db"
        initialize_schema(legacy_db)

        specs = [
            ("hypogeum", "ep_a", 5),
            ("hypogeum", None, 0),
            ("hypogeum", "ep_b", 0),
            ("hypogeum", None, -10),
        ]
        bounded_ids_by_index = {}
        legacy_ids_by_index = {}
        for i, (channel_id, episode_id, priority) in enumerate(specs):
            qid = enqueue(db_path=db_path, channel_id=channel_id, episode_id=episode_id, priority=priority, source="schedule")
            bounded_ids_by_index[qid] = i
            lid = enqueue(db_path=legacy_db, channel_id=channel_id, episode_id=episode_id, priority=priority, source="schedule")
            legacy_ids_by_index[lid] = i

        provision_bounded_run("br_order", max_episode_claims=len(specs), publishing_allowed=False, db_path=db_path)

        bounded_order = []
        for i in range(len(specs)):
            claimed = claim_next_bounded(worker_id=f"w{i}", bounded_run_id="br_order", db_path=db_path)
            bounded_order.append(bounded_ids_by_index[claimed["queue_id"]])

        legacy_order = []
        for i in range(len(specs)):
            claimed = claim_next(worker_id=f"legacy{i}", db_path=legacy_db)
            legacy_order.append(legacy_ids_by_index[claimed["queue_id"]])

        assert bounded_order == legacy_order == [3, 1, 2, 0]  # priority ASC, then FIFO (queued_at)

    def test_cap_1_blocks_second_null_episode_candidate_leaves_it_null(self, db_path):
        provision_bounded_run("br_cap_null", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        first_queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")
        second_queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=1, source="schedule")

        first = claim_next_bounded(worker_id="w1", bounded_run_id="br_cap_null", db_path=db_path)
        assert first["queue_id"] == first_queue_id
        assert first["episode_id"] == f"ep_{first_queue_id[:8]}"

        with pytest.raises(BoundedRunError):
            claim_next_bounded(worker_id="w1", bounded_run_id="br_cap_null", db_path=db_path)

        second_row = _run_queue_row(db_path, second_queue_id)
        assert second_row["status"] == "queued"
        assert second_row["episode_id"] is None  # untouched -- never synthesized


# ===========================================================================
# 18/19. Legacy unbounded behavior unchanged
# ===========================================================================

class TestLegacyUnboundedUnchanged:
    def test_legacy_claim_next_never_synthesizes_episode_id(self, db_path):
        """claim_next() (no bounded_run_id anywhere) still claims an item
        with NULL episode_id, and NEVER synthesizes one -- proves
        synthesize_episode_id's default (False) preserves prior behavior
        exactly."""
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")
        claimed = claim_next(worker_id="w1", db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == queue_id
        assert claimed["episode_id"] is None

    def test_process_next_without_bounded_run_id_never_calls_claim_next_bounded(self, db_path, monkeypatch):
        from hypogeum.calog import set_channel_state
        set_channel_state("hypogeum", "active", db_path=db_path)

        called = []
        monkeypatch.setattr(
            "hypogeum.queue.claim_next_bounded",
            lambda *a, **k: called.append(1) or None,
        )
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_x", priority=0, source="manual")

        pr = process_next(worker_id="w1", db_path=db_path, dry_run=True)

        assert called == []
        assert pr.status == "completed"

    def test_existing_queue_priority_and_fifo_ordering_unchanged(self, db_path):
        first = _enqueue_with_episode(db_path, "ep_lowpri", priority=5)
        second = _enqueue_with_episode(db_path, "ep_highpri", priority=0)
        third = _enqueue_with_episode(db_path, "ep_highpri_later", priority=0)

        c1 = claim_next(worker_id="w1", db_path=db_path)
        c2 = claim_next(worker_id="w1", db_path=db_path)
        c3 = claim_next(worker_id="w1", db_path=db_path)

        # priority 0 items first (FIFO among themselves), then priority 5.
        assert c1["queue_id"] == second
        assert c2["queue_id"] == third
        assert c3["queue_id"] == first


# ===========================================================================
# 16. Publishing prohibition — publishing_allowed=0
# ===========================================================================

def _make_operations_profile(db_path, channel_id="hypogeum"):
    from hypogeum.channel_config import create_channel_operations_profile, create_channel_profile
    from hypogeum.calog import set_channel_state

    create_channel_profile(channel_id=channel_id, name="Test Channel", db_path=db_path)
    profile = create_channel_operations_profile(
        channel_id=channel_id,
        channel_name="Test Channel",
        timezone="Asia/Dubai",
        operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        blocked_platform_reasons={},
        discovery_enabled=True,
        production_enabled=True,
        publishing_enabled=True,
        analytics_sync_enabled=True,
        unattended_operation_enabled=False,
        max_concurrent_episodes=5,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
        failure_halt_policy={
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        db_path=db_path,
    )
    set_channel_state(channel_id, "active", db_path=db_path)
    return profile


def _patch_pipeline_capture(monkeypatch, call_log):
    def _fake(*, topic_input, episode_id, config, channel_id, db_path=None,
              cancel_check=None, publishing_platform_filter=None, youtube_privacy=None):
        call_log.append({
            "episode_id": episode_id,
            "publishing_platform_filter": publishing_platform_filter,
        })
    monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake)


class TestDivergenceGuard:
    def test_divergent_topic_episode_id_fails_closed_zero_pipeline_calls(self, db_path):
        _make_operations_profile(db_path)
        provision_bounded_run("br_divergence", max_episode_claims=1, publishing_allowed=False, db_path=db_path)

        # A stale/conflicting topic row that already carries a DIFFERENT
        # episode_id than what the bounded claim will synthesize below.
        enqueue_topic(
            "hypogeum", "Conflicting Topic", priority=0, validate_channel=True,
            db_path=db_path, episode_id="ep_conflicting_preexisting",
        )
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        call_log = []

        def _spy_executor(*, topic_input, episode_id, config, channel_id,
                           cancel_check=None, queue_id=None, worker_id=None):
            call_log.append({"episode_id": episode_id})

        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=_spy_executor, bounded_run_id="br_divergence",
        )

        assert pr.status == "failed"
        assert "divergence" in (pr.error_message or "").lower()
        assert call_log == []  # zero pipeline/provider/publishing calls

        synthesized = f"ep_{queue_id[:8]}"
        row = _run_queue_row(db_path, queue_id)
        assert row["episode_id"] == synthesized  # ledgered identity never overwritten
        assert row["status"] == "failed"

        conn = get_connection(db_path)
        try:
            ledger = conn.execute(
                "SELECT episode_id FROM bounded_run_claims WHERE bounded_run_id = ?",
                ("br_divergence",),
            ).fetchone()
        finally:
            conn.close()
        assert ledger["episode_id"] == synthesized  # ledger row never overwritten either

    def test_full_autonomous_process_next_uses_same_ledgered_episode_id(self, db_path):
        """Non-divergent case: a genuine fresh discovery topic (episode_id
        NULL on the topic row, exactly what topic_discovery/enqueue_topic
        produces for source='discovered') -- the value passed to the
        pipeline must equal the already-ledgered episode_id."""
        _make_operations_profile(db_path)
        provision_bounded_run("br_full_auto", max_episode_claims=1, publishing_allowed=False, db_path=db_path)

        enqueue_topic(
            "hypogeum", "Freshly Discovered Topic", priority=0, validate_channel=True,
            db_path=db_path, source="discovered",
        )
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        call_log = []

        def _spy_executor(*, topic_input, episode_id, config, channel_id,
                           cancel_check=None, queue_id=None, worker_id=None):
            call_log.append({"episode_id": episode_id, "topic_input": topic_input})

        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=_spy_executor, bounded_run_id="br_full_auto",
        )

        expected = f"ep_{queue_id[:8]}"
        assert pr.status == "completed"
        assert len(call_log) == 1
        assert call_log[0]["episode_id"] == expected

        conn = get_connection(db_path)
        try:
            ledger = conn.execute(
                "SELECT episode_id FROM bounded_run_claims WHERE bounded_run_id = ?",
                ("br_full_auto",),
            ).fetchone()
        finally:
            conn.close()
        assert ledger["episode_id"] == expected


class TestPublishingProhibition:
    def test_publishing_allowed_false_forces_empty_filter_despite_enabled_platform(self, db_path, monkeypatch):
        _make_operations_profile(db_path)
        provision_bounded_run("br_pub_off", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        call_log = []
        _patch_pipeline_capture(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_pub_off", priority=0, source="manual")

        from hypogeum import worker_integration as wi
        executor = wi.build_gated_pipeline_executor(db_path=db_path, bounded_run_id="br_pub_off")
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            topic_resolver=lambda row: "Test Topic", pipeline_executor=executor,
            bounded_run_id="br_pub_off",
        )

        assert pr.status == "completed"
        assert len(call_log) == 1
        assert call_log[0]["publishing_platform_filter"] == frozenset()  # active filter, zero platforms

    def test_publishing_allowed_true_leaves_profile_filter_intact(self, db_path, monkeypatch):
        _make_operations_profile(db_path)
        provision_bounded_run("br_pub_on", max_episode_claims=1, publishing_allowed=True, db_path=db_path)
        call_log = []
        _patch_pipeline_capture(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_pub_on", priority=0, source="manual")

        from hypogeum import worker_integration as wi
        executor = wi.build_gated_pipeline_executor(db_path=db_path, bounded_run_id="br_pub_on")
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            topic_resolver=lambda row: "Test Topic", pipeline_executor=executor,
            bounded_run_id="br_pub_on",
        )

        assert pr.status == "completed"
        assert call_log[0]["publishing_platform_filter"] == frozenset({"youtube"})

    def test_unprovisioned_bounded_run_id_in_executor_fails_closed_to_zero_platforms(self, db_path, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_capture(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_unknown_br", priority=0, source="manual")

        from hypogeum import worker_integration as wi
        # bounded_run_id passed to the executor but never provisioned --
        # must not be treated as "publishing authorized".
        executor = wi.build_gated_pipeline_executor(db_path=db_path, bounded_run_id="br_never_provisioned")
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            topic_resolver=lambda row: "Test Topic", pipeline_executor=executor,
        )

        assert pr.status == "completed"
        assert call_log[0]["publishing_platform_filter"] == frozenset()

    def test_unscoped_item_with_bounded_forced_off_passes_empty_not_none_filter(self, db_path, monkeypatch):
        """channel_id=None branch: publishing_platform_filter=None means
        UNRESTRICTED downstream, so bounded-forced-off must pass frozenset(),
        never None, even when there is no channel to gate."""
        provision_bounded_run("br_unscoped", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        call_log = []

        def _fake(*, topic_input, episode_id, config, channel_id, db_path=None,
                  cancel_check=None, publishing_platform_filter=None):
            call_log.append({"publishing_platform_filter": publishing_platform_filter})
        monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake)

        enqueue(db_path=db_path, channel_id=None, episode_id="ep_unscoped", priority=0, source="manual")

        from hypogeum import worker_integration as wi
        executor = wi.build_gated_pipeline_executor(db_path=db_path, bounded_run_id="br_unscoped")
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            topic_resolver=lambda row: "Test Topic", pipeline_executor=executor,
        )

        assert pr.status == "completed"
        assert call_log[0]["publishing_platform_filter"] == frozenset()

    def test_publishing_allowed_false_blocks_all_platforms_for_synthesized_episode_id(self, db_path, monkeypatch):
        """The actual target scenario: a freshly-discovered, NULL-episode_id
        item claimed autonomously (not a pre-existing --gated-episode-id) --
        publishing_allowed=0 must still block every platform."""
        _make_operations_profile(db_path)
        provision_bounded_run("br_pub_synth", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        call_log = []
        _patch_pipeline_capture(monkeypatch, call_log)

        enqueue_topic(
            "hypogeum", "Freshly Discovered Topic", priority=0, validate_channel=True,
            db_path=db_path, source="discovered",
        )
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        from hypogeum import worker_integration as wi
        executor = wi.build_gated_pipeline_executor(db_path=db_path, bounded_run_id="br_pub_synth")
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=executor, bounded_run_id="br_pub_synth",
        )

        assert pr.status == "completed"
        assert len(call_log) == 1
        assert call_log[0]["episode_id"] == f"ep_{queue_id[:8]}"
        assert call_log[0]["publishing_platform_filter"] == frozenset()


# ===========================================================================
# 20. Schema installation
# ===========================================================================

class TestSchemaInstallation:
    def test_schema_creates_tables_with_expected_constraints(self, db_path):
        conn = get_connection(db_path)
        try:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bounded_run%'"
                ).fetchall()
            }
            assert names == {"bounded_runs", "bounded_run_claims"}

            # No orphaned rejected-design artifact (mutable claims_consumed
            # counter table) leaks in via a fresh schema install.
            assert not conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'bounded_run_allowances'"
            ).fetchone()
        finally:
            conn.close()

    def test_max_episode_claims_check_constraint(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_runs (bounded_run_id, max_episode_claims, created_at) "
                    "VALUES ('bad', 0, '2026-01-01T00:00:00+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_publishing_allowed_check_constraint(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_runs (bounded_run_id, max_episode_claims, publishing_allowed, created_at) "
                    "VALUES ('bad2', 1, 7, '2026-01-01T00:00:00+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_status_check_constraint(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_runs (bounded_run_id, max_episode_claims, created_at, status) "
                    "VALUES ('bad3', 1, '2026-01-01T00:00:00+00:00', 'not_a_real_status')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_bounded_run_claims_rejects_null_episode_id(self, db_path):
        """The corrective design deliberately synthesizes episode_id inside
        the claim transaction rather than relaxing this constraint --
        confirms bounded_run_claims.episode_id is still NOT NULL."""
        provision_bounded_run("br_notnull", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                    "VALUES ('br_notnull', 'q1', NULL, '2026-01-01T00:00:00+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_bounded_run_claims_unique_episode_id_per_run(self, db_path):
        provision_bounded_run("br_unique", max_episode_claims=5, publishing_allowed=False, db_path=db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                "VALUES ('br_unique', 'q1', 'ep_dup', '2026-01-01T00:00:00+00:00')"
            )
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                    "VALUES ('br_unique', 'q2', 'ep_dup', '2026-01-01T00:00:01+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_bounded_run_claims_primary_key_per_queue_id(self, db_path):
        provision_bounded_run("br_pk", max_episode_claims=5, publishing_allowed=False, db_path=db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                "VALUES ('br_pk', 'q1', 'ep_1', '2026-01-01T00:00:00+00:00')"
            )
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                    "VALUES ('br_pk', 'q1', 'ep_2', '2026-01-01T00:00:01+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_bounded_run_claims_fk_to_bounded_runs(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at) "
                    "VALUES ('does_not_exist', 'q1', 'ep_1', '2026-01-01T00:00:00+00:00')"
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_schema_installation_is_idempotent(self, db_path):
        provision_bounded_run("br_idempotent", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        initialize_schema(db_path)  # second call must not drop/recreate/error
        initialize_schema(db_path)  # third call, same
        row = _bounded_run_row(db_path, "br_idempotent")
        assert row is not None
        assert row["status"] == "active"


# ===========================================================================
# 13/14/15/17. Normal autonomous worker wiring (run_worker.py)
# ===========================================================================

class TestNormalAutonomousWorkerWiring:
    def test_bounded_run_id_alone_reaches_run_worker_not_gated_cli(self, monkeypatch, tmp_path):
        """--bounded-run-id does NOT require --gated-episode-id -- it must
        reach the normal daemon-loop entry point (run_worker()), never the
        manual/gated CLI path."""
        import run_worker

        captured = {}

        def _spy_run_worker(**kwargs):
            captured.update(kwargs)
            return 0
        monkeypatch.setattr(run_worker, "run_worker", _spy_run_worker)

        def _fail_gated_cli(*a, **k):
            raise AssertionError("must not enter the gated/manual CLI path")
        monkeypatch.setattr(run_worker, "_run_gated_cli", _fail_gated_cli)

        rc = run_worker.main(["--once", "--bounded-run-id", "br_daemon", "--db-path", str(tmp_path / "x.db")])

        assert rc == 0
        assert captured.get("bounded_run_id") == "br_daemon"

    def test_run_iteration_forwards_bounded_run_id_to_poll_and_executor(self, tmp_path, monkeypatch):
        import run_worker

        iter_db_path = tmp_path / "iter.db"
        initialize_schema(iter_db_path)

        class _FakeClearance:
            cleared = True
            reason = None
            validation_exemption = None
        monkeypatch.setattr(
            "hypogeum.factory_director.check_run_clearance", lambda **k: _FakeClearance(),
        )

        captured_executor_kwargs = {}

        def _spy_build_executor(db_path=None, project_root=None, bounded_run_id=None):
            captured_executor_kwargs["bounded_run_id"] = bounded_run_id
            return lambda **k: None
        monkeypatch.setattr(
            "hypogeum.worker_integration.build_gated_pipeline_executor", _spy_build_executor,
        )

        from hypogeum.queue import PollResult

        captured_poll_kwargs = {}

        def _spy_poll(**kwargs):
            captured_poll_kwargs.update(kwargs)
            return PollResult()
        monkeypatch.setattr("hypogeum.queue.poll", _spy_poll)

        run_worker.run_iteration(
            worker_id="w1", db_path=iter_db_path, dry_run=False, channel_id=None,
            config=object(), iteration=1, bounded_run_id="br_iter",
        )

        assert captured_executor_kwargs.get("bounded_run_id") == "br_iter"
        assert captured_poll_kwargs.get("bounded_run_id") == "br_iter"

    def test_run_worker_rejects_unknown_bounded_run_before_any_iteration(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "reject_unknown.db"

        def _fail_iteration(**k):
            raise AssertionError("run_iteration must never be called for an unknown bounded_run_id")
        monkeypatch.setattr(run_worker, "run_iteration", _fail_iteration)

        rc = run_worker.run_worker(
            db_path=db_path, dry_run=True, interval=0, once=True, channel_id=None,
            bounded_run_id="br_never_provisioned",
        )
        assert rc == 1

    def test_run_worker_rejects_exhausted_bounded_run_before_any_iteration(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "reject_exhausted.db"
        initialize_schema(db_path)
        provision_bounded_run("br_exh_daemon", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        conn = get_connection(db_path)
        try:
            conn.execute("UPDATE bounded_runs SET status = 'exhausted' WHERE bounded_run_id = ?", ("br_exh_daemon",))
            conn.commit()
        finally:
            conn.close()

        def _fail_iteration(**k):
            raise AssertionError("run_iteration must never be called for an exhausted bounded_run_id")
        monkeypatch.setattr(run_worker, "run_iteration", _fail_iteration)

        rc = run_worker.run_worker(
            db_path=db_path, dry_run=True, interval=0, once=True, channel_id=None,
            bounded_run_id="br_exh_daemon",
        )
        assert rc == 1

    def test_run_worker_rejects_cancelled_bounded_run_before_any_iteration(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "reject_cancelled.db"
        initialize_schema(db_path)
        provision_bounded_run("br_cancel_daemon", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        conn = get_connection(db_path)
        try:
            conn.execute("UPDATE bounded_runs SET status = 'cancelled' WHERE bounded_run_id = ?", ("br_cancel_daemon",))
            conn.commit()
        finally:
            conn.close()

        def _fail_iteration(**k):
            raise AssertionError("run_iteration must never be called for a cancelled bounded_run_id")
        monkeypatch.setattr(run_worker, "run_iteration", _fail_iteration)

        rc = run_worker.run_worker(
            db_path=db_path, dry_run=True, interval=0, once=True, channel_id=None,
            bounded_run_id="br_cancel_daemon",
        )
        assert rc == 1

    def test_publishing_allowed_flag_without_provisioning_fails_closed(self, tmp_path):
        import run_worker
        rc = run_worker.main(["--publishing-allowed", "--db-path", str(tmp_path / "x.db")])
        assert rc == 1

    def test_max_episode_claims_flag_without_provisioning_fails_closed(self, tmp_path):
        import run_worker
        rc = run_worker.main(["--max-episode-claims", "3", "--db-path", str(tmp_path / "x.db")])
        assert rc == 1

    def test_invalid_bounded_run_id_never_falls_back_to_unbounded_claim(self, db_path, monkeypatch):
        called = {"claim_next": False}

        def _spy_claim_next(*a, **k):
            called["claim_next"] = True
            return None
        monkeypatch.setattr("hypogeum.queue.claim_next", _spy_claim_next)

        enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")

        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=True,
            bounded_run_id="br_never_provisioned_2",
        )

        assert pr.status == "empty"
        assert called["claim_next"] is False
