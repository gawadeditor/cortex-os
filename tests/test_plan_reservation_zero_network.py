"""
tests/test_plan_reservation_zero_network.py — regression coverage for the
Closure 0 post-plan paid-boundary guarantee (2026-08-27 hardening):

  1. A denied plan-level weekly reservation makes ZERO provider network
     calls at every post-plan boundary.
  2. channel_id=None reaches a post-plan boundary (a Production Plan file
     exists) => the boundary FAILS CLOSED before provider I/O -- for MUAPI
     video/upscale, thumbnails, narration, music, shorts-title text,
     OpenRouter coverage regeneration, AND the legacy MUAPI video path.
  3. A MUAPI-specific dry-run flag NEVER exempts a non-MUAPI provider:
       - MUAPI_DRY_RUN + live ElevenLabs   => reservation still required
       - MUAPI_DRY_RUN + live OpenRouter   => reservation still required
       - MUAPI_DRY_RUN + live thumbnail (Claude Phase 1) => still required
     Only a genuinely zero-network dry-run (MUAPI upscaler under
     MUAPI_DRY_RUN, MUAPI thumbnail primary under MUAPI_DRY_RUN) is exempt.

All tests use tmp_path databases + an isolated persisted plan; no real
network is reachable (every provider primitive is a sentinel that raises).
"""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.channel_config as channel_config
import hypogeum.db as db
from hypogeum.budget_scopes import BudgetError
from hypogeum.plan_reservation import PlanReservationMissingError
from hypogeum.production_plan import (
    PLAN_SCHEMA_VERSION,
    ROLE_REQUIRED,
    PlannedAsset,
    ProductionPlan,
    payload_content_hash,
    persist_production_plan,
    prompt_fingerprint,
)


class _NetworkTouched(AssertionError):
    """Raised by any provider network sentinel -- must never fire."""


def _sentinel(*a, **k):
    raise _NetworkTouched("a provider network primitive was reached")


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


def _cfg(**over):
    from types import SimpleNamespace
    base = dict(
        MUAPI_DRY_RUN=False, MUAPI_CHEAP_MODE=False, MUAPI_RUNWAY_ONLY_MODE=False,
        MUAPI_API_KEY="k", MUAPI_DEFAULT_TIER=2, MUAPI_ALLOW_PAID_FALLBACKS=False,
        ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v", ELEVENLABS_MODEL_ID=None,
        ELEVENLABS_OUTPUT_FORMAT=None, ELEVENLABS_STABILITY=None,
        ELEVENLABS_SIMILARITY_BOOST=None, ELEVENLABS_TIMEOUT_SECONDS=30,
        ELEVENLABS_MUSIC_MODEL_ID=None,
        THUMBNAIL_IMAGE_PROVIDER="muapi", THUMBNAIL_FALLBACK_PROVIDER="",
        THUMBNAIL_FALLBACK2_PROVIDER="", CINEMATIC_DIRECTOR_MAX_RETRIES=2,
        BGA_ENABLED=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _persist_plan(db_path, episode_dir, *, channel_id="cz", episode_id="ep_cz",
                   ceiling=25.0, maximum=10.0):
    db.initialize_schema(db_path)
    if channel_config.get_channel_profile(channel_id, db_path=db_path) is None:
        channel_config.create_channel_profile(
            channel_id=channel_id, name="CZ", db_path=db_path,
        )
    if channel_config.get_channel_operations_profile(channel_id, db_path=db_path) is None:
        channel_config.create_channel_operations_profile(
            channel_id=channel_id, channel_name="CZ", timezone="UTC",
            weekly_video_budget_usd=float(ceiling), db_path=db_path,
        )
    episode_dir.mkdir(parents=True, exist_ok=True)
    prompt = "cinematic shot"
    asset = PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=f"asset_{episode_id}", scene_id="s01", short_id=None,
        role=ROLE_REQUIRED, prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt),
        shot_signature="", aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model="veo3.1-lite-text-to-video", unit_price_usd=0.15,
        enhancement_price_usd=0.05, max_chargeable_attempts=1,
    )
    kwargs = dict(
        schema_version=PLAN_SCHEMA_VERSION, channel_id=channel_id, episode_id=episode_id,
        deliverable_contract_id="c1", deliverable_ids=["main_episode"],
        thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=1, thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.36, thumbnail_cost_maximum_usd=1.08,
        narration_char_count=0, narration_price_per_char_usd=0.0,
        narration_cost_usd=0.0, narration_is_realized=False,
        music_section_count=0, music_seconds_per_section=120.0,
        music_price_per_second_usd=0.0, music_cost_usd=0.0,
        planning_call_estimate_usd=0.05, enhancement_cost_maximum_usd=0.05,
        normal_expected_cost_usd=float(maximum), maximum_bounded_cost_usd=float(maximum),
    )
    payload = {**kwargs, "assets": [asdict(asset)]}
    plan = ProductionPlan(assets=[asset], content_hash=payload_content_hash(payload),
                          compiled_at="2026-08-26T00:00:00+00:00", **kwargs)
    persist_production_plan(plan, episode_dir)
    return plan.content_hash


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "cz.db"
    episode_dir = tmp_path / "episodes" / "ep_cz"
    _persist_plan(db_path, episode_dir)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    # Point the canonical resolver at this episode dir so
    # classify_plan_lifecycle() treats it as the authoritative plan
    # location regardless of how a boundary derives its own path.
    import hypogeum.production_plan as pp
    monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: episode_dir)
    return dict(db_path=db_path, episode_dir=episode_dir)


