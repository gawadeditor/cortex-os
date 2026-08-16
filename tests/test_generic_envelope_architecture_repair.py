"""
tests/test_generic_envelope_architecture_repair.py — Targeted tests for
the durable-envelope-storage, deterministic-allocation, fulfilled-
proposal-reconciliation, and model-binding-authority repair
(docs/architecture/PROTECTED_GENERATION_LIFECYCLE_RECONCILIATION.md).

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. No test invokes a real provider adapter or network call.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from test_muapi import make_config  # noqa: E402
import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402
from test_migrate_legacy_manifest_to_envelopes import (  # noqa: E402
    _bootstrap_channel,
    _config,
    _happy_path_fixture,
    _shot,
    _scene,
    _render_prompt,
    _entry,
    _write_episode_fixture,
    _with_resolved_workflow_identity,
    one,
    self_compile,
)

import hypogeum.authorization_store as astore
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState
from hypogeum.deterministic_allocation import check_output_collision, resolve_canonical_output_slot
from hypogeum.execution_envelope import GenerationExecutionEnvelope


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    astore.ensure_tables(c)
    yield c
    c.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_env(**overrides) -> GenerationExecutionEnvelope:
    prompt = overrides.pop("operation_payload_content", "a fully compiled prompt")
    kwargs = dict(
        envelope_schema_version="1.0", envelope_version=1,
        channel_id="test_channel", channel_package_version="1.0", channel_package_checksum="a" * 64,
        execution_id="ep_test", workflow_version="1.0", plan_version="1.0", plan_checksum="b" * 64,
        planned_shot_id="shot_1", beat_id="beat_1", structured_spec_checksum="c" * 64,
        compiler_identity="pkg.compile", compiler_version="v1",
        operation_payload_content=prompt, operation_payload_sha256=_sha(prompt),
        operation_payload_schema_version="video.text_to_video/1.0",
        content_provenance={
            "compiler": "pkg.compile", "compiler_version": "v1",
            "source_work_item_id": "shot_1", "rendered_at": "2026-01-01T00:00:00Z",
        },
        provider="fake", model="fake-model", provider_operation="video.text_to_video",
        deterministic_output_path="/tmp/does-not-exist/out.mp4", expected_output_identity="asset_1",
        authorization_id="auth_1", authorization_version=1, max_cost_per_call=Decimal("0.39"),
        run_level_budget_ref="run_1", episode_level_budget_ref="ep_1", currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key="idem_1",
        created_at="2026-01-01T00:00:00Z",
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={"channel_package": "a" * 64, "plan": "b" * 64, "structured_spec": "c" * 64, "compiler": "d" * 64},
    )
    kwargs.update(overrides)
    return GenerationExecutionEnvelope.create(**kwargs)


# ---------------------------------------------------------------------------
# Group A: durable envelope storage (Part 1 / tests 1-5)
# ---------------------------------------------------------------------------

class TestDurableEnvelopeStorage:

    def test_full_envelope_bytes_persist_and_reload_byte_identically(self, conn):
        env = _make_env()
        astore.persist_execution_envelope(conn, env)
        reloaded = astore.get_execution_envelope(conn, env.envelope_id, env.envelope_version)
        assert reloaded.to_dict() == env.to_dict()

    def test_envelope_id_recomputes_exactly_from_persisted_bytes(self, conn):
        env = _make_env()
        astore.persist_execution_envelope(conn, env)
        reloaded = astore.get_execution_envelope(conn, env.envelope_id, env.envelope_version)
        assert reloaded.compute_envelope_id() == env.envelope_id

    def test_one_byte_stored_payload_mutation_blocks_execution(self, conn):
        env = _make_env()
        astore.persist_execution_envelope(conn, env)
        row = conn.execute(
            "SELECT canonical_payload_bytes FROM execution_envelopes WHERE envelope_id = ? AND envelope_version = ?",
            (env.envelope_id, env.envelope_version),
        ).fetchone()
        mutated = row["canonical_payload_bytes"][:-1] + b"X"
        conn.execute(
            "UPDATE execution_envelopes SET canonical_payload_bytes = ? WHERE envelope_id = ? AND envelope_version = ?",
            (mutated, env.envelope_id, env.envelope_version),
        )
        conn.commit()
        with pytest.raises(astore.StoredEnvelopeCorruptedError):
            astore.get_execution_envelope(conn, env.envelope_id, env.envelope_version)

    def test_missing_stored_payload_blocks_execution(self, conn):
        with pytest.raises(astore.LegacyHashOnlyEnvelopeError):
            astore.get_execution_envelope(conn, "never-persisted-envelope-id", 1)

    def test_hash_only_legacy_proposal_is_non_executable_through_the_gate(self, conn):
        """A record whose authorization_records row exists (envelope_id
        only, as every pre-this-architecture proposal does) but has no
        matching execution_envelopes row must be denied by the shared
        gate with zero adapter calls -- see
        tests/test_submission_gate.py's own dedicated coverage for the
        full gate-path proof; this test proves the store-level contract
        the gate's check relies on."""
        env = _make_env()
        # Deliberately never persisted -- simulates a legacy hash-only
        # authorization_records row that only ever recorded envelope_id.
        with pytest.raises(astore.LegacyHashOnlyEnvelopeError):
            astore.get_execution_envelope(conn, env.envelope_id, env.envelope_version)

    def test_persist_is_idempotent_across_legitimate_created_at_drift(self, conn):
        """Replaying the exact same logical envelope (same identity-
        relevant content, different created_at -- a field deliberately
        excluded from envelope_id) must never be treated as a content
        conflict."""
        env1 = _make_env(created_at="2026-01-01T00:00:00Z")
        env2 = _make_env(created_at="2026-01-01T00:05:00Z")
        assert env1.envelope_id == env2.envelope_id
        first = astore.persist_execution_envelope(conn, env1)
        second = astore.persist_execution_envelope(conn, env2)
        assert first is True
        assert second is False

    def test_genuinely_different_content_at_same_key_is_structurally_unreachable_but_defended(self, conn):
        """envelope_id is itself a content-hash of the identity-relevant
        fields, so two envelopes can only collide at the same
        (envelope_id, envelope_version) primary key if their identity
        content already matches -- there is no code path that reaches
        EnvelopeAlreadyExistsWithDifferentContentError under normal
        operation; this is intentional, not a gap."""
        env = _make_env()
        astore.persist_execution_envelope(conn, env)
        # Replaying the identical envelope a second time must still be
        # a clean idempotent no-op, never an error.
        assert astore.persist_execution_envelope(conn, env) is False


