"""
tests/test_decision_reviews.py — F4: Decision Review Persistence unit tests

Coverage:
    TestSchema              — decision_reviews table is created by initialize_schema
    TestSyncCreates         — sync inserts pending reviews for decisions with evidence
    TestSyncSkipsEmpty      — sync skips decisions with no evidence
    TestSyncIdempotent      — running sync twice with same decisions = idempotent
    TestSyncPreservesReview — accepted/rejected rows survive re-sync unchanged
    TestSyncUpdatesPending  — pending rows are updated when engine recomputes them
    TestAcceptReject        — accept/reject set operator_status + reviewed_at
    TestAcceptRejectNotFound — unknown review_id returns ok=False, code=not_found
    TestListFilter          — list_decision_reviews filters by status correctly
    TestGetReview           — get_decision_review returns row or None
    TestNoSideEffects       — accept/reject do not create episodes, runs, or publish
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.decision_reviews import (
    initialize_decision_reviews_schema,
    sync_decisions_to_reviews,
    list_decision_reviews,
    get_decision_review,
    accept_decision,
    reject_decision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_decision(
    *,
    decision_id: str | None = None,
    title: str = "Pick a topic",
    recommendation: str = "Do the thing",
    evidence: list | None = None,
    confidence: float = 0.75,
    priority: int = 2,
    dtype: str = "topic_selection",
) -> dict:
    return {
        "decision_id":    decision_id or str(uuid.uuid4()),
        "type":           dtype,
        "title":          title,
        "recommendation": recommendation,
        "evidence":       evidence if evidence is not None else ["signal A", "signal B"],
        "confidence":     confidence,
        "priority":       priority,
        "investigate_url": "/decisions",
        "action_label":   "Decide",
    }


def _fake_ds(decisions: list[dict], engine_status: str = "ready") -> dict:
    return {
        "checked_at":        "2026-01-01T00:00:00+00:00",
        "engine_status":     engine_status,
        "summary":           "test",
        "decisions":         decisions,
        "insufficient_data": [],
    }


@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "reviews_test.db"
    initialize_schema(p)
    return p


# ---------------------------------------------------------------------------
# TestSchema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_decision_reviews_table_exists(self, db):
        conn = get_connection(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_reviews'"
        ).fetchone()
        conn.close()
        assert row is not None, "decision_reviews table must exist after initialize_schema"

    def test_initialize_decision_reviews_schema_idempotent(self, db):
        initialize_decision_reviews_schema(db_path=db)
        initialize_decision_reviews_schema(db_path=db)
        conn = get_connection(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_reviews'"
        ).fetchone()
        conn.close()
        assert row is not None


# ---------------------------------------------------------------------------
# TestSyncCreates
# ---------------------------------------------------------------------------

class TestSyncCreates:
    def test_sync_inserts_pending_review(self, db):
        d = _fake_decision()
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds([d]),
        ):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["ok"] is True
        assert result["created"] == 1
        reviews = list_decision_reviews(db_path=db)
        assert len(reviews) == 1
        assert reviews[0]["operator_status"] == "pending"
        assert reviews[0]["decision_id"] == d["decision_id"]

    def test_sync_sets_correct_fields(self, db):
        d = _fake_decision(title="Specific title", recommendation="Do X", confidence=0.9)
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds([d]),
        ):
            sync_decisions_to_reviews(db_path=db)

        rev = list_decision_reviews(db_path=db)[0]
        assert rev["decision_title"] == "Specific title"
        assert rev["recommendation"] == "Do X"
        assert abs(rev["confidence"] - 0.9) < 0.001
        assert rev["reviewed_at"] is None


# ---------------------------------------------------------------------------
# TestSyncSkipsEmpty
# ---------------------------------------------------------------------------

class TestSyncSkipsEmpty:
    def test_sync_skips_decision_with_empty_evidence(self, db):
        d = _fake_decision(evidence=[])
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds([d]),
        ):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["ok"] is True
        assert result["created"] == 0
        assert result["skipped"] == 1
        assert list_decision_reviews(db_path=db) == []

    def test_sync_skips_decision_with_none_evidence(self, db):
        d = _fake_decision()
        d["evidence"] = None   # override after construction since helper defaults away None
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds([d]),
        ):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["created"] == 0
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# TestSyncIdempotent
# ---------------------------------------------------------------------------

class TestSyncIdempotent:
    def test_sync_twice_creates_only_one_row(self, db):
        d = _fake_decision()
        ds = _fake_ds([d])
        with patch("hypogeum.decision_engine.get_decision_state", return_value=ds):
            sync_decisions_to_reviews(db_path=db)
            result = sync_decisions_to_reviews(db_path=db)

        assert result["created"] == 0
        assert result["updated"] == 1
        assert len(list_decision_reviews(db_path=db)) == 1


# ---------------------------------------------------------------------------
# TestSyncPreservesReview
# ---------------------------------------------------------------------------

class TestSyncPreservesReview:
    def test_accepted_row_unchanged_on_resync(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)

        rev = list_decision_reviews(db_path=db)[0]
        accept_decision(rev["review_id"], note="ok", db_path=db)

        # Resync with updated engine output
        d2 = dict(d, title="Updated title", recommendation="New rec")
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d2])):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["skipped"] == 1
        after = get_decision_review(rev["review_id"], db_path=db)
        assert after["decision_title"] == d["title"]     # preserved
        assert after["operator_status"] == "accepted"    # preserved

    def test_rejected_row_unchanged_on_resync(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)

        rev = list_decision_reviews(db_path=db)[0]
        reject_decision(rev["review_id"], note="no", db_path=db)

        d2 = dict(d, title="Changed again")
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d2])):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["skipped"] == 1
        after = get_decision_review(rev["review_id"], db_path=db)
        assert after["operator_status"] == "rejected"


# ---------------------------------------------------------------------------
# TestSyncUpdatesPending
# ---------------------------------------------------------------------------

class TestSyncUpdatesPending:
    def test_pending_row_updated_on_resync(self, db):
        d = _fake_decision(title="Old title")
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)

        d2 = dict(d, title="New title", recommendation="New rec", confidence=0.55)
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d2])):
            result = sync_decisions_to_reviews(db_path=db)

        assert result["updated"] == 1
        rev = list_decision_reviews(db_path=db)[0]
        assert rev["decision_title"] == "New title"
        assert rev["recommendation"] == "New rec"
        assert abs(rev["confidence"] - 0.55) < 0.001
        assert rev["operator_status"] == "pending"


# ---------------------------------------------------------------------------
# TestAcceptReject
# ---------------------------------------------------------------------------

class TestAcceptReject:
    def _seed(self, db) -> str:
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        return list_decision_reviews(db_path=db)[0]["review_id"]

    def test_accept_sets_status(self, db):
        rid = self._seed(db)
        result = accept_decision(rid, note="approved", db_path=db)
        assert result["ok"] is True
        assert result["status"] == "accepted"
        rev = get_decision_review(rid, db_path=db)
        assert rev["operator_status"] == "accepted"
        assert rev["operator_note"] == "approved"
        assert rev["reviewed_at"] is not None

    def test_reject_sets_status(self, db):
        rid = self._seed(db)
        result = reject_decision(rid, note="declined", db_path=db)
        assert result["ok"] is True
        assert result["status"] == "rejected"
        rev = get_decision_review(rid, db_path=db)
        assert rev["operator_status"] == "rejected"
        assert rev["reviewed_at"] is not None

    def test_accept_returns_reviewed_at(self, db):
        rid = self._seed(db)
        result = accept_decision(rid, db_path=db)
        assert "reviewed_at" in result
        assert result["reviewed_at"] is not None


# ---------------------------------------------------------------------------
# TestAcceptRejectNotFound
# ---------------------------------------------------------------------------

class TestAcceptRejectNotFound:
    def test_accept_unknown_review_id(self, db):
        result = accept_decision("no-such-id", db_path=db)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_reject_unknown_review_id(self, db):
        result = reject_decision("no-such-id", db_path=db)
        assert result["ok"] is False
        assert result["code"] == "not_found"


# ---------------------------------------------------------------------------
# TestListFilter
# ---------------------------------------------------------------------------

class TestListFilter:
    def _seed_three(self, db):
        """Seed one pending, one accepted, one rejected."""
        decisions = [_fake_decision(decision_id=f"d_three_{i}") for i in range(3)]
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds(decisions),
        ):
            sync_decisions_to_reviews(db_path=db)

        # Look up review_ids by decision_id (stable, not positional)
        all_rows = list_decision_reviews(db_path=db)
        by_did = {r["decision_id"]: r["review_id"] for r in all_rows}
        ids = [by_did[f"d_three_{i}"] for i in range(3)]

        accept_decision(ids[1], db_path=db)
        reject_decision(ids[2], db_path=db)
        return ids

    def test_list_all_returns_three(self, db):
        self._seed_three(db)
        assert len(list_decision_reviews(db_path=db)) == 3

    def test_list_pending_only(self, db):
        self._seed_three(db)
        rows = list_decision_reviews(status="pending", db_path=db)
        assert all(r["operator_status"] == "pending" for r in rows)
        assert len(rows) == 1

    def test_list_accepted_only(self, db):
        self._seed_three(db)
        rows = list_decision_reviews(status="accepted", db_path=db)
        assert all(r["operator_status"] == "accepted" for r in rows)
        assert len(rows) == 1

    def test_list_rejected_only(self, db):
        self._seed_three(db)
        rows = list_decision_reviews(status="rejected", db_path=db)
        assert all(r["operator_status"] == "rejected" for r in rows)
        assert len(rows) == 1

    def test_list_pending_first(self, db):
        self._seed_three(db)
        rows = list_decision_reviews(db_path=db)
        assert rows[0]["operator_status"] == "pending"


# ---------------------------------------------------------------------------
# TestGetReview
# ---------------------------------------------------------------------------

class TestGetReview:
    def test_get_existing_review(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rid = list_decision_reviews(db_path=db)[0]["review_id"]
        rev = get_decision_review(rid, db_path=db)
        assert rev is not None
        assert rev["review_id"] == rid

    def test_get_nonexistent_returns_none(self, db):
        assert get_decision_review("ghost-id", db_path=db) is None

    def test_get_decodes_evidence(self, db):
        d = _fake_decision(evidence=["ev1", "ev2"])
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rid = list_decision_reviews(db_path=db)[0]["review_id"]
        rev = get_decision_review(rid, db_path=db)
        assert isinstance(rev["evidence"], list)
        assert "ev1" in rev["evidence"]


# ---------------------------------------------------------------------------
# TestNoSideEffects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_accept_does_not_create_run(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rid = list_decision_reviews(db_path=db)[0]["review_id"]
        accept_decision(rid, db_path=db)

        conn = get_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_reject_does_not_create_run(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rid = list_decision_reviews(db_path=db)[0]["review_id"]
        reject_decision(rid, db_path=db)

        conn = get_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0
