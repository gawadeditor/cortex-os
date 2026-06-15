"""
tests/test_channel_config.py — Phase A: Channel Abstraction tests

Coverage:
    TestBrandProfile          — create, get, update, list, CRUD round-trip
    TestPublishingProfile     — create, get, update, list, platform filter
    TestModelProfile          — create, get, update, list, defaults
    TestChannelProfile        — create, get, update, list, status filter, deactivate
    TestValidateChannelConfig — required fields, bad platform, good config
    TestBootstrapChannel      — full workflow, idempotent, creates all sub-profiles
    TestLoadChannelConfig     — joined config, missing profiles, missing channel
    TestChannelConfigSchema   — schema dict present and complete
    TestMultiChannelConcurrent — multiple channels independent, no cross-contamination
    TestNoNetworkCalls        — no sockets opened
    TestNoProductionSideEffects — no ep_000, no episode state written
"""

from __future__ import annotations

import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import (
    CHANNEL_CONFIG_SCHEMA,
    SUPPORTED_PLATFORMS,
    BrandProfile,
    ChannelConfig,
    ChannelProfile,
    ModelProfile,
    PublishingProfile,
    bootstrap_channel,
    create_brand_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
    deactivate_channel,
    get_brand_profile,
    get_channel_profile,
    get_model_profile,
    get_publishing_profile,
    list_brand_profiles,
    list_channel_profiles,
    list_model_profiles,
    list_publishing_profiles,
    load_channel_config,
    update_brand_profile,
    update_channel_profile,
    update_model_profile,
    update_publishing_profile,
    validate_channel_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "channel_config_test.db"
    initialize_schema(p)
    return p


_FULL_CONFIG = {
    "channel_id":  "tech_ch_001",
    "name":        "Tech Explained",
    "description": "Deep dives into software engineering",
    "brand": {
        "persona_name":    "Alex Chen",
        "tone":            "conversational",
        "target_audience": "software engineers 25-40",
        "visual_style":    "dark minimal",
        "language":        "en",
        "hashtags":        ["#tech", "#coding"],
        "color_palette":   {"primary": "#0F0F0F"},
        "typography":      {"headline": "Inter"},
        "voice_guidelines":"Speak clearly and with authority.",
        "content_pillars": ["AI", "SWE"],
    },
    "publishing": {
        "enabled_platforms":    ["youtube", "tiktok"],
        "schedule":             {"cadence": "weekly"},
        "default_tags":         ["tech", "tutorial"],
        "ai_disclosure_required": True,
        "auto_thumbnail":       True,
        "auto_captions":        True,
    },
    "models": {
        "episode_producer":   "anthropic/claude-sonnet-4.6",
        "research_director":  "anthropic/claude-opus-4.6",
        "thumbnail_planner":  "anthropic/claude-sonnet-4.6",
        "brand_guardian_p1":  "anthropic/claude-sonnet-4.6",
        "brand_guardian_p2":  "openai/gpt-5.5-pro",
        "narration_voice_id": "EXAVITQu4vr4xnSDxMaL",
        "image_provider":     "muapi",
        "video_provider":     "muapi",
    },
}


# ===========================================================================
# TestBrandProfile
# ===========================================================================

class TestBrandProfile:
    def test_create_returns_brand_profile(self, db_path):
        bp = create_brand_profile("My Brand", db_path=db_path)
        assert isinstance(bp, BrandProfile)
        assert bp.name == "My Brand"

    def test_create_persists_to_db(self, db_path):
        bp = create_brand_profile("My Brand", db_path=db_path)
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert fetched is not None
        assert fetched.name == "My Brand"

    def test_persona_name_round_trip(self, db_path):
        bp = create_brand_profile("B", persona_name="Dr. A", db_path=db_path)
        assert get_brand_profile(bp.brand_profile_id, db_path=db_path).persona_name == "Dr. A"

    def test_hashtags_round_trip(self, db_path):
        bp = create_brand_profile("B", hashtags=["#one", "#two"], db_path=db_path)
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert "#one" in fetched.hashtags
        assert "#two" in fetched.hashtags

    def test_color_palette_round_trip(self, db_path):
        bp = create_brand_profile("B", color_palette={"primary": "#FFF"}, db_path=db_path)
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert fetched.color_palette["primary"] == "#FFF"

    def test_content_pillars_round_trip(self, db_path):
        bp = create_brand_profile("B", content_pillars=["AI", "SWE"], db_path=db_path)
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert fetched.content_pillars == ["AI", "SWE"]

    def test_language_default_en(self, db_path):
        bp = create_brand_profile("B", db_path=db_path)
        assert get_brand_profile(bp.brand_profile_id, db_path=db_path).language == "en"

    def test_get_nonexistent_returns_none(self, db_path):
        assert get_brand_profile("no_such_id", db_path=db_path) is None

    def test_update_tone(self, db_path):
        bp = create_brand_profile("B", tone="casual", db_path=db_path)
        update_brand_profile(bp.brand_profile_id, db_path=db_path, tone="serious")
        assert get_brand_profile(bp.brand_profile_id, db_path=db_path).tone == "serious"

    def test_update_hashtags(self, db_path):
        bp = create_brand_profile("B", hashtags=["#old"], db_path=db_path)
        update_brand_profile(bp.brand_profile_id, db_path=db_path,
                             hashtags=["#new1", "#new2"])
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert fetched.hashtags == ["#new1", "#new2"]

    def test_update_other_fields_unchanged(self, db_path):
        bp = create_brand_profile("B", tone="casual", persona_name="Alex",
                                  db_path=db_path)
        update_brand_profile(bp.brand_profile_id, db_path=db_path, tone="serious")
        fetched = get_brand_profile(bp.brand_profile_id, db_path=db_path)
        assert fetched.persona_name == "Alex"

    def test_update_nonexistent_returns_true(self, db_path):
        # SQL UPDATE affecting zero rows is not an error
        ok = update_brand_profile("no_such", db_path=db_path, tone="x")
        assert ok is True

    def test_list_empty(self, db_path):
        assert list_brand_profiles(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        create_brand_profile("B1", db_path=db_path)
        create_brand_profile("B2", db_path=db_path)
        assert len(list_brand_profiles(db_path=db_path)) == 2

    def test_created_at_present(self, db_path):
        bp = create_brand_profile("B", db_path=db_path)
        assert "T" in bp.created_at

    def test_custom_id(self, db_path):
        bid = "custom-brand-id"
        bp = create_brand_profile("B", brand_profile_id=bid, db_path=db_path)
        assert bp.brand_profile_id == bid
        assert get_brand_profile(bid, db_path=db_path) is not None

    def test_bad_db_create_still_returns(self, tmp_path):
        bad = tmp_path / "bad_dir"; bad.mkdir()
        bp = create_brand_profile("B", db_path=bad)
        assert isinstance(bp, BrandProfile)

    def test_bad_db_get_returns_none(self, tmp_path):
        bad = tmp_path / "bad_dir"; bad.mkdir()
        assert get_brand_profile("x", db_path=bad) is None

    def test_bad_db_list_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"; bad.mkdir()
        assert list_brand_profiles(db_path=bad) == []


# ===========================================================================
# TestPublishingProfile
# ===========================================================================

class TestPublishingProfile:
    def test_create_returns_profile(self, db_path):
        pp = create_publishing_profile("P", db_path=db_path)
        assert isinstance(pp, PublishingProfile)

    def test_enabled_platforms_round_trip(self, db_path):
        pp = create_publishing_profile("P", enabled_platforms=["youtube", "tiktok"],
                                       db_path=db_path)
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert "youtube" in fetched.enabled_platforms
        assert "tiktok" in fetched.enabled_platforms

    def test_schedule_round_trip(self, db_path):
        pp = create_publishing_profile("P", schedule={"cadence": "weekly"},
                                       db_path=db_path)
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert fetched.schedule["cadence"] == "weekly"

    def test_default_tags_round_trip(self, db_path):
        pp = create_publishing_profile("P", default_tags=["tech"], db_path=db_path)
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert "tech" in fetched.default_tags

    def test_booleans_preserved(self, db_path):
        pp = create_publishing_profile("P", ai_disclosure_required=False,
                                       auto_thumbnail=False, auto_captions=True,
                                       db_path=db_path)
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert fetched.ai_disclosure_required is False
        assert fetched.auto_thumbnail is False
        assert fetched.auto_captions is True

    def test_update_enabled_platforms(self, db_path):
        pp = create_publishing_profile("P", enabled_platforms=["youtube"],
                                       db_path=db_path)
        update_publishing_profile(pp.publishing_profile_id, db_path=db_path,
                                  enabled_platforms=["youtube", "instagram"])
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert "instagram" in fetched.enabled_platforms

    def test_update_auto_thumbnail(self, db_path):
        pp = create_publishing_profile("P", auto_thumbnail=True, db_path=db_path)
        update_publishing_profile(pp.publishing_profile_id, db_path=db_path,
                                  auto_thumbnail=False)
        fetched = get_publishing_profile(pp.publishing_profile_id, db_path=db_path)
        assert fetched.auto_thumbnail is False

    def test_list_empty(self, db_path):
        assert list_publishing_profiles(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        create_publishing_profile("P1", db_path=db_path)
        create_publishing_profile("P2", db_path=db_path)
        assert len(list_publishing_profiles(db_path=db_path)) == 2


# ===========================================================================
# TestModelProfile
# ===========================================================================

class TestModelProfile:
    def test_create_returns_profile(self, db_path):
        mp = create_model_profile("M", db_path=db_path)
        assert isinstance(mp, ModelProfile)

    def test_defaults_applied(self, db_path):
        mp = create_model_profile("M", db_path=db_path)
        fetched = get_model_profile(mp.model_profile_id, db_path=db_path)
        assert fetched.episode_producer  == "anthropic/claude-sonnet-4.6"
        assert fetched.research_director == "anthropic/claude-opus-4.6"
        assert fetched.image_provider    == "muapi"

    def test_custom_slugs_round_trip(self, db_path):
        mp = create_model_profile("M",
             episode_producer="anthropic/claude-opus-4.6",
             db_path=db_path)
        fetched = get_model_profile(mp.model_profile_id, db_path=db_path)
        assert fetched.episode_producer == "anthropic/claude-opus-4.6"

    def test_narration_voice_id_round_trip(self, db_path):
        mp = create_model_profile("M", narration_voice_id="voice_abc", db_path=db_path)
        fetched = get_model_profile(mp.model_profile_id, db_path=db_path)
        assert fetched.narration_voice_id == "voice_abc"

    def test_narration_voice_id_none_by_default(self, db_path):
        mp = create_model_profile("M", db_path=db_path)
        assert get_model_profile(mp.model_profile_id, db_path=db_path).narration_voice_id is None

    def test_update_episode_producer(self, db_path):
        mp = create_model_profile("M", db_path=db_path)
        update_model_profile(mp.model_profile_id, db_path=db_path,
                             episode_producer="openai/gpt-4o")
        fetched = get_model_profile(mp.model_profile_id, db_path=db_path)
        assert fetched.episode_producer == "openai/gpt-4o"

    def test_update_unknown_field_ignored(self, db_path):
        mp = create_model_profile("M", db_path=db_path)
        ok = update_model_profile(mp.model_profile_id, db_path=db_path,
                                  nonexistent_field="value")
        assert ok is True  # should not fail, just ignore

    def test_list_empty(self, db_path):
        assert list_model_profiles(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        create_model_profile("M1", db_path=db_path)
        create_model_profile("M2", db_path=db_path)
        assert len(list_model_profiles(db_path=db_path)) == 2


# ===========================================================================
# TestChannelProfile
# ===========================================================================

class TestChannelProfile:
    def test_create_returns_profile(self, db_path):
        cp = create_channel_profile("ch1", "Channel One", db_path=db_path)
        assert isinstance(cp, ChannelProfile)
        assert cp.channel_id == "ch1"

    def test_persisted_to_db(self, db_path):
        create_channel_profile("ch1", "Channel One", db_path=db_path)
        fetched = get_channel_profile("ch1", db_path=db_path)
        assert fetched is not None
        assert fetched.name == "Channel One"

    def test_description_round_trip(self, db_path):
        create_channel_profile("ch1", "C", description="My desc", db_path=db_path)
        assert get_channel_profile("ch1", db_path=db_path).description == "My desc"

    def test_status_default_active(self, db_path):
        create_channel_profile("ch1", "C", db_path=db_path)
        assert get_channel_profile("ch1", db_path=db_path).status == "active"

    def test_profile_ids_stored(self, db_path):
        bp = create_brand_profile("B", db_path=db_path)
        pp = create_publishing_profile("P", db_path=db_path)
        mp = create_model_profile("M", db_path=db_path)
        create_channel_profile("ch1", "C",
                               brand_profile_id=bp.brand_profile_id,
                               publishing_profile_id=pp.publishing_profile_id,
                               model_profile_id=mp.model_profile_id,
                               db_path=db_path)
        fetched = get_channel_profile("ch1", db_path=db_path)
        assert fetched.brand_profile_id == bp.brand_profile_id
        assert fetched.publishing_profile_id == pp.publishing_profile_id
        assert fetched.model_profile_id == mp.model_profile_id

    def test_get_nonexistent_returns_none(self, db_path):
        assert get_channel_profile("no_such", db_path=db_path) is None

    def test_update_name(self, db_path):
        create_channel_profile("ch1", "Old Name", db_path=db_path)
        update_channel_profile("ch1", db_path=db_path, name="New Name")
        assert get_channel_profile("ch1", db_path=db_path).name == "New Name"

    def test_list_empty(self, db_path):
        assert list_channel_profiles(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        create_channel_profile("ch1", "C1", db_path=db_path)
        create_channel_profile("ch2", "C2", db_path=db_path)
        assert len(list_channel_profiles(db_path=db_path)) == 2

    def test_list_filter_by_status(self, db_path):
        create_channel_profile("ch1", "C1", db_path=db_path)
        create_channel_profile("ch2", "C2", db_path=db_path)
        deactivate_channel("ch2", db_path=db_path)
        active = list_channel_profiles(status="active", db_path=db_path)
        assert len(active) == 1
        assert active[0].channel_id == "ch1"

    def test_deactivate_sets_archived(self, db_path):
        create_channel_profile("ch1", "C1", db_path=db_path)
        deactivate_channel("ch1", db_path=db_path)
        assert get_channel_profile("ch1", db_path=db_path).status == "archived"

    def test_created_at_present(self, db_path):
        cp = create_channel_profile("ch1", "C", db_path=db_path)
        assert "T" in cp.created_at


# ===========================================================================
# TestValidateChannelConfig
# ===========================================================================

class TestValidateChannelConfig:
    def test_valid_config_no_errors(self):
        assert validate_channel_config(_FULL_CONFIG) == []

    def test_missing_channel_id(self):
        cfg = {**_FULL_CONFIG}; del cfg["channel_id"]
        errs = validate_channel_config(cfg)
        assert any("channel_id" in e for e in errs)

    def test_missing_name(self):
        cfg = {**_FULL_CONFIG}; del cfg["name"]
        errs = validate_channel_config(cfg)
        assert any("name" in e for e in errs)

    def test_missing_brand(self):
        cfg = {**_FULL_CONFIG}; del cfg["brand"]
        errs = validate_channel_config(cfg)
        assert any("brand" in e for e in errs)

    def test_missing_publishing(self):
        cfg = {**_FULL_CONFIG}; del cfg["publishing"]
        errs = validate_channel_config(cfg)
        assert any("publishing" in e for e in errs)

    def test_missing_models(self):
        cfg = {**_FULL_CONFIG}; del cfg["models"]
        errs = validate_channel_config(cfg)
        assert any("models" in e for e in errs)

    def test_unsupported_platform(self):
        cfg = {**_FULL_CONFIG, "publishing": {
            **_FULL_CONFIG["publishing"],
            "enabled_platforms": ["youtube", "myspace"],
        }}
        errs = validate_channel_config(cfg)
        assert any("myspace" in e for e in errs)

    def test_empty_channel_id(self):
        cfg = {**_FULL_CONFIG, "channel_id": "  "}
        errs = validate_channel_config(cfg)
        assert any("channel_id" in e for e in errs)

    def test_not_dict_returns_error(self):
        errs = validate_channel_config("not a dict")
        assert errs != []

    def test_missing_brand_language(self):
        cfg = {**_FULL_CONFIG, "brand": {k: v for k, v in _FULL_CONFIG["brand"].items()
                                          if k != "language"}}
        errs = validate_channel_config(cfg)
        assert any("language" in e for e in errs)


# ===========================================================================
# TestBootstrapChannel
# ===========================================================================

class TestBootstrapChannel:
    def test_returns_channel_profile(self, db_path):
        cp = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert isinstance(cp, ChannelProfile)

    def test_channel_id_matches(self, db_path):
        cp = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert cp.channel_id == "tech_ch_001"

    def test_channel_persisted(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cp = get_channel_profile("tech_ch_001", db_path=db_path)
        assert cp is not None

    def test_brand_profile_created(self, db_path):
        cp = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert cp.brand_profile_id is not None
        bp = get_brand_profile(cp.brand_profile_id, db_path=db_path)
        assert bp is not None
        assert bp.persona_name == "Alex Chen"

    def test_publishing_profile_created(self, db_path):
        cp = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert cp.publishing_profile_id is not None
        pp = get_publishing_profile(cp.publishing_profile_id, db_path=db_path)
        assert pp is not None
        assert "youtube" in pp.enabled_platforms

    def test_model_profile_created(self, db_path):
        cp = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert cp.model_profile_id is not None
        mp = get_model_profile(cp.model_profile_id, db_path=db_path)
        assert mp is not None
        assert mp.narration_voice_id == "EXAVITQu4vr4xnSDxMaL"

    def test_idempotent_same_channel_id(self, db_path):
        cp1 = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cp2 = bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        assert cp1.channel_id == cp2.channel_id
        # Only one channel profile row
        all_channels = list_channel_profiles(db_path=db_path)
        assert len(all_channels) == 1

    def test_raises_on_invalid_config(self, db_path):
        bad = {**_FULL_CONFIG}; del bad["name"]
        with pytest.raises(ValueError, match="Invalid channel config"):
            bootstrap_channel(bad, db_path=db_path)

    def test_minimal_config(self, db_path):
        """Bootstrap with only required fields + defaults."""
        minimal = {
            "channel_id": "min_ch",
            "name": "Minimal",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {},
        }
        cp = bootstrap_channel(minimal, db_path=db_path)
        assert cp.channel_id == "min_ch"

    def test_model_defaults_applied(self, db_path):
        minimal = {
            "channel_id": "def_ch",
            "name": "Defaults",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": []},
            "models": {},
        }
        cp = bootstrap_channel(minimal, db_path=db_path)
        mp = get_model_profile(cp.model_profile_id, db_path=db_path)
        assert mp.episode_producer == "anthropic/claude-sonnet-4.6"
        assert mp.image_provider   == "muapi"


# ===========================================================================
# TestLoadChannelConfig
# ===========================================================================

class TestLoadChannelConfig:
    def test_returns_channel_config(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cfg = load_channel_config("tech_ch_001", db_path=db_path)
        assert isinstance(cfg, ChannelConfig)

    def test_channel_field_set(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cfg = load_channel_config("tech_ch_001", db_path=db_path)
        assert cfg.channel.channel_id == "tech_ch_001"

    def test_brand_field_resolved(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cfg = load_channel_config("tech_ch_001", db_path=db_path)
        assert cfg.brand is not None
        assert cfg.brand.persona_name == "Alex Chen"
        assert cfg.brand.tone == "conversational"

    def test_publishing_field_resolved(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cfg = load_channel_config("tech_ch_001", db_path=db_path)
        assert cfg.publishing is not None
        assert "youtube" in cfg.publishing.enabled_platforms

    def test_models_field_resolved(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        cfg = load_channel_config("tech_ch_001", db_path=db_path)
        assert cfg.models is not None
        assert cfg.models.narration_voice_id == "EXAVITQu4vr4xnSDxMaL"

    def test_missing_channel_returns_none(self, db_path):
        assert load_channel_config("no_such", db_path=db_path) is None

    def test_channel_without_profiles_returns_nones(self, db_path):
        create_channel_profile("bare_ch", "Bare", db_path=db_path)
        cfg = load_channel_config("bare_ch", db_path=db_path)
        assert cfg is not None
        assert cfg.brand is None
        assert cfg.publishing is None
        assert cfg.models is None


# ===========================================================================
# TestChannelConfigSchema
# ===========================================================================

class TestChannelConfigSchema:
    def test_schema_is_dict(self):
        assert isinstance(CHANNEL_CONFIG_SCHEMA, dict)

    def test_schema_has_required(self):
        assert "required" in CHANNEL_CONFIG_SCHEMA
        assert "channel_id" in CHANNEL_CONFIG_SCHEMA["required"]
        assert "name" in CHANNEL_CONFIG_SCHEMA["required"]

    def test_schema_defines_brand(self):
        assert "brand" in CHANNEL_CONFIG_SCHEMA["properties"]

    def test_schema_defines_publishing(self):
        assert "publishing" in CHANNEL_CONFIG_SCHEMA["properties"]

    def test_schema_defines_models(self):
        assert "models" in CHANNEL_CONFIG_SCHEMA["properties"]

    def test_supported_platforms_has_five(self):
        assert len(SUPPORTED_PLATFORMS) == 5
        assert "youtube" in SUPPORTED_PLATFORMS
        assert "email" in SUPPORTED_PLATFORMS


# ===========================================================================
# TestMultiChannelConcurrent
# ===========================================================================

class TestMultiChannelConcurrent:
    def test_two_channels_independent(self, db_path):
        cfg_a = {**_FULL_CONFIG, "channel_id": "ch_a", "name": "Channel A"}
        cfg_b = {**_FULL_CONFIG, "channel_id": "ch_b", "name": "Channel B",
                 "brand": {**_FULL_CONFIG["brand"], "persona_name": "Bob"},
                 "models": {**_FULL_CONFIG["models"],
                            "narration_voice_id": "voice_bob"}}

        cp_a = bootstrap_channel(cfg_a, db_path=db_path)
        cp_b = bootstrap_channel(cfg_b, db_path=db_path)

        # Channels are distinct
        assert cp_a.channel_id != cp_b.channel_id

        # Brand profiles are distinct
        assert cp_a.brand_profile_id != cp_b.brand_profile_id

        # Brand data is isolated
        brand_a = get_brand_profile(cp_a.brand_profile_id, db_path=db_path)
        brand_b = get_brand_profile(cp_b.brand_profile_id, db_path=db_path)
        assert brand_a.persona_name == "Alex Chen"
        assert brand_b.persona_name == "Bob"

    def test_three_channels_in_list(self, db_path):
        for i in range(3):
            cfg = {**_FULL_CONFIG, "channel_id": f"ch_{i}", "name": f"Chan {i}"}
            bootstrap_channel(cfg, db_path=db_path)
        channels = list_channel_profiles(db_path=db_path)
        assert len(channels) == 3

    def test_deactivating_one_does_not_affect_others(self, db_path):
        cfg_a = {**_FULL_CONFIG, "channel_id": "ch_x", "name": "X"}
        cfg_b = {**_FULL_CONFIG, "channel_id": "ch_y", "name": "Y"}
        bootstrap_channel(cfg_a, db_path=db_path)
        bootstrap_channel(cfg_b, db_path=db_path)
        deactivate_channel("ch_x", db_path=db_path)
        active = list_channel_profiles(status="active", db_path=db_path)
        assert len(active) == 1
        assert active[0].channel_id == "ch_y"

    def test_model_profiles_not_shared(self, db_path):
        cfg_a = {**_FULL_CONFIG, "channel_id": "m_a", "name": "A"}
        cfg_b = {**_FULL_CONFIG, "channel_id": "m_b", "name": "B",
                 "models": {**_FULL_CONFIG["models"],
                            "episode_producer": "openai/gpt-4o"}}
        cp_a = bootstrap_channel(cfg_a, db_path=db_path)
        cp_b = bootstrap_channel(cfg_b, db_path=db_path)
        mp_a = get_model_profile(cp_a.model_profile_id, db_path=db_path)
        mp_b = get_model_profile(cp_b.model_profile_id, db_path=db_path)
        assert mp_a.episode_producer != mp_b.episode_producer


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block(self, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("channel_config opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_bootstrap_no_network(self, db_path, monkeypatch):
        self._block(monkeypatch)
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)

    def test_load_no_network(self, db_path, monkeypatch):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        self._block(monkeypatch)
        load_channel_config("tech_ch_001", db_path=db_path)

    def test_crud_no_network(self, db_path, monkeypatch):
        self._block(monkeypatch)
        bp = create_brand_profile("B", db_path=db_path)
        get_brand_profile(bp.brand_profile_id, db_path=db_path)
        list_brand_profiles(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        conn = get_connection(db_path)
        try:
            # episodes table may not exist in an isolated test DB — that's fine
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "episodes" in tables:
                n = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE episode_id='ep_000'"
                ).fetchone()[0]
                assert n == 0
        finally:
            conn.close()

    def test_no_runs_created(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_no_asset_versions_created(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_load_is_read_only(self, db_path):
        bootstrap_channel(_FULL_CONFIG, db_path=db_path)
        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM channel_profiles"
            ).fetchone()[0]
        finally:
            conn.close()
        load_channel_config("tech_ch_001", db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM channel_profiles"
            ).fetchone()[0]
        finally:
            conn.close()
        assert before == after


# ===========================================================================
# P1-01: TestBootstrapHypogeum
# Tests for the Hypogeum-specific bootstrap config used by
# scripts/bootstrap_channel.py. All writes go to a tmp_path DB.
# ===========================================================================

# The canonical Hypogeum bootstrap config, mirroring bootstrap_channel.py
# but with pinned test model slugs instead of reading from .env.
_HYPOGEUM_CONFIG = {
    "channel_id": "hypogeum",
    "name": "The Hypogeum Chronicles",
    "description": (
        "Ancient mysteries, hidden histories, and archaeological revelations. "
        "Investigation-led documentary content for history and archaeology enthusiasts."
    ),
    "brand": {
        "persona_name": "The Hypogeum Chronicles",
        "tone": "investigative, evidence-based, mystery-led",
        "target_audience": "history and archaeology enthusiasts, 25-55",
        "visual_style": (
            "warm earth tones, underground and ancient architecture, "
            "atmospheric darkness, archaeological sites and artefacts"
        ),
        "language": "en",
        "hashtags": [
            "#history", "#archaeology", "#ancienthistory",
            "#mystery", "#hiddenhistory", "#documentary",
        ],
        "color_palette": {
            "void_black":      "#080C0E",
            "deep_stone":      "#1A1D20",
            "charcoal_earth":  "#2B2E33",
            "ancient_soil":    "#5C4A2F",
            "antique_gold":    "#C49A42",
            "warm_gold":       "#E7D6B3",
            "parchment_cream": "#F5F3EE",
        },
        "typography": {
            "display_font": "Cinzel SemiBold",
            "body_font":    "Raleway Regular",
            "display_case": "ALL CAPS",
            "body_case":    "sentence case",
        },
        "voice_guidelines": (
            "Narrator: authoritative yet accessible. Investigation-led. "
            "No sensationalism. Evidence presented before conclusions."
        ),
        "content_pillars": [
            "ancient history",
            "archaeology",
            "historical mysteries",
            "hidden histories",
            "artefact investigation",
            "civilisation origins",
        ],
    },
    "publishing": {
        "enabled_platforms": ["youtube"],
        "schedule": {
            "cadence":     "weekly",
            "day_of_week": "thursday",
            "time_of_day": "18:00",
            "timezone":    "UTC",
        },
        "default_tags": [
            "history", "archaeology", "ancient history",
            "historical mysteries", "documentary", "hidden history",
        ],
        "ai_disclosure_required": True,
        "auto_thumbnail":          True,
        "auto_captions":           True,
    },
    "models": {
        "episode_producer":   "anthropic/claude-fable-5",
        "research_director":  "anthropic/claude-opus-4.6",
        "thumbnail_planner":  "openai/gpt-5.5-pro",
        "brand_guardian_p1":  "anthropic/claude-sonnet-4.6",
        "brand_guardian_p2":  "openai/gpt-5.5-pro",
        "narration_voice_id": "j9jfwdrw7BRfcR43Qohk",
        "image_provider":     "muapi",
        "video_provider":     "muapi",
    },
}


class TestBootstrapHypogeum:
    """
    P1-01 acceptance tests.

    All four sub-profile tables must have rows. The channel must be
    retrievable and fully resolved through load_channel_config().
    """

    def test_bootstrap_creates_channel(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        assert cp.channel_id == "hypogeum"
        assert cp.name == "The Hypogeum Chronicles"
        assert cp.status == "active"

    def test_bootstrap_is_idempotent(self, db_path):
        bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)  # second call must not raise
        channels = list_channel_profiles(db_path=db_path)
        assert len(channels) == 1, "Idempotent: exactly one channel after two bootstraps"

    def test_brand_profile_populated(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        assert cp.brand_profile_id is not None
        bp = get_brand_profile(cp.brand_profile_id, db_path=db_path)
        assert bp is not None
        assert bp.language == "en"
        assert "ancient history" in bp.content_pillars
        assert "#history" in bp.hashtags
        assert "void_black" in bp.color_palette
        assert bp.color_palette["antique_gold"] == "#C49A42"

    def test_publishing_profile_populated(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        assert cp.publishing_profile_id is not None
        pp = get_publishing_profile(cp.publishing_profile_id, db_path=db_path)
        assert pp is not None
        assert "youtube" in pp.enabled_platforms
        assert pp.ai_disclosure_required is True
        assert pp.auto_thumbnail is True
        assert pp.auto_captions is True

    def test_publishing_schedule_fields(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        pp = get_publishing_profile(cp.publishing_profile_id, db_path=db_path)
        assert pp is not None
        assert pp.schedule.get("cadence") == "weekly"
        assert pp.schedule.get("day_of_week") == "thursday"
        assert pp.schedule.get("time_of_day") == "18:00"

    def test_model_profile_populated(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        assert cp.model_profile_id is not None
        mp = get_model_profile(cp.model_profile_id, db_path=db_path)
        assert mp is not None
        assert mp.episode_producer == "anthropic/claude-fable-5"
        assert mp.research_director == "anthropic/claude-opus-4.6"
        assert mp.thumbnail_planner == "openai/gpt-5.5-pro"
        assert mp.brand_guardian_p1 == "anthropic/claude-sonnet-4.6"
        assert mp.brand_guardian_p2 == "openai/gpt-5.5-pro"
        assert mp.narration_voice_id == "j9jfwdrw7BRfcR43Qohk"
        assert mp.image_provider == "muapi"
        assert mp.video_provider == "muapi"

    def test_load_channel_config_resolves_all_profiles(self, db_path):
        bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        cc = load_channel_config("hypogeum", db_path=db_path)
        assert cc is not None
        assert cc.channel.channel_id == "hypogeum"
        assert cc.brand is not None
        assert cc.publishing is not None
        assert cc.models is not None

    def test_all_four_tables_have_rows(self, db_path):
        bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        conn = get_connection(db_path)
        try:
            for table in ("brand_profiles", "publishing_profiles",
                          "model_profiles", "channel_profiles"):
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                assert count >= 1, f"{table} must have at least 1 row after bootstrap"
        finally:
            conn.close()

    def test_channel_description_stored(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        fetched = get_channel_profile("hypogeum", db_path=db_path)
        assert fetched is not None
        assert "archaeology" in fetched.description.lower()

    def test_color_palette_has_all_seven_swatches(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        bp = get_brand_profile(cp.brand_profile_id, db_path=db_path)
        assert bp is not None
        expected_swatches = {
            "void_black", "deep_stone", "charcoal_earth",
            "ancient_soil", "antique_gold", "warm_gold", "parchment_cream",
        }
        assert expected_swatches == set(bp.color_palette.keys())

    def test_voice_id_is_pinned(self, db_path):
        cp = bootstrap_channel(_HYPOGEUM_CONFIG, db_path=db_path)
        mp = get_model_profile(cp.model_profile_id, db_path=db_path)
        assert mp is not None
        # Voice ID is pinned and irreversible per project memory
        assert mp.narration_voice_id == "j9jfwdrw7BRfcR43Qohk"
