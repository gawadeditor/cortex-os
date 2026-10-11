"""
tests/test_muapi_weekly_gate_integration.py — Phase 3 integration: the
paid MUAPI video boundary (muapi.submit_scene with channel_id) enforces
the per-channel weekly budget BEFORE child authorization, grant-ledger
reservation, adapter invocation, and any network activity.

Proves, through the real gate path with fully mocked HTTP:
  - MUAPI_DRY_RUN short-circuits before envelope/authorization/lease/
    reservation: generation_status="generated" placeholder contract, ZERO
    requests, ZERO budget_reservations rows of ANY kind.
  - Missing weekly ceiling / exhausted remaining budget deny pre-network
    with zero side effects anywhere in the authorization machinery.
  - An accepted call commits its CHANNEL_WEEKLY reservation together with
    CALL/EPISODE scopes and persists immutable window evidence.
  - A provider rejection (failed-before-provider) RELEASES the weekly
    reservation; headroom is restored immediately.
  - Unknown acceptance keeps the weekly reservation held (conservative).
  - Multiple scenes of one episode accumulate against one channel-week;
    Shorts ride exactly the same scope as main-episode scenes.
  - Resume (run_visual_package_director_from_plan), repair
    (clip_repair_agent) and scripts/recover_scenes.py all forward an
    authoritative channel_id into this enforced boundary; recover_scenes
    refuses to run ungated at all.

All databases are tmp_path-isolated; HTTP is always patched out.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_muapi import make_config, make_scene_spec, _mock_submit_ok, _mock_poll_completed, _mock_poll_processing
from test_muapi_gate_path import (
    AT,
    _configure_channel_weekly,
    _persist_matching_finance_record,
    _persist_matching_plan,
    _register_grant,
)

import hypogeum.db as db
from hypogeum.muapi import _compute_asset_id, submit_scene

PRICE = Decimal("0.15")  # MODEL_CREDIT_COSTS["runway-text-to-video"] == plan unit price


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    import hypogeum.adapter_registry as adapter_registry
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture(autouse=True)
def _stub_capability_boundary_weekly_binding(monkeypatch):
    """capability_boundary._verify_post_plan_weekly_binding (36b4530) is a
    defense-in-depth check that resolves the CANONICAL episode dir
    independently of any tmp path: with no plan there it classifies every
    call PRE_PLAN and requires a pre-production hold this suite doesn't
    create. That check has its own coverage (test_capability_boundary.py /
    test_preplan_reservation.py); THIS suite is about the weekly-ledger
    reservation accounting _submit_scene_via_gate itself performs, so stub
    the boundary check the same way tests/test_muapi_gate_path.py does."""
    import hypogeum.capability_boundary as _cb
    monkeypatch.setattr(_cb, "_verify_post_plan_weekly_binding", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolated_channel_identity(monkeypatch):
    import hypogeum.channel_config as channel_config
    import hypogeum.workflow_executor as workflow_executor
    monkeypatch.setattr(
        channel_config, "compute_channel_package_identity",
        lambda channel_id, db_path=None: {"available": True, "checksum": "b" * 64, "version": "1.0"},
    )
    monkeypatch.setattr(
        workflow_executor, "resolve_workflow_binding",
        lambda channel_id, workflow_id, workflow_version, **kw: {
            "workflow_id": workflow_id, "workflow_version": workflow_version,
            "workflow_checksum": "a" * 64, "registry_definition_schema_version": "1.0",
            "registration_identity": "test-registration", "evaluation_timestamp": AT,
        },
    )


@pytest.fixture
def keypair(monkeypatch):
    import hypogeum.authorization_authority as authority
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


def _weekly_rows(db_path):
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM budget_reservations WHERE scope_kind = 'channel_weekly'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _count(db_path, table):
    conn = db.get_connection(db_path)
    try:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return 0  # table never created -> zero side effects
    finally:
        conn.close()


def _persist_custom_plan(tmp_path, config, *, assets, episode_id="ep_gate_test"):
    """Persist a verified plan carrying exactly `assets` (PlannedAsset list)
    plus its matching finance record; return the verified content_hash."""
    from dataclasses import replace as dc_replace
    from hypogeum.production_plan import (
        payload_content_hash, persist_production_plan,
    )
    from test_muapi_gate_path import _build_matching_plan
    plan = _build_matching_plan(model=config.MUAPI_T2_MODEL)
    plan = dc_replace(plan, episode_id=episode_id, assets=list(assets))
    payload = plan.to_dict()
    verified = dc_replace(plan, content_hash=payload_content_hash(payload))
    persist_production_plan(verified, tmp_path)
    _persist_matching_finance_record(tmp_path, content_hash=verified.content_hash)
    return verified.content_hash


def _asset_for(config, *, scene_id, prompt, short_id=None):
    """A single PlannedAsset whose fingerprint matches the FULLY ASSEMBLED
    prompt submit_scene will send."""
    from hypogeum.production_plan import (
        PLAN_SCHEMA_VERSION, ROLE_REQUIRED, PlannedAsset, prompt_fingerprint,
    )
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    assembled = assemble_video_scene_prompt(prompt)
    return PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=_compute_asset_id("ep_gate_test", scene_id, short_id), scene_id=scene_id,
        short_id=short_id, role=ROLE_REQUIRED, prompt=assembled,
        prompt_fingerprint=prompt_fingerprint(assembled), shot_signature="",
        aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model=config.MUAPI_T2_MODEL, unit_price_usd=0.15, enhancement_price_usd=0.05,
        max_chargeable_attempts=2,
    )


def _ffmpeg_placeholder(cmd, capture_output, text, timeout):
    out_path = Path(cmd[-1])
    if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKEMP4" * 2000)
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# Dry run: zero network, zero weekly reservations
# ---------------------------------------------------------------------------


class TestDryRunGatePath:
    def test_dry_run_makes_zero_network_calls_and_consumes_zero_budget(
        self, keypair, tmp_path, monkeypatch,
    ):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        config.MUAPI_DRY_RUN = True
        spec = make_scene_spec(scene_id="s01b01")
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.subprocess.run", side_effect=_ffmpeg_placeholder):
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post.called is False
            assert mock_get.called is False

        # Same structural downstream contract as a real success:
        assert result["generation_status"] == "generated"
        assert result["failure_reason"] is None
        assert result["request_id"].startswith("dry-run-")
        clip = tmp_path / "video" / "sc_s01b01.mp4"
        assert clip.exists() and clip.stat().st_size > 0

        # And ZERO ledger/authorization side effects of any kind:
        assert _weekly_rows(db_path) == []
        assert _count(db_path, "budget_reservations") == 0
        assert _count(db_path, "authorization_records") == 0
        assert _count(db_path, "grant_ledger_reservations") == 0

    def test_dry_run_does_not_reach_the_submission_gate(self, keypair, tmp_path, monkeypatch):
        """AST-level guarantee by construction: patch submission_gate.submit
        and prove the dry-run path returns without ever calling it."""
        import hypogeum.submission_gate as submission_gate
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        config.MUAPI_DRY_RUN = True
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch.object(submission_gate, "submit") as mock_gate, \
             patch("hypogeum.muapi.requests.post"), \
             patch("hypogeum.muapi.requests.get"), \
             patch("hypogeum.muapi.subprocess.run", side_effect=_ffmpeg_placeholder):
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")

        mock_gate.assert_not_called()
        assert result["generation_status"] == "generated"


# ---------------------------------------------------------------------------
# Fail-closed denials before any network or authorization side effect
# ---------------------------------------------------------------------------


class TestWeeklyDenialPreNetwork:
    def test_missing_ceiling_denies_pre_network(self, keypair, tmp_path, monkeypatch):
        """Deployment-safety proof: a channel whose operations profile has NO
        weekly_video_budget_usd can never spend -- even with a valid grant,
        a verified plan, and a matching finance record."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=None)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post.called is False
            assert mock_get.called is False

        assert result["generation_status"] == "generation_failed"
        assert "weekly_budget_denied" in result["failure_reason"]
        assert "weekly_video_budget_usd" in result["failure_reason"]
        assert _count(db_path, "budget_reservations") == 0
        assert _count(db_path, "authorization_records") == 0

    def test_exhausted_remaining_budget_denies_pre_network(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        # Ceiling below one call's plan-priced amount:
        _configure_channel_weekly(db_path, weekly_video_budget_usd=0.10)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post.called is False
            assert mock_get.called is False

        assert result["generation_status"] == "generation_failed"
        assert "weekly_budget_exhausted" in result["failure_reason"]
        assert _weekly_rows(db_path) == []
        assert _count(db_path, "authorization_records") == 0


# ---------------------------------------------------------------------------
# Accepted / rejected / unknown outcomes vs the weekly row
# ---------------------------------------------------------------------------


# NOTE: the weekly reservation is PLAN-SCOPED since the Phase 3 correction:
# ONE reservation of ProductionPlan.maximum_bounded_cost_usd per verified
# episode plan per channel-week, keyed
# "plan-weekly:<channel>:<episode>:<plan_content_hash>". The fixture plan's
# maximum_bounded_cost_usd is 0.90 (see _build_matching_plan in
# test_muapi_gate_path.py).
PLAN_MAX = Decimal("0.90")


class TestOutcomeAccounting:
    def _setup(self, keypair, tmp_path, monkeypatch, *, ceiling=5.00):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config(max_poll_attempts=4)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        if ceiling is not None:
            _configure_channel_weekly(db_path, weekly_video_budget_usd=ceiling)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)
        return config, db_path

    def test_accepted_call_commits_plan_maximum_with_window_evidence(self, keypair, tmp_path, monkeypatch):
        config, db_path = self._setup(keypair, tmp_path, monkeypatch)
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_wk_acc")
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generated", result
        rows = _weekly_rows(db_path)
        # ONE reservation for the whole verified plan -- not per scene/tier:
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "committed"
        assert Decimal(row["reserved_amount"]) == PLAN_MAX  # the verified plan maximum
        assert row["idempotency_key"].startswith("plan-weekly:test_channel:ep_gate_test:")
        assert row["channel_id"] == "test_channel"
        # Window is the CURRENT ISO week (the gate resolves it via
        # datetime.now()) — computed, not a fixed calendar anchor.
        import datetime as _dt
        from hypogeum.weekly_budget import compute_weekly_window, week_scope_ref
        _win = compute_weekly_window(_dt.datetime.now(_dt.timezone.utc), "UTC")
        assert row["scope_ref"] == week_scope_ref("test_channel", _win)
        assert row["weekly_timezone_snapshot"] == "UTC"
        assert row["weekly_window_start_utc"] == _win.start_utc.isoformat()
        assert row["weekly_window_end_utc"] == _win.end_utc.isoformat()
        assert Decimal(row["weekly_ceiling_snapshot"]) == Decimal("5.00")
        # Per-call CALL/EPISODE scopes remain scene-level alongside the
        # single plan-level weekly row:
        conn = db.get_connection(db_path)
        try:
            kinds = {}
            for r in conn.execute(
                "SELECT scope_kind, COUNT(*) c FROM budget_reservations GROUP BY scope_kind"
            ).fetchall():
                kinds[r["scope_kind"]] = r["c"]
        finally:
            conn.close()
        assert kinds == {"call": 1, "episode": 1, "channel_weekly": 1}

    def test_provider_rejection_keeps_full_plan_reservation_held(self, keypair, tmp_path, monkeypatch):
        """Conservative locked policy: once the provider boundary was entered,
        the plan-level hold persists even on a definitive rejection -- only a
        PRE-provider failure of the creating attempt may release."""
        config, db_path = self._setup(keypair, tmp_path, monkeypatch)
        rejected = MagicMock()
        rejected.status_code = 400  # attempt-hard-fail -> clean non-raising rejection
        rejected.json.return_value = {}
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = rejected
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generation_failed"
        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"  # HELD, full plan maximum
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX
        # The per-scene CALL scope WAS released by the gate (its own policy):
        conn = db.get_connection(db_path)
        try:
            call_rows = [dict(r) for r in conn.execute(
                "SELECT status FROM budget_reservations WHERE scope_kind='call'").fetchall()]
        finally:
            conn.close()
        assert call_rows[0]["status"] == "released"

    def test_creator_pre_provider_failure_releases_exactly_its_own_row(self, keypair, tmp_path, monkeypatch):
        """Output-collision denial happens inside the gate BEFORE the adapter
        is invoked -> a creating attempt releases its own weekly row."""
        config, db_path = self._setup(keypair, tmp_path, monkeypatch)
        dest = tmp_path / "video" / "sc_s01b01.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 100)  # occupied, but below resume-safety size threshold

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post.called is False
            assert mock_get.called is False

        assert result["generation_status"] == "generation_failed"
        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "released"  # creator's own pre-provider release
        conn = db.get_connection(db_path)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM budget_reservations "
                "WHERE status IN ('reserved','committed')").fetchone()[0]
        finally:
            conn.close()
        assert active == 0  # headroom fully restored

    def test_unknown_acceptance_keeps_full_plan_reservation_held(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config(poll_interval=0, max_poll_attempts=2)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("req_lost_response")
            mock_get.return_value = _mock_poll_processing()  # never terminal
            result = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generation_failed"
        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"  # deliberately NOT released
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

    def test_retry_after_unknown_cannot_release_the_hold(self, keypair, tmp_path, monkeypatch):
        """THE ownership regression the correction closes: a retrying attempt
        replays the existing hold (created=False) and must leave it exactly
        as it found it -- held, single, uncommitted."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config(poll_interval=0, max_poll_attempts=2)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("req_lost_a")
            mock_get.return_value = _mock_poll_processing()
            first = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert first["generation_status"] == "generation_failed"

        # Retry the same scene (clip absent): denied safely, never releasing.
        with patch("hypogeum.muapi.requests.post") as mock_post2, \
             patch("hypogeum.muapi.requests.get") as mock_get2:
            second = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            # no paid submission. A read-only status poll of the already-recorded provider job is
            # allowed (2026-10-11): the retry first asks the provider what became of the first
            # attempt (the clip may have finished there, already paid) before deciding anything.
            assert mock_post2.called is False
        assert second["generation_status"] == "generation_failed"

        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

    def test_held_unknown_of_one_plan_denies_a_different_episode(self, keypair, tmp_path, monkeypatch):
        """Episode A's unknown hold consumes its plan maximum from the shared
        channel-week; episode B's plan maximum no longer fits and is denied
        BEFORE child authorization/network."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config(poll_interval=0, max_poll_attempts=2)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        max_calls=10, max_total_spend=Decimal("20.00"))
        _configure_channel_weekly(db_path, weekly_video_budget_usd=1.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        # Episode A: acceptance lost -> 0.90 held of the 1.00 ceiling.
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("req_lost_a")
            mock_get.return_value = _mock_poll_processing()
            res_a = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert res_a["generation_status"] == "generation_failed"

        # Episode B: separate dir/plan/grant; its 0.90 cannot fit in 0.10.
        dir_b = tmp_path / "ep_b"
        _persist_matching_plan(dir_b, model=config.MUAPI_T2_MODEL, episode_id="ep_gate_test_b",
                               scene_id="b01b01")
        from hypogeum.production_plan import load_production_plan as _load_plan
        plan_b_hash = _load_plan("ep_gate_test_b", dir_b).content_hash
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        grant_id="g_ep_gate_test_b",
                        episode_id="ep_gate_test_b",
                        production_plan_content_hash=plan_b_hash)
        with patch("hypogeum.muapi.requests.post") as mock_post_b, \
             patch("hypogeum.muapi.requests.get") as mock_get_b:
            res_b = submit_scene(
                make_scene_spec(scene_id="b01b01"), config,
                "ep_gate_test_b", dir_b, channel_id="test_channel")
            assert mock_post_b.called is False
            assert mock_get_b.called is False
        assert "weekly_budget_exhausted" in res_b["failure_reason"]

        rows = _weekly_rows(db_path)
        assert len(rows) == 1  # B was denied before any reservation
        assert rows[0]["idempotency_key"].startswith("plan-weekly:test_channel:ep_gate_test:")


# ---------------------------------------------------------------------------
# One verified plan == ONE weekly reservation: scenes, Shorts, attempts,
# retries, resume, and repairs all replay it. A different episode or a
# different verified plan hash gets its own row.
# ---------------------------------------------------------------------------

PLAN_MAX = Decimal("0.90")  # fixture plan maximum_bounded_cost_usd


def _persist_plan_with_assets(tmp_path, config, *, assets, episode_id="ep_gate_test",
                              maximum_bounded_cost_usd=None):
    """Persist a verified plan carrying exactly `assets` plus the matching
    finance record; returns the verified content_hash."""
    from dataclasses import replace as dc_replace
    from hypogeum.production_plan import (
        payload_content_hash, persist_production_plan,
    )
    from test_muapi_gate_path import _build_matching_plan
    plan = _build_matching_plan(model=config.MUAPI_T2_MODEL)
    replaces = dict(episode_id=episode_id, assets=list(assets))
    if maximum_bounded_cost_usd is not None:
        replaces["maximum_bounded_cost_usd"] = maximum_bounded_cost_usd
    plan = dc_replace(plan, **replaces)
    payload = plan.to_dict()
    verified = dc_replace(plan, content_hash=payload_content_hash(payload))
    persist_production_plan(verified, tmp_path)
    _persist_matching_finance_record(tmp_path, episode_id=episode_id,
                                     content_hash=verified.content_hash)
    return verified.content_hash


def _asset_for(config, *, scene_id, prompt, short_id=None):
    """A single PlannedAsset whose fingerprint matches the FULLY ASSEMBLED
    prompt submit_scene will send."""
    from hypogeum.production_plan import (
        ROLE_REQUIRED, PlannedAsset, prompt_fingerprint,
    )
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    assembled = assemble_video_scene_prompt(prompt)
    return PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=_compute_asset_id("ep_gate_test", scene_id, short_id), scene_id=scene_id,
        short_id=short_id, role=ROLE_REQUIRED, prompt=assembled,
        prompt_fingerprint=prompt_fingerprint(assembled), shot_signature="",
        aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model=config.MUAPI_T2_MODEL, unit_price_usd=0.15, enhancement_price_usd=0.05,
        max_chargeable_attempts=2,
    )


