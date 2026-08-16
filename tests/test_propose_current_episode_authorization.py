"""
tests/test_propose_current_episode_authorization.py — Focused tests for
scripts/propose_current_episode_authorization.py (plan §6 Commit M).

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. No test invokes hypogeum.muapi.submit_scene(), any
provider adapter, or any network call. Every test proves this script
never activates anything -- it either stops at
awaiting_explicit_authorization or reports why it couldn't.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from test_generic_envelope_architecture_repair import (  # noqa: E402
    _make_awaiting_explicit_record, _make_env, _make_qualified_allocated_record, _make_submitted_record,
)
from test_migrate_legacy_manifest_to_envelopes import (  # noqa: E402
    _bootstrap_channel, _entry, _render_prompt, _scene, _shot, _write_episode_fixture,
)
from test_muapi import make_config  # noqa: E402
import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
import hypogeum.workflow_executor as workflow_executor  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationState  # noqa: E402
from hypogeum.execution_envelope import GenerationExecutionEnvelope  # noqa: E402
import propose_current_episode_authorization as pcea  # noqa: E402

_AUTHORIZED_MODEL = "veo3.1-lite-text-to-video"


@pytest.fixture(autouse=True)
def _resolved_workflow(monkeypatch):
    """Isolate the Workflow Registry resolution -- never the real
    'hypogeum' channel's actually-registered production workflow."""
    monkeypatch.setattr(
        workflow_executor, "resolve_workflow_binding",
        lambda channel_id, workflow_id, workflow_version, **kw: {
            "workflow_id": workflow_id, "workflow_version": workflow_version,
            "workflow_checksum": "a" * 64, "registry_definition_schema_version": "1.0",
            "registration_identity": f"{channel_id}:{workflow_id}:{workflow_version}",
            "evaluation_timestamp": "2026-01-01T00:00:00Z",
        },
    )


def _make_two_entry_episode(tmp_path: Path, episode_id: str = "ep_test"):
    shot1 = _shot("b_001__shot1", "b_001")
    scene1 = _scene("s01", "b_001", [shot1], primary_tier=2)
    rendered1 = _render_prompt(scene1, shot1)
    entry1 = _entry(shot1["shot_id"], "b_001", rendered1["prompt_checksum"], auth_index=1)

    shot2 = _shot("b_002__shot1", "b_002")
    scene2 = _scene("s02", "b_002", [shot2], primary_tier=2)
    rendered2 = _render_prompt(scene2, shot2)
    entry2 = _entry(shot2["shot_id"], "b_002", rendered2["prompt_checksum"], auth_index=2)

    manifest_path, plan_md5 = _write_episode_fixture(tmp_path, episode_id, [entry1, entry2], [scene1, scene2])
    return manifest_path