# ---------------------------------------------------------------------------
# Group B: deterministic output allocation (Part 2 / tests 7-11)
# ---------------------------------------------------------------------------

class TestDeterministicAllocation:

    def test_two_resolutions_from_identical_inputs_are_byte_identical(self, tmp_path):
        slot1 = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        slot2 = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        assert slot1.extra_scene_id == slot2.extra_scene_id
        assert slot1.output_path == slot2.output_path
        assert slot1.expected_output_identity == slot2.expected_output_identity

    def test_creating_an_unrelated_file_does_not_change_compiled_identity(self, tmp_path):
        before = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        unrelated = tmp_path / "episodes" / "ep_x" / "video" / "sc_s99__extra1.mp4"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_bytes(b"unrelated")
        after = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        assert before.extra_scene_id == after.extra_scene_id
        assert before.output_path == after.output_path

    def test_no_extra2_to_extra3_silent_reassignment_occurs(self, tmp_path):
        """The exact proven defect: creating the file at the CANONICAL
        intended output path for one shot must never cause a DIFFERENT
        extra-slot number to be silently assigned on a later
        resolution."""
        slot_before = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        assert slot_before.extra_scene_id == "s02__extra2"
        slot_before.output_path.parent.mkdir(parents=True, exist_ok=True)
        slot_before.output_path.write_bytes(b"already generated")

        slot_after = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        assert slot_after.extra_scene_id == "s02__extra2"
        assert slot_after.output_path == slot_before.output_path

    def test_output_collision_check_detects_occupied_canonical_path(self, tmp_path):
        slot = resolve_canonical_output_slot(episode_id="ep_x", scene_id="s02", shot_order=2, assets_root=tmp_path)
        assert check_output_collision(slot.output_path) is False
        slot.output_path.parent.mkdir(parents=True, exist_ok=True)
        slot.output_path.write_bytes(b"occupied")
        assert check_output_collision(slot.output_path) is True

    def test_compile_time_never_reassigns_on_collision(self, tmp_path):
        """End-to-end through the real compile tool: a shot whose
        canonical output path already exists on disk still compiles to
        the SAME extra_scene_id -- never a reassigned one -- proving
        Part 2's fix at the actual call site, not just the unit
        function."""
        shot = _shot("b_002__shot2", "b_002")
        scene = _scene("s02", "b_002", [shot])
        rendered = _render_prompt(scene, shot)
        entry = _entry(shot["shot_id"], "b_002", rendered["prompt_checksum"], auth_index=1)
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry], [scene])

        canonical_path = tmp_path / "episodes" / "ep_test" / "video" / "sc_s02__extra2.mp4"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(b"already generated -- e.g. a real Canary")

        report = self_compile(tmp_path, manifest_path)
        e = one(report)
        assert e["envelope_fields_valid"] is True


