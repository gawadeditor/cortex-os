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

    def test_bounded_claiming_skips_items_without_episode_id(self, db_path):
        provision_bounded_run("br_epid", max_episode_claims=1, publishing_allowed=False, db_path=db_path)
        # No episode_id -- a topic-backlog-driven item, not yet resolved.
        backlog_queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=-100, source="schedule")
        eligible_queue_id = _enqueue_with_episode(db_path, "ep_known", priority=0)

        claimed = claim_next_bounded(worker_id="w1", bounded_run_id="br_epid", db_path=db_path)

        assert claimed is not None
        assert claimed["queue_id"] == eligible_queue_id
        backlog_row = _run_queue_row(db_path, backlog_queue_id)
        assert backlog_row["status"] == "queued"  # untouched


# ===========================================================================
# 18/19. Legacy unbounded behavior unchanged
# ===========================================================================

class TestLegacyUnboundedUnchanged:
    def test_legacy_claim_next_unaffected_by_bounded_addition(self, db_path):
        """claim_next() (no bounded_run_id anywhere) still claims an item
        with NULL episode_id -- proves require_episode_id's default (False)
        preserves prior behavior exactly."""
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id=None, priority=0, source="schedule")
        claimed = claim_next(worker_id="w1", db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == queue_id

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
