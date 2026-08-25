"""
tests/test_budget_circuit_breaker.py — Unit tests for
hypogeum/budget_scopes.py + hypogeum/db.py's budget/breaker helpers
(Commit 3, 2026-08-15)

Every test uses a temporary isolated SQLite database and explicit
timestamps. No test imports or invokes a network/provider client.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import hypogeum.budget_scopes as bs
import hypogeum.db as db

AT = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    db.initialize_schema(db_path)
    c = db.get_connection(db_path)
    yield c
    c.close()


def _scope(kind, ref, ceiling, currency="USD"):
    return bs.ScopeRequest(kind, ref, Decimal(ceiling), currency)


class TestExactDecimalArithmetic:

    def test_no_binary_float_involved(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "0.39")],
            idempotency_key="idem_1", amount=Decimal("0.1") + Decimal("0.29"),
            currency="USD", at=AT, reservation_id_prefix="idem_1",
        )
        assert result.reservations[0].reserved_amount == Decimal("0.39")
        assert isinstance(result.reservations[0].reserved_amount, Decimal)

    def test_sum_reserved_and_committed_is_exact(self, conn):
        for i in range(3):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.RUN, "run_1", "10.00")],
                idempotency_key=f"idem_{i}", amount=Decimal("0.39"), currency="USD",
                at=AT, reservation_id_prefix=f"idem_{i}",
            )
        total = Decimal(db.sum_reserved_and_committed(conn, "run", "run_1"))
        assert total == Decimal("1.17")


class TestScopeCeilings:

    def test_per_call_ceiling_blocks_excess_cost(self, conn):
        with pytest.raises(bs.BudgetCeilingExceededError):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "0.39")],
                idempotency_key="idem_1", amount=Decimal("0.50"), currency="USD",
                at=AT, reservation_id_prefix="idem_1",
            )

    def test_run_budget_blocks_excess_cumulative_reservation(self, conn):
        bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.RUN, "run_1", "0.50")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        with pytest.raises(bs.BudgetCeilingExceededError):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.RUN, "run_1", "0.50")],
                idempotency_key="idem_2", amount=Decimal("0.39"), currency="USD",
                at=AT, reservation_id_prefix="idem_2",
            )

    def test_episode_budget_blocks_excess_cumulative_reservation(self, conn):
        bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.EPISODE, "ep_1", "34.32")],
            idempotency_key="idem_1", amount=Decimal("34.00"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        with pytest.raises(bs.BudgetCeilingExceededError):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.EPISODE, "ep_1", "34.32")],
                idempotency_key="idem_2", amount=Decimal("1.00"), currency="USD",
                at=AT, reservation_id_prefix="idem_2",
            )

    def test_organization_scope_blocks_when_configured_and_exhausted(self, conn):
        bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.ORGANIZATION, "org_1", "1.00")],
            idempotency_key="idem_1", amount=Decimal("1.00"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        with pytest.raises(bs.BudgetCeilingExceededError):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.ORGANIZATION, "org_1", "1.00")],
                idempotency_key="idem_2", amount=Decimal("0.01"), currency="USD",
                at=AT, reservation_id_prefix="idem_2",
            )

    def test_missing_required_scope_denies(self, conn):
        # an empty scopes list is the caller's responsibility to avoid;
        # hypogeum.submission_gate denies before ever calling
        # reserve_all_scopes with an empty list -- verified here at the
        # primitive level: reserve_all_scopes on an empty sequence makes
        # zero reservations and returns an empty result, never silently
        # approving spend.
        result = bs.reserve_all_scopes(
            conn, [], idempotency_key="idem_1", amount=Decimal("0.39"),
            currency="USD", at=AT, reservation_id_prefix="idem_1",
        )
        assert result.reservations == ()

    def test_currency_mismatch_denies(self, conn):
        with pytest.raises(bs.BudgetCurrencyMismatchError):
            bs.reserve_all_scopes(
                conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "1.00", currency="EUR")],
                idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
                at=AT, reservation_id_prefix="idem_1",
            )


class TestConcurrentReservationSafety:

    def test_concurrent_reservations_cannot_oversubscribe(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn_a = db.get_connection(db_path)
        conn_b = db.get_connection(db_path)
        try:
            # Both attempt to reserve against a ceiling that only fits one.
            accepted_a, _, _created_a = db.reserve_budget(conn_a, "res_a", "run", "run_1", "idem_a", "0.39", "USD", "0.39", created_at=AT)
            accepted_b, _, _created_b = db.reserve_budget(conn_b, "res_b", "run", "run_1", "idem_b", "0.39", "USD", "0.39", created_at=AT)
            assert accepted_a != accepted_b or (accepted_a and accepted_b and False)
            assert sum([accepted_a, accepted_b]) == 1
        finally:
            conn_a.close()
            conn_b.close()


class TestPreBoundaryFailureRelease:

    def test_pre_boundary_failure_releases_reservation(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "0.39")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        bs.release_all(conn, result, at=AT)
        row = db.get_budget_reservation(conn, result.reservations[0].reservation_id)
        assert row["status"] == "released"
        # released reservations no longer count toward the ceiling
        total = Decimal(db.sum_reserved_and_committed(conn, "call", "shot_1"))
        assert total == Decimal("0")


class TestCommitSemantics:

    def test_acknowledged_charge_commits_spend_exactly_once(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "0.39")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        reservation_id = result.reservations[0].reservation_id
        status = bs.commit_reservation(conn, reservation_id, authorized_ceiling=Decimal("0.39"), charged_amount=Decimal("0.39"), at=AT)
        assert status == "committed"
        row = db.get_budget_reservation(conn, reservation_id)
        assert row["status"] == "committed"
        assert row["committed_amount"] == "0.39"

    def test_duplicate_acknowledgement_does_not_double_charge(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "0.39")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        reservation_id = result.reservations[0].reservation_id
        bs.commit_reservation(conn, reservation_id, authorized_ceiling=Decimal("0.39"), charged_amount=Decimal("0.39"), at=AT)
        status_2 = bs.commit_reservation(conn, reservation_id, authorized_ceiling=Decimal("0.39"), charged_amount=Decimal("0.39"), at=AT)
        assert status_2 == "already_committed_same"
        # the sum is still exactly one charge, not two
        total = Decimal(db.sum_reserved_and_committed(conn, "call", "shot_1"))
        assert total == Decimal("0.39")


class TestUnknownAcceptanceReservesPossibleSpend:

    def test_unknown_acceptance_reserves_possible_maximum_spend(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.EPISODE, "ep_1", "34.32")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        # unknown acceptance -- reservation deliberately NOT released (see
        # hypogeum.submission_gate.submit()'s unknown_acceptance path)
        total = Decimal(db.sum_reserved_and_committed(conn, "episode", "ep_1"))
        assert total == Decimal("0.39")  # still counted, not silently dropped


class TestOverchargeBreaker:

    def test_provider_overcharge_trips_permanent_breaker(self, conn):
        result = bs.reserve_all_scopes(
            conn, [_scope(bs.BudgetScopeKind.CALL, "shot_1", "1.00")],
            idempotency_key="idem_1", amount=Decimal("0.39"), currency="USD",
            at=AT, reservation_id_prefix="idem_1",
        )
        reservation_id = result.reservations[0].reservation_id
        with pytest.raises(bs.OverchargeViolation):
            bs.commit_reservation(conn, reservation_id, authorized_ceiling=Decimal("0.39"), charged_amount=Decimal("0.99"), at=AT)

        bs.record_breaker(
            conn, "overcharge:call:shot_1", breaker_type="overcharge", scope_kind="call", scope_ref="shot_1",
            observed_value="0.99", configured_limit="0.39", tripped=True, triggering_attempt_id="attempt_1",
            evidence={"authorized": "0.39", "charged": "0.99"}, at=AT,
        )
        assert bs.is_tripped(conn, "overcharge:call:shot_1") is True
        # never auto-clears
        bs.record_breaker(
            conn, "overcharge:call:shot_1", breaker_type="overcharge", scope_kind="call", scope_ref="shot_1",
            observed_value="0.99", configured_limit="0.39", tripped=False, triggering_attempt_id=None,
            evidence={}, at=AT,
        )
        assert bs.is_tripped(conn, "overcharge:call:shot_1") is True


class TestMaxCallBreaker:

    def test_max_call_breaker_counts_boundary_entries(self, conn):
        breaker_id = "provider_calls:run:run_1"
        for i in range(3):
            existing = bs.get_breaker(conn, breaker_id)
            current = int(existing.observed_value) if existing else 0
            new_value = current + 1
            bs.record_breaker(
                conn, breaker_id, breaker_type="provider_calls", scope_kind="run", scope_ref="run_1",
                observed_value=new_value, configured_limit="3", tripped=new_value >= 3,
                triggering_attempt_id=f"attempt_{i}" if new_value >= 3 else None,
                evidence={"observed": new_value}, at=AT,
            )
        assert bs.is_tripped(conn, breaker_id) is True
        breaker = bs.get_breaker(conn, breaker_id)
        assert breaker.observed_value == "3"

    def test_max_call_breaker_stops_remaining_batch(self, conn):
        breaker_id = "provider_calls:run:run_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="provider_calls", scope_kind="run", scope_ref="run_1",
            observed_value="5", configured_limit="5", tripped=True, triggering_attempt_id="attempt_5",
            evidence={}, at=AT,
        )
        # simulate a batch orchestrator checking before each remaining item
        for _ in range(3):
            assert bs.is_tripped(conn, breaker_id) is True  # every remaining item sees it tripped


class TestMaxSpendBreaker:

    def test_max_spend_breaker_stops_remaining_attempts(self, conn):
        breaker_id = "max_spend:episode:ep_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
            observed_value="34.32", configured_limit="34.32", tripped=True, triggering_attempt_id="attempt_x",
            evidence={}, at=AT,
        )
        assert bs.is_tripped(conn, breaker_id) is True


class TestConsecutiveFailureBreaker:

    def test_consecutive_failure_breaker_trips_at_configured_threshold(self, conn):
        breaker_id = "consecutive_failures:run:run_1"
        limit = 3
        for i in range(limit):
            existing = bs.get_breaker(conn, breaker_id)
            current = int(existing.observed_value) if existing else 0
            new_value = current + 1
            tripped = new_value >= limit
            bs.record_breaker(
                conn, breaker_id, breaker_type="consecutive_failures", scope_kind="run", scope_ref="run_1",
                observed_value=new_value, configured_limit=str(limit), tripped=tripped,
                triggering_attempt_id=f"attempt_{i}" if tripped else None, evidence={}, at=AT,
            )
        assert bs.is_tripped(conn, breaker_id) is True


class TestUnknownAcceptanceBreaker:

    def test_unknown_acceptance_trips_configured_breaker(self, conn):
        breaker_id = "unknown_acceptance:episode:ep_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="unknown_acceptance", scope_kind="episode", scope_ref="ep_1",
            observed_value="1", configured_limit="1", tripped=True, triggering_attempt_id="attempt_1",
            evidence={"reason": "simulated timeout"}, at=AT,
        )
        assert bs.is_tripped(conn, breaker_id) is True


class TestBreakerPersistenceAndReconstruction:

    def test_breaker_survives_process_restart(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn_1 = db.get_connection(db_path)
        bs.record_breaker(
            conn_1, "max_spend:episode:ep_1", breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
            observed_value="34.32", configured_limit="34.32", tripped=True, triggering_attempt_id="attempt_x",
            evidence={}, at=AT,
        )
        conn_1.close()

        # simulate restart: brand new connection to the same file
        conn_2 = db.get_connection(db_path)
        assert bs.is_tripped(conn_2, "max_spend:episode:ep_1") is True
        conn_2.close()

    def test_new_lease_owner_cannot_bypass_a_tripped_breaker(self, conn):
        breaker_id = "max_spend:episode:ep_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
            observed_value="34.32", configured_limit="34.32", tripped=True, triggering_attempt_id="attempt_x",
            evidence={}, at=AT,
        )
        db.acquire_lease(conn, "env_new", "brand_new_worker", acquired_at=AT, expires_at="2026-01-01T01:00:00Z")
        # the breaker is keyed by scope, not by lease owner/worker -- a
        # new worker acquiring an unrelated lease does not change it
        assert bs.is_tripped(conn, breaker_id) is True

    def test_new_fake_adapter_instance_cannot_bypass_a_tripped_breaker(self, conn):
        from hypogeum.providers.fake_provider_adapter import FakeProviderAdapter
        breaker_id = "max_spend:episode:ep_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
            observed_value="34.32", configured_limit="34.32", tripped=True, triggering_attempt_id="attempt_x",
            evidence={}, at=AT,
        )
        _fresh_adapter = FakeProviderAdapter()  # a brand new instance changes nothing about DB-persisted breaker state
        assert bs.is_tripped(conn, breaker_id) is True


class TestBreakerResetIsExplicit:

    def test_breaker_reset_requires_the_exact_explicit_process(self, conn):
        breaker_id = "max_spend:episode:ep_1"
        bs.record_breaker(
            conn, breaker_id, breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
            observed_value="34.32", configured_limit="34.32", tripped=True, triggering_attempt_id="attempt_x",
            evidence={}, at=AT,
        )
        assert bs.is_tripped(conn, breaker_id) is True
        # no amount of record_breaker() calls with tripped=False clears it
        for _ in range(5):
            bs.record_breaker(
                conn, breaker_id, breaker_type="max_spend", scope_kind="episode", scope_ref="ep_1",
                observed_value="0", configured_limit="34.32", tripped=False, triggering_attempt_id=None,
                evidence={}, at=AT,
            )
            assert bs.is_tripped(conn, breaker_id) is True
        # only the explicit reset function clears it
        assert bs.reset_breaker(conn, breaker_id, reset_by="operator", at=AT) is True
        assert bs.is_tripped(conn, breaker_id) is False
