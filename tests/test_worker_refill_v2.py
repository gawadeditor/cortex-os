"""
tests/test_worker_refill_v2.py — Worker-level refill tests for v1 and v2 engines

Tests that _check_and_refill_queue() drives the correct discovery path depending
on DISCOVERY_ENGINE, and that the DB effects are correct in each case.

Structure:
  TestRefillV1           — v1 heuristic path via run_discovery_cycle()
  TestRefillV2           — v2 director path via run_discovery_cycle()
  TestStubConfigReadsEnv — _build_v2_config() reads AUTO_DISCOVERY_AUTO_APPROVE
                           and DISCOVERY_VERIFY_THRESHOLD from os.environ
  TestV1Unchanged        — v1 behavior is not affected by v2 changes

All DB tests use a real in-memory SQLite initialized via initialize_schema.
External API calls (run_agent, query_perplexity) are always mocked.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ─── import the function under test from run_worker ────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_worker import _check_and_refill_queue


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

CHANNEL_ID = "ch_refill_test"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db(tmp_path: Path) -> Path:
    """Full schema + channel_profiles + channels rows for validate_channel and list_channels."""
    from hypogeum.db import initialize_schema, get_connection
    from hypogeum.channel_config import _ensure_tables as _ensure_channel_tables
    from hypogeum.channel_manager import _ensure_tables as _ensure_manager_tables

    db = tmp_path / "refill.db"
    initialize_schema(db)

    conn = get_connection(db)
    try:
        # channel_profiles: needed by enqueue_topic(validate_channel=True)
        _ensure_channel_tables(conn)
        # channels: needed by list_channels() and channel_manager
        _ensure_manager_tables(conn)

        conn.execute(
            "INSERT OR IGNORE INTO channel_profiles (channel_id, name, created_at) "
            "VALUES (?,?,?)",
            (CHANNEL_ID, "Refill Test Channel", _now()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_id, name, created_at) "
            "VALUES (?,?,?)",
            (CHANNEL_ID, "Refill Test Channel", _now()),
        )
        conn.commit()
    finally:
        conn.close()

    # 2026-08-23: _check_and_refill_queue()'s per-channel operator_enabled
    # gate now fails CLOSED on a missing ChannelOperationsProfile
    # (autonomy-safety correction -- discovery is a paid, mutating
    # action). A real row here (this file uses a real DB, unlike
    # test_autonomous_discovery_loop.py's globally-mocked connection)
    # keeps every existing test in this file exercising the v1/v2 refill
    # logic they actually target, rather than being blocked earlier by
    # the (now correct) missing-profile gate.
    from hypogeum.channel_config import create_channel_operations_profile
    create_channel_operations_profile(
        channel_id=CHANNEL_ID, channel_name="Refill Test Channel", timezone="UTC",
        operator_enabled=True, db_path=db,
    )
    return db


def _row_counts(db: Path) -> dict:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        return {
            "topic_candidates":   conn.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0],
            "discovery_cycles":   conn.execute("SELECT COUNT(*) FROM discovery_cycles").fetchone()[0],
            "opportunities":      conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
            "opportunity_events": conn.execute("SELECT COUNT(*) FROM opportunity_events").fetchone()[0],
            "episode_topics":     conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0],
        }
    finally:
        conn.close()


def _fake_channel():
    ch = SimpleNamespace()
    ch.channel_id = CHANNEL_ID
    return ch


def _mock_llm_batch(channel_id: str = CHANNEL_ID) -> dict:
    return {
        "batch_id":    str(uuid.uuid4()),
        "channel_id":  channel_id,
        "engine":      "v2",
        "opportunities": [
            {
                "topic_text":            "The Lost City of Helike",
                "angle":                 "Underwater archaeology perspective",
                "mystery_hook":          "Why did an entire Greek city vanish beneath the waves overnight?",
                "differentiation_claim": "First video using 2001 Katsonopoulou sonar dataset",
                "visual_signal":         "Underwater ruins at golden hour",
                "evidence_signal":       "Peer-reviewed 2001 sonar survey paper",
            },
            {
                "topic_text":            "Roman Aqueducts Engineering Secrets",
                "angle":                 "Civil engineering marvel",
                "mystery_hook":          "How did ancient Romans build water systems still working after 2000 years?",
                "differentiation_claim": "Using newly digitized Vatican archive manuscripts",
                "visual_signal":         "Segovia aqueduct at dawn",
                "evidence_signal":       "Vatican Library manuscript digitization project 2019",
            },
            {
                "topic_text":            "The Bronze Age Collapse",
                "angle":                 "Multi-civilization simultaneous collapse",
                "mystery_hook":          "What single event destroyed twelve civilizations across three continents?",
                "differentiation_claim": "Cross-referencing Egyptian, Hittite, and Mycenaean primary sources",
                "visual_signal":         "Archaeological sites of Bronze Age cities",
                "evidence_signal":       "Eric Cline Sea Peoples research 2014",
            },
            {
                "topic_text":            "Göbekli Tepe Before Agriculture",
                "angle":                 "Rewriting human history",
                "mystery_hook":          "How did hunter-gatherers build the world's oldest temple without agriculture?",
                "differentiation_claim": "Using Schmidt's original excavation notes from 1994–2014",
                "visual_signal":         "T-shaped pillars at Göbekli Tepe at sunrise",
                "evidence_signal":       "Klaus Schmidt original excavation reports",
            },
            {
                "topic_text":            "Antikythera Mechanism Origins",
                "angle":                 "Lost technological knowledge",
                "mystery_hook":          "How did ancient Greeks know enough astronomy to build a 2000-year-old computer?",
                "differentiation_claim": "Using 2016 AMRP X-ray tomography data",
                "visual_signal":         "Close-up of bronze gears and inscriptions",
                "evidence_signal":       "AMRP 2016 X-ray tomography dataset",
            },
            {
                "topic_text":            "Cleopatra's Lost Tomb",
                "angle":                 "Archaeological detective story",
                "mystery_hook":          "Why has the most famous queen's tomb never been found after 2000 years?",
                "differentiation_claim": "Combining Taposiris Magna reports with 2023 GPR surveys",
                "visual_signal":         "Taposiris Magna temple at sunset",
                "evidence_signal":       "Martinez 2023 GPR survey",
            },
        ],
    }


def _mock_plex_result(n_citations: int = 3) -> MagicMock:
    result = MagicMock()
    result.citations = [f"https://academic.example.com/src{i}" for i in range(n_citations)]
    return result


# ---------------------------------------------------------------------------
# 1. V1 refill path
# ---------------------------------------------------------------------------

class TestRefillV1:
    """_check_and_refill_queue with DISCOVERY_ENGINE unset → v1 heuristic."""

    def test_empty_queue_triggers_v1_discovery(self, tmp_path):
        db = _init_db(tmp_path)
        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]):
                result = _check_and_refill_queue(db_path=db, threshold=2, auto_approve=False)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        assert result["channels_checked"] == 1
        assert result["channels_refilled"] == 1
        assert len(result["cycles_run"]) == 1

    def test_v1_writes_topic_candidates(self, tmp_path):
        db = _init_db(tmp_path)
        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]):
                _check_and_refill_queue(db_path=db, threshold=2, auto_approve=False)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        counts = _row_counts(db)
        assert counts["topic_candidates"] == 7

    def test_v1_does_not_write_discovery_cycles(self, tmp_path):
        db = _init_db(tmp_path)
        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]):
                _check_and_refill_queue(db_path=db, threshold=2, auto_approve=False)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        counts = _row_counts(db)
        assert counts["discovery_cycles"] == 0
        assert counts["opportunities"] == 0

    def test_v1_above_threshold_no_discovery(self, tmp_path):
        db = _init_db(tmp_path)
        # Insert 3 queued topics so depth >= threshold=2
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO episode_topics "
                    "(topic_id, channel_id, topic_text, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), CHANNEL_ID, f"Topic {i}", "queued", _now(), _now()),
                )
            conn.commit()
        finally:
            conn.close()

        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]):
                result = _check_and_refill_queue(db_path=db, threshold=2, auto_approve=False)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        assert result["channels_refilled"] == 0
        assert _row_counts(db)["topic_candidates"] == 0

    def test_v1_auto_approve_enqueues_topic(self, tmp_path):
        db = _init_db(tmp_path)
        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]):
                result = _check_and_refill_queue(db_path=db, threshold=2, auto_approve=True)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        counts = _row_counts(db)
        assert counts["episode_topics"] == 1
        assert len(result["topics_enqueued"]) == 1


# ---------------------------------------------------------------------------
# 2. V2 refill path
# ---------------------------------------------------------------------------

class TestRefillV2:
    """_check_and_refill_queue with DISCOVERY_ENGINE=v2 → Discovery Director."""

    def _run_v2_refill(self, db: Path, auto_approve: bool = False):
        """Run _check_and_refill_queue with DISCOVERY_ENGINE=v2 and mocked externals."""
        old_engine = os.environ.get("DISCOVERY_ENGINE")
        old_approve = os.environ.get("AUTO_DISCOVERY_AUTO_APPROVE")
        os.environ["DISCOVERY_ENGINE"] = "v2"
        os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = "true" if auto_approve else "false"
        os.environ["DISCOVERY_VERIFY_THRESHOLD"] = "0.40"   # generous → all opps qualify

        try:
            with patch("hypogeum.channel_config.list_channel_profiles", return_value=[_fake_channel()]), \
                 patch("hypogeum.discovery.topic_ideator.run_agent",
                       return_value=_mock_llm_batch()), \
                 patch("hypogeum.discovery.source_verifier.query_perplexity",
                       return_value=_mock_plex_result(3)):
                return _check_and_refill_queue(db_path=db, threshold=2, auto_approve=auto_approve)
        finally:
            if old_engine is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old_engine
            if old_approve is None:
                os.environ.pop("AUTO_DISCOVERY_AUTO_APPROVE", None)
            else:
                os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = old_approve
            os.environ.pop("DISCOVERY_VERIFY_THRESHOLD", None)

    def test_v2_triggers_director_on_empty_queue(self, tmp_path):
        db = _init_db(tmp_path)
        result = self._run_v2_refill(db)
        assert result["channels_refilled"] == 1
        assert len(result["cycles_run"]) == 1

    def test_v2_writes_discovery_cycles_row(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db)
        counts = _row_counts(db)
        assert counts["discovery_cycles"] == 1

    def test_v2_writes_opportunities(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db)
        counts = _row_counts(db)
        assert counts["opportunities"] == 6

    def test_v2_writes_opportunity_events(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db)
        counts = _row_counts(db)
        assert counts["opportunity_events"] >= 7  # 6 discovered + 1 selected

    def test_v2_pending_approval_no_episode_topics(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db, auto_approve=False)
        counts = _row_counts(db)
        assert counts["episode_topics"] == 0

    def test_v2_auto_approve_writes_episode_topics(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db, auto_approve=True)
        counts = _row_counts(db)
        assert counts["episode_topics"] == 1

    def test_v2_does_not_write_topic_candidates(self, tmp_path):
        """v2 uses the repository tables, not topic_candidates."""
        db = _init_db(tmp_path)
        self._run_v2_refill(db)
        counts = _row_counts(db)
        assert counts["topic_candidates"] == 0

    def test_v2_discovery_cycle_row_status_complete(self, tmp_path):
        db = _init_db(tmp_path)
        self._run_v2_refill(db)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT status, engine_used FROM discovery_cycles LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "complete"
        assert row[1] == "v2"

    def test_v2_enqueued_opportunity_status(self, tmp_path):
        """When auto_approve=True, the winning opportunity must be marked enqueued."""
        db = _init_db(tmp_path)
        self._run_v2_refill(db, auto_approve=True)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            statuses = [
                row[0] for row in conn.execute(
                    "SELECT status FROM opportunities"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert "enqueued" in statuses

    def test_v2_pending_opportunity_status(self, tmp_path):
        """When auto_approve=False, the winning opportunity must be marked selected."""
        db = _init_db(tmp_path)
        self._run_v2_refill(db, auto_approve=False)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            statuses = [
                row[0] for row in conn.execute(
                    "SELECT status FROM opportunities"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert "selected" in statuses

    def test_v2_result_has_ok_true(self, tmp_path):
        db = _init_db(tmp_path)
        result = self._run_v2_refill(db)
        assert result["channels_refilled"] == 1
        assert result["channels_checked"] == 1

    def test_v2_above_threshold_no_discovery(self, tmp_path):
        db = _init_db(tmp_path)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO episode_topics "
                    "(topic_id, channel_id, topic_text, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), CHANNEL_ID, f"Topic {i}", "queued", _now(), _now()),
                )
            conn.commit()
        finally:
            conn.close()

        result = self._run_v2_refill(db)
        assert result["channels_refilled"] == 0
        counts = _row_counts(db)
        assert counts["discovery_cycles"] == 0


# ---------------------------------------------------------------------------
# 3. Stub config construction
# ---------------------------------------------------------------------------

class TestStubConfigReadsEnv:
    """_build_v2_config() reads relevant keys from os.environ."""

    def test_reads_auto_discovery_auto_approve_true(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.get("AUTO_DISCOVERY_AUTO_APPROVE")
        try:
            os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = "true"
            cfg = _build_v2_config()
            assert cfg.AUTO_DISCOVERY_AUTO_APPROVE is True
        finally:
            if old is None:
                os.environ.pop("AUTO_DISCOVERY_AUTO_APPROVE", None)
            else:
                os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = old

    def test_reads_auto_discovery_auto_approve_false(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.get("AUTO_DISCOVERY_AUTO_APPROVE")
        try:
            os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = "false"
            cfg = _build_v2_config()
            assert cfg.AUTO_DISCOVERY_AUTO_APPROVE is False
        finally:
            if old is None:
                os.environ.pop("AUTO_DISCOVERY_AUTO_APPROVE", None)
            else:
                os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = old

    def test_reads_verify_threshold(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.get("DISCOVERY_VERIFY_THRESHOLD")
        try:
            os.environ["DISCOVERY_VERIFY_THRESHOLD"] = "0.55"
            cfg = _build_v2_config()
            assert cfg.DISCOVERY_VERIFY_THRESHOLD == pytest.approx(0.55)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_VERIFY_THRESHOLD", None)
            else:
                os.environ["DISCOVERY_VERIFY_THRESHOLD"] = old

    def test_reads_verify_delta(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.get("DISCOVERY_VERIFY_DELTA")
        try:
            os.environ["DISCOVERY_VERIFY_DELTA"] = "0.15"
            cfg = _build_v2_config()
            assert cfg.DISCOVERY_VERIFY_DELTA == pytest.approx(0.15)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_VERIFY_DELTA", None)
            else:
                os.environ["DISCOVERY_VERIFY_DELTA"] = old

    def test_defaults_auto_approve_false_when_unset(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.pop("AUTO_DISCOVERY_AUTO_APPROVE", None)
        try:
            cfg = _build_v2_config()
            assert cfg.AUTO_DISCOVERY_AUTO_APPROVE is False
        finally:
            if old is not None:
                os.environ["AUTO_DISCOVERY_AUTO_APPROVE"] = old

    def test_default_verify_threshold_065(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.pop("DISCOVERY_VERIFY_THRESHOLD", None)
        try:
            cfg = _build_v2_config()
            assert cfg.DISCOVERY_VERIFY_THRESHOLD == pytest.approx(0.65)
        finally:
            if old is not None:
                os.environ["DISCOVERY_VERIFY_THRESHOLD"] = old

    def test_reads_openrouter_api_key(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.get("OPENROUTER_API_KEY")
        try:
            os.environ["OPENROUTER_API_KEY"] = "sk-test-key-xyz"
            cfg = _build_v2_config()
            assert cfg.OPENROUTER_API_KEY == "sk-test-key-xyz"
        finally:
            if old is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old

    def test_openrouter_app_name_defaults_to_hypogeum_brain(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.pop("OPENROUTER_APP_NAME", None)
        try:
            cfg = _build_v2_config()
            assert cfg.OPENROUTER_APP_NAME == "hypogeum-brain"
        finally:
            if old is not None:
                os.environ["OPENROUTER_APP_NAME"] = old

    def test_openrouter_app_url_defaults_to_none(self):
        from hypogeum.topic_discovery import _build_v2_config
        old = os.environ.pop("OPENROUTER_APP_URL", None)
        try:
            cfg = _build_v2_config()
            assert cfg.OPENROUTER_APP_URL is None
        finally:
            if old is not None:
                os.environ["OPENROUTER_APP_URL"] = old

    def test_config_has_all_openrouter_headers_fields(self):
        """Config must supply every field that openrouter._build_headers() reads."""
        from hypogeum.topic_discovery import _build_v2_config
        cfg = _build_v2_config()
        assert hasattr(cfg, "OPENROUTER_API_KEY")
        assert hasattr(cfg, "OPENROUTER_APP_NAME")
        assert hasattr(cfg, "OPENROUTER_APP_URL")

    def test_stub_passes_config_to_director(self):
        """_run_discovery_cycle_v2_stub must forward a config (not None) to the director."""
        from hypogeum.topic_discovery import _run_discovery_cycle_v2_stub
        configs_received = []

        def fake_director(channel_id, config=None, db_path=None, **kw):
            configs_received.append(config)
            return {
                "ok": True, "engine_used": "v2", "cycle_id": "c1",
                "channel_id": channel_id, "candidate_count": 0,
                "verified_count": 0, "selected_opportunity_id": None,
                "topic_id_enqueued": None, "route_status": "no_candidate",
                "opportunities": [],
            }

        with patch("hypogeum.agents.discovery_director.run", side_effect=fake_director):
            _run_discovery_cycle_v2_stub("ch_test")

        assert len(configs_received) == 1
        assert configs_received[0] is not None
        assert hasattr(configs_received[0], "AUTO_DISCOVERY_AUTO_APPROVE")


# ---------------------------------------------------------------------------
# 4. V1 unchanged by v2 changes
# ---------------------------------------------------------------------------

class TestV1Unchanged:

    def test_v1_score_seed_still_frozen(self):
        from hypogeum.topic_discovery import _score_seed
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.82

    def test_v1_run_discovery_cycle_produces_7_candidates(self, tmp_path):
        db = _init_db(tmp_path)
        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            from hypogeum.topic_discovery import run_discovery_cycle
            result = run_discovery_cycle(CHANNEL_ID, db_path=db)
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7

    def test_engine_default_remains_v1(self):
        """Without DISCOVERY_ENGINE=v2, the director is never called."""
        v2_called = []

        def fake_director(*a, **kw):
            v2_called.append(True)
            return {}

        old = os.environ.pop("DISCOVERY_ENGINE", None)
        try:
            from hypogeum.topic_discovery import run_discovery_cycle
            with patch("hypogeum.agents.discovery_director.run", side_effect=fake_director):
                with patch("hypogeum.topic_discovery._run_discovery_cycle_v1",
                           return_value={"ok": True, "cycle_id": "c", "channel_id": "ch",
                                         "candidate_count": 7, "selected_candidate_id": None,
                                         "candidates": []}):
                    run_discovery_cycle("ch_test")
        finally:
            if old:
                os.environ["DISCOVERY_ENGINE"] = old

        assert v2_called == [], "Director must not be called when DISCOVERY_ENGINE is unset"


import pytest
