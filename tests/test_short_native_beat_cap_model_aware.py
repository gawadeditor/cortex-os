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
    # No channel model -> veo3.1 family default 8s clip. The word cap is the
    # SHORT_NATIVE_WORDS_PER_SECOND estimate (8*2.9=23) clamped so even the
    # slow-voice floor fits one clip at the editorial ceiling
    # (8*1.15*1.9 ~= 17), so ~17 words survive.
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, _cfg(), _Log(), channel_id=None)
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    assert 14 <= kept <= 19


def test_seedance_channel_trims_tighter(monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: 5.04,
    )
    # 5.04s clip: est 5.04*2.9=~14, slow-voice hard cap 5.04*1.15*1.9=~11 ->
    # ~11 words, much tighter than the ~17 default.
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, _cfg(), _Log(), channel_id="twinkle")
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    assert kept <= 13, kept


def test_slow_voice_hard_cap_bounds_a_fast_wps_setting(monkeypatch):
    """Even if SHORT_NATIVE_WORDS_PER_SECOND is set optimistically high, the
    per-beat word cap stays low enough that the slowest plausible voice still
    fits one clip at the editorial-speed ceiling — no coverage-fill deficit."""
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: 5.04,
    )
    c = _cfg()
    c.SHORT_NATIVE_WORDS_PER_SECOND = 4.0  # absurdly fast
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, c, _Log(), channel_id="twinkle")
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    assert kept <= 12, kept


def test_slowest_beat_plus_elevenlabs_tail_still_fits_one_clip(monkeypatch):
    """2026-09-06 regression (ep_e51a43ef b_013): the word cap left NO
    headroom for ElevenLabs' ~0.2s trailing silence, so the slowest,
    most-emphatic bedtime line ('He yawns one last, long, slow yawn.' at
    ~1.83 wps) rendered 0.24s over one clip's editorial ceiling and lost
    the whole episode to an unfixable single-shot coverage deficit. The cap
    must now keep even a 1.8-wps beat + a 0.21s tail inside clip*1.15."""
    monkeypatch.setattr("hypogeum.channel_config.resolve_channel_video_model",
                        lambda cid, db_path=None: ("wan2.7-text-to-video", 0.10))
    monkeypatch.setattr("hypogeum.channel_config.resolve_channel_video_duration",
                        lambda cid, db_path=None: 5.0)
    sp = _one_beat_script(60)
    _enforce_short_native_budget(sp, _cfg(), _Log(), channel_id="twinkle")
    kept = len(sp["sections"][0]["narration_beats"][0]["text"].split())
    worst_case_real_seconds = kept / 1.8 + 0.21   # slowest voice + EL tail
    assert worst_case_real_seconds <= 5.0 * 1.15, (kept, worst_case_real_seconds)