class TestHappyPathReachesAwaitingExplicitAuthorization:

    def test_all_entries_pass_and_proposal_is_persisted(self, tmp_path):
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
        )

        assert report["status"] == "awaiting_explicit_authorization", report
        assert report["total"] == 2
        assert report["pass_count"] == 2
        assert len(report["authorization_ids"]) == 2
        assert report["proposed_version"] == 2
        assert report["superseded_version"] == 1

        conn = db.get_connection(db_path)
        try:
            for auth_id in report["authorization_ids"]:
                rec = astore.get_authorization_record(conn, auth_id)
                assert rec is not None
                assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
                assert rec.history[-1].to_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
                assert rec.history[-1].evidence["proposal_artifact_checksum"] == report["proposal_checksum"]

            proposal = astore.get_authorization_proposal(conn, report["proposal_id"])
            assert proposal is not None
            assert proposal.proposal_checksum == report["proposal_checksum"]
            assert set(proposal.authorization_ids) == set(report["authorization_ids"])
        finally:
            conn.close()

    def test_exact_lite_binding_and_decimal_cost(self, tmp_path):
        """Counts and Decimal cost: 2 entries, USD 0.39 ceiling each,
        USD 0.78 total -- exact Decimal values, never floating-point
        rounding. provider/model/operation bindings are exactly the
        single Lite binding -- no Fast model, no upscaler operation,
        can ever appear (this script only ever compiles
        video.text_to_video envelopes; Fast/upscaler are separate
        domains this script never touches)."""
        from decimal import Decimal

        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        assert report["status"] == "awaiting_explicit_authorization", report
        assert report["provider_model_operation_bindings"] == [("muapi", _AUTHORIZED_MODEL, "video.text_to_video")]
        assert Decimal(report["max_cost_per_call_ceiling"]) == Decimal("0.39")
        assert Decimal(report["max_total_spend"]) == Decimal("0.78")

    def test_full_envelope_persisted_for_every_entry(self, tmp_path):
        """Full Envelope storage: every persisted AuthorizationRecord's
        envelope_id/envelope_version resolves through
        get_execution_envelope() to a complete, integrity-verified
        canonical payload -- never merely a hash."""
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        conn = db.get_connection(db_path)
        try:
            envelope_ids = set()
            for auth_id in report["authorization_ids"]:
                rec = astore.get_authorization_record(conn, auth_id)
                env = astore.get_execution_envelope(conn, rec.envelope_id, rec.envelope_version)
                assert env.provider == "muapi"
                assert env.model == _AUTHORIZED_MODEL
                assert env.provider_operation == "video.text_to_video"
                assert env.max_cost_per_call.__class__.__name__ == "Decimal"
                envelope_ids.add(env.envelope_id)
            assert len(envelope_ids) == 2  # unique per entry
        finally:
            conn.close()

    def test_zero_provider_calls_and_zero_spend(self, tmp_path):
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        assert report["compile_report_summary"]["provider_calls_made"] == 0
        assert report["compile_report_summary"]["spend_usd"] == 0.0

    def test_legacy_manifest_untouched(self, tmp_path):
        manifest_path = _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        before = manifest_path.read_bytes()
        pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        after = manifest_path.read_bytes()
        assert before == after

    def test_never_reaches_authorized_state(self, tmp_path):
        """The whole point of Commit M: this script's own execution
        must never produce an AUTHORIZED record -- only a human,
        separately, with a verified ApprovalArtifact, can do that."""
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        conn = db.get_connection(db_path)
        try:
            for auth_id in report["authorization_ids"]:
                rec = astore.get_authorization_record(conn, auth_id)
                assert rec.current_state != AuthorizationState.AUTHORIZED
                assert all(e.to_state != AuthorizationState.AUTHORIZED for e in rec.history)
        finally:
            conn.close()


class TestWorkflowResolutionFailureBlocksCleanly:

    def test_unresolvable_workflow_blocks_before_touching_manifest(self, tmp_path, monkeypatch):
        manifest_path = _make_two_entry_episode(tmp_path)
        before = manifest_path.read_bytes()

        def _raise(*a, **kw):
            raise workflow_executor.WorkflowArtifactUnreadableError("no such workflow")
        monkeypatch.setattr(workflow_executor, "resolve_workflow_binding", _raise)

        db_path = tmp_path / "shared.db"
        db.initialize_schema(db_path)
        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
        )
        assert report["stage"] == "workflow_resolution"
        assert report["status"] == "blocked"
        assert manifest_path.read_bytes() == before


class TestObservationOnlyFailureLeavesEverythingAwaitingAuthorization:

    def test_a_stale_dependency_at_evaluation_time_is_reported_never_activated(self, tmp_path, monkeypatch):
        """Simulate a check failure inside evaluate_shadow() itself
        (dominance_leak_validator forced to report a leak) -- proves
        the failure path reports which entries failed and leaves every
        record at AWAITING_AUTHORIZATION, never proceeding to a
        proposal."""
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        import hypogeum.shadow_observation as shadow_observation
        real_evaluate_shadow = shadow_observation.evaluate_shadow

        def _forced_fail(*args, **kwargs):
            kwargs["dominance_leak_validator"] = lambda env: "forced_test_leak"
            return real_evaluate_shadow(*args, **kwargs)

        monkeypatch.setattr(pcea, "evaluate_shadow", _forced_fail)

        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        assert report["stage"] == "observation_only"
        assert report["status"] == "not_ready"
        assert report["failure_count"] == 2
        assert all("forced_test_leak" in str(f["blockers"]) for f in report["failures"])

        conn = db.get_connection(db_path)
        try:
            for f in report["failures"]:
                rec = astore.get_authorization_record(conn, f["authorization_id"])
                assert rec.current_state == AuthorizationState.AWAITING_AUTHORIZATION
            assert astore.get_authorization_proposal(conn, "does-not-exist") is None
        finally:
            conn.close()


