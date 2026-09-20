"""A channel's music-provider override (ModelProfile.music_provider) lets
one channel run on VODER (free, local) while another stays on ElevenLabs,
without a factory-wide MUSIC_PROVIDER env flag forcing the same choice on
every channel. Added 2026-09-20 alongside the VODER music integration —
see memory: project_voder_evaluation_20260920.
"""

from __future__ import annotations

from hypogeum.channel_config import (
    create_channel_profile, create_model_profile, resolve_channel_music_provider,
    update_model_profile,
)


def _channel_with_music_provider(db, channel_id="c", provider=None):
    mp = create_model_profile(f"{channel_id} models", db_path=db)
    create_channel_profile(channel_id, channel_id.title(),
                           model_profile_id=mp.model_profile_id, db_path=db)
    if provider:
        update_model_profile(mp.model_profile_id, db_path=db, music_provider=provider)
    return mp


def test_resolve_channel_music_provider_returns_override(tmp_path):
    db = tmp_path / "t.db"
    _channel_with_music_provider(db, "twinkle", provider="voder")
    assert resolve_channel_music_provider("twinkle", db_path=db) == "voder"


def test_resolve_channel_music_provider_none_when_unset(tmp_path):
    db = tmp_path / "t.db"
    _channel_with_music_provider(db, "hypogeum", provider=None)
    assert resolve_channel_music_provider("hypogeum", db_path=db) is None


def test_resolve_channel_music_provider_none_for_unknown_channel(tmp_path):
    db = tmp_path / "t.db"
    assert resolve_channel_music_provider("nope", db_path=db) is None


def test_two_channels_can_differ(tmp_path):
    db = tmp_path / "t.db"
    _channel_with_music_provider(db, "twinkle", provider="voder")
    _channel_with_music_provider(db, "hypogeum", provider=None)
    assert resolve_channel_music_provider("twinkle", db_path=db) == "voder"
    assert resolve_channel_music_provider("hypogeum", db_path=db) is None