def _weekly_rows(db_path):
    conn = db.get_connection(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='channel_weekly'"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — reservation denial => zero provider network, at each boundary
# ---------------------------------------------------------------------------

class TestDenialIsZeroNetwork:
    def _tiny_ceiling(self, db_path):
        channel_config.update_channel_operations_profile(
            "cz", db_path=db_path, weekly_video_budget_usd=1.0,  # < plan max 10.0
        )

    def test_upscaler_enhance(self, env, monkeypatch):
        import hypogeum.providers.muapi_video_upscaler as up
        self._tiny_ceiling(env["db_path"])
        monkeypatch.setattr(up, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}), raising=False)
        prov = up.MuapiVideoUpscalerProvider()
        monkeypatch.setattr(prov, "_enhance_via_gate", _sentinel, raising=False)
        res = prov.enhance(
            source_path=env["episode_dir"] / "s.mp4", dest_path=env["episode_dir"] / "d.mp4",
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            config=_cfg(), episode_id="ep_cz", scene_id="s01", log=_Log(),
            channel_id="cz", output_dir=env["episode_dir"],
        )
        assert res.success is False
        assert "plan_weekly_reservation_denied" in (res.failure_reason or "")

    def test_thumbnail_generation(self, env, monkeypatch):
        import hypogeum.agents.thumbnail_generation_agent as tga
        self._tiny_ceiling(env["db_path"])
        monkeypatch.setattr(tga, "generate_image", _sentinel, raising=False)
        with pytest.raises(BudgetError):
            tga.generate_thumbnails(
                generation_plan={}, config=_cfg(THUMBNAIL_IMAGE_PROVIDER="openai"),
                episode_id="ep_cz", output_dir=env["episode_dir"], log=_Log(),
                channel_id="cz",
            )

    def test_coverage_regen(self, env, monkeypatch):
        import hypogeum.agents.cinematic_director as cd
        self._tiny_ceiling(env["db_path"])
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _sentinel)
        sp = {"sections": [{"section_id": "s", "title": "t",
                            "narration_beats": [{"beat_id": "b_001", "text": "x"}]}],
              "shorts_scripts": []}
        np_ = {"narration_segments": [{"beat_reference": "b_001", "duration_seconds": 8.0}]}
        vp = {"episode_metadata": {}, "asset_manifest": {"assets": []},
              "scenes": [{"scene_id": "s01", "narration_reference": "b_001",
                          "source_type": "main_episode", "production_spec": {}}]}
        with pytest.raises(Exception) as ei:
            cd.regenerate_cinematic_coverage_plan(
                sp, np_, vp, "ep_cz", _cfg(), output_dir=env["episode_dir"],
                force=True, channel_id="cz",
            )
        assert not isinstance(ei.value, _NetworkTouched)