def _run_scene_ok(config, tmp_path, scene_id, prompt, *, request_tag):
    with patch("hypogeum.muapi.requests.post") as mock_post, \
         patch("hypogeum.muapi.requests.get") as mock_get, \
         patch("hypogeum.muapi.time.sleep"), \
         patch("hypogeum.muapi._download_file", return_value=(True, "")), \
         patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
        mock_post.return_value = _mock_submit_ok(request_tag)
        mock_get.return_value = _mock_poll_completed()
        res = submit_scene(
            make_scene_spec(scene_id=scene_id, primary_prompt=prompt),
            config, "ep_gate_test", tmp_path, channel_id="test_channel",
        )
    assert res["generation_status"] == "generated", res


class TestOneReservationPerVerifiedPlan:
    def test_ten_scenes_of_one_plan_consume_one_reservation(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        prompts = {
            f"s{i:02d}b01": f"Scene {i}: the mechanism gears under lamplight."
            for i in range(1, 11)
        }
        assets = [_asset_for(config, scene_id=sid, prompt=p) for sid, p in prompts.items()]
        plan_hash = _persist_plan_with_assets(tmp_path, config, assets=assets)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        max_calls=25, production_plan_content_hash=plan_hash)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        for i, (sid, p) in enumerate(prompts.items()):
            _run_scene_ok(config, tmp_path, sid, p, request_tag=f"req_ten_{i}")

        rows = _weekly_rows(db_path)
        # TEN provider submissions -- ONE plan-level weekly reservation:
        assert len(rows) == 1
        assert rows[0]["status"] == "committed"
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX
        assert rows[0]["idempotency_key"] == f"plan-weekly:test_channel:ep_gate_test:{plan_hash}"
        # ...while per-call CALL scopes remain ten separate reservations:
        conn = db.get_connection(db_path)
        try:
            calls = conn.execute(
                "SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='call'").fetchone()[0]
        finally:
            conn.close()
        assert calls == 10

    def test_rejected_scene_then_sibling_scene_share_one_reservation(self, keypair, tmp_path, monkeypatch):
        """Provider REJECTED scene A: its weekly hold persists (post-boundary),
        its own identity is never auto-retryable (Phase 1), and a sibling
        scene B of the SAME verified plan replays the one hold and commits
        nothing new."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        prompt_a = "Ancient Greek ruins at dawn, wide establishing shot."
        prompt_b = "Ancient Greek ruins at dusk, torches burning."
        assets = [
            _asset_for(config, scene_id="s01b01", prompt=prompt_a),
            _asset_for(config, scene_id="s01b02", prompt=prompt_b),
        ]
        _persist_plan_with_assets(tmp_path, config, assets=assets)
        from hypogeum.production_plan import load_production_plan as _load
        plan_hash = _load("ep_gate_test", tmp_path).content_hash
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        production_plan_content_hash=plan_hash)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        # Scene A: clean provider rejection.
        hard_fail = MagicMock()
        hard_fail.status_code = 400
        hard_fail.json.return_value = {}
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get"), \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = hard_fail
            first = submit_scene(
                make_scene_spec(scene_id="s01b01", primary_prompt=prompt_a),
                config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert first["generation_status"] == "generation_failed"

        rows = _weekly_rows(db_path)
        assert len(rows) == 1 and rows[0]["status"] == "reserved"
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

        # Identical retry of the REJECTED identity: structurally denied
        # before any network call, and it must not release or duplicate
        # the plan-level hold.
        with patch("hypogeum.muapi.requests.post") as mock_retry_post, \
             patch("hypogeum.muapi.requests.get") as mock_retry_get:
            retry = submit_scene(
                make_scene_spec(scene_id="s01b01", primary_prompt=prompt_a),
                config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_retry_post.called is False
            assert mock_retry_get.called is False
        assert retry["generation_status"] == "generation_failed"

        # Sibling scene B of the SAME verified plan: accepted, still ONE row.
        _run_scene_ok(config, tmp_path, "s01b02", prompt_b, request_tag="req_sibling")

        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "committed"  # B's acceptance committed THE row
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

    def test_main_and_shorts_in_one_plan_consume_one_reservation(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        main_prompt = "Ancient Greek ruins at dawn, wide establishing shot."
        short_prompt = "Vertical cut of the mechanism gear train rotating slowly."
        assets = [
            _asset_for(config, scene_id="s01b01", prompt=main_prompt),
            _asset_for(config, scene_id="sh01b01", prompt=short_prompt, short_id="sh_01"),
        ]
        plan_hash = _persist_plan_with_assets(tmp_path, config, assets=assets)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        production_plan_content_hash=plan_hash)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        _run_scene_ok(config, tmp_path, "s01b01", main_prompt, request_tag="req_main")
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_short")
            mock_get.return_value = _mock_poll_completed()
            res = submit_scene(
                make_scene_spec(scene_id="sh01b01", short_id="sh_01", aspect_ratio="9:16",
                                primary_prompt=short_prompt),
                config, "ep_gate_test", tmp_path, channel_id="test_channel",
            )
        assert res["generation_status"] == "generated", res

        rows = _weekly_rows(db_path)
        assert len(rows) == 1  # Shorts ride the SAME plan-level reservation
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

    def test_resume_replay_consumes_no_additional_reservation(self, keypair, tmp_path, monkeypatch):
        """A retry/resume of the same verified plan reuses the hold: no new
        row, no release of the committed one."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_first")
            mock_get.return_value = _mock_poll_completed()
            first = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert first["generation_status"] == "generated"

        # (the mocked download never wrote a real clip, so the retry runs
        # the full gate path again -- exactly a resume-without-artifact)

        with patch("hypogeum.muapi.requests.post") as mock_post2, \
             patch("hypogeum.muapi.requests.get") as mock_get2:
            second = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post2.called is False
            assert mock_get2.called is False
        assert second["generation_status"] == "generation_failed"

        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "committed"  # untouched by the replaying attempt
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX

    def test_different_episodes_accumulate_distinct_reservations(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        # Episode B reaches full generation, so its output_dir must sit under
        # the conftest-redirected capability-boundary receipt root
        # (_DEFAULT_ASSETS_ROOT = tmp_path) as <root>/episodes/<episode_id>,
        # or the gate's ABOUT_TO_SUBMIT receipt lands where the boundary
        # check never looks ("never entered the gate").
        dir_b = tmp_path / "episodes" / "ep_gate_test_b"
        _persist_matching_plan(dir_b, model=config.MUAPI_T2_MODEL, episode_id="ep_gate_test_b",
                               scene_id="b01b01")
        from hypogeum.production_plan import load_production_plan as _load
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        grant_id="g_ep_b", episode_id="ep_gate_test_b",
                        production_plan_content_hash=_load("ep_gate_test_b", dir_b).content_hash)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=2.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        _run_scene_ok(config, tmp_path, "s01b01",
                      make_scene_spec()["production_spec"]["primary_prompt"],
                      request_tag="req_ep_a")
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_ep_b")
            mock_get.return_value = _mock_poll_completed()
            res = submit_scene(make_scene_spec(scene_id="b01b01"), config,
                               "ep_gate_test_b", dir_b, channel_id="test_channel")
        assert res["generation_status"] == "generated", res

        rows = _weekly_rows(db_path)
        assert len(rows) == 2  # two episodes -> two plan-level reservations
        assert all(Decimal(r["reserved_amount"]) == PLAN_MAX for r in rows)
        keys = sorted(r["idempotency_key"] for r in rows)
        assert keys[0].startswith("plan-weekly:test_channel:ep_gate_test:")
        assert keys[1].startswith("plan-weekly:test_channel:ep_gate_test_b:")

    def test_different_verified_plan_hash_gets_a_distinct_reservation(self, keypair, tmp_path, monkeypatch):
        """Same episode, replaced self-consistent plan (new content_hash,
        new grant): the weekly identity binds the plan hash, so the old
        reservation cannot be silently reused."""
        from dataclasses import replace as dc_replace
        from hypogeum.production_plan import load_production_plan, payload_content_hash, persist_production_plan
        from test_muapi_gate_path import _build_matching_plan
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()

        v1_hash = _build_matching_plan(model=config.MUAPI_T2_MODEL).content_hash
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        production_plan_content_hash=v1_hash)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=5.00)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_v1")
            mock_get.return_value = _mock_poll_completed()
            first = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert first["generation_status"] == "generated"

        old_plan = load_production_plan("ep_gate_test", tmp_path)
        v2 = dc_replace(old_plan, enhancement_cost_maximum_usd=0.07,
                        maximum_bounded_cost_usd=0.80)
        v2_payload = v2.to_dict()
        v2 = dc_replace(v2, content_hash=payload_content_hash(v2_payload))
        (tmp_path / "production_plan_ep_gate_test.json").unlink()
        persist_production_plan(v2, tmp_path)
        _persist_matching_finance_record(tmp_path, content_hash=v2.content_hash)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL,
                        grant_id="g_v2_hash",
                        production_plan_content_hash=v2.content_hash)

        with patch("hypogeum.muapi.requests.post") as mock_post2, \
             patch("hypogeum.muapi.requests.get") as mock_get2:
            second = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post2.called is False
            assert mock_get2.called is False

        rows = _weekly_rows(db_path)
        assert len(rows) == 2  # v1's hold + v2's own distinct reservation
        amounts = sorted(Decimal(r["reserved_amount"]) for r in rows)
        assert amounts == [Decimal("0.80"), Decimal("0.90")]
        statuses = sorted(r["status"] for r in rows)
        assert statuses == ["committed", "released"]  # v2's creator failed pre-provider
        hashes = {r["idempotency_key"].rsplit(":", 1)[1] for r in rows}
        assert hashes == {old_plan.content_hash, v2.content_hash}

    def test_exact_ceiling_equals_plan_maximum_is_allowed(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        config = make_config()
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        _configure_channel_weekly(db_path, weekly_video_budget_usd=0.90)  # EXACTLY the plan max
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_exact")
            mock_get.return_value = _mock_poll_completed()
            res = submit_scene(make_scene_spec(), config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert res["generation_status"] == "generated"
        rows = _weekly_rows(db_path)
        assert len(rows) == 1
        assert Decimal(rows[0]["reserved_amount"]) == PLAN_MAX == Decimal("0.90")


# ---------------------------------------------------------------------------
# Every live entry path forwards an authoritative channel_id
# ---------------------------------------------------------------------------


class TestEntryPathsForwardChannelId:
    def test_vpd_resume_forwards_channel_id(self, keypair, tmp_path, monkeypatch):
        from test_visual_package_director import (
            _make_visual_package as _tvp_package,
            _make_submit_result as _tvp_result,
            make_config as _tvp_make_config,
        )
        from hypogeum.agents.visual_package_director import run_visual_package_director_from_plan
        from test_visual_package_director import _persist_minimal_plan

        cfg = _tvp_make_config()
        plan = _tvp_package()
        _persist_minimal_plan(tmp_path)
        captured = {}
        def _capture(*args, **kwargs):
            captured["positional"] = list(args)
            captured.update(kwargs)
            return _tvp_result()
        with patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=None), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   side_effect=_capture):
            run_visual_package_director_from_plan(plan, cfg, "ep_001", tmp_path, channel_id="ch_resume")

        assert captured.get("channel_id") == "ch_resume"
        # submit_scene(scene_spec, config, episode_id, output_dir, ...):
        assert captured["positional"][2] == "ep_001"

    def test_clip_repair_forwards_channel_id(self, keypair, tmp_path):
        from test_clip_repair_agent import _make_canonical_scene, _make_visual_package as _repair_pkg
        from hypogeum.agents.clip_repair_agent import run_clip_repair

        cfg = make_config()
        scene = _make_canonical_scene("sc_002", asset_id="asset_sc002")
        vp = _repair_pkg([scene])
        captured = {}

        def _capture_submit(*args, **kwargs):
            captured.update(kwargs)
            spec = kwargs.get("scene_spec") or (args[0] if args else {})
            return {
                "scene_id": spec.get("scene_id"),
                "asset_id": "asset_sc002", "generation_status": "generated",
                "provider": "muapi", "tier_used": 2, "model_used": cfg.MUAPI_T2_MODEL,
                "prompt_used": None, "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_capture_submit), \
             patch("hypogeum.muapi.scene_file_path") as fake_path:
            fake_path.side_effect = lambda output_dir, scene_id, short_id=None: (
                Path(output_dir) / "video" / f"sc_{scene_id}.mp4"
            )
            run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=[],
                assets_root=tmp_path,
                config=cfg,
                episode_id="ep_rep_01",
                channel_id="ch_repair",
            )

        assert captured.get("channel_id") == "ch_repair"

