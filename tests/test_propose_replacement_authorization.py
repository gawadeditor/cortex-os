"""
tests/test_propose_replacement_authorization.py — Focused tests for
scripts/propose_replacement_authorization.py (Phase 3 of the
ep_f3619022 canonical-operation-payload-compiler-bug program).

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. No test invokes hypogeum.muapi.submit_scene(), any
provider adapter, or any network call. git show is used read-only
against this repository's own real, permanent commit history (to
compute the compiler's before/after checksum exactly as the real
invocation does) -- it never writes anything.
"""

from __future__ import annotations

import json
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
import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
import hypogeum.workflow_executor as workflow_executor  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationState  # noqa: E402
import propose_current_episode_authorization as pcea  # noqa: E402
import propose_replacement_authorization as pra  # noqa: E402

_AUTHORIZED_MODEL = "veo3.1-lite-text-to-video"
# A real, permanent commit in THIS repository whose tools/migrate_legacy_
# manifest_to_envelopes.py content is byte-different from the current
# working tree (the canonical-JSON-payload fix landed after it) --
# exactly the same reference the real Phase 3 invocation uses.
_OLD_COMPILER_HEAD = "89a5786"


@pytest.fixture(autouse=True)
def _resolved_workflow(monkeypatch):
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


def _make_authorized_old_proposal(tmp_path, episode_id="ep_test"):
    """Compiles a v1 proposal (mirrors real production's own eventual
    activation), then manually advances every one of its records to
    AUTHORIZED -- simulating "a human already signed and activated
    this proposal" without needing the full ApprovalArtifact machinery,
    exactly as this test's own narrow scope requires."""
    _make_two_entry_episode(tmp_path, episode_id)
    db_path = tmp_path / "shared.db"
    _bootstrap_channel(db_path)

    old_report = pcea.propose_current_episode_authorization(
        episode_id, "hypogeum", assets_root=tmp_path, db_path=db_path,
        proposed_version=1, superseded_version=None, config=make_config(t2_model=_AUTHORIZED_MODEL),
    )
    assert old_report["status"] == "awaiting_explicit_authorization", old_report

    conn = db.get_connection(db_path)
    try:
        for aid in old_report["authorization_ids"]:
            rec = astore.get_authorization_record(conn, aid)
            rec = rec.transition(
                AuthorizationState.AUTHORIZED,
                evidence={
                    "authorized_by": "test_human", "authorization_method": "explicit_human_approval_proposal_batch",
                    "approval_artifact_id": "test_artifact", "approval_subject_checksum": old_report["proposal_checksum"],
                },
                at="2026-08-16T00:00:00Z",
            )
            astore.persist_authorization_record(conn, rec)
        conn.commit()
    finally:
        conn.close()

    return old_report, db_path


class TestReplacementProposalHappyPath:

    def test_creates_disjoint_replacement_and_supersedes_old(self, tmp_path):
        old_report, db_path = _make_authorized_old_proposal(tmp_path)

        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        assert result["status"] == "awaiting_explicit_authorization", result
        assert result["supersession_count"] == 2
        assert set(result["new_authorization_ids"]) == set(result["old_to_new_authorization_id"].values())
        assert result["new_proposal_id"] != old_report["proposal_id"]
        assert result["new_proposal_checksum"] != old_report["proposal_checksum"]
        # Cost ceilings preserved exactly.
        assert result["max_cost_per_call_ceiling"] == old_report["max_cost_per_call_ceiling"]
        assert result["max_total_spend"] == old_report["max_total_spend"]

        conn = db.get_connection(db_path)
        try:
            for old_id, new_id in result["old_to_new_authorization_id"].items():
                old_rec = astore.get_authorization_record(conn, old_id)
                new_rec = astore.get_authorization_record(conn, new_id)
                assert old_rec.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
                assert old_rec.history[-1].evidence["superseding_authorization_id"] == new_id
                assert new_rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
                assert new_rec.superseded_authorization_id == old_id
                # Disjoint identity -- never the same authorization_id string.
                assert old_id != new_id
                # Same underlying auth_index preserved across the replacement.
                assert old_id.rsplit(":", 1)[1] == new_id.rsplit(":", 1)[1]
        finally:
            conn.close()

    def test_live_manifest_file_untouched(self, tmp_path):
        old_report, db_path = _make_authorized_old_proposal(tmp_path)
        live_manifest_path = mle._episode_dir(tmp_path, "ep_test") / "generation_authorization_manifest_ep_test.json"
        before = live_manifest_path.read_bytes()

        pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        after = live_manifest_path.read_bytes()
        assert before == after

    def test_replacement_manifest_written_to_isolated_path_with_new_manifest_id(self, tmp_path):
        old_report, db_path = _make_authorized_old_proposal(tmp_path)
        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        replacement_path = Path(result["replacement_manifest_path"])
        assert replacement_path.exists()
        assert replacement_path != (mle._episode_dir(tmp_path, "ep_test") / "generation_authorization_manifest_ep_test.json")
        replacement_manifest = json.loads(replacement_path.read_text())
        assert replacement_manifest["manifest_id"] == "genauth_ep_test_test_replacement_v2"
        assert replacement_manifest["active_entry_count"] == 2

    def test_old_records_history_preserved_not_erased(self, tmp_path):
        old_report, db_path = _make_authorized_old_proposal(tmp_path)
        conn = db.get_connection(db_path)
        try:
            before_history_len = len(astore.get_authorization_record(conn, old_report["authorization_ids"][0]).history)
        finally:
            conn.close()

        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        conn = db.get_connection(db_path)
        try:
            after = astore.get_authorization_record(conn, old_report["authorization_ids"][0])
            assert len(after.history) == before_history_len + 1  # appended, never truncated/rewritten
        finally:
            conn.close()


class TestReplacementProposalFailsClosed:

    def test_no_such_old_proposal(self, tmp_path):
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)
        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id="proposal_does_not_exist", proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        assert result["status"] == "blocked"
        assert result["stage"] == "load_old_proposal"

    def test_old_records_not_yet_authorized_blocks(self, tmp_path):
        # Old proposal exists but was never advanced past
        # awaiting_explicit_authorization -- must not be supersedable.
        _make_two_entry_episode(tmp_path)
        db_path = tmp_path / "shared.db"
        _bootstrap_channel(db_path)
        old_report = pcea.propose_current_episode_authorization(
            "ep_test", "hypogeum", assets_root=tmp_path, db_path=db_path,
            proposed_version=1, superseded_version=None, config=make_config(t2_model=_AUTHORIZED_MODEL),
        )
        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        assert result["status"] == "blocked"
        assert result["stage"] == "verify_old_records"

        # Nothing was mutated -- old records remain exactly as they were.
        conn = db.get_connection(db_path)
        try:
            for aid in old_report["authorization_ids"]:
                assert astore.get_authorization_record(conn, aid).current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_identical_compiler_content_refuses_to_claim_a_fix(self, tmp_path, monkeypatch):
        old_report, db_path = _make_authorized_old_proposal(tmp_path)
        monkeypatch.setattr(pra, "_git_show_file_bytes", lambda head, relpath: (pra._ROOT / relpath).read_bytes())
        result = pra.propose_replacement_authorization(
            "ep_test", "hypogeum", old_proposal_id=old_report["proposal_id"], proposed_version=2,
            old_compiler_head=_OLD_COMPILER_HEAD, assets_root=tmp_path, db_path=db_path,
        )
        assert result["status"] == "blocked"
        assert result["stage"] == "verify_compiler_changed"
