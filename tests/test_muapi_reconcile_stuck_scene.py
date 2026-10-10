"""
tests/test_muapi_reconcile_stuck_scene.py — Unit tests for
hypogeum.muapi.reconcile_stuck_scene_authorization() (2026-09-05).

Live-observed root cause: a poll timeout after a successful MUAPI
submission leaves the authorization permanently at UNKNOWN_ACCEPTANCE_
REQUIRES_RECONCILIATION -- no future attempt at that exact scene (same
idempotency_key) can ever be resubmitted, because "authorization state is
'submitting', not 'authorized'" fails closed every time. Two consecutive
real hypogeum episodes (ep_edaa9625, ep_14a830cc) each lost an otherwise-
complete render to exactly one scene stuck this way. This function is the
reconciliation the architecture always intended
(hypogeum.submission_gate.submit()'s own "an explicit reconciliation
event resolves it" comment) but that nothing ever actually called.

Every test uses a temporary isolated filesystem root (tmp_path) and a
temporary isolated SQLite database. No test performs real network I/O —
hypogeum.muapi._poll_until_complete is always monkeypatched.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.db as db
import hypogeum.muapi as muapi_mod
from hypogeum.logger import get_logger
from hypogeum.provider_receipt import (
    EventType,
    ProviderReceiptEvent,
    RecoveryState,
    WriteAheadReceiptStore,
)

EPISODE_ID = "ep_test_reconcile"
SCENE_ID = "s05"
IDEM_KEY = f"{EPISODE_ID}:{SCENE_ID}:primary"

log = get_logger(agent="test")


def _event(event_type: EventType, event_seq: int, evidence: dict = None, **overrides) -> ProviderReceiptEvent:
    kwargs = dict(
        envelope_id="env_1", envelope_version=1, authorization_id=IDEM_KEY,
        authorization_version=1, attempt_id="attempt_1", idempotency_key=IDEM_KEY,
        event_type=event_type, event_seq=event_seq, at="2026-01-01T00:00:00Z",
        provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
        provider_bound_content_sha256="a" * 64, max_cost_per_call=Decimal("0.39"),
        currency="USD", evidence=evidence or {},
    )
    kwargs.update(overrides)
    return ProviderReceiptEvent(**kwargs)


def _make_stuck_trail(output_dir: Path, *, provider_job_id="req_xyz") -> WriteAheadReceiptStore:
    """Writes exactly the event sequence a real timed-out submission
    leaves behind: prepared -> submission started -> provider acknowledged
    (with a real job id) -> unknown_acceptance (the poll timeout)."""
    store = WriteAheadReceiptStore(output_dir)
    store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EPISODE_ID)
    store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EPISODE_ID)
    if provider_job_id is not None:
        store.append_event(
            _event(EventType.PROVIDER_ACKNOWLEDGED, 2,
                   {"provider_job_id": provider_job_id, "provider_request_id": provider_job_id}),
            execution_id=EPISODE_ID,
        )
    store.append_event(
        _event(EventType.UNKNOWN_ACCEPTANCE, 3 if provider_job_id is not None else 2,
               {"detected_by": "test", "reason": "poll timeout"}),
        execution_id=EPISODE_ID,
    )
    return store


def _seed_reservation(db_path: Path, *, status="reserved"):
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO budget_reservations "
            "(reservation_id, scope_kind, scope_ref, idempotency_key, reserved_amount, "
            "currency, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"{IDEM_KEY}:call", "call", EPISODE_ID, IDEM_KEY, "0.39",
             "USD", status, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _reservation_status(db_path: Path) -> str:
    conn = db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM budget_reservations WHERE idempotency_key = ?", (IDEM_KEY,),
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db.initialize_schema(db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    return db_path


class TestNotApplicable:

    def test_no_receipt_trail_at_all(self, tmp_path, isolated_db):
        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )
        assert result == "not_applicable"

    def test_state_not_the_stuck_one(self, tmp_path, isolated_db):
        store = WriteAheadReceiptStore(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EPISODE_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EPISODE_ID)
        store.append_event(
            _event(EventType.PROVIDER_ACKNOWLEDGED, 2, {"provider_job_id": "req_1"}),
            execution_id=EPISODE_ID,
        )
        store.append_event(_event(EventType.DOWNLOADED, 3), execution_id=EPISODE_ID)

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )
        assert result == "not_applicable"


class TestReleasesWhenSafe:

    def test_no_provider_job_id_ever_recorded_releases(self, tmp_path, isolated_db):
        _make_stuck_trail(tmp_path, provider_job_id=None)
        _seed_reservation(isolated_db)

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert result == "released"
        assert _reservation_status(isolated_db) == "released"

    def test_provider_confirms_failed_releases(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_failed")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(muapi_mod, "_poll_until_complete", lambda *a, **k: (None, "job_status_failed"))

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert result == "released"
        assert _reservation_status(isolated_db) == "released"

    def test_provider_confirms_cancelled_releases(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_cancelled")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(muapi_mod, "_poll_until_complete", lambda *a, **k: (None, "job_status_cancelled"))

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert result == "released"
        assert _reservation_status(isolated_db) == "released"

    def test_records_a_reconciled_event(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_failed")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(muapi_mod, "_poll_until_complete", lambda *a, **k: (None, "job_status_failed"))

        muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        store = WriteAheadReceiptStore(tmp_path)
        events = store.read_events(EPISODE_ID, IDEM_KEY)
        assert events[-1].event_type == EventType.RECONCILED
        assert events[-1].evidence["resolution"] == "confirmed_not_submitted"

    def test_idempotent_second_call_still_released_no_error(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_failed")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(muapi_mod, "_poll_until_complete", lambda *a, **k: (None, "job_status_failed"))

        first = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )
        # Second call: state is now COMPLETED_TERMINAL (RECONCILED present),
        # not UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION any more -- correctly
        # a no-op, not a re-release or an error.
        second = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert first == "released"
        assert second == "not_applicable"


class TestLeavesUnresolvedWhenUnsafe:

    def test_still_pending_timeout_again_leaves_alone(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_pending")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(muapi_mod, "_poll_until_complete", lambda *a, **k: (None, "timeout"))

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert result == "still_unresolved"
        assert _reservation_status(isolated_db) == "reserved"

    def test_unexpected_http_400_leaves_alone(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_400")
        _seed_reservation(isolated_db)
        monkeypatch.setattr(
            muapi_mod, "_poll_until_complete",
            lambda *a, **k: (None, "http_400_persistent: bad request"),
        )

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )

        assert result == "still_unresolved"
        assert _reservation_status(isolated_db) == "reserved"


class TestNeverRaises:

    def test_poll_function_itself_raising_is_swallowed(self, tmp_path, isolated_db, monkeypatch):
        _make_stuck_trail(tmp_path, provider_job_id="req_x")
        _seed_reservation(isolated_db)

        def _boom(*a, **k):
            raise RuntimeError("simulated network chaos")

        monkeypatch.setattr(muapi_mod, "_poll_until_complete", _boom)

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )
        assert result == "still_unresolved"


class TestRecoversAJobThatCompletedAfterTheTimeout:
    """2026-10-10 (ep_014876fc): 4 MUAPI jobs finished after our poll gave up; the clips were paid for but
    the episode was lost. The clip is now pulled down and reused; money state is left alone."""

    def _patch(self, monkeypatch, costs):
        monkeypatch.setattr(
            muapi_mod, "_poll_until_complete",
            lambda *a, **k: ({"outputs": ["https://cdn/x.mp4"]}, "ok"),
        )

        def _dl(url, dest, log):
            dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * (muapi_mod._MIN_VIDEO_BYTES + 10))
            return True, ""

        monkeypatch.setattr(muapi_mod, "_download_file", _dl)
        monkeypatch.setattr(muapi_mod._finance, "record_cost", lambda **k: costs.append(k))

    def test_clip_is_downloaded_cost_recorded_once_and_reservation_untouched(self, tmp_path, isolated_db, monkeypatch):
        costs = []
        self._patch(monkeypatch, costs)
        _make_stuck_trail(tmp_path, provider_job_id="req_done")
        _seed_reservation(isolated_db)
        kw = dict(episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
                  output_dir=tmp_path, config=object(), log=log)

        assert muapi_mod.reconcile_stuck_scene_authorization(**kw) == "recovered_clip"
        assert muapi_mod.scene_file_path(tmp_path, SCENE_ID, None).exists()
        assert _reservation_status(isolated_db) == "reserved"      # never released or committed here
        # the repair loop reconciles on every attempt: a second call must not charge or download again
        assert muapi_mod.reconcile_stuck_scene_authorization(**kw) == "recovered_clip"
        assert len(costs) == 1 and costs[0]["call_type"] == "scene_generation"

    def test_a_failed_download_leaves_it_unresolved_and_charges_nothing(self, tmp_path, isolated_db, monkeypatch):
        costs = []
        self._patch(monkeypatch, costs)
        monkeypatch.setattr(muapi_mod, "_download_file", lambda *a, **k: (False, "net down"))
        _make_stuck_trail(tmp_path, provider_job_id="req_done")
        _seed_reservation(isolated_db)

        result = muapi_mod.reconcile_stuck_scene_authorization(
            episode_id=EPISODE_ID, scene_id=SCENE_ID, short_id=None, is_fallback=False,
            output_dir=tmp_path, config=object(), log=log,
        )
        assert result == "still_unresolved" and costs == []
        assert not muapi_mod.scene_file_path(tmp_path, SCENE_ID, None).exists()
