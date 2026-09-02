"""
tests/test_audit.py — Unit verification for hypogeum/audit.py

Exercises the minimal Audit Record persistence (DATA_MODEL.md's
canonical "Audit Record" entity — "Represents a significant platform
event"): append-only, generic across entity_type/action, value
serialization, JSON-metadata validation, and transaction participation
(passing an existing connection so a caller's own write and the audit
row commit or roll back together).

All tests use tmp_path — never the production database, per
CLAUDE.md's Test Isolation rules.

Run with:  python3 -m pytest tests/test_audit.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema
from hypogeum.audit import record_audit_event


def make_db(path: Path) -> Path:
    initialize_schema(path)
    return path


class TestRecordAuditEventStandalone:
    def test_append_success_returns_id_and_persists(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")

        audit_id = record_audit_event(
            entity_type="factory",
            entity_id="hypogeum",
            action="factory_policy_updated",
            field_name="factory_policies.monthly_ceiling_usd",
            previous_value=50.0,
            new_value=75.0,
            triggered_by="alice",
            reason="raising for burn-in test",
            metadata={"source": "dashboard"},
            db_path=db_path,
        )

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT * FROM audit_records WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None

    def test_all_fields_stored_correctly(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        audit_id = record_audit_event(
            entity_type="factory",
            entity_id="hypogeum",
            action="factory_policy_updated",
            field_name="factory_policies.monthly_ceiling_usd",
            previous_value=50.0,
            new_value=75.0,
            triggered_by="alice",
            reason="raising for burn-in test",
            metadata={"source": "dashboard", "route": "/api/factory/budget-ceiling"},
            db_path=db_path,
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM audit_records WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["entity_type"] == "factory"
        assert row["entity_id"] == "hypogeum"
        assert row["action"] == "factory_policy_updated"
        assert row["field_name"] == "factory_policies.monthly_ceiling_usd"
        assert row["previous_value"] == "50.0"
        assert row["new_value"] == "75.0"
        assert row["triggered_by"] == "alice"
        assert row["reason"] == "raising for burn-in test"
        assert row["occurred_at"] is not None
        assert json.loads(row["metadata_json"]) == {
            "source": "dashboard", "route": "/api/factory/budget-ceiling",
        }

    def test_triggered_by_defaults_to_operator(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        audit_id = record_audit_event(
            entity_type="factory", entity_id="hypogeum", action="test_action",
            db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT triggered_by FROM audit_records WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "operator"

    def test_none_values_stay_none_not_the_string_none(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        audit_id = record_audit_event(
            entity_type="factory", entity_id="hypogeum", action="test_action",
            previous_value=None, new_value=None, db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT previous_value, new_value FROM audit_records WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] is None
        assert row[1] is None

    def test_string_values_pass_through_unquoted(self, tmp_path):
        """A string value should be stored as-is, not JSON-quoted."""
        db_path = make_db(tmp_path / "hypogeum.db")
        audit_id = record_audit_event(
            entity_type="factory", entity_id="hypogeum", action="test_action",
            previous_value="active", new_value="suspended", db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT previous_value, new_value FROM audit_records WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "active"
        assert row[1] == "suspended"

    def test_invalid_metadata_raises(self, tmp_path):
        """Metadata must be validated JSON — an unserializable object must
        raise rather than silently storing something unusable."""
        db_path = make_db(tmp_path / "hypogeum.db")

        class NotSerializable:
            pass

        with pytest.raises(TypeError):
            record_audit_event(
                entity_type="factory", entity_id="hypogeum", action="test_action",
                metadata={"bad": NotSerializable()}, db_path=db_path,
            )

    def test_generic_across_entity_types_not_budget_specific(self, tmp_path):
        """The writer itself has no budget-specific behavior — confirms
        genericity, matching the operator's explicit requirement."""
        db_path = make_db(tmp_path / "hypogeum.db")
        record_audit_event(
            entity_type="channel", entity_id="hypogeum_channel",
            action="status_changed", field_name="status",
            previous_value="active", new_value="suspended",
            db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT entity_type, action FROM audit_records WHERE entity_id = ?",
                ("hypogeum_channel",),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("channel", "status_changed")


class TestRecordAuditEventNoUpdateOrDelete:
    def test_module_exposes_no_update_or_delete_function(self):
        """Append-only through the public API is a design requirement, not
        just current behavior — assert the module surface itself enforces it."""
        import hypogeum.audit as audit_mod
        public_names = [n for n in dir(audit_mod) if not n.startswith("_")]
        assert not any("update" in n.lower() for n in public_names)
        assert not any("delete" in n.lower() for n in public_names)


class TestRecordAuditEventTransactionParticipation:
    def test_passing_conn_does_not_commit_or_close(self, tmp_path):
        """When conn is supplied, record_audit_event() must leave commit/
        close entirely to the caller — this is what makes atomicity with
        a caller's own write possible."""
        db_path = make_db(tmp_path / "hypogeum.db")
        conn = sqlite3.connect(str(db_path))
        try:
            record_audit_event(
                entity_type="factory", entity_id="hypogeum", action="test_action",
                conn=conn,
            )
            # Not committed yet — a second, independent connection must not see it.
            other_conn = sqlite3.connect(str(db_path))
            try:
                row = other_conn.execute(
                    "SELECT COUNT(*) FROM audit_records"
                ).fetchone()
            finally:
                other_conn.close()
            assert row[0] == 0  # uncommitted, invisible to other connections

            conn.commit()  # caller's responsibility
        finally:
            conn.close()

        # Now visible after the caller's own commit.
        verify_conn = sqlite3.connect(str(db_path))
        try:
            row = verify_conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
        finally:
            verify_conn.close()
        assert row[0] == 1

    def test_rollback_on_shared_connection_removes_the_audit_row_too(self, tmp_path):
        """Proves the atomicity guarantee at the audit.py level: if the
        caller rolls back (e.g. because ITS OWN write failed after the
        audit insert), the audit row goes with it — no orphaned audit
        record for a change that never actually happened."""
        db_path = make_db(tmp_path / "hypogeum.db")
        conn = sqlite3.connect(str(db_path))
        try:
            record_audit_event(
                entity_type="factory", entity_id="hypogeum", action="test_action",
                conn=conn,
            )
            conn.rollback()
        finally:
            conn.close()

        verify_conn = sqlite3.connect(str(db_path))
        try:
            row = verify_conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
        finally:
            verify_conn.close()
        assert row[0] == 0
