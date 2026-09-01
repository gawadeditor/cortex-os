"""Short-native per-beat narration cap is model-aware: a channel on a
shorter-clip model (seedance ~5s) trims each beat to what ONE of its clips
covers, so Assembly's coverage-fill is never exercised on a short."""

from __future__ import annotations

from hypogeum.agents.script_engine import _enforce_short_native_budget
from hypogeum.config import Config


def _cfg():
    c = Config()
    c.SHORT_NATIVE_MODE = True
    c.SHORT_NATIVE_MAX_BEAT_SECONDS = 8.5
    c.SHORT_NATIVE_WORDS_PER_SECOND = 2.9
    c.SHORT_NATIVE_MAX_NARRATION_WORDS = 300  # high, so the per-beat trim is what bites
    c.MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
    c.MUAPI_DEFAULT_TIER = 2
    return c


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass


def _one_beat_script(n_words: int) -> dict:
    text = " ".join(f"word{i}." for i in range(n_words))  # each "word." is a sentence
    return {"sections": [{"section_id": "s1", "narration_beats": [
        {"beat_id": "b_001", "text": text},
    ]}], "text_overlay_plan": []}


def test_default_cap_when_no_channel_model(monkeypatch):
    # 8.5s * 2.9 wps = 24 words allowed
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, _cfg(), _Log(), channel_id=None)
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    assert 20 <= kept <= 26


def test_seedance_channel_trims_tighter(monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: 5.04,
    )
    # 5.04s * 2.9 wps = ~14 words allowed -- much tighter than the 24 default
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, _cfg(), _Log(), channel_id="twinkle")
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    assert kept <= 16, kept
