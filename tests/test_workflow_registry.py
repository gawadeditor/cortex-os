"""
tests/test_workflow_registry.py — Focused tests for
hypogeum/workflow_registry.py.

All fixtures use synthetic channel_id/workflow_id values
("test_channel"/"test_workflow") and isolated temp DBs -- never
"hypogeum" and never the live .hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from hypogeum import workflow_registry as wr


def _definition_bytes(**overrides) -> bytes:
    base = {
        "canonical_serialization_version": "1.0",
        "stage_order": ["stage_a", "stage_b"],
        "stage_handler_identity": {"stage_a": "mod.a", "stage_b": "mod.b"},
        "required_inputs": {}, "required_outputs": {}, "dependencies": {},
        "entry_conditions": {}, "exit_conditions": {}, "retry_policy": {},
        "fallback_policy": {}, "authorization_requirements": {}, "provider_boundary_policy": {},
    }
    base.update(overrides)
    return json.dumps(base, sort_keys=True).encode("utf-8")


class TestSchemaValidation:

    def test_missing_required_field_raises_schema_error(self):
        bad = json.dumps({"stage_order": ["a"]}).encode()
        with pytest.raises(wr.WorkflowSchemaError):
            wr.parse_workflow_definition(bad)

    def test_empty_stage_order_raises_schema_error(self):
        bad = _definition_bytes(stage_order=[])
        with pytest.raises(wr.WorkflowSchemaError):
            wr.parse_workflow_definition(bad)

    def test_non_json_bytes_raises_schema_error(self):
        with pytest.raises(wr.WorkflowSchemaError):
            wr.parse_workflow_definition(b"not json at all")

    def test_valid_definition_parses(self):
        parsed = wr.parse_workflow_definition(_definition_bytes())
        assert parsed["stage_order"] == ["stage_a", "stage_b"]


class TestAtomicRegistration:

    def test_register_and_resolve(self, tmp_path):
        db_path = tmp_path / "wf.db"
        d = wr.register_workflow("test_channel", "test_workflow", "1.0.0", _definition_bytes(), db_path=db_path)
        assert d.channel_id == "test_channel"
        assert d.workflow_id == "test_workflow"
        assert d.workflow_version == "1.0.0"
        assert len(d.workflow_checksum) == 64
        assert d.status == "active"

        resolved = wr.resolve_workflow_version("test_channel", "test_workflow", "1.0.0", db_path=db_path)
        assert resolved.workflow_checksum == d.workflow_checksum

    def test_checksum_is_exact_bytes_sha256_not_reserialized(self, tmp_path):
        raw = _definition_bytes()
        db_path = tmp_path / "wf.db"
        d = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw, db_path=db_path)
        assert d.workflow_checksum == wr.compute_definition_checksum(raw)

    def test_idempotent_replay_with_identical_bytes(self, tmp_path):
        db_path = tmp_path / "wf.db"
        raw = _definition_bytes()
        d1 = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw, db_path=db_path)
        d2 = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw, db_path=db_path)
        assert d1.workflow_checksum == d2.workflow_checksum
        assert d1.registration_id == d2.registration_id

    def test_same_version_different_content_raises_integrity_error(self, tmp_path):
        db_path = tmp_path / "wf.db"
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", _definition_bytes(), db_path=db_path)
        with pytest.raises(wr.WorkflowIntegrityError) as exc_info:
            wr.register_workflow(
                "test_channel", "test_workflow", "1.0.0",
                _definition_bytes(stage_order=["stage_a", "stage_b", "stage_c"]),
                db_path=db_path,
            )
        assert exc_info.value.workflow_version == "1.0.0"

    def test_different_version_same_channel_workflow_both_coexist(self, tmp_path):
        db_path = tmp_path / "wf.db"
        d1 = wr.register_workflow("test_channel", "test_workflow", "1.0.0", _definition_bytes(), db_path=db_path)
        d2 = wr.register_workflow(
            "test_channel", "test_workflow", "2.0.0",
            _definition_bytes(stage_order=["stage_a", "stage_b", "stage_c"]),
            db_path=db_path,
        )
        assert d1.workflow_checksum != d2.workflow_checksum
        # both remain independently resolvable -- historical immutability
        assert wr.resolve_workflow_version("test_channel", "test_workflow", "1.0.0", db_path=db_path).workflow_checksum == d1.workflow_checksum
        assert wr.resolve_workflow_version("test_channel", "test_workflow", "2.0.0", db_path=db_path).workflow_checksum == d2.workflow_checksum


class TestHistoricalImmutability:

    def test_registering_new_version_does_not_alter_old_version_row(self, tmp_path):
        db_path = tmp_path / "wf.db"
        raw_v1 = _definition_bytes()
        d1 = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw_v1, db_path=db_path)
        wr.register_workflow(
            "test_channel", "test_workflow", "1.1.0",
            _definition_bytes(retry_policy={"max_attempts": 3}),
            db_path=db_path,
        )
        reread_v1 = wr.resolve_workflow_version("test_channel", "test_workflow", "1.0.0", db_path=db_path)
        assert reread_v1.workflow_checksum == d1.workflow_checksum
        assert reread_v1.raw_definition == d1.raw_definition

    def test_resolve_active_returns_most_recently_registered(self, tmp_path):
        db_path = tmp_path / "wf.db"
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", _definition_bytes(), db_path=db_path)
        wr.register_workflow(
            "test_channel", "test_workflow", "2.0.0",
            _definition_bytes(stage_order=["stage_a", "stage_b", "stage_c"]),
            db_path=db_path,
        )
        active = wr.resolve_active_workflow("test_channel", "test_workflow", db_path=db_path)
        assert active.workflow_version == "2.0.0"


class TestConcurrentRegistrationRace:

    def test_two_connections_racing_to_register_same_new_key(self, tmp_path):
        db_path = tmp_path / "wf.db"
        raw = _definition_bytes()
        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def _attempt(name):
            barrier.wait()
            try:
                results[name] = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw, db_path=db_path)
            except Exception as exc:  # noqa: BLE001
                errors[name] = exc

        t1 = threading.Thread(target=_attempt, args=("t1",))
        t2 = threading.Thread(target=_attempt, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # SQLite serializes writers; both attempts either succeed
        # (idempotent, since content is identical) or one raises a
        # transient locking error -- but the end state must be exactly
        # one row for this key, and any succeeded result must agree.
        assert not errors or all(isinstance(e, sqlite3.OperationalError) for e in errors.values())
        succeeded = list(results.values())
        assert len(succeeded) >= 1
        checksums = {d.workflow_checksum for d in succeeded}
        assert len(checksums) == 1

        final = wr.resolve_workflow_version("test_channel", "test_workflow", "1.0.0", db_path=db_path)
        assert final is not None
        assert final.workflow_checksum == wr.compute_definition_checksum(raw)

    def test_two_connections_racing_with_conflicting_content_only_one_wins(self, tmp_path):
        db_path = tmp_path / "wf.db"
        raw_a = _definition_bytes(retry_policy={"variant": "a"})
        raw_b = _definition_bytes(retry_policy={"variant": "b"})
        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def _attempt(name, raw):
            barrier.wait()
            try:
                results[name] = wr.register_workflow("test_channel", "test_workflow", "1.0.0", raw, db_path=db_path)
            except Exception as exc:  # noqa: BLE001
                errors[name] = exc

        t1 = threading.Thread(target=_attempt, args=("t1", raw_a))
        t2 = threading.Thread(target=_attempt, args=("t2", raw_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Exactly one distinct checksum must have won; any failure must
        # be a WorkflowIntegrityError (the loser correctly detecting a
        # conflicting registration) or a transient sqlite lock error.
        final = wr.resolve_workflow_version("test_channel", "test_workflow", "1.0.0", db_path=db_path)
        assert final is not None
        winning_checksum = final.workflow_checksum
        assert winning_checksum in (wr.compute_definition_checksum(raw_a), wr.compute_definition_checksum(raw_b))
        for exc in errors.values():
            assert isinstance(exc, (wr.WorkflowIntegrityError, sqlite3.OperationalError))
