"""
tests/test_correct_authorization_proposal_version.py — Focused tests
for scripts/correct_authorization_proposal_version.py.

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from test_generic_envelope_architecture_repair import _make_awaiting_explicit_record, _make_env  # noqa: E402

import correct_authorization_proposal_version as corrector  # noqa: E402
import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402

AT = "2026-01-01T00:00:00Z"


def _make_proposal_fixture(db_path, *, n=3, wrong_version=1, real_version=3):
    conn = db.get_connection(db_path)
    try:
        envs = []
        for i in range(n):
            env = _make_env(
                authorization_id=f"auth_{i}", planned_shot_id=f"shot_{i}", authorization_version=real_version,
            )
            _make_awaiting_explicit_record(conn, f"auth_{i}", env)
            envs.append(env)
        conn.commit()
    finally:
        conn.close()

    conn = db.get_connection(db_path)
    try:
        for i in range(n):
            rec = astore.get_authorization_record(conn, f"auth_{i}")
            import dataclasses
            wrong = dataclasses.replace(rec, authorization_version=wrong_version)
            astore.persist_authorization_record(conn, wrong)
        conn.commit()
    finally:
        conn.close()

    authorization_ids = [f"auth_{i}" for i in range(n)]
    bindings = [("fake", "fake-model", "video.text_to_video")]
    from decimal import Decimal
    checksum = astore.compute_proposal_checksum(
        channel_id="test_channel", episode_id="ep_test", proposed_version=real_version, superseded_version=1,
        workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
        channel_package_version="1.0", channel_package_checksum="b" * 64,
        authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
        max_cost_per_call_ceiling=Decimal("0.39"), max_total_spend=Decimal("0.39") * n, currency="USD",
    )
    proposal = astore.AuthorizationProposal(
        proposal_id=f"proposal_ep_test_v{real_version}", channel_id="test_channel", episode_id="ep_test",
        proposed_version=real_version, superseded_version=1, workflow_id="wf", workflow_version="1.0.0",
        workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
        authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
        max_cost_per_call_ceiling=Decimal("0.39"), max_total_spend=Decimal("0.39") * n, currency="USD",
        created_at=AT, proposal_checksum=checksum,
    )
    conn = db.get_connection(db_path)
    try:
        astore.persist_authorization_proposal(conn, proposal)
        conn.commit()
    finally:
        conn.close()
    return checksum, proposal.proposal_id, authorization_ids


class TestCorrectAuthorizationProposalVersion:

    def test_corrects_all_named_records_atomically(self, tmp_path):
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        checksum, proposal_id, authorization_ids = _make_proposal_fixture(db_path, n=3)

        result = corrector.correct_authorization_proposal_version(
            db_path=db_path, proposal_checksum=checksum, expected_proposal_id=proposal_id,
            old_version=1, new_version=3,
        )
        assert result["count"] == 3

        conn = db.get_connection(db_path)
        try:
            for aid in authorization_ids:
                rec = astore.get_authorization_record(conn, aid)
                assert rec.authorization_version == 3
                assert rec.current_state.value == "awaiting_explicit_authorization"
        finally:
            conn.close()

    def test_aborts_with_zero_writes_on_missing_record(self, tmp_path):
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        checksum, proposal_id, authorization_ids = _make_proposal_fixture(db_path, n=3)

        conn = db.get_connection(db_path)
        try:
            conn.execute("DELETE FROM authorization_records WHERE authorization_id = ?", (authorization_ids[0],))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemExit):
            corrector.correct_authorization_proposal_version(
                db_path=db_path, proposal_checksum=checksum, expected_proposal_id=proposal_id,
                old_version=1, new_version=3,
            )

        conn = db.get_connection(db_path)
        try:
            for aid in authorization_ids[1:]:
                rec = astore.get_authorization_record(conn, aid)
                assert rec.authorization_version == 1  # untouched -- whole batch aborted
        finally:
            conn.close()

    def test_aborts_when_old_version_does_not_match(self, tmp_path):
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        checksum, proposal_id, _ = _make_proposal_fixture(db_path, n=2, wrong_version=2)

        with pytest.raises(SystemExit):
            corrector.correct_authorization_proposal_version(
                db_path=db_path, proposal_checksum=checksum, expected_proposal_id=proposal_id,
                old_version=1, new_version=3,
            )

    def test_aborts_on_proposal_id_mismatch(self, tmp_path):
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        checksum, proposal_id, _ = _make_proposal_fixture(db_path, n=2)

        with pytest.raises(SystemExit):
            corrector.correct_authorization_proposal_version(
                db_path=db_path, proposal_checksum=checksum, expected_proposal_id="wrong_proposal_id",
                old_version=1, new_version=3,
            )

    def test_aborts_on_unknown_checksum(self, tmp_path):
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        _make_proposal_fixture(db_path, n=2)

        with pytest.raises(SystemExit):
            corrector.correct_authorization_proposal_version(
                db_path=db_path, proposal_checksum="0" * 64, expected_proposal_id="anything",
                old_version=1, new_version=3,
            )