# ---------------------------------------------------------------------------
# 2 — channel_id=None at a POST-plan boundary => fail closed before I/O
# ---------------------------------------------------------------------------

class TestChannelIdNoneFailsClosedPostPlan:
    def test_upscaler(self, env, monkeypatch):
        import hypogeum.providers.muapi_video_upscaler as up
        monkeypatch.setattr(up, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}), raising=False)
        prov = up.MuapiVideoUpscalerProvider()
        monkeypatch.setattr(prov, "_enhance_via_gate", _sentinel, raising=False)
        res = prov.enhance(
            source_path=env["episode_dir"] / "s.mp4", dest_path=env["episode_dir"] / "d.mp4",
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            config=_cfg(), episode_id="ep_cz", scene_id="s01", log=_Log(),
            channel_id=None, output_dir=env["episode_dir"],
        )
        assert res.success is False
        assert "reservation_denied" in (res.failure_reason or "")

    def test_narration(self, env, monkeypatch):
        import hypogeum.elevenlabs as el
        monkeypatch.setattr(el, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}))
        with pytest.raises(PlanReservationMissingError):
            el.generate_narration_segment(
                beat_id="b1", text="hello", config=_cfg(), episode_id="ep_cz",
                output_dir=env["episode_dir"], channel_id=None,
            )

    def test_music(self, env, monkeypatch):
        import hypogeum.elevenlabs as el
        monkeypatch.setattr(el, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}))
        (env["episode_dir"].parent.parent / "channel" / "music").mkdir(parents=True, exist_ok=True)
        with pytest.raises(PlanReservationMissingError):
            el.generate_music_track(
                category="tension", required_duration_seconds=30.0, config=_cfg(),
                episode_id="ep_cz", assets_root=env["episode_dir"].parent.parent,
                channel_id=None,
            )

    def test_legacy_muapi_video_path(self, env, monkeypatch):
        import hypogeum.muapi as muapi
        monkeypatch.setattr(muapi.requests, "post", _sentinel, raising=False)
        monkeypatch.setattr(muapi.requests, "get", _sentinel, raising=False)
        spec = {
            "scene_id": "s01", "short_id": None,
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {"primary_prompt": "p", "primary_tier": 2,
                                "fallback_prompt": "f", "fallback_tier": 2},
        }
        out = muapi.submit_scene(spec, _cfg(), "ep_cz", env["episode_dir"])  # no channel_id
        assert out["generation_status"] == "generation_failed"
        assert "no_authoritative_channel_identity_post_plan" in out["failure_reason"]


# ---------------------------------------------------------------------------
# 3 — MUAPI_DRY_RUN must NOT exempt a non-MUAPI provider
# ---------------------------------------------------------------------------