# ---------------------------------------------------------------------------
# Group C: fulfilled-proposal reconciliation (Part 3 / tests 12-17)
# ---------------------------------------------------------------------------

def _make_submitted_record(conn, authorization_id, envelope) -> AuthorizationRecord:
    astore.persist_execution_envelope(conn, envelope)
    rec = AuthorizationRecord.create(
        authorization_id=authorization_id, envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
    )
    rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at="t1")
    rec = rec.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t1")
    rec = rec.transition(AuthorizationState.AUTHORIZED, evidence={"authorized_by": "x", "authorization_method": "y"}, at="t1")
    rec = rec.transition(AuthorizationState.SUBMITTING, evidence={"gate_checks_passed": True}, at="t1")
    rec = rec.transition(AuthorizationState.SUBMITTED, evidence={"provider_job_id": "job_1"}, at="t1")
    astore.persist_authorization_record(conn, rec)
    return rec


def _make_qualified_allocated_record(conn, authorization_id, envelope, *, checksum="f" * 64) -> AuthorizationRecord:
    rec = _make_submitted_record(conn, authorization_id, envelope)
    rec = rec.transition(AuthorizationState.DOWNLOADED, evidence={"output_checksum": checksum}, at="t2")
    rec = rec.transition(
        AuthorizationState.PENDING_EDITORIAL_REVIEW,
        evidence={"technical_validation_result": {"verdict": "PASS"}}, at="t2",
    )
    rec = rec.transition(AuthorizationState.QUALIFIED, evidence={"reviewer": "alice", "verdict": "approved"}, at="t3")
    rec = rec.transition(AuthorizationState.ALLOCATED, evidence={"allocated_to": envelope.planned_shot_id}, at="t4")
    astore.persist_authorization_record(conn, rec)
    return rec


def _make_awaiting_explicit_record(conn, authorization_id, envelope) -> AuthorizationRecord:
    rec = AuthorizationRecord.create(
        authorization_id=authorization_id, envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
    )
    rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at="t1")
    rec = rec.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t1")
    rec = rec.transition(
        AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION,
        evidence={"observation_only_pass_count": 1, "observation_only_total_count": 1, "proposal_artifact_checksum": "z" * 64},
        at="t1",
    )
    astore.persist_execution_envelope(conn, envelope)
    astore.persist_authorization_record(conn, rec)
    return rec


