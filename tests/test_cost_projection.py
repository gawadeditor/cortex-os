"""
tests/test_cost_projection.py — Focused tests for
hypogeum/cost_projection.py: the idempotent bridge from the
authoritative protected-execution spend ledger
(run_authorization_grant_consumption) to dashboard/legacy cost
reporting (costs + protected_path_cost_projections).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

import hypogeum.authorization_store as store
import hypogeum.cost_projection as cp


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, total_cost_usd REAL)
        """
    )
    c.execute(
        """
        CREATE TABLE costs (
            cost_id TEXT PRIMARY KEY, run_id TEXT, episode_id TEXT, provider TEXT,
            model TEXT, call_type TEXT, tokens_or_credits REAL, cost_usd REAL,
            called_at TEXT, metadata_version TEXT DEFAULT 'v1'
        )
        """
    )
    store.ensure_tables(c)
    cp.ensure_tables(c)
    yield c
    c.close()


def _insert_committed_ledger_row(
    conn, *, reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x",
    channel_id="chan_x", provider="fake", model="fake-model", provider_operation="video.text_to_video",
    authorization_id="auth1", idempotency_key="idem1", charged_amount="0.39", currency="USD",
):
    conn.execute(
        """
        INSERT INTO run_authorization_grants
            (grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
             workflow_version, workflow_checksum, bindings_json, max_total_spend_all_bindings,
             currency, expires_at, approval_id, approval_version, status, registered_at)
        VALUES (?, ?, 'checksum1', ?, ?, 'wf', '1.0', 'wfchecksum', '[]', NULL, ?, '2099-01-01T00:00:00Z',
                'approval1', 1, 'active', '2026-01-01T00:00:00Z')
        """,
        (grant_id, grant_version, channel_id, episode_id, currency),
    )
    conn.execute(
        """
        INSERT INTO run_authorization_grant_consumption
            (reservation_id, grant_id, grant_version, provider, model, provider_operation,
             idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount,
             state, reconciliation_resolution, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'env1', ?, ?, ?, 'committed', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (reservation_id, grant_id, grant_version, provider, model, provider_operation,
         idempotency_key, authorization_id, charged_amount, charged_amount),
    )
    conn.commit()


class TestProjectCommittedCharge:

    def test_inserts_one_row(self, conn):
        inserted = cp.project_committed_charge(
            conn, reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x",
            channel_id="chan_x", provider="fake", model="fake-model", provider_operation="video.text_to_video",
            authorization_id="auth1", charged_amount=Decimal("0.39"), currency="USD", idempotency_key="idem1",
        )
        assert inserted is True
        total = cp.get_projected_total_for_episode(conn, "ep_x")
        assert total == Decimal("0.39")

    def test_replay_is_idempotent(self, conn):
        kwargs = dict(
            reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x", channel_id="chan_x",
            provider="fake", model="fake-model", provider_operation="video.text_to_video",
            authorization_id="auth1", charged_amount=Decimal("0.39"), currency="USD", idempotency_key="idem1",
        )
        first = cp.project_committed_charge(conn, **kwargs)
        second = cp.project_committed_charge(conn, **kwargs)
        assert first is True
        assert second is False
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")


class TestReconcileAllCommittedCharges:

    def test_reconciles_a_committed_ledger_row(self, conn):
        _insert_committed_ledger_row(conn)
        result = cp.reconcile_all_committed_charges(conn)
        assert result == {"projected": 1, "already_present": 0, "total_committed_ledger_rows": 1}
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")

    def test_reconciliation_is_idempotent(self, conn):
        _insert_committed_ledger_row(conn)
        cp.reconcile_all_committed_charges(conn)
        second = cp.reconcile_all_committed_charges(conn)
        assert second == {"projected": 0, "already_present": 1, "total_committed_ledger_rows": 1}
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")

    def test_non_committed_states_are_never_projected(self, conn):
        """reserved/boundary_entered/released/possible_unknown_acceptance
        rows must never appear in dashboard-visible cost -- only a
        confirmed, charged 'committed' outcome is real spend."""
        conn.execute(
            """
            INSERT INTO run_authorization_grants
                (grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
                 workflow_version, workflow_checksum, bindings_json, max_total_spend_all_bindings,
                 currency, expires_at, approval_id, approval_version, status, registered_at)
            VALUES ('grant2', 1, 'c2', 'chan_x', 'ep_x', 'wf', '1.0', 'wfc', '[]', NULL, 'USD',
                    '2099-01-01T00:00:00Z', 'approval2', 1, 'active', '2026-01-01T00:00:00Z')
            """
        )
        for state in ("reserved", "boundary_entered", "released", "possible_unknown_acceptance"):
            conn.execute(
                """
                INSERT INTO run_authorization_grant_consumption
                    (reservation_id, grant_id, grant_version, provider, model, provider_operation,
                     idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount,
                     state, reconciliation_resolution, created_at, updated_at)
                VALUES (?, 'grant2', 1, 'fake', 'fake-model', 'video.text_to_video', ?, 'env1', ?,
                        '0.39', '0.39', ?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (f"res_{state}", f"idem_{state}", f"auth_{state}", state),
            )
        conn.commit()
        result = cp.reconcile_all_committed_charges(conn)
        assert result["projected"] == 0
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0")

    def test_generic_across_multiple_episodes_and_channels(self, conn):
        """Never filtered to one episode/channel -- proves the same
        mechanism generalizes."""
        _insert_committed_ledger_row(conn, reservation_id="res_a", grant_id="grant_a", episode_id="ep_a", channel_id="chan_a")
        _insert_committed_ledger_row(conn, reservation_id="res_b", grant_id="grant_b", episode_id="ep_b", channel_id="chan_b")
        result = cp.reconcile_all_committed_charges(conn)
        assert result["projected"] == 2
        assert cp.get_projected_total_for_episode(conn, "ep_a") == Decimal("0.39")
        assert cp.get_projected_total_for_episode(conn, "ep_b") == Decimal("0.39")


class TestGetReconciledEpisodeTotal:

    def test_combines_legacy_costs_and_protected_path_projections(self, conn):
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES ('c1', 'run1', 'ep_x', 'openrouter', 'research', 1.50, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        _insert_committed_ledger_row(conn)
        cp.reconcile_all_committed_charges(conn)
        total = cp.get_reconciled_episode_total(conn, "ep_x")
        assert total == Decimal("1.50") + Decimal("0.39")

    def test_zero_when_nothing_recorded(self, conn):
        assert cp.get_reconciled_episode_total(conn, "ep_nonexistent") == Decimal("0")

    def test_exact_decimal_arithmetic(self, conn):
        """0.1 + 0.2 != 0.3 in float; this must never surface here."""
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES ('c1', 'run1', 'ep_x', 'openrouter', 'research', 0.1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        _insert_committed_ledger_row(conn, charged_amount="0.2")
        cp.reconcile_all_committed_charges(conn)
        total = cp.get_reconciled_episode_total(conn, "ep_x")
        assert total == Decimal("0.3")
        assert str(total) == "0.3"
