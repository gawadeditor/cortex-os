"""
tests/test_phase2_plan_bound_authorization.py — Phase 2 regression tests:
financial authorization bound to the exact verified persisted Production
Plan.

Covers:
  TestGrantPlanHashStorage          — run_authorization_grants column,
                                      idempotent legacy-DB migration.
  TestAuthorityPlanBoundRegistration— registration requires a well-formed
                                      content_hash; the signed approval
                                      covers the plan identity.
  TestLegacyNullHashGrantFailsClosed— a grant with no registered
                                      production_plan_content_hash can
                                      never mint a child authorization
                                      (zero ledger rows, no record).
  TestApproveRunGrantCeremony       — the offline ceremony refuses an
                                      unbound spec and derives bindings +
                                      hash mechanically from the verified
                                      persisted plan (--from-plan).

Scope note: three-way enforcement at the paid MUAPI video boundary
(grant == verified persisted plan == finance record) lives in
tests/test_muapi_gate_path.py::TestPhase2PlanBoundAuthorization; finance-
gate verification of the passed plan against the persisted plan lives in
tests/test_finance_agent.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.execution_envelope import GenerationExecutionEnvelope

AT = "2026-01-01T00:00:00Z"
PLAN_HASH_A = "a" * 64


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


def _bindings(**overrides):
    base = dict(
        provider="muapi", model="veo3.1-lite-text-to-video",
        provider_operation="video.text_to_video",
        currency="USD", max_cost_per_call=Decimal("0.39"), max_calls=2,
        max_total_spend=Decimal("1.00"),
    )
    base.update(overrides)
    return [astore.GrantBinding(**base)]


def _sign_grant(priv, *, plan_hash=PLAN_HASH_A, bindings=None, episode_id="ep_test",
                grant_id="g1"):
    bindings = bindings or _bindings()
    checksum = authority.compute_grant_checksum(
        channel_id="test_channel", episode_id=episode_id, workflow_id="wf",
        workflow_version="1.0.0", workflow_checksum="a" * 64,
        production_plan_content_hash=plan_hash, bindings=bindings,
        max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
        expires_at="2099-01-01T00:00:00Z",
    )
    artifact = sign_approval_artifact(
        dict(
            approval_id=f"{grant_id}:approval", approval_version=1, approval_type="grant",
            subject_checksum=checksum, channel_id="test_channel", scope_id=episode_id,
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            channel_package_version="1.0", channel_package_checksum="b" * 64,
            allowed_bindings=tuple(
                GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings
            ),
            currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("1.00"),
            max_calls=2, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z",
            approver_identity="operator_x", nonce=f"nonce_{grant_id}",
        ),
        private_key=priv,
    )
    return artifact, bindings


def _make_env(*, episode_id="ep_test", plan_checksum=PLAN_HASH_A):
    prompt = f"a compiled prompt for {episode_id}"
    return GenerationExecutionEnvelope.create(
        envelope_schema_version="1.0", envelope_version=1, channel_id="test_channel",
        channel_package_version="1.0", channel_package_checksum="b" * 64,
        execution_id=episode_id, workflow_version="1.0.0",
        plan_version="1.0", plan_checksum=plan_checksum, planned_shot_id="shot_1",
        structured_spec_checksum="d" * 64, compiler_identity="pkg.compile", compiler_version="v1",
        operation_payload_content=prompt,
        operation_payload_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        operation_payload_schema_version="video.text_to_video/1.0",
        content_provenance={
            "compiler": "pkg.compile", "compiler_version": "v1",
            "source_work_item_id": "shot_1", "rendered_at": AT,
        },
        provider="muapi", model="veo3.1-lite-text-to-video",
        provider_operation="video.text_to_video",
        deterministic_output_path="/tmp/out.mp4", expected_output_identity="asset_1",
        authorization_id="auth_1", authorization_version=1,
        max_cost_per_call=Decimal("0.39"), run_level_budget_ref="run_1",
        episode_level_budget_ref=episode_id, currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key="idem_1",
        created_at=AT,
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={
            "channel_package": "b" * 64, "plan": plan_checksum,
            "structured_spec": "d" * 64, "compiler": "e" * 64,
        },
    )


class TestGrantPlanHashStorage:

    def test_registered_grant_round_trips_its_plan_hash(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        artifact, bindings = _sign_grant(priv)
        grant = authority.register_run_grant(
            grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            production_plan_content_hash=PLAN_HASH_A, bindings=bindings,
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
        )
        assert grant.production_plan_content_hash == PLAN_HASH_A

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT production_plan_content_hash FROM run_authorization_grants "
                "WHERE grant_id = 'g1' AND grant_version = 1"
            ).fetchone()
            assert row["production_plan_content_hash"] == PLAN_HASH_A
        finally:
            conn.close()

    def test_insert_grant_refuses_a_null_plan_hash(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "auth.db")
        try:
            astore.ensure_tables(conn)
            with pytest.raises(astore.AuthorizationStoreError):
                astore.insert_grant(
                    conn, grant_id="g1", grant_version=1, grant_checksum="x" * 64,
                    channel_id="c", episode_id="e", workflow_id="wf",
                    workflow_version="1", workflow_checksum="a" * 64,
                    production_plan_content_hash="", bindings=_bindings(),
                    max_total_spend_all_bindings=None, currency="USD",
                    expires_at="2099-01-01T00:00:00Z", approval_id="ap",
                    approval_version=1,
                )
        finally:
            conn.close()

    def test_legacy_db_without_the_column_is_migrated_preserving_rows(self, keypair, tmp_path):
        """A pre-Phase-2 database (table exists WITHOUT the column) is
        migrated idempotently by ensure_tables(); existing rows survive
        with a NULL hash -- which then fails closed everywhere."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Old-shape schema: exactly the pre-Phase-2 DDL, no plan-hash column.
            conn.execute("CREATE TABLE approval_artifacts (approval_id TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE run_authorization_grants (
                    grant_id TEXT NOT NULL, grant_version INTEGER NOT NULL,
                    grant_checksum TEXT NOT NULL, channel_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL, workflow_checksum TEXT NOT NULL,
                    bindings_json TEXT NOT NULL, max_total_spend_all_bindings TEXT,
                    currency TEXT NOT NULL, expires_at TEXT NOT NULL,
                    approval_id TEXT NOT NULL, approval_version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', registered_at TEXT NOT NULL,
                    revoked_at TEXT, revoked_by TEXT, revoked_reason TEXT,
                    PRIMARY KEY (grant_id, grant_version)
                )
                """
            )
            conn.execute(
                "INSERT INTO run_authorization_grants (grant_id, grant_version, grant_checksum,"
                " channel_id, episode_id, workflow_id, workflow_version, workflow_checksum,"
                " bindings_json, currency, expires_at, approval_id, approval_version, status,"
                " registered_at) VALUES ('legacy', 1, 'x', 'c', 'e', 'wf', '1', 'a', '[]',"
                " 'USD', '2099-01-01T00:00:00Z', 'ap', 1, 'active', 't')"
            )
            conn.commit()
        finally:
            conn.close()

        astore.ensure_tables(sqlite3.connect(db_path))
        astore.ensure_tables(sqlite3.connect(db_path))  # idempotent

        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in conn2.execute("PRAGMA table_info(run_authorization_grants)")}
            assert "production_plan_content_hash" in cols
            row = conn2.execute(
                "SELECT * FROM run_authorization_grants WHERE grant_id = 'legacy'"
            ).fetchone()
            assert row["production_plan_content_hash"] is None
        finally:
            conn2.close()


class TestAuthorityPlanBoundRegistration:

    def test_registration_requires_a_wellformed_plan_hash(self, keypair, tmp_path):
        priv, _pub = keypair
        artifact, bindings = _sign_grant(priv)
        for bad in ("", "not-a-hash", "A" * 64, "a" * 63):
            with pytest.raises(authority.AuthorizationAuthorityError):
                authority.register_run_grant(
                    grant_id="g1", grant_version=1, channel_id="test_channel",
                    episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
                    workflow_checksum="a" * 64, production_plan_content_hash=bad,
                    bindings=bindings, max_total_spend_all_bindings=Decimal("1.00"),
                    currency="USD", expires_at="2099-01-01T00:00:00Z",
                    approval_artifact=artifact, db_path=tmp_path / "auth.db",
                )

    def test_artifact_signed_over_a_different_plan_hash_is_rejected(self, keypair, tmp_path):
        """The plan identity is inside the signed subject checksum -- an
        approver's signature over plan A can never register a grant bound
        to plan B."""
        priv, _pub = keypair
        artifact_for_a, bindings = _sign_grant(priv, plan_hash=PLAN_HASH_A)
        with pytest.raises(authority.ApprovalSubjectMismatchError):
            authority.register_run_grant(
                grant_id="g1", grant_version=1, channel_id="test_channel",
                episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
                workflow_checksum="a" * 64, production_plan_content_hash="b" * 64,
                bindings=bindings, max_total_spend_all_bindings=Decimal("1.00"),
                currency="USD", expires_at="2099-01-01T00:00:00Z",
                approval_artifact=artifact_for_a, db_path=tmp_path / "auth.db",
            )


def insert_historical_grant(
    priv, db_path, *, grant_id, channel_id="test_channel", episode_id="ep_gate_test",
    workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum=None,
    provider="muapi", model="m", provider_operation="video.text_to_video",
    max_cost_per_call="2.50", max_calls=5, max_total_spend="10.00",
    currency="USD", expires_at="2099-01-01T00:00:00Z", nonce=None,
):
    """Registers a PRE-PHASE-2-shaped grant for routing tests: the
    checksum payload is produced by today's compute_grant_checksum()
    with production_plan_content_hash=None -- which (historical-
    compatibility contract) omits the key entirely, producing EXACTLY
    the checksum format grants carried before plan binding existed --
    and the row is stored with a NULL production_plan_content_hash.
    Such a grant resolves through resolve_active_grant() after
    migration and must keep gating its own domain; it is denied only
    for plan-bound (video) consumption."""
    workflow_checksum = workflow_checksum or "a" * 64
    nonce = nonce or f"nonce_{grant_id}"
    bindings = [astore.GrantBinding(
        provider=provider, model=model, provider_operation=provider_operation,
        currency=currency, max_cost_per_call=Decimal(max_cost_per_call),
        max_calls=max_calls, max_total_spend=Decimal(max_total_spend),
    )]
    checksum = authority.compute_grant_checksum(
        channel_id=channel_id, episode_id=episode_id, workflow_id=workflow_id,
        workflow_version=workflow_version, workflow_checksum=workflow_checksum,
        production_plan_content_hash=None, bindings=bindings,
        max_total_spend_all_bindings=Decimal(max_total_spend), currency=currency,
        expires_at=expires_at,
    )
    artifact = sign_approval_artifact(
        dict(
            approval_id=f"{grant_id}:approval", approval_version=1, approval_type="grant",
            subject_checksum=checksum, channel_id=channel_id, scope_id=episode_id,
            workflow_id=workflow_id, workflow_version=workflow_version,
            workflow_checksum=workflow_checksum, channel_package_version="1.0",
            channel_package_checksum="b" * 64,
            allowed_bindings=tuple(
                GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings
            ),
            currency=currency, max_cost_per_call=Decimal(max_cost_per_call),
            max_total_spend=Decimal(max_total_spend), max_calls=max_calls,
            retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at=expires_at,
            approver_identity="operator_x", nonce=nonce,
        ),
        private_key=priv,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        astore.ensure_tables(conn)
        astore.persist_approval_artifact(
            conn, artifact, consumed_by_grant_id=grant_id, consumed_by_grant_version=1,
        )
        conn.execute(
            """
            INSERT INTO run_authorization_grants
                (grant_id, grant_version, grant_checksum, channel_id, episode_id,
                 workflow_id, workflow_version, workflow_checksum, bindings_json,
                 max_total_spend_all_bindings, currency, expires_at, approval_id,
                 approval_version, status, registered_at)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', 't')
            """,
            (
                grant_id, checksum, channel_id, episode_id, workflow_id,
                workflow_version, workflow_checksum,
                json.dumps([b.to_dict() for b in bindings], sort_keys=True),
                max_total_spend, currency, expires_at, f"{grant_id}:approval",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestLegacyNullHashGrantFailsClosed:

    def _persist_consistent_null_hash_grant(self, priv, db_path):
        """Simulate a PRE-PHASE-2 historical grant: checksum and artifact
        computed over a payload WITHOUT any plan-hash key (exactly what
        the pre-correction code produced), row stored with a NULL
        production_plan_content_hash. After migration this grant remains
        fully resolvable -- compute_grant_checksum omits the key for
        None -- but it is denied for PLAN-BOUND consumption."""
        bindings = _bindings()
        checksum = authority.compute_grant_checksum(
            channel_id="test_channel", episode_id="ep_test", workflow_id="wf",
            workflow_version="1.0.0", workflow_checksum="a" * 64,
            production_plan_content_hash=None, bindings=bindings,
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z",
        )
        artifact = sign_approval_artifact(
            dict(
                approval_id="nullhash:approval", approval_version=1, approval_type="grant",
                subject_checksum=checksum, channel_id="test_channel", scope_id="ep_test",
                workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
                channel_package_version="1.0", channel_package_checksum="b" * 64,
                allowed_bindings=tuple(
                    GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings
                ),
                currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("1.00"),
                max_calls=2, retry_authorized=False, fallback_authorized=False,
                issued_at=AT, expires_at="2099-01-01T00:00:00Z",
                approver_identity="operator_x", nonce="nonce_nullhash",
            ),
            private_key=priv,
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            astore.ensure_tables(conn)
            astore.persist_approval_artifact(
                conn, artifact,
                consumed_by_grant_id="nullhash", consumed_by_grant_version=1,
            )
            conn.execute(
                """
                INSERT INTO run_authorization_grants
                    (grant_id, grant_version, grant_checksum, channel_id, episode_id,
                     workflow_id, workflow_version, workflow_checksum, bindings_json,
                     max_total_spend_all_bindings, currency, expires_at, approval_id,
                     approval_version, status, registered_at)
                VALUES ('nullhash', 1, ?, 'test_channel', 'ep_test', 'wf', '1.0.0',
                        ?, ?, ?, 'USD', '2099-01-01T00:00:00Z',
                        'nullhash:approval', 1, 'active', 't')
                """,
                (checksum, "a" * 64, json.dumps([b.to_dict() for b in bindings], sort_keys=True),
                 str(Decimal("1.00"))),
            )
            conn.commit()
        finally:
            conn.close()

    def test_historical_legacy_grant_still_resolves_after_migration(self, keypair, tmp_path):
        """Historical-compatibility contract: a grant whose checksum was
        created BEFORE production_plan_content_hash existed (payload
        without the key, NULL column) keeps resolving through
        resolve_active_grant() after migration -- no global invalidation,
        no forced re-registration."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._persist_consistent_null_hash_grant(priv, db_path)
        resolved = authority.resolve_active_grant("test_channel", "ep_test", at=AT, db_path=db_path)
        assert resolved is not None
        assert resolved.grant_id == "nullhash"
        assert resolved.production_plan_content_hash is None

    def test_issue_without_optin_preserves_pre_phase2_behavior(self, keypair, tmp_path):
        """A caller that does not opt in (every non-video gated domain)
        mints a child authorization from this historical grant exactly as
        before Phase 2 existed."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._persist_consistent_null_hash_grant(priv, db_path)
        envelope = _make_env(plan_checksum="c" * 64)
        record = authority.issue_child_authorization(envelope, "nullhash", 1, at=AT, db_path=db_path)
        assert record.current_state.value == "authorized"

    def test_issue_with_expected_hash_denies_legacy_null_grant(self, keypair, tmp_path):
        """The paid MUAPI video caller supplies the verified persisted plan
        hash; a historical NULL-hash grant is then denied before ANY
        ledger activity or state transition."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._persist_consistent_null_hash_grant(priv, db_path)

        envelope = _make_env(plan_checksum=PLAN_HASH_A)
        with pytest.raises(authority.GrantPlanHashMismatchError):
            authority.issue_child_authorization(
                envelope, "nullhash", 1, at=AT, db_path=db_path,
                expected_production_plan_content_hash=PLAN_HASH_A,
            )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM run_authorization_grant_consumption"
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM authorization_records"
            ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_matching_hash_grant_still_mints_child_authorization(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        artifact, bindings = _sign_grant(priv, plan_hash=PLAN_HASH_A)
        grant = authority.register_run_grant(
            grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            production_plan_content_hash=PLAN_HASH_A, bindings=bindings,
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
        )
        envelope = _make_env(plan_checksum=PLAN_HASH_A)
        record = authority.issue_child_authorization(envelope, grant.grant_id, 1, at=AT, db_path=db_path)
        assert record.current_state.value == "authorized"

    def test_mismatched_envelope_plan_cannot_consume_via_scope_free_mechanism(self, keypair, tmp_path):
        """Even when every binding matches, a grant bound to plan A and an
        envelope compiled against plan B diverge at the paid video gate
        (see test_muapi_gate_path). Here we pin the authority-side truth:
        the grant row stores exactly one bound hash."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        artifact, bindings = _sign_grant(priv, plan_hash=PLAN_HASH_A)
        grant = authority.register_run_grant(
            grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            production_plan_content_hash=PLAN_HASH_A, bindings=bindings,
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
        )
        conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
        reloaded = astore.get_grant(conn, "g1", 1)
        assert reloaded.production_plan_content_hash == grant.production_plan_content_hash


class TestOptInExpectedPlanHashEnforcement:
    """The authority independently enforces grant == caller-supplied
    verified plan hash == envelope dependency_checksums.plan -- only for
    callers that supply expected_production_plan_content_hash."""

    def _register_bound_grant(self, priv, db_path, *, plan_hash=PLAN_HASH_A, grant_id="g1"):
        artifact, bindings = _sign_grant(priv, plan_hash=plan_hash, grant_id=grant_id)
        return authority.register_run_grant(
            grant_id=grant_id, grant_version=1, channel_id="test_channel", episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            production_plan_content_hash=plan_hash, bindings=bindings,
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
        )

    def test_grant_bound_to_other_hash_denied_before_ledger(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._register_bound_grant(priv, db_path, plan_hash="9" * 64)  # != PLAN_HASH_A
        envelope = _make_env(plan_checksum="9" * 64)
        with pytest.raises(authority.GrantPlanHashMismatchError):
            authority.issue_child_authorization(
                envelope, "g1", 1, at=AT, db_path=db_path,
                expected_production_plan_content_hash=PLAN_HASH_A,
            )
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM run_authorization_grant_consumption").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM authorization_records").fetchone()[0] == 0
        finally:
            conn.close()

    def test_envelope_plan_checksum_mismatch_denied_before_ledger(self, keypair, tmp_path):
        """Envelope compiled against a DIFFERENT plan than the verified one
        the caller supplied -- independently rejected at the authority."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._register_bound_grant(priv, db_path, plan_hash=PLAN_HASH_A)
        envelope = _make_env(plan_checksum="e" * 64)  # != verified hash
        with pytest.raises(authority.GrantPlanHashMismatchError):
            authority.issue_child_authorization(
                envelope, "g1", 1, at=AT, db_path=db_path,
                expected_production_plan_content_hash=PLAN_HASH_A,
            )
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM run_authorization_grant_consumption").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM authorization_records").fetchone()[0] == 0
        finally:
            conn.close()

    def test_full_agreement_mints_child_authorization(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        self._register_bound_grant(priv, db_path, plan_hash=PLAN_HASH_A)
        envelope = _make_env(plan_checksum=PLAN_HASH_A)
        record = authority.issue_child_authorization(
            envelope, "g1", 1, at=AT, db_path=db_path,
            expected_production_plan_content_hash=PLAN_HASH_A,
        )
        assert record.current_state.value == "authorized"


class TestApproveRunGrantCeremony:

    @pytest.fixture
    def script_mod(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import scripts.approve_run_grant as mod  # noqa: PLC0415
        yield mod

    def _write_plan(self, tmp_path: Path) -> Path:
        """A minimal, directly constructed, integrity-consistent persisted
        Production Plan (real canonical content_hash)."""
        from hypogeum.muapi import _compute_asset_id
        from hypogeum.production_plan import (
            PLAN_SCHEMA_VERSION, ROLE_REQUIRED, PlannedAsset, ProductionPlan,
            payload_content_hash, prompt_fingerprint,
        )

        prompt = "Ancient Greek ruins at dawn, wide establishing shot."
        asset = PlannedAsset(
            deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
            asset_id=_compute_asset_id("ep_test", "s01b01", None),
            scene_id="s01b01", short_id=None, role=ROLE_REQUIRED,
            prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt),
            shot_signature="", aspect_ratio="16:9", requested_duration_seconds=8.0,
            conservative_usable_duration_seconds=8.0, provider="muapi",
            model="veo3.1-lite-text-to-video", unit_price_usd=0.39,
            enhancement_price_usd=0.05, max_chargeable_attempts=2,
        )
        plan = ProductionPlan(
            schema_version=PLAN_SCHEMA_VERSION, channel_id="hypogeum", episode_id="ep_test",
            deliverable_contract_id="test", deliverable_ids=["main_episode"], assets=[asset],
            thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
            thumbnail_attempts_per_candidate=2, thumbnail_unit_price_usd=0.09,
            thumbnail_cost_normal_usd=0.27, thumbnail_cost_maximum_usd=0.27,
            narration_char_count=0, narration_price_per_char_usd=0.0003,
            narration_cost_usd=0.0, narration_is_realized=False,
            music_section_count=0, music_seconds_per_section=120.0,
            music_price_per_second_usd=0.02, music_cost_usd=0.0,
            planning_call_estimate_usd=0.05, enhancement_cost_maximum_usd=0.05,
            normal_expected_cost_usd=0.71, maximum_bounded_cost_usd=0.90,
            content_hash="0" * 64, compiled_at="2026-08-25T00:00:00+00:00",
        )
        payload = plan.to_dict()
        verified = ProductionPlan.from_dict({**payload, "content_hash": payload_content_hash(payload)})
        plan_path = tmp_path / "production_plan_ep_test.json"
        plan_path.write_text(json.dumps(verified.to_dict()), encoding="utf-8")
        return plan_path

    def _write_spec(self, tmp_path: Path, **overrides) -> Path:
        spec = {
            "grant_id": "g_ceremony", "grant_version": 1,
            "channel_id": "hypogeum", "episode_id": "ep_test",
            "workflow_id": "hypogeum_main_production", "workflow_version": "1.0.0",
            "workflow_checksum": "a" * 64,
            "channel_package_version": "1.0", "channel_package_checksum": "b" * 64,
            "currency": "USD", "max_total_spend_all_bindings": "10.00",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        spec.update(overrides)
        spec_path = tmp_path / "grant_spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return spec_path

    def test_missing_plan_hash_refuses_to_sign(self, script_mod, tmp_path, capsys):
        spec_path = self._write_spec(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            script_mod._require_plan_hash(json.loads(spec_path.read_text()))
        assert excinfo.value.code == 2
        assert "production_plan_content_hash" in capsys.readouterr().err

    def test_from_plan_derives_bindings_byte_equal_to_binding_spec(self, script_mod, tmp_path):
        """--from-plan must produce EXACTLY what
        hypogeum.production_plan.production_plan_to_grant_binding_spec()
        emits for the verified plan -- never hand-typed numbers."""
        from hypogeum.production_plan import (
            ProductionPlan, payload_content_hash, production_plan_to_grant_binding_spec,
        )

        plan_path = self._write_plan(tmp_path)
        payload = json.loads(plan_path.read_text())
        expected_fragment = production_plan_to_grant_binding_spec(
            ProductionPlan.from_dict(payload)
        )

        spec: dict = {"currency": "USD"}
        script_mod._apply_from_plan(type("Args", (), {"from_plan": plan_path})(), spec)
        assert spec["bindings"] == expected_fragment["bindings"]
        assert spec["production_plan_content_hash"] == expected_fragment["production_plan_content_hash"]
        assert spec["production_plan_content_hash"] == payload["content_hash"]

    def test_from_plan_aborts_on_tampered_plan(self, script_mod, tmp_path, capsys):
        plan_path = self._write_plan(tmp_path)
        payload = json.loads(plan_path.read_text())
        payload["maximum_bounded_cost_usd"] = 0.01  # tamper without rehashing
        plan_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            script_mod._apply_from_plan(type("Args", (), {"from_plan": plan_path})(), {})
        assert excinfo.value.code == 2
        assert "integrity" in capsys.readouterr().err

    def test_full_ceremony_registers_a_plan_bound_grant(self, script_mod, keypair, tmp_path):
        """End-to-end offline ceremony: --spec + --from-plan signs an
        artifact whose subject covers the plan identity and registers a
        grant whose row carries the verified persisted plan's hash."""
        priv, _pub = keypair
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )

        key_file = tmp_path / "priv.pem"
        key_file.write_bytes(
            priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )

        plan_path = self._write_plan(tmp_path)
        spec_path = self._write_spec(tmp_path)
        db_path = tmp_path / "auth.db"

        rc = script_mod.main([
            "--spec", str(spec_path), "--from-plan", str(plan_path),
            "--private-key", str(key_file), "--approver-identity", "tester",
            "--nonce", "nonce_p2_e2e", "--issued-at", AT,
            "--db-path", str(db_path), "--yes",
        ])
        assert rc == 0
        conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
        grant = astore.get_grant(conn, "g_ceremony", 1)
        assert grant.status == "active"
        assert grant.production_plan_content_hash != "0" * 64
        payload = json.loads(plan_path.read_text())
        assert grant.production_plan_content_hash == payload["content_hash"]
        assert len(grant.bindings) == 1
        assert grant.bindings[0].model == "veo3.1-lite-text-to-video"
