"""configure_channel_operating_grant._build_bindings must authorize a
channel's per-channel adopted video model (Video Model bake-off), or every
paid video call for that channel is denied at the submission gate."""

from __future__ import annotations

from pathlib import Path

from tools.configure_channel_operating_grant import _build_bindings


class _Cfg:
    # minimal surface _build_bindings reads
    OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"
    PERPLEXITY_MODEL = "sonar-pro"
    ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
    ELEVENLABS_MUSIC_MODEL_ID = "music_v1"
    MUAPI_T1_MODEL = "veo3-text-to-video"
    MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
    MUAPI_T3_MODEL = "veo3.1-lite-text-to-video"
    MUAPI_I2V_MODEL = "veo3-image-to-video"
    MUAPI_IMAGE_MODEL = "flux-2-dev"
    THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"

    def __getattr__(self, _n):  # any *_MODEL slug lookups fall through cleanly
        return None


def _muapi_video_models(bindings):
    return {b["model"] for b in bindings
            if b["provider"] == "muapi" and b["provider_operation"] == "video.text_to_video"}


def test_no_channel_id_keeps_the_global_model_set():
    models = _muapi_video_models(_build_bindings(_Cfg()))
    assert "seedance-pro-t2v-fast" not in models
    assert "veo3.1-lite-text-to-video" in models


def test_channel_adopted_model_is_authorized(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    models = _muapi_video_models(_build_bindings(_Cfg(), "twinkle"))
    assert "seedance-pro-t2v-fast" in models
    # the global models are still there for a mixed plan
    assert "veo3.1-lite-text-to-video" in models


def test_channel_without_adopted_model_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: (None, None),
    )
    assert _muapi_video_models(_build_bindings(_Cfg(), "ch")) == \
        _muapi_video_models(_build_bindings(_Cfg()))


def test_channel_adopted_i2v_model_is_also_authorized(monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_i2v_model",
        lambda cid, db_path=None: ("runway-image-to-video", 0.15),
    )
    models = _muapi_video_models(_build_bindings(_Cfg(), "twinkle"))
    assert "seedance-pro-t2v-fast" in models
    assert "runway-image-to-video" in models
