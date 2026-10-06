"""Tests for hypogeum.episode_repair — the bounded, operator-authorized path
that repairs a finished episode the production output gate rejects for
under-resolution source clips (2026-10-05 ep_31d1891b)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypogeum import episode_repair as er
from hypogeum.agents.production_output_gate import GateCheckResult, ProductionOutputGateResult
from hypogeum.db import initialize_schema


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


def _gate(*checks):
    return ProductionOutputGateResult(passed=False, episode_id="ep1", evaluated_at="x", checks=list(checks))


def _video(*clips):
    return GateCheckResult("A_VIDEO", "fail", "below resolution", evidence={"offending_clips": [
        {"scene_id": sid, "measured_resolution": m, "required_resolution": "1080x1920"} for sid, m in clips]})


_COMPAT = GateCheckResult("F_ARTIFACT_COMPATIBILITY", "fail", "x",
                          evidence={"problems": ["1 incompatible source clip(s): ['s03']"]})


def test_under_resolution_portrait_clip_is_repairable():
    assert er.repairable_scenes(_gate(_video(("s03", "720x1280")), _COMPAT)) == ["s03"]


@pytest.mark.parametrize("gate", [
    _gate(_video(("s03", "1280x720"))),                                     # wrong orientation: regenerate, not upscale
    _gate(_video(("s03", "0x0"))),                                          # unreadable clip
    _gate(_video(("s03", "720x1280")), GateCheckResult("C_THUMBNAIL", "fail", "x")),  # anything else failing
    _gate(GateCheckResult("F_ARTIFACT_COMPATIBILITY", "fail", "x",
                          evidence={"problems": ["thumbnail_package version None != '2.0'"]})),
])
def test_other_gate_failures_are_not_repairable(gate):
    assert er.repairable_scenes(gate) is None


def test_repair_window_open_only_while_running_and_recent(db):
    assert er.repair_window_open("ch", "ep1", db_path=db) is False
    rid = er._open("ch", "ep1", ["s03"], 1.0, db)
    assert er.repair_window_open("ch", "ep1", db_path=db) is True
    assert er.repair_window_open("ch", "ep2", db_path=db) is False          # this episode only
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert er.repair_window_open("ch", "ep1", db_path=db, now=later) is False  # a crashed repair expires
    er._close(rid, "failed", "x", db)
    assert er.repair_window_open("ch", "ep1", db_path=db) is False


def test_grant_routes_through_operating_grant_only_inside_a_repair_window(monkeypatch):
    import hypogeum.authorization_authority as authority
    from hypogeum import muapi

    monkeypatch.setattr(authority, "resolve_active_operating_grant", lambda ch, at: "OPERATING")
    monkeypatch.setattr(authority, "resolve_active_grant", lambda ch, ep, at: None)
    monkeypatch.setattr("hypogeum.runtime_admission.get_admission_binding", lambda ch, ep: None)

    monkeypatch.setattr(er, "repair_window_open", lambda ch, ep: False)
    assert muapi._resolve_authorization_grant("ch", "ep1", at="t") == (None, False)
    monkeypatch.setattr(er, "repair_window_open", lambda ch, ep: True)
    assert muapi._resolve_authorization_grant("ch", "ep1", at="t") == ("OPERATING", True)


def _no_spend(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no paid / render work may start")
    monkeypatch.setattr("hypogeum.source_enhancement.run_source_enhancement", boom)
    monkeypatch.setattr(er, "_reassemble", boom)


def test_cap_attempts_and_scope_stop_before_any_spend(db, tmp_path, monkeypatch):
    _no_spend(monkeypatch)
    sent = []
    monkeypatch.setattr(er, "_notify", lambda *a, **k: sent.append(a))
    ok_gate = _gate(_video(("s03", "720x1280")), _COMPAT)

    assert er.try_repair_blocked_episode("ch", "ep1", _gate(GateCheckResult("C_THUMBNAIL", "fail", "x")),
                                         assets_root=tmp_path, db_path=db) == "not_repairable"

    monkeypatch.setenv("EPISODE_REPAIR_MAX_USD", "0.15")                    # 2 clips x $0.10 > $0.15
    two = _gate(_video(("s03", "720x1280"), ("s04", "720x1280")))
    assert er.try_repair_blocked_episode("ch", "ep1", two, assets_root=tmp_path, db_path=db) == "over_cap"

    monkeypatch.setenv("EPISODE_REPAIR_ENABLED", "false")
    assert er.try_repair_blocked_episode("ch", "ep1", ok_gate, assets_root=tmp_path, db_path=db) == "disabled"
    monkeypatch.delenv("EPISODE_REPAIR_ENABLED")

    for _ in range(er._MAX_REPAIRS_PER_EPISODE):
        er._close(er._open("ch", "ep1", ["s03"], 1.0, db), "failed", "x", db)
    assert er.try_repair_blocked_episode("ch", "ep1", ok_gate, assets_root=tmp_path, db_path=db) == "limit_reached"
    assert sent == []                                                       # nothing attempted, nothing announced


def test_failed_repair_closes_its_window_and_tells_the_operator(db, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(er, "_notify", lambda key, ch, ar, en, dbp: sent.append(ar))
    monkeypatch.setattr(er, "build_channel_config", lambda ch, dbp: object())
    # no visual_package on disk -> the repair fails before any provider call
    got = er.try_repair_blocked_episode("ch", "ep1", _gate(_video(("s03", "720x1280")), _COMPAT),
                                        assets_root=tmp_path, db_path=db)
    assert got == "failed"
    assert er.repair_window_open("ch", "ep1", db_path=db) is False
    assert len(sent) == 1 and "ومنفعش" in sent[0]


def test_stranded_record_without_real_candidate_is_cleared(tmp_path):
    empty = tmp_path / "sc_s03.enhanced.mp4"
    empty.write_bytes(b"")
    real = tmp_path / "sc_s04.enhanced.mp4"
    real.write_bytes(b"real bytes")
    vp = {"asset_manifest": {"assets": [
        {"scene_id": "s03", "source_enhancement": {"validation_result": "failed", "archived_path": str(empty)}},
        {"scene_id": "s04", "source_enhancement": {"validation_result": "failed", "archived_path": str(real)}},
        {"scene_id": "s05", "source_enhancement": {"validation_result": "failed", "archived_path": None}},
    ]}}
    er._clear_stranded_records(vp, ["s03", "s04"])
    assets = {a["scene_id"]: a for a in vp["asset_manifest"]["assets"]}
    assert "source_enhancement" not in assets["s03"] and not empty.exists()
    assert "source_enhancement" in assets["s04"] and real.exists()          # real candidate: stage's own rules
    assert "source_enhancement" in assets["s05"]                            # not part of this repair


def test_upscale_key_moves_to_recovery_after_a_stranded_attempt(monkeypatch):
    from hypogeum.provider_receipt import RecoveryState, WriteAheadReceiptStore
    from hypogeum.providers import muapi_video_upscaler as up

    stranded = {"ep1:upscale:s03"}
    monkeypatch.setattr(WriteAheadReceiptStore, "classify_recovery_state",
                        lambda self, ex, key: RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION
                        if key in stranded else RecoveryState.AUTHORIZED_NO_ATTEMPT)
    assert up._next_upscale_key_suffix("ep1", "s04") == ""
    assert up._next_upscale_key_suffix("ep1", "s03") == ":recovery1"
    stranded |= {"ep1:upscale:s03:recovery1", "ep1:upscale:s03:recovery2"}
    assert up._next_upscale_key_suffix("ep1", "s03") is None                # bounded
