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

from test_migrate_legacy_manifest_to_envelopes import (  # noqa: E402
    _bootstrap_channel, _entry, _render_prompt, _scene, _shot, _write_episode_fixture,
)
from test_muapi import make_config  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
import hypogeum.workflow_executor as workflow_executor  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationState  # noqa: E402
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