class TestMuapiDryRunDoesNotExemptOtherProviders:
    def test_muapi_dryrun_plus_live_elevenlabs_still_reserves(self, env, monkeypatch):
        """MUAPI dry-run + live ElevenLabs narration regeneration: the
        weekly reservation is still established (row created)."""
        import hypogeum.elevenlabs as el
        # Sentinel the TTS network; the reservation must succeed BEFORE it,
        # then a downstream failure is fine -- we only assert the row exists.
        monkeypatch.setattr(el, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}))
        with pytest.raises(_NetworkTouched):
            el.generate_narration_segment(
                beat_id="b1", text="hello", config=_cfg(MUAPI_DRY_RUN=True),
                episode_id="ep_cz", output_dir=env["episode_dir"], channel_id="cz",
            )
        assert _weekly_rows(env["db_path"]) == 1  # reservation happened

    def test_muapi_dryrun_plus_live_openrouter_still_reserves(self, env, monkeypatch):
        import hypogeum.agents.cinematic_director as cd
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _sentinel)
        sp = {"sections": [{"section_id": "s", "title": "t",
                            "narration_beats": [{"beat_id": "b_001", "text": "x"}]}],
              "shorts_scripts": []}
        np_ = {"narration_segments": [{"beat_reference": "b_001", "duration_seconds": 8.0}]}
        vp = {"episode_metadata": {}, "asset_manifest": {"assets": []},
              "scenes": [{"scene_id": "s01", "narration_reference": "b_001",
                          "source_type": "main_episode", "production_spec": {}}]}
        with pytest.raises(Exception):
            cd.regenerate_cinematic_coverage_plan(
                sp, np_, vp, "ep_cz", _cfg(MUAPI_DRY_RUN=True),
                output_dir=env["episode_dir"], force=True, channel_id="cz",
            )
        # The Step-0 weekly reservation ran to completion (row exists) even
        # though MUAPI_DRY_RUN is set -- an OpenRouter call is never exempt.
        assert _weekly_rows(env["db_path"]) == 1

    def test_muapi_dryrun_plus_nonmuapi_thumbnail_primary_still_reserves(self, env, monkeypatch):
        import hypogeum.agents.thumbnail_generation_agent as tga
        monkeypatch.setattr(tga, "generate_image", _sentinel, raising=False)
        # THUMBNAIL_IMAGE_PROVIDER != "muapi" => MUAPI_DRY_RUN is NOT a
        # zero-network proof for thumbnails => reservation still required.
        with pytest.raises(Exception):
            tga.generate_thumbnails(
                generation_plan={}, config=_cfg(MUAPI_DRY_RUN=True, THUMBNAIL_IMAGE_PROVIDER="openai"),
                episode_id="ep_cz", output_dir=env["episode_dir"], log=_Log(), channel_id="cz",
            )
        assert _weekly_rows(env["db_path"]) == 1

    def test_true_zero_network_dry_run_reserves_nothing(self, env, monkeypatch):
        """MUAPI upscaler under MUAPI_DRY_RUN: writes a placeholder, makes no
        MUAPI call, consumes zero weekly budget, needs no channel identity."""
        import hypogeum.providers.muapi_video_upscaler as up
        monkeypatch.setattr(up, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}), raising=False)
        src = env["episode_dir"] / "s.mp4"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
            capture_output=True,
        )
        prov = up.MuapiVideoUpscalerProvider()
        res = prov.enhance(
            source_path=src, dest_path=env["episode_dir"] / "d.mp4",
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            config=_cfg(MUAPI_DRY_RUN=True), episode_id="ep_cz", scene_id="s01",
            log=_Log(), channel_id=None, output_dir=env["episode_dir"],
        )
        assert res.success is True
        assert _weekly_rows(env["db_path"]) == 0


# ---------------------------------------------------------------------------
# 4 — CALLER-LEVEL wrong-output_dir bypass: a canonical plan exists, the
#     boundary is handed a DIFFERENT empty dir. The canonical resolver in
#     classify_plan_lifecycle() (independent of output_dir) makes every
#     affected caller fail closed before provider I/O -- NOT treat it as
#     pre-plan.
# ---------------------------------------------------------------------------