def _make_three_entry_episode(tmp_path: Path, episode_id: str = "ep_test"):
    shot1 = _shot("b_001__shot1", "b_001")
    scene1 = _scene("s01", "b_001", [shot1], primary_tier=2)
    rendered1 = _render_prompt(scene1, shot1)
    entry1 = _entry(shot1["shot_id"], "b_001", rendered1["prompt_checksum"], auth_index=1)

    shot2 = _shot("b_002__shot1", "b_002")
    scene2 = _scene("s02", "b_002", [shot2], primary_tier=2)
    rendered2 = _render_prompt(scene2, shot2)
    entry2 = _entry(shot2["shot_id"], "b_002", rendered2["prompt_checksum"], auth_index=2)

    shot3 = _shot("b_003__shot1", "b_003")
    scene3 = _scene("s03", "b_003", [shot3], primary_tier=2)
    rendered3 = _render_prompt(scene3, shot3)
    entry3 = _entry(shot3["shot_id"], "b_003", rendered3["prompt_checksum"], auth_index=3)

    _write_episode_fixture(tmp_path, episode_id, [entry1, entry2, entry3], [scene1, scene2, scene3])
    return "genauth_ep_test_test:2"  # auth_index 2's authorization_id, matching _write_episode_fixture's manifest_id


def _seed_obsolete_awaiting_explicit_record(tmp_path, db_path, episode_id, authorization_id, config):
    """Directly compiles (never through propose_current_episode_
    authorization() itself, to avoid a same-process shadow-lease
    collision with the real call under test -- entirely a test-timing
    artifact, not a production concern: the real ep_f3619022 history
    this reconciles against was persisted hours earlier by a genuinely
    separate process) and persists a pre-existing
    AWAITING_EXPLICIT_AUTHORIZATION record + durable envelope for
    exactly one authorization_id -- simulating "a prior real Commit M
    run already produced this proposal"."""
    resolver = pcea._real_workflow_identity_resolver("hypogeum", pcea._HYPOGEUM_WORKFLOW_ID, pcea._HYPOGEUM_WORKFLOW_VERSION)
    parser = mle.build_arg_parser()
    args = parser.parse_args(["--episode-id", episode_id, "--channel-id", "hypogeum", "--dry-run", "--assets-root", str(tmp_path)])
    args.manifest_path = str(mle._episode_dir(tmp_path, episode_id) / f"generation_authorization_manifest_{episode_id}.json")
    args.db_path = str(db_path)
    report = mle.compile_manifest_to_envelopes(
        args, config=config, workflow_identity_resolver=resolver,
        tag_as_reconstructed=False, authorization_version=2, include_envelope_dicts=True,
    )
    entry = next(e for e in report["entries"] if e["envelope_dict"]["authorization_id"] == authorization_id)
    env = GenerationExecutionEnvelope.from_dict(entry["envelope_dict"])
    conn = db.get_connection(db_path)
    try:
        _make_awaiting_explicit_record(conn, authorization_id, env)
        conn.commit()
    finally:
        conn.close()
    return env