# ---------------------------------------------------------------------------
# recover_scenes.py: no ungated paid fallback anymore
# ---------------------------------------------------------------------------


class TestRecoverScenesGate:
    def _script_module(self):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "scripts" / "recover_scenes.py"
        spec = importlib.util.spec_from_file_location("recover_scenes_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_channel_id_flag_is_rejected_by_argparse(self, monkeypatch, capsys):
        module = self._script_module()
        monkeypatch.setattr(sys, "argv", ["recover_scenes.py", "--episode-id", "ep_x", "--scenes", "s02"])
        with pytest.raises(SystemExit) as excinfo:
            module.main()
        assert excinfo.value.code == 2
        assert "--channel-id" in capsys.readouterr().err

    def test_blank_channel_id_fails_closed_before_any_runner_call(self, monkeypatch):
        module = self._script_module()
        monkeypatch.setattr(sys, "argv", [
            "recover_scenes.py", "--channel-id", "   ",
            "--episode-id", "ep_x", "--scenes", "s02",
        ])
        with patch.object(module, "run_visual_package_director_from_plan") as runner:
            rc = module.main()
        assert rc == 1
        runner.assert_not_called()

    def test_authorized_recovery_forwards_channel_id_into_enforced_boundary(self, monkeypatch, tmp_path):
        module = self._script_module()
        monkeypatch.setattr(sys, "argv", [
            "recover_scenes.py", "--channel-id", "ch_recovery",
            "--episode-id", "ep_rec_01", "--scenes", "s02",
        ])
        visual_package = {"scenes": [{"scene_id": "s02"}]}
        captured = {}
        result_payload = {
            "asset_manifest": {"assets": [
                {"scene_id": "s02", "generation_status": "generated", "asset_id": "aid"}
            ]},
        }
        monkeypatch.setattr(module, "_load_visual_package", lambda ep, root: visual_package)
        with patch.object(module, "run_visual_package_director_from_plan",
                          side_effect=lambda **kw: (captured.update(kw), result_payload)[1]):
            rc = module.main()

        assert rc == 0
        assert captured.get("channel_id") == "ch_recovery"
        assert captured.get("episode_id") == "ep_rec_01"
