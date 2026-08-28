"""
tests/test_plan_lifecycle_canonical_authority.py — the canonical episode
directory is the SOLE authority for a verified Production Plan
(2026-08-28 tightening).

A caller-controlled directory holding a copied plan file must NOT become an
authorization substitute merely because its content_hash matches the
canonical plan. Closure 0 has no alias/relocation record system, so any
non-canonical supplied output_dir is INCONSISTENT_PATH and fails closed
before provider I/O.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.db as db
import hypogeum.production_plan as pp
from hypogeum.plan_reservation import (
    PlanReservationInconsistentPathError,
    ensure_verified_plan_weekly_reservation,
)
from hypogeum.production_plan import (
    PLAN_SCHEMA_VERSION, ROLE_REQUIRED, PlanLifecycle, PlannedAsset, ProductionPlan,
    classify_plan_lifecycle, payload_content_hash, persist_production_plan, prompt_fingerprint,
)


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass


def _make_plan(episode_id, channel_id, maximum):
    from dataclasses import asdict
    prompt = f"shot {maximum}"
    a = PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=f"asset_{episode_id}", scene_id="s01", short_id=None, role=ROLE_REQUIRED,
        prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt), shot_signature="",
        aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model="veo3.1-lite-text-to-video", unit_price_usd=0.15,
        enhancement_price_usd=0.05, max_chargeable_attempts=1,
    )
    kw = dict(
        schema_version=PLAN_SCHEMA_VERSION, channel_id=channel_id, episode_id=episode_id,
        deliverable_contract_id="c1", deliverable_ids=["main_episode"],
        thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=1, thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.36, thumbnail_cost_maximum_usd=1.08,
        narration_char_count=0, narration_price_per_char_usd=0.0, narration_cost_usd=0.0,
        narration_is_realized=False, music_section_count=0, music_seconds_per_section=120.0,
        music_price_per_second_usd=0.0, music_cost_usd=0.0, planning_call_estimate_usd=0.05,
        enhancement_cost_maximum_usd=0.05, normal_expected_cost_usd=float(maximum),
        maximum_bounded_cost_usd=float(maximum),
    )
    payload = {**kw, "assets": [asdict(a)]}
    return ProductionPlan(assets=[a], content_hash=payload_content_hash(payload),
                          compiled_at="2026-08-28T00:00:00+00:00", **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """canonical_episode_dir -> tmp_path/episodes/<ep>; a real channel + a
    verified plan persisted at that canonical location."""
    db_path = tmp_path / "canon.db"
    db.initialize_schema(db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep)

    import hypogeum.channel_config as cc
    cc.create_channel_profile(channel_id="cz", name="CZ", db_path=db_path)
    cc.create_channel_operations_profile(channel_id="cz", channel_name="CZ", timezone="UTC",
                                         weekly_video_budget_usd=200.0, db_path=db_path)

    canon = tmp_path / "episodes" / "ep_c"
    canon.mkdir(parents=True)
    plan = _make_plan("ep_c", "cz", 10.0)
    persist_production_plan(plan, canon)
    return SimpleNamespace(tmp_path=tmp_path, canon=canon, plan=plan, db_path=db_path)


def _reserve(output_dir):
    return ensure_verified_plan_weekly_reservation(
        channel_id="cz", episode_id="ep_c", config=SimpleNamespace(), log=_Log(),
        output_dir=output_dir,
    )


def test_classify_canonical_dir_is_post_plan(env):
    assert classify_plan_lifecycle("ep_c", env.canon) is PlanLifecycle.POST_PLAN
    assert classify_plan_lifecycle("ep_c", None) is PlanLifecycle.POST_PLAN


def test_canonical_dir_and_canonical_plan_reservation_succeeds(env):
    out = _reserve(env.canon)
    assert out is not None and out.created is True


def test_empty_wrong_directory_fails_closed(env):
    wrong = env.tmp_path / "wrong_empty"
    wrong.mkdir()
    assert classify_plan_lifecycle("ep_c", wrong) is PlanLifecycle.INCONSISTENT_PATH
    with pytest.raises(PlanReservationInconsistentPathError):
        _reserve(wrong)


def test_copied_same_hash_plan_elsewhere_still_fails_closed(env):
    copy_dir = env.tmp_path / "copy_dir"
    copy_dir.mkdir()
    shutil.copy(
        env.canon / f"production_plan_ep_c.json",
        copy_dir / f"production_plan_ep_c.json",
    )
    # byte-identical -> same content_hash -> still NOT an authorization substitute
    assert (copy_dir / "production_plan_ep_c.json").read_bytes() == \
           (env.canon / "production_plan_ep_c.json").read_bytes()
    assert classify_plan_lifecycle("ep_c", copy_dir) is PlanLifecycle.INCONSISTENT_PATH
    with pytest.raises(PlanReservationInconsistentPathError):
        _reserve(copy_dir)


def test_different_hash_plan_elsewhere_fails_closed(env):
    diff_dir = env.tmp_path / "diff_dir"
    diff_dir.mkdir()
    other = _make_plan("ep_c", "cz", 99.0)  # different maximum -> different content_hash
    assert other.content_hash != env.plan.content_hash
    persist_production_plan(other, diff_dir)
    assert classify_plan_lifecycle("ep_c", diff_dir) is PlanLifecycle.INCONSISTENT_PATH
    with pytest.raises(PlanReservationInconsistentPathError):
        _reserve(diff_dir)


def test_tmp_path_only_authoritative_when_canonical_is_redirected(env, monkeypatch):
    """With canonical redirected to the tmp tree (env fixture) the tmp plan
    IS canonical. Point canonical_episode_dir at the REAL repo path and the
    same tmp plan is now a misdirected copy -> INCONSISTENT_PATH."""
    assert classify_plan_lifecycle("ep_c", env.canon) is PlanLifecycle.POST_PLAN

    real_canonical = Path(pp.__file__).resolve().parent.parent / ".hypogeum-assets" / "episodes"
    monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: real_canonical / ep)
    assert classify_plan_lifecycle("ep_c", env.canon) is PlanLifecycle.INCONSISTENT_PATH
    with pytest.raises(PlanReservationInconsistentPathError):
        _reserve(env.canon)
