"""Channel generated-outro: a closing follow/like line appended to every
script as its own per-clip-sized beats (beat_type='outro'), which then get
their own generated shots through the normal pipeline — no static outro.mp4.

Covers:
  - _append_generated_outro splits the follow line into clip-sized beats
  - no-op for a channel with no outro_policy.json / mode != "generated"
  - idempotent (resume-safe)
  - scene_planner adds a scene slot per outro beat on top of the story cap
  - assembly skips the static outro append when the channel is generated-outro
"""
from __future__ import annotations

import json

import pytest

from hypogeum.agents.script_engine import _append_generated_outro, _generated_outro_policy
from hypogeum.config import Config


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass


def _cfg():
    c = Config()
    c.SHORT_NATIVE_MODE = True
    c.SHORT_NATIVE_WORDS_PER_SECOND = 2.9
    c.MUAPI_T2_MODEL = "wan2.7-text-to-video"
    return c


def _script(n_story_beats: int = 3) -> dict:
    return {"sections": [{"section_id": "s1", "title": "T", "narration_beats": [
        {"beat_id": f"b_{i}", "text": f"Story beat {i} words here now.", "beat_type": "resolution"}
        for i in range(n_story_beats)
    ]}], "shorts_scripts": []}


@pytest.fixture
def generated_outro_channel(tmp_path, monkeypatch):
    pkgs = tmp_path / "channel_packages"
    (pkgs / "ch_gen").mkdir(parents=True)
    (pkgs / "ch_gen" / "outro_policy.json").write_text(json.dumps({
        "mode": "generated",
        "follow_line": "If you liked tonight's story, tap the little heart and "
                       "follow for another one tomorrow night. Sleep tight.",
    }))
    (pkgs / "ch_plain").mkdir(parents=True)  # no policy file
    monkeypatch.setattr("hypogeum.channel_package_scaffold._PACKAGES", pkgs)
    return "ch_gen"


def test_policy_only_for_generated_mode(generated_outro_channel):
    assert _generated_outro_policy("ch_gen") is not None
    assert _generated_outro_policy("ch_plain") is None
    assert _generated_outro_policy(None) is None


def test_outro_beats_appended_and_clip_sized(generated_outro_channel):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_gen")
    beats = sp["sections"][0]["narration_beats"]
    outro = [b for b in beats if b.get("beat_type") == "outro"]
    assert len(outro) >= 1
    assert outro == beats[-len(outro):], "outro beats must be last"
    # the last outro beat carries the final words of the follow line
    assert outro[-1]["text"].rstrip(".!").endswith("Sleep tight") or "Sleep tight" in outro[-1]["text"]
    _, per_beat_words, _ = __import__(
        "hypogeum.agents.script_engine", fromlist=["_short_native_beat_budget"]
    )._short_native_beat_budget(_cfg(), "ch_gen")
    for b in outro:
        assert len(b["text"].split()) <= per_beat_words


def test_noop_for_plain_channel(generated_outro_channel):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_plain")
    assert all(b.get("beat_type") != "outro" for b in sp["sections"][0]["narration_beats"])


def test_idempotent(generated_outro_channel):
    sp = _script()
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_gen")
    n = len(sp["sections"][0]["narration_beats"])
    _append_generated_outro(sp, _cfg(), _Log(), channel_id="ch_gen")
    assert len(sp["sections"][0]["narration_beats"]) == n


def test_scene_planner_reserves_a_slot_per_outro_beat(generated_outro_channel, monkeypatch):
    from hypogeum.agents.scene_planner import build_scene_plan_input
    cfg = _cfg()
    cfg.EPISODE_ZERO_MAX_SCENES = 3  # story hard cap
    sp = _script(n_story_beats=5)  # 5 story beats, cap 3 → 3 story scenes
    _append_generated_outro(sp, cfg, _Log(), channel_id="ch_gen")
    n_outro = sum(1 for b in sp["sections"][0]["narration_beats"] if b.get("beat_type") == "outro")

    inp = build_scene_plan_input(sp, cfg, "ep_x", channel_id="ch_gen")
    cap = inp["generation_constraints"]["max_main_scenes"]
    assert cap == 3 + n_outro, f"expected story cap 3 + {n_outro} outro, got {cap}"