class TestFulfilledExclusionAndReconciliation:
    """Proposal exclusion + fulfilled-proposal reconciliation: the
    generic exclusion/reconciliation input added to
    propose_current_episode_authorization()."""

    def test_excluded_proposal_is_reconciled_and_removed_from_new_version_active_set(self, tmp_path):
        obsolete_authorization_id = _make_three_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        # Seed the pre-existing state: a prior compile/propose run (no
        # exclusions) already persisted the excluded entry at
        # AWAITING_EXPLICIT_AUTHORIZATION with a real, durably-stored
        # envelope -- exactly the real ep_f3619022 production history
        # this mechanism reconciles against.
        config = make_config(t2_model=_AUTHORIZED_MODEL)
        _seed_obsolete_awaiting_explicit_record(tmp_path, db_path, "ep_test", obsolete_authorization_id, config)

        fulfilling_env = _make_env(
            authorization_id="ep_test:s02:primary", planned_shot_id="b_002__shot1", beat_id="b_002",
        )
        conn = db.get_connection(db_path)
        try:
            _make_qualified_allocated_record(conn, "ep_test:s02:primary", fulfilling_env)
            conn.commit()
        finally:
            conn.close()

        exclusion = pcea.FulfilledProposalExclusion(
            authorization_id=obsolete_authorization_id, fulfilling_authorization_id="ep_test:s02:primary",
            reason="fulfilled_by_qualified_allocated_canary",
        )
        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=config,
            fulfilled_exclusions=[exclusion],
        )

        assert report["status"] == "awaiting_explicit_authorization", report
        assert report["total"] == 2  # 3 active entries minus the 1 excluded
        assert obsolete_authorization_id not in report["authorization_ids"]
        assert len(report["authorization_ids"]) == 2
        assert report["reconciliations"] == [{
            "authorization_id": obsolete_authorization_id, "fulfilling_authorization_id": "ep_test:s02:primary",
            "reason": "fulfilled_by_qualified_allocated_canary", "final_state": "invalidated_source_changed",
        }]

        conn = db.get_connection(db_path)
        try:
            obsolete_rec = astore.get_authorization_record(conn, obsolete_authorization_id)
            assert obsolete_rec.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
            evidence = obsolete_rec.history[-1].evidence
            assert evidence["fulfilling_authorization_id"] == "ep_test:s02:primary"
            assert evidence["reconciliation_reason"] == "fulfilled_by_qualified_allocated_canary"

            # Complete audit evidence preserved: the excluded entry's own
            # envelope is now durably persisted too (Part 1), never
            # merely a hash.
            reloaded = astore.get_execution_envelope(conn, obsolete_rec.envelope_id, obsolete_rec.envelope_version)
            assert reloaded.planned_shot_id == "b_002__shot1"

            for auth_id in report["authorization_ids"]:
                rec = astore.get_authorization_record(conn, auth_id)
                assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
                env = astore.get_execution_envelope(conn, rec.envelope_id, rec.envelope_version)
                assert env.provider == "muapi"
                assert env.model == _AUTHORIZED_MODEL
        finally:
            conn.close()

    def test_reconciliation_replay_is_idempotent_across_two_calls(self, tmp_path):
        obsolete_authorization_id = _make_three_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        config = make_config(t2_model=_AUTHORIZED_MODEL)
        _seed_obsolete_awaiting_explicit_record(tmp_path, db_path, "ep_test", obsolete_authorization_id, config)

        fulfilling_env = _make_env(
            authorization_id="ep_test:s02:primary", planned_shot_id="b_002__shot1", beat_id="b_002",
        )
        conn = db.get_connection(db_path)
        try:
            _make_qualified_allocated_record(conn, "ep_test:s02:primary", fulfilling_env)
            conn.commit()
        finally:
            conn.close()

        exclusion = pcea.FulfilledProposalExclusion(
            authorization_id=obsolete_authorization_id, fulfilling_authorization_id="ep_test:s02:primary",
        )
        kwargs = dict(
            assets_root=tmp_path, db_path=db_path, proposed_version=2, superseded_version=1,
            config=make_config(t2_model=_AUTHORIZED_MODEL), fulfilled_exclusions=[exclusion],
        )
        first = pcea.propose_current_episode_authorization("ep_test", "hypogeum", **kwargs)
        assert first["status"] == "awaiting_explicit_authorization"

        conn = db.get_connection(db_path)
        try:
            obsolete_rec = astore.get_authorization_record(conn, obsolete_authorization_id)
            history_len_after_first = len(obsolete_rec.history)
        finally:
            conn.close()

        # Replaying reconciliation alone (via the store function directly,
        # matching the idempotent-replay contract already proven in
        # tests/test_generic_envelope_architecture_repair.py) must not
        # grow the obsolete record's history again.
        conn = db.get_connection(db_path)
        try:
            replayed = astore.reconcile_fulfilled_proposal(
                conn, obsolete_authorization_id, fulfilling_authorization_id="ep_test:s02:primary", at="t99",
            )
            assert len(replayed.history) == history_len_after_first
        finally:
            conn.close()

    def test_unqualified_fulfilling_authorization_fails_closed_and_persists_nothing(self, tmp_path):
        obsolete_authorization_id = _make_three_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        baseline = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        assert baseline["status"] == "awaiting_explicit_authorization"

        conn = db.get_connection(db_path)
        try:
            obsolete_before = astore.get_authorization_record(conn, obsolete_authorization_id)
            history_len_before = len(obsolete_before.history)
            other_before = astore.get_authorization_record(conn, "genauth_ep_test_test:1")
            other_history_len_before = len(other_before.history)
        finally:
            conn.close()

        # SUBMITTED only -- never technically validated, qualified, or
        # allocated. Cannot fulfill anything.
        not_yet_qualified_env = _make_env(
            authorization_id="ep_test:s02:primary", planned_shot_id="b_002__shot1", beat_id="b_002",
        )
        conn = db.get_connection(db_path)
        try:
            _make_submitted_record(conn, "ep_test:s02:primary", not_yet_qualified_env)
            conn.commit()
        finally:
            conn.close()

        exclusion = pcea.FulfilledProposalExclusion(
            authorization_id=obsolete_authorization_id, fulfilling_authorization_id="ep_test:s02:primary",
        )
        with pytest.raises(astore.ProposalReconciliationConflictError):
            pcea.propose_current_episode_authorization(
                "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
                proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
                fulfilled_exclusions=[exclusion],
            )

        conn = db.get_connection(db_path)
        try:
            # Nothing from this failed attempt was persisted: the
            # excluded entry's own AuthorizationRecord and every other
            # active entry are byte-for-byte unchanged from the baseline
            # (the reconciliation transaction was never committed).
            obsolete_after = astore.get_authorization_record(conn, obsolete_authorization_id)
            assert obsolete_after.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
            assert len(obsolete_after.history) == history_len_before
            other_after = astore.get_authorization_record(conn, "genauth_ep_test_test:1")
            assert len(other_after.history) == other_history_len_before
        finally:
            conn.close()

    def test_proposal_5_equivalent_not_excluded_stays_in_active_set(self, tmp_path):
        """Only the named authorization_id is excluded -- every other
        active entry, including one sharing the same beat as the
        excluded shot, remains in the new version's active set,
        unreconciled, untouched."""
        shot1 = _shot("b_002__shot2", "b_002")
        shot2 = _shot("b_002__shot3", "b_002")
        scene = _scene("s02", "b_002", [shot1, shot2], primary_tier=2)
        rendered1 = _render_prompt(scene, shot1)
        entry1 = _entry(shot1["shot_id"], "b_002", rendered1["prompt_checksum"], auth_index=4)
        rendered2 = _render_prompt(scene, shot2)
        entry2 = _entry(shot2["shot_id"], "b_002", rendered2["prompt_checksum"], auth_index=5)

        _write_episode_fixture(tmp_path, "ep_test", [entry1, entry2], [scene])
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)

        config = make_config(t2_model=_AUTHORIZED_MODEL)
        _seed_obsolete_awaiting_explicit_record(tmp_path, db_path, "ep_test", "genauth_ep_test_test:4", config)

        fulfilling_env = _make_env(
            authorization_id="ep_test:s02__extra2:primary", planned_shot_id="b_002__shot2", beat_id="b_002",
        )
        conn = db.get_connection(db_path)
        try:
            _make_qualified_allocated_record(conn, "ep_test:s02__extra2:primary", fulfilling_env)
            conn.commit()
        finally:
            conn.close()

        exclusion = pcea.FulfilledProposalExclusion(
            authorization_id="genauth_ep_test_test:4", fulfilling_authorization_id="ep_test:s02__extra2:primary",
            reason="fulfilled_by_qualified_allocated_canary",
        )
        report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=2, superseded_version=1, config=make_config(t2_model=_AUTHORIZED_MODEL),
            fulfilled_exclusions=[exclusion],
        )
        assert report["status"] == "awaiting_explicit_authorization", report
        assert report["total"] == 1
        assert "genauth_ep_test_test:4" not in report["authorization_ids"]
        assert "genauth_ep_test_test:5" in report["authorization_ids"]

        conn = db.get_connection(db_path)
        try:
            rec5 = astore.get_authorization_record(conn, "genauth_ep_test_test:5")
            assert rec5.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
            env5 = astore.get_execution_envelope(conn, rec5.envelope_id, rec5.envelope_version)
            assert env5.planned_shot_id == "b_002__shot3"
        finally:
            conn.close()
