"""
tests/test_opportunity_repository.py — Unit tests for hypogeum/discovery/repository.py

Tests:
  StartCycle        — inserts row, idempotent duplicate returns structured failure
  SaveOpportunities — inserts all, preserves six string fields, canonical_subject,
                      assigns fresh opportunity_id, appends discovered event per opp
  UpdateOpportunity — mutates row and appends event atomically
  RecordEvent       — appends audit event with actor/detail in detail_json
  CompleteCycle     — writes funnel metrics and status=complete
  FailCycle         — marks status=failed
  Resilience        — invalid DB path returns {ok: False} without raising
  V1Regression      — v1 discovery unaffected

All tests use a real in-memory/tmp SQLite DB via initialize_schema.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hypogeum.discovery.repository import (
    complete_cycle,
    fail_cycle,
    record_event,
    save_opportunities,
    start_cycle,
    update_opportunity,
)
from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_id() -> str:
    return str(uuid.uuid4())


def _init_db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    db = tmp_path / "test.db"
    initialize_schema(db)
    return db


def _opp(
    topic_text: str = "Lost City of Helike",
    **kw,
) -> dict:
    base = {
        "topic_text":            topic_text,
        "angle":                 "The Poseidon legend",
        "mystery_hook":          "Why did an entire Greek city vanish in a single night?",
        "differentiation_claim": "First video using 2001 sonar data from Katsonopoulou",
        "visual_signal":         "Underwater ruins at golden hour",
        "evidence_signal":       "2001 sonar survey peer-reviewed paper",
        "canonical_subject":     _make_canonical_subject(topic_text),
        "initial_composite":     0.72,
    }
    base.update(kw)
    return base


def _fetch_cycles(db: Path) -> list:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        return conn.execute("SELECT * FROM discovery_cycles").fetchall()
    finally:
        conn.close()


def _fetch_opps(db: Path) -> list:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        return conn.execute("SELECT * FROM opportunities").fetchall()
    finally:
        conn.close()


def _fetch_events(db: Path, opportunity_id: str = None) -> list:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        if opportunity_id:
            return conn.execute(
                "SELECT * FROM opportunity_events WHERE opportunity_id=? ORDER BY created_at",
                (opportunity_id,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM opportunity_events ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def _opp_row(db: Path, opportunity_id: str) -> dict:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute(
            "SELECT * FROM opportunities WHERE opportunity_id=?", (opportunity_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def _cycle_row(db: Path, cycle_id: str) -> dict:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    conn.row_factory = __import__("sqlite3").Row
    try:
        row = conn.execute(
            "SELECT * FROM discovery_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# 1. start_cycle
# ---------------------------------------------------------------------------

class TestStartCycle:

    def test_inserts_discovery_cycles_row(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = start_cycle(cid, "ch_test", db_path=db)
        assert result["ok"] is True
        rows = _fetch_cycles(db)
        assert len(rows) == 1

    def test_row_has_correct_fields(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", engine_used="v2", db_path=db)
        row = _cycle_row(db, cid)
        assert row["cycle_id"] == cid
        assert row["channel_id"] == "ch_test"
        assert row["engine_used"] == "v2"
        assert row["status"] == "running"
        assert row["created_at"]

    def test_engine_used_default_is_v2(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        row = _cycle_row(db, cid)
        assert row["engine_used"] == "v2"

    def test_duplicate_cycle_id_returns_structured_failure(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        result = start_cycle(cid, "ch_other", db_path=db)
        assert result["ok"] is False
        assert result["failure_reason"] == "duplicate_cycle"

    def test_duplicate_does_not_raise(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        result = start_cycle(cid, "ch_test", db_path=db)  # must not raise
        assert "ok" in result

    def test_returns_cycle_id_and_channel_id(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = start_cycle(cid, "ch_abc", db_path=db)
        assert result["cycle_id"] == cid
        assert result["channel_id"] == "ch_abc"


# ---------------------------------------------------------------------------
# 2. save_opportunities
# ---------------------------------------------------------------------------

class TestSaveOpportunities:

    def test_inserts_all_opportunities(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opps = [_opp(f"Topic {i}") for i in range(5)]
        result = save_opportunities(opps, cid, db_path=db)
        assert result["ok"] is True
        assert result["saved_count"] == 5
        assert len(_fetch_opps(db)) == 5

    def test_preserves_six_string_fields(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp("Ancient Roman Aqueducts")
        result = save_opportunities([opp], cid, db_path=db)
        opp_id = result["opportunity_ids"][0]
        row = _opp_row(db, opp_id)
        assert row["topic_text"]            == opp["topic_text"]
        assert row["angle"]                 == opp["angle"]
        assert row["mystery_hook"]          == opp["mystery_hook"]
        assert row["differentiation_claim"] == opp["differentiation_claim"]
        assert row["visual_signal"]         == opp["visual_signal"]
        assert row["evidence_signal"]       == opp["evidence_signal"]

    def test_preserves_canonical_subject(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp("The Helike Underwater Mystery")
        result = save_opportunities([opp], cid, db_path=db)
        opp_id = result["opportunity_ids"][0]
        row = _opp_row(db, opp_id)
        assert row["canonical_subject"] == opp["canonical_subject"]

    def test_opportunity_id_assigned_by_repo_not_caller(self, tmp_path):
        """The repository must assign a fresh UUID; no opportunity_id comes from the caller."""
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp()
        result = save_opportunities([opp], cid, db_path=db)
        assert len(result["opportunity_ids"]) == 1
        assigned_id = result["opportunity_ids"][0]
        row = _opp_row(db, assigned_id)
        assert row["opportunity_id"] == assigned_id
        # The assigned id must be a valid UUID
        uuid.UUID(assigned_id)  # raises if invalid

    def test_appends_discovered_event_per_opportunity(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opps = [_opp(f"Topic {i}") for i in range(3)]
        result = save_opportunities(opps, cid, db_path=db)
        events = _fetch_events(db)
        assert len(events) == 3

    def test_event_type_is_discovered(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = save_opportunities([_opp()], cid, db_path=db)
        opp_id = result["opportunity_ids"][0]
        events = _fetch_events(db, opp_id)
        assert len(events) == 1
        # event_type column index in the row
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            ev = conn.execute(
                "SELECT * FROM opportunity_events WHERE opportunity_id=?", (opp_id,)
            ).fetchone()
        finally:
            conn.close()
        assert dict(ev)["event_type"] == "discovered"

    def test_event_cycle_id_matches(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = save_opportunities([_opp()], cid, db_path=db)
        opp_id = result["opportunity_ids"][0]
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            ev = conn.execute(
                "SELECT cycle_id FROM opportunity_events WHERE opportunity_id=?", (opp_id,)
            ).fetchone()
        finally:
            conn.close()
        assert dict(ev)["cycle_id"] == cid

    def test_preserves_initial_composite(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp(initial_composite=0.83)
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert abs(row["initial_composite"] - 0.83) < 1e-6

    def test_verification_fields_preserved(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp(verification_status="verified", source_count=3, source_score=1.0)
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["verification_status"] == "verified"
        assert row["source_count"] == 3
        assert abs(row["source_score"] - 1.0) < 1e-6

    def test_missing_optional_fields_inserted_as_null(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        # Minimal opportunity — only required fields
        opp = {"topic_text": "Minimal Topic Only", "canonical_subject": "minimal_topic_only"}
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["angle"] is None
        assert row["mystery_hook"] is None
        assert row["initial_composite"] is None

    def test_status_is_active_on_creation(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = save_opportunities([_opp()], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["status"] == "active"

    def test_returns_opportunity_ids_list(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opps = [_opp(f"T{i}") for i in range(4)]
        result = save_opportunities(opps, cid, db_path=db)
        assert len(result["opportunity_ids"]) == 4
        # All IDs distinct
        assert len(set(result["opportunity_ids"])) == 4

    def test_persists_generation_source_llm_ideation(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp(generation_source="llm_ideation")
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["generation_source"] == "llm_ideation"

    def test_persists_generation_source_fallback_seed(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = _opp(generation_source="fallback_seed")
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["generation_source"] == "fallback_seed"

    def test_generation_source_null_when_absent(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        opp = {"topic_text": "Topic Without Source", "canonical_subject": "topic_without_source"}
        result = save_opportunities([opp], cid, db_path=db)
        row = _opp_row(db, result["opportunity_ids"][0])
        assert row["generation_source"] is None


# ---------------------------------------------------------------------------
# 3. update_opportunity
# ---------------------------------------------------------------------------

class TestUpdateOpportunity:

    def _setup(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = save_opportunities([_opp()], cid, db_path=db)
        opp_id = result["opportunity_ids"][0]
        return db, opp_id

    def test_mutates_opportunity_row(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        update_opportunity(
            opp_id, {"status": "selected"}, "consumed", "discovery_director",
            db_path=db,
        )
        row = _opp_row(db, opp_id)
        assert row["status"] == "selected"

    def test_appends_event_atomically(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        # One event already exists (discovered from save_opportunities)
        result = update_opportunity(
            opp_id, {"status": "selected"}, "consumed", "discovery_director",
            db_path=db,
        )
        events = _fetch_events(db, opp_id)
        assert len(events) == 2  # discovered + consumed

    def test_returns_event_id(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        result = update_opportunity(
            opp_id, {"status": "rejected"}, "rejected", "director", db_path=db,
        )
        assert result["ok"] is True
        assert "event_id" in result
        assert result["event_id"]

    def test_fields_updated_list_in_result(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        result = update_opportunity(
            opp_id, {"source_score": 0.67, "source_count": 2},
            "verified", "source_verifier", db_path=db,
        )
        assert "source_score" in result["fields_updated"]
        assert "source_count" in result["fields_updated"]

    def test_unknown_fields_silently_dropped(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        result = update_opportunity(
            opp_id, {"status": "selected", "nonexistent_col": "boom"},
            "consumed", "director", db_path=db,
        )
        assert result["ok"] is True
        assert "nonexistent_col" not in result["fields_updated"]

    def test_last_seen_at_always_refreshed(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        before = _opp_row(db, opp_id)["last_seen_at"]
        import time; time.sleep(0.01)
        update_opportunity(opp_id, {"status": "selected"}, "consumed", "d", db_path=db)
        after = _opp_row(db, opp_id)["last_seen_at"]
        assert after >= before

    def test_actor_stored_in_detail_json(self, tmp_path):
        db, opp_id = self._setup(tmp_path)
        result = update_opportunity(
            opp_id, {"status": "rejected"}, "rejected", "test_actor",
            detail="low score", db_path=db,
        )
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            ev = conn.execute(
                "SELECT detail_json FROM opportunity_events WHERE event_id=?",
                (result["event_id"],),
            ).fetchone()
        finally:
            conn.close()
        detail = json.loads(dict(ev)["detail_json"])
        assert detail["actor"] == "test_actor"
        assert detail["detail"] == "low score"


# ---------------------------------------------------------------------------
# 4. record_event
# ---------------------------------------------------------------------------

class TestRecordEvent:

    def _setup(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        result = save_opportunities([_opp()], cid, db_path=db)
        return db, result["opportunity_ids"][0], cid

    def test_appends_event_row(self, tmp_path):
        db, opp_id, cid = self._setup(tmp_path)
        record_event(opp_id, "verified", "source_verifier", db_path=db)
        events = _fetch_events(db, opp_id)
        assert len(events) == 2  # discovered + verified

    def test_returns_event_id(self, tmp_path):
        db, opp_id, _ = self._setup(tmp_path)
        result = record_event(opp_id, "verified", "verifier", db_path=db)
        assert result["ok"] is True
        assert result["event_id"]

    def test_actor_in_detail_json(self, tmp_path):
        db, opp_id, _ = self._setup(tmp_path)
        result = record_event(opp_id, "verified", "src_verifier",
                              detail="3 sources found", db_path=db)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            ev = conn.execute(
                "SELECT detail_json FROM opportunity_events WHERE event_id=?",
                (result["event_id"],),
            ).fetchone()
        finally:
            conn.close()
        payload = json.loads(dict(ev)["detail_json"])
        assert payload["actor"] == "src_verifier"
        assert payload["detail"] == "3 sources found"

    def test_cycle_id_and_channel_id_stored(self, tmp_path):
        db, opp_id, cid = self._setup(tmp_path)
        result = record_event(
            opp_id, "verified", "verifier",
            cycle_id=cid, channel_id="ch_test", db_path=db,
        )
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.row_factory = __import__("sqlite3").Row
        try:
            ev = conn.execute(
                "SELECT * FROM opportunity_events WHERE event_id=?",
                (result["event_id"],),
            ).fetchone()
        finally:
            conn.close()
        row = dict(ev)
        assert row["cycle_id"] == cid
        assert row["channel_id"] == "ch_test"

    def test_invalid_event_type_returns_ok_false(self, tmp_path):
        db, opp_id, _ = self._setup(tmp_path)
        result = record_event(opp_id, "not_a_real_event_type", "actor", db_path=db)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 5. complete_cycle
# ---------------------------------------------------------------------------

class TestCompleteCycle:

    def test_sets_status_complete(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        complete_cycle(cid, {}, db_path=db)
        row = _cycle_row(db, cid)
        assert row["status"] == "complete"

    def test_sets_completed_at(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        complete_cycle(cid, {}, db_path=db)
        row = _cycle_row(db, cid)
        assert row["completed_at"] is not None

    def test_writes_funnel_metrics(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        summary = {
            "raw_candidates_count":     8,
            "viable_after_feasibility": 7,
            "verify_eligible_count":    5,
            "verified_count":           3,
            "rejected_source_count":    2,
            "rejected_score_count":     0,
        }
        complete_cycle(cid, summary, db_path=db)
        row = _cycle_row(db, cid)
        assert row["raw_candidates_count"] == 8
        assert row["verify_eligible_count"] == 5
        assert row["verified_count"] == 3

    def test_writes_selected_opportunity_id(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        opp_id = str(uuid.uuid4())
        complete_cycle(cid, {
            "selected_opportunity_id": opp_id,
            "selected_canonical_subject": "helike_underwater_mystery",
        }, db_path=db)
        row = _cycle_row(db, cid)
        assert row["selected_opportunity_id"] == opp_id
        assert row["selected_canonical_subject"] == "helike_underwater_mystery"

    def test_extra_summary_keys_ignored(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        result = complete_cycle(cid, {"unknown_key": "value"}, db_path=db)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# 6. fail_cycle
# ---------------------------------------------------------------------------

class TestFailCycle:

    def test_marks_status_failed(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        fail_cycle(cid, "LLM call exhausted credits", db_path=db)
        row = _cycle_row(db, cid)
        assert row["status"] == "failed"

    def test_sets_completed_at(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        fail_cycle(cid, "timeout", db_path=db)
        row = _cycle_row(db, cid)
        assert row["completed_at"] is not None

    def test_returns_ok_true(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        result = fail_cycle(cid, "reason", db_path=db)
        assert result["ok"] is True
        assert result["cycle_id"] == cid

    def test_reason_in_return_dict(self, tmp_path):
        db = _init_db(tmp_path)
        cid = _cycle_id()
        start_cycle(cid, "ch_test", db_path=db)
        result = fail_cycle(cid, "credits_exhausted", db_path=db)
        assert result["reason"] == "credits_exhausted"


# ---------------------------------------------------------------------------
# 7. Resilience — invalid DB path / SQL error
# ---------------------------------------------------------------------------

class TestResilience:

    def test_start_cycle_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = start_cycle("cyc_1", "ch_1", db_path=bad)
        assert result["ok"] is False
        assert "failure_reason" in result

    def test_save_opportunities_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = save_opportunities([_opp()], "cyc_1", db_path=bad)
        assert result["ok"] is False

    def test_record_event_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = record_event("opp_x", "verified", "actor", db_path=bad)
        assert result["ok"] is False

    def test_update_opportunity_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = update_opportunity(
            "opp_x", {"status": "selected"}, "consumed", "actor", db_path=bad,
        )
        assert result["ok"] is False

    def test_complete_cycle_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = complete_cycle("cyc_1", {}, db_path=bad)
        assert result["ok"] is False

    def test_fail_cycle_bad_path_returns_ok_false(self, tmp_path):
        bad = tmp_path / "nonexistent_dir" / "test.db"
        result = fail_cycle("cyc_1", "reason", db_path=bad)
        assert result["ok"] is False

    def test_no_function_raises_on_bad_path(self, tmp_path):
        bad = tmp_path / "no_dir" / "x.db"
        for fn, args in [
            (start_cycle,          ("c1", "ch")),
            (save_opportunities,   ([_opp()], "c1")),
            (record_event,         ("o1", "verified", "a")),
            (update_opportunity,   ("o1", {"status": "active"}, "consumed", "a")),
            (complete_cycle,       ("c1", {})),
            (fail_cycle,           ("c1", "r")),
        ]:
            result = fn(*args, db_path=bad)  # must not raise
            assert "ok" in result


# ---------------------------------------------------------------------------
# 8. V1 regression
# ---------------------------------------------------------------------------

class TestV1Regression:

    def test_v1_score_seed_unaffected(self):
        from hypogeum.topic_discovery import _score_seed
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.82

    def test_v1_run_discovery_cycle_produces_candidates(self, tmp_path):
        import os
        from hypogeum.db import initialize_schema
        from hypogeum.topic_discovery import run_discovery_cycle
        db = tmp_path / "v1.db"
        initialize_schema(db)
        old = os.environ.get("DISCOVERY_ENGINE")
        try:
            os.environ.pop("DISCOVERY_ENGINE", None)
            result = run_discovery_cycle("ch_v1_repo", db_path=db)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7

    def test_repository_import_does_not_touch_v1(self):
        from hypogeum.topic_discovery import _score_seed
        from hypogeum.discovery import repository  # noqa: F401
        seed = {
            "topic": "Secret societies and the empires they shaped",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.64