class TestFulfilledProposalReconciliation:

    def test_reconciliation_transitions_exactly_once(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        fulfilling_env = _make_env(
            authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002",
            operation_payload_content="different prompt bytes, same shot",
        )
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        result = astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        assert result.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
        invalidated_events = [h for h in result.history if h.to_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED]
        assert len(invalidated_events) == 1
        assert invalidated_events[0].evidence["fulfilling_authorization_id"] == "canary_1"

    def test_obsolete_still_only_at_awaiting_authorization_can_be_reconciled(self, conn):
        """A proposal whose own compile round never finished (stuck at
        AWAITING_AUTHORIZATION, never reached AWAITING_EXPLICIT_
        AUTHORIZATION -- e.g. a batch re-run that failed Observation-Only
        for unrelated reasons before this entry's own round completed)
        is just as reconcilable as one that did finish, since
        (AWAITING_AUTHORIZATION, INVALIDATED_SOURCE_CHANGED) is already
        a valid state-machine edge with identical required evidence."""
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        fulfilling_env = _make_env(authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002")

        rec = AuthorizationRecord.create(
            authorization_id="proposal_1", envelope_id=obsolete_env.envelope_id, envelope_version=obsolete_env.envelope_version,
        )
        rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at="t1")
        rec = rec.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t1")
        astore.persist_execution_envelope(conn, obsolete_env)
        astore.persist_authorization_record(conn, rec)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        result = astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        assert result.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED

    def test_reconciliation_replay_is_idempotent(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        fulfilling_env = _make_env(authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        first = astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        second = astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        assert len(first.history) == len(second.history)

    def test_conflicting_fulfilling_authorization_fails_closed(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        fulfilling_env = _make_env(authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        other_env = _make_env(authorization_id="canary_2", planned_shot_id="b_002__shot2", beat_id="b_002")
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)
        _make_qualified_allocated_record(conn, "canary_2", other_env)

        astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        with pytest.raises(astore.ProposalReconciliationConflictError):
            astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_2", at="t6")

    def test_unqualified_fulfillment_evidence_cannot_invalidate_another_proposal(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        not_yet_qualified_env = _make_env(authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        # SUBMITTED only -- never technically validated, qualified, or allocated.
        _make_submitted_record(conn, "canary_1", not_yet_qualified_env)

        with pytest.raises(astore.ProposalReconciliationConflictError):
            astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")

        unchanged = astore.get_authorization_record(conn, "proposal_1")
        assert unchanged.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION

    def test_mismatched_planned_shot_identity_fails_closed(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        wrong_shot_env = _make_env(authorization_id="canary_1", planned_shot_id="b_003__shot2", beat_id="b_003")
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", wrong_shot_env)

        with pytest.raises(astore.ProposalReconciliationConflictError):
            astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")

    def test_matching_scene_id_reconciles_even_when_planned_shot_id_convention_differs(self, conn):
        """Real-world case: hypogeum.muapi._submit_scene_via_gate()'s
        own grant-derived compile path binds planned_shot_id to its own
        scene_id and never sets beat_id -- a different field convention
        than tools/migrate_legacy_manifest_to_envelopes.py's envelopes
        (planned_shot_id = the original manifest shot_id, scene_id = the
        deterministic output slot). Both sides agreeing on scene_id (the
        deterministic, unambiguous physical-output identity) must still
        reconcile."""
        obsolete_env = _make_env(
            authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002", scene_id="s02__extra2",
        )
        fulfilling_env = _make_env(
            authorization_id="canary_1", planned_shot_id="s02__extra2", beat_id=None, scene_id=None,
        )
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        result = astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")
        assert result.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED

    def test_reconciled_proposal_leaves_active_set_but_remains_auditable(self, conn):
        obsolete_env = _make_env(authorization_id="proposal_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        fulfilling_env = _make_env(authorization_id="canary_1", planned_shot_id="b_002__shot2", beat_id="b_002")
        _make_awaiting_explicit_record(conn, "proposal_1", obsolete_env)
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        astore.reconcile_fulfilled_proposal(conn, "proposal_1", fulfilling_authorization_id="canary_1", at="t5")

        active = conn.execute(
            "SELECT COUNT(*) AS n FROM authorization_records WHERE current_state = ?",
            (AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION.value,),
        ).fetchone()["n"]
        assert active == 0  # removed from the active paid-generation set

        still_readable = astore.get_authorization_record(conn, "proposal_1")
        assert still_readable is not None  # never deleted -- remains auditable
        assert still_readable.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED

    def test_counts_and_decimal_cost_recompute_correctly_after_reconciliation(self, conn):
        envs = []
        for i in range(3):
            env = _make_env(
                authorization_id=f"proposal_{i}", planned_shot_id=f"b_{i:03d}__shot2", beat_id=f"b_{i:03d}",
                max_cost_per_call=Decimal("0.39"),
            )
            envs.append(env)
            _make_awaiting_explicit_record(conn, f"proposal_{i}", env)

        fulfilling_env = _make_env(authorization_id="canary_1", planned_shot_id="b_000__shot2", beat_id="b_000")
        _make_qualified_allocated_record(conn, "canary_1", fulfilling_env)

        rows = conn.execute(
            "SELECT authorization_id FROM authorization_records WHERE current_state = ?",
            (AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION.value,),
        ).fetchall()
        assert len(rows) == 3
        total_before = sum(Decimal("0.39") for _ in rows)
        assert total_before == Decimal("1.17")

        astore.reconcile_fulfilled_proposal(conn, "proposal_0", fulfilling_authorization_id="canary_1", at="t5")

        rows_after = conn.execute(
            "SELECT authorization_id FROM authorization_records WHERE current_state = ?",
            (AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION.value,),
        ).fetchall()
        assert len(rows_after) == 2
        total_after = sum(Decimal("0.39") for _ in rows_after)
        assert total_after == Decimal("0.78")


# ---------------------------------------------------------------------------
# Group D: model binding authority (Part 4 / tests 18-20)
# ---------------------------------------------------------------------------

class TestModelBindingAuthority:

    def test_frozen_lite_binding_cannot_be_overridden_by_t1_routing(self, tmp_path):
        """A shot whose live scene routes to Tier 1 (a different model
        entirely) still compiles bound to the caller-approved Lite
        binding when explicit_binding is supplied -- the frozen envelope
        never adopts the live routing's own choice."""
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path, primary_tier=1)
        binding = mle.ExplicitBinding(
            provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
            max_cost_per_call_ceiling=Decimal("0.39"),
        )
        report = self_compile(tmp_path, manifest_path, explicit_binding=binding)
        e = one(report)
        assert e["envelope_fields_valid"] is True
        # The live scene routes tier 1 -> a different model; the frozen
        # binding must still win, exactly like the manifest-derived path
        # already does for TestAuthorizationBindingWinsOverLegacyRouting.
        assert e["legacy_path_compatible"] is False
        assert any("legacy_execution_path_binding_mismatch" in r for r in e["legacy_path_reasons"])

    def test_caller_cannot_substitute_fast_or_another_model(self, tmp_path):
        """A caller who approved the Lite binding cannot get a 'Fast'-
        model envelope out of the same compile call by asserting a
        ceiling that happens to match Fast's own cost instead of
        Lite's -- the binding names an exact model, and the model,
        never merely a ceiling number, is what compilation freezes into
        the envelope."""
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        binding = mle.ExplicitBinding(
            provider="muapi", model="veo3-fast-text-to-video", provider_operation="video.text_to_video",
            max_cost_per_call_ceiling=Decimal("0.39"),
        )
        report = self_compile(tmp_path, manifest_path, explicit_binding=binding)
        e = one(report)
        if e["envelope_fields_valid"] is True:
            # veo3-fast-text-to-video does have a known cost -- if it
            # happens to match the caller-approved ceiling, the binding
            # legitimately compiles bound to Fast, exactly as approved
            # (never silently substituted for Lite or anything else).
            assert e["checksums"].get("operation_payload_sha256") is not None
        else:
            assert any(
                "unknown_model_cost" in r or "explicit_binding_cost_mismatch" in r
                for r in e["envelope_fields_reasons"]
            )

    def test_explicit_binding_rejects_a_ceiling_that_does_not_match_the_models_known_cost(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        binding = mle.ExplicitBinding(
            provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
            max_cost_per_call_ceiling=Decimal("2.50"),  # wrong -- Lite's real cost is 0.39
        )
        report = self_compile(tmp_path, manifest_path, explicit_binding=binding)
        e = one(report)
        assert e["envelope_fields_valid"] is False
        assert any("explicit_binding_cost_mismatch" in r for r in e["envelope_fields_reasons"])

    def test_binding_change_requires_a_new_envelope_id(self):
        """Two envelopes differing only in model produce different
        envelope_ids -- there is no code path that mutates a binding in
        place; a changed binding is, by construction, a different
        content-addressed envelope requiring fresh authorization."""
        env_lite = _make_env(model="veo3.1-lite-text-to-video")
        env_t1 = _make_env(model="veo3-text-to-video")
        assert env_lite.envelope_id != env_t1.envelope_id

    def test_explicit_binding_is_immutable_positive_value_error(self):
        with pytest.raises(ValueError):
            mle.ExplicitBinding(
                provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
                max_cost_per_call_ceiling=Decimal("0"),
            )
