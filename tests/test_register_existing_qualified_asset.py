"""
tests/test_register_existing_qualified_asset.py — Focused tests for
scripts/register_existing_qualified_asset.py.

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. No test invokes a real provider adapter or network call.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from test_generic_envelope_architecture_repair import _make_env  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationState  # noqa: E402
import register_existing_qualified_asset as req  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    astore.ensure_tables(c)
    yield c
    c.close()


def _seed_source_envelope(conn, *, scene_id="s19__extra1", planned_shot_id="b_019__shot1", beat_id="b_019"):
    env = _make_env(
        authorization_id="genauth_ep_test_final:38", planned_shot_id=planned_shot_id, beat_id=beat_id,
        scene_id=scene_id,
    )
    astore.persist_execution_envelope(conn, env)
    return env


class TestRegisterExistingQualifiedAsset:

    def test_approved_asset_reaches_allocated_with_full_evidence(self, conn):
        env = _seed_source_envelope(conn)
        rec = req.register_existing_qualified_asset(
            conn, source_envelope_id=env.envelope_id, source_envelope_version=env.envelope_version,
            new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
            technical_validation_result={"decode_errors": 0, "frame_count": 192, "black_frames": 0, "frozen_frames": 0},
            reviewer="Muhamed Abdelgawad", verdict="approved", allocated_to="b_019__shot1",
            provider_job_id="cost_1", submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z",
            at="2026-08-16T20:00:00Z",
        )
        assert rec.current_state == AuthorizationState.ALLOCATED
        assert rec.envelope_id == env.envelope_id

        history_states = [h.to_state for h in rec.history]
        assert AuthorizationState.SUBMITTED in history_states
        assert AuthorizationState.DOWNLOADED in history_states
        assert AuthorizationState.QUALIFIED in history_states
        assert AuthorizationState.ALLOCATED in history_states

        downloaded_evidence = next(h.evidence for h in rec.history if h.to_state == AuthorizationState.DOWNLOADED)
        assert downloaded_evidence["output_checksum"] == "c" * 64
        qualified_evidence = next(h.evidence for h in rec.history if h.to_state == AuthorizationState.QUALIFIED)
        assert qualified_evidence["reviewer"] == "Muhamed Abdelgawad"
        assert qualified_evidence["verdict"] == "approved"
        allocated_evidence = next(h.evidence for h in rec.history if h.to_state == AuthorizationState.ALLOCATED)
        assert allocated_evidence["allocated_to"] == "b_019__shot1"

        # Zero provider calls, zero spend -- this function only writes
        # AuthorizationRecord/execution_envelopes rows.
        reloaded_env = astore.get_execution_envelope(conn, rec.envelope_id, rec.envelope_version)
        assert reloaded_env.envelope_id == env.envelope_id

    def test_idempotent_replay_returns_same_final_state(self, conn):
        env = _seed_source_envelope(conn)
        kwargs = dict(
            source_envelope_id=env.envelope_id, source_envelope_version=env.envelope_version,
            new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
            technical_validation_result={"decode_errors": 0}, reviewer="Muhamed Abdelgawad", verdict="approved",
            allocated_to="b_019__shot1", provider_job_id="cost_1",
            submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z", at="2026-08-16T20:00:00Z",
        )
        first = req.register_existing_qualified_asset(conn, **kwargs)
        second = req.register_existing_qualified_asset(conn, **kwargs)
        assert first.current_state == second.current_state == AuthorizationState.ALLOCATED
        assert len(first.history) == len(second.history)

    def test_reused_fulfilling_authorization_reconciles_the_original_proposal(self, conn):
        """End-to-end: the registered asset (a new authorization_id,
        reusing the proposal's own already-compiled envelope) qualifies
        as a fulfilling authorization for reconcile_fulfilled_proposal()
        against the ORIGINAL proposal authorization_id."""
        env = _seed_source_envelope(conn)

        obsolete_rec_evidence_env = env  # same envelope both sides -- always matches by construction
        from test_generic_envelope_architecture_repair import _make_awaiting_explicit_record
        _make_awaiting_explicit_record(conn, "genauth_ep_test_final:38", obsolete_rec_evidence_env)

        req.register_existing_qualified_asset(
            conn, source_envelope_id=env.envelope_id, source_envelope_version=env.envelope_version,
            new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
            technical_validation_result={"decode_errors": 0}, reviewer="Muhamed Abdelgawad", verdict="approved",
            allocated_to="b_019__shot1", provider_job_id="cost_1",
            submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z", at="2026-08-16T20:00:00Z",
        )

        reconciled = astore.reconcile_fulfilled_proposal(
            conn, "genauth_ep_test_final:38", fulfilling_authorization_id="ep_test:s19__extra1:primary", at="t9",
        )
        assert reconciled.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED

    def test_rejected_verdict_never_reaches_allocated(self, conn):
        env = _seed_source_envelope(conn)
        rec = req.register_existing_qualified_asset(
            conn, source_envelope_id=env.envelope_id, source_envelope_version=env.envelope_version,
            new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
            technical_validation_result={"decode_errors": 0}, reviewer="Muhamed Abdelgawad", verdict="rejected",
            allocated_to=None, provider_job_id="cost_1",
            submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z", at="2026-08-16T20:00:00Z",
        )
        assert rec.current_state == AuthorizationState.REJECTED
        assert all(h.to_state != AuthorizationState.ALLOCATED for h in rec.history)

    def test_missing_source_envelope_fails_closed(self, conn):
        with pytest.raises(astore.EnvelopeStoreError):
            req.register_existing_qualified_asset(
                conn, source_envelope_id="a" * 64, source_envelope_version=1,
                new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
                technical_validation_result={"decode_errors": 0}, reviewer="Muhamed Abdelgawad", verdict="approved",
                allocated_to="b_019__shot1", provider_job_id="cost_1",
                submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z", at="2026-08-16T20:00:00Z",
            )

    def test_zero_provider_calls_and_zero_spend(self, conn):
        """Never calls a provider, never persists a costs/receipt row --
        this function's only writes are AuthorizationRecord and
        execution_envelopes rows within the connection it was given."""
        env = _seed_source_envelope(conn)
        req.register_existing_qualified_asset(
            conn, source_envelope_id=env.envelope_id, source_envelope_version=env.envelope_version,
            new_authorization_id="ep_test:s19__extra1:primary", output_checksum="c" * 64,
            technical_validation_result={"decode_errors": 0}, reviewer="Muhamed Abdelgawad", verdict="approved",
            allocated_to="b_019__shot1", provider_job_id="cost_1",
            submitting_at="2026-08-04T22:00:00Z", submitted_at="2026-08-04T22:22:33Z", at="2026-08-16T20:00:00Z",
        )
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "costs" not in tables
        assert "runtime_leases" not in tables
