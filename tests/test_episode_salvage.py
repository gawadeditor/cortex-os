"""
tests/test_episode_salvage.py — salvage_preconditions() gate.

The one that matters: a salvage MUST have a loadable visual_package with a
non-empty asset manifest on disk, or run_pipeline() re-runs VPD Phase 1
and drifts the plan hash into "a Production Plan already exists which
differs" — the exact ep_367a0d78 four-times loop. Everything else here is
the ordinary fail-closed precondition surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hypogeum.db as db
from hypogeum.episode_salvage import salvage_preconditions


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "salvage.db"
    db.initialize_schema(p)
    conn = db.get_connection(p)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_admissions (
            admission_id TEXT PRIMARY KEY, channel_id TEXT, episode_id TEXT,
            status TEXT, admitted_at TEXT)"""
    )
    conn.commit()
    return p


def _seed(dbp, ep, *, run_status="failed", adm_status="failed", channel="hypogeum"):
    conn = db.get_connection(dbp)
    conn.execute(
        "INSERT INTO episodes (episode_id, title, topic, created_at, status) VALUES (?,?,?,?,?)",
        (ep, "T", "The Antikythera Mechanism", "2026-09-06T00:00:00+00:00", "in_progress"),
    )
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
        (f"run_{ep}", ep, "2026-09-06T01:00:00+00:00", run_status, channel),
    )
    if adm_status is not None:
        conn.execute(
            "INSERT INTO execution_admissions (admission_id, channel_id, episode_id, status, admitted_at) "
            "VALUES (?,?,?,?,?)",
            (f"admit:{channel}:{ep}", channel, ep, adm_status, "2026-09-06T01:00:00+00:00"),
        )
    conn.commit()


def _files(root: Path, ep, *, plan=True, finance=True, vp_assets=3, clips=3):
    d = root / ep
    (d / "video").mkdir(parents=True, exist_ok=True)
    if plan:
        (d / f"production_plan_{ep}.json").write_text('{"content_hash":"x"}', encoding="utf-8")
    if finance:
        (d / f"finance_post_vpd_authorization_{ep}.json").write_text('{"verdict":"AUTHORIZED"}', encoding="utf-8")
    if vp_assets is not None:
        (d / f"visual_package_{ep}.json").write_text(
            json.dumps({"asset_manifest": {"assets": [{"scene_id": f"s{i}"} for i in range(vp_assets)]}}),
            encoding="utf-8",
        )
    for i in range(clips):
        (d / "video" / f"sc_s{i:02d}.mp4").write_bytes(b"FAKEMP4")


def test_all_preconditions_met(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_ok")
    _files(root, "ep_ok")
    pre = salvage_preconditions("ep_ok", db_path=dbp, assets_root=root)
    assert pre["ok"] is True, pre["blockers"]
    assert pre["channel_id"] == "hypogeum"
    assert pre["visual_package_resumable"] is True
    assert pre["manifest_asset_count"] == 3


def test_no_visual_package_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_novp")
    _files(root, "ep_novp", vp_assets=None)
    pre = salvage_preconditions("ep_novp", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("visual_package" in b for b in pre["blockers"])


def test_empty_visual_package_manifest_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_emptyvp")
    _files(root, "ep_emptyvp", vp_assets=0)
    pre = salvage_preconditions("ep_emptyvp", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert pre["visual_package_resumable"] is False


def test_run_not_failed_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_running", run_status="running")
    _files(root, "ep_running")
    pre = salvage_preconditions("ep_running", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("terminal failure" in b for b in pre["blockers"])


def test_completed_admission_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_done", adm_status="completed")
    _files(root, "ep_done")
    pre = salvage_preconditions("ep_done", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("re-admittable" in b or "completed" in b for b in pre["blockers"])


def test_zero_clips_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_noclips")
    _files(root, "ep_noclips", clips=0)
    pre = salvage_preconditions("ep_noclips", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("zero scene clips" in b for b in pre["blockers"])


def test_missing_finance_auth_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_nofin")
    _files(root, "ep_nofin", finance=False)
    pre = salvage_preconditions("ep_nofin", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("finance authorization" in b for b in pre["blockers"])
