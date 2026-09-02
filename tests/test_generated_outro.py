"""Channel generated-outro: a short spoken follow/like line appended to every
short-native script as its own per-clip-sized beats (beat_type='outro'), which
then get their own generated shots through the normal pipeline — no static
outro.mp4.

General rule (2026-09-02): EVERY short-native channel gets a generated outro
with a tone-appropriate default line. A channel overrides the wording with
channel_packages/<id>/outro_policy.json, or opts out with {"mode": "none"}.

Covers:
  - a short-native channel with NO policy file still gets an outro (general rule)
  - a policy file's follow_line / tie_directive override the default
  - {"mode": "none"} opts a channel out
  - a non-short-native channel gets one only via an explicit {"mode": "generated"}
  - _append_generated_outro splits the follow line into clip-sized beats
  - idempotent (resume-safe)
  - scene_planner adds a scene slot per outro beat on top of the story cap
"""
from __future__ import annotations

import json

import pytest

from hypogeum.agents.script_engine import (
    _append_generated_outro,
    _generated_outro_policy,
)
from hypogeum.config import Config


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass


def _cfg(short_native: bool = True):
    c = Config()
    c.SHORT_NATIVE_MODE = short_native
    c.SHORT_NATIVE_WORDS_PER_SECOND = 2.9
    c.MUAPI_T2_MODEL = "wan2.7-text-to-video"
    return c


def _script(n_story_beats: int = 3) -> dict:
    return {"sections": [{"section_id": "s1", "title": "T", "narration_beats": [
        {"beat_id": f"b_{i}", "text": f"Story beat {i} words here now.", "beat_type": "resolution"}
        for i in range(n_story_beats)
    ]}], "shorts_scripts": []}


@pytest.fixture
def outro_channels(tmp_path, monkeypatch):
    pkgs = tmp_path / "channel_packages"
    (pkgs / "ch_custom").mkdir(parents=True)
    (pkgs / "ch_custom" / "outro_policy.json").write_text(json.dumps({
        "mode": "generated",
        "follow_line": "Custom closing line one. Custom closing line two here.",
        "tie_directive": "custom tie",
    }))
    (pkgs / "ch_default").mkdir(parents=True)          # no policy file at all
    (pkgs / "ch_optout").mkdir(parents=True)
    (pkgs / "ch_optout" / "outro_policy.json").write_text(json.dumps({"mode": "none"}))
    monkeypatch.setattr("hypogeum.channel_package_scaffold._PACKAGES", pkgs)
    return pkgs


def test_short_native_channel_gets_outro_without_a_policy_file(outro_channels):
    pol = _generated_outro_policy("ch_default", _cfg(short_native=True))
    assert pol is not None
    assert pol["follow_line"].strip()
    assert pol["tie_directive"].strip()


def test_policy_file_overrides_default_wording(outro_channels):
    pol = _generated_outro_policy("ch_custom", _cfg(short_native=True))
    assert pol["follow_line"].startswith("Custom closing line one")
    assert pol["tie_directive"] == "custom tie"


def test_mode_none_opts_the_channel_out(outro_channels):
    assert _generated_outro_policy("ch_optout", _cfg(short_native=True)) is None


def test_non_short_native_channel_gets_no_outro_without_explicit_file(outro_channels):
    assert _generated_outro_policy("ch_default", _cfg(short_native=False)) is None
    # ...but an explicit {"mode": "generated"} file still opts one in
    assert _generated_outro_policy("ch_custom", _cfg(short_native=False)) is not None


def test_none_channel_id(outro_channels):
    assert _generated_outro_policy(None, _cfg(short_native=True)) is None


def test_outro_beats_appended_and_clip_sized(outro_channels):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_default")
    beats = sp["sections"][0]["narration_beats"]
    outro = [b for b in beats if b.get("beat_type") == "outro"]
    assert len(outro) >= 1
    assert outro == beats[-len(outro):], "outro beats must be last"
    _, per_beat_words, _ = __import__(
        "hypogeum.agents.script_engine", fromlist=["_short_native_beat_budget"]
    )._short_native_beat_budget(_cfg(), "ch_default")
    for b in outro:
        assert len(b["text"].split()) <= per_beat_words


def test_noop_when_opted_out(outro_channels):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_optout")
    assert all(b.get("beat_type") != "outro" for b in sp["sections"][0]["narration_beats"])


def test_idempotent(outro_channels):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_default")
    n = len(sp["sections"][0]["narration_beats"])
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_default")
    assert len(sp["sections"][0]["narration_beats"]) == n


def test_scene_planner_reserves_a_slot_per_outro_beat(outro_channels, monkeypatch):
    from hypogeum.agents.scene_planner import build_scene_plan_input
    cfg = _cfg()
    cfg.EPISODE_ZERO_MAX_SCENES = 3  # story hard cap
    sp = _script(n_story_beats=5)  # 5 story beats, cap 3 → 3 story scenes
    _append_generated_outro(sp, cfg, _Log(), channel_id="ch_custom")
    n_outro = sum(1 for b in sp["sections"][0]["narration_beats"] if b.get("beat_type") == "outro")
    assert n_outro >= 1

    inp = build_scene_plan_input(sp, cfg, "ep_x", channel_id="ch_custom")
    cap = inp["generation_constraints"]["max_main_scenes"]
    assert cap == 3 + n_outro, f"expected story cap 3 + {n_outro} outro, got {cap}"