class TestWrongOutputDirFailsClosedBeforeNetwork:
    """env fixture points canonical_episode_dir at env['episode_dir'] (which
    holds the verified plan). Each boundary is called with output_dir set to
    a SEPARATE empty directory -- the classic caller-level bypass."""

    def _empty(self, tmp_path):
        d = tmp_path / "wrong" / "elsewhere"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_upscaler(self, env, tmp_path, monkeypatch):
        import hypogeum.providers.muapi_video_upscaler as up
        monkeypatch.setattr(up, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}), raising=False)
        prov = up.MuapiVideoUpscalerProvider()
        monkeypatch.setattr(prov, "_enhance_via_gate", _sentinel, raising=False)
        monkeypatch.setattr(prov, "_dry_run", _sentinel, raising=False)
        res = prov.enhance(
            source_path=tmp_path / "s.mp4", dest_path=tmp_path / "d.mp4",
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            config=_cfg(), episode_id="ep_cz", scene_id="s01", log=_Log(),
            channel_id="cz", output_dir=self._empty(tmp_path),
        )
        assert res.success is False
        assert "misdirected path" in (res.failure_reason or "").lower()

    def test_narration(self, env, tmp_path, monkeypatch):
        import hypogeum.elevenlabs as el
        from hypogeum.plan_reservation import PlanReservationInconsistentPathError
        monkeypatch.setattr(el, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}))
        with pytest.raises(PlanReservationInconsistentPathError):
            el.generate_narration_segment(
                beat_id="b1", text="hello", config=_cfg(), episode_id="ep_cz",
                output_dir=self._empty(tmp_path), channel_id="cz",
            )

    def test_music(self, env, tmp_path, monkeypatch):
        import hypogeum.elevenlabs as el
        from hypogeum.plan_reservation import PlanReservationInconsistentPathError
        monkeypatch.setattr(el, "requests", type("R", (), {
            "post": staticmethod(_sentinel), "get": staticmethod(_sentinel)}))
        wrong_root = tmp_path / "wrongroot"
        (wrong_root / "channel" / "music").mkdir(parents=True, exist_ok=True)
        (wrong_root / "episodes" / "ep_cz").mkdir(parents=True, exist_ok=True)
        with pytest.raises(PlanReservationInconsistentPathError):
            el.generate_music_track(
                category="tension", required_duration_seconds=30.0, config=_cfg(),
                episode_id="ep_cz", assets_root=wrong_root, channel_id="cz",
            )

    def test_thumbnail_generation(self, env, tmp_path, monkeypatch):
        import hypogeum.agents.thumbnail_generation_agent as tga
        from hypogeum.plan_reservation import PlanReservationInconsistentPathError
        monkeypatch.setattr(tga, "generate_image", _sentinel, raising=False)
        with pytest.raises(PlanReservationInconsistentPathError):
            tga.generate_thumbnails(
                generation_plan={}, config=_cfg(THUMBNAIL_IMAGE_PROVIDER="openai"),
                episode_id="ep_cz", output_dir=self._empty(tmp_path), log=_Log(),
                channel_id="cz",
            )

    def test_coverage_regen(self, env, tmp_path, monkeypatch):
        import hypogeum.agents.cinematic_director as cd
        monkeypatch.setattr("hypogeum.openrouter.call_claude", _sentinel)
        sp = {"sections": [{"section_id": "s", "title": "t",
                            "narration_beats": [{"beat_id": "b_001", "text": "x"}]}],
              "shorts_scripts": []}
        np_ = {"narration_segments": [{"beat_reference": "b_001", "duration_seconds": 8.0}]}
        vp = {"episode_metadata": {}, "asset_manifest": {"assets": []},
              "scenes": [{"scene_id": "s01", "narration_reference": "b_001",
                          "source_type": "main_episode", "production_spec": {}}]}
        with pytest.raises(Exception) as ei:
            cd.regenerate_cinematic_coverage_plan(
                sp, np_, vp, "ep_cz", _cfg(), output_dir=self._empty(tmp_path),
                force=True, channel_id="cz",
            )
        assert not isinstance(ei.value, _NetworkTouched)

    def test_legacy_muapi_video(self, env, tmp_path, monkeypatch):
        import hypogeum.muapi as muapi
        monkeypatch.setattr(muapi.requests, "post", _sentinel, raising=False)
        monkeypatch.setattr(muapi.requests, "get", _sentinel, raising=False)
        spec = {
            "scene_id": "s01", "short_id": None,
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {"primary_prompt": "p", "primary_tier": 2,
                                "fallback_prompt": "f", "fallback_tier": 2},
        }
        out = muapi.submit_scene(spec, _cfg(), "ep_cz", self._empty(tmp_path))
        assert out["generation_status"] == "generation_failed"
        assert "no_authoritative_channel_identity_post_plan" in out["failure_reason"]
