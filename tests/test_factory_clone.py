"""
tests/test_factory_clone.py — Phase B: Factory Clone System tests

Coverage:
    TestDeepSubstitute       — string, dict, list, nested, missing keys, non-string
    TestBrandPackageSystem   — export, import, attach to channel, bad package_type, error paths
    TestIdentityPackageSystem — export, import, channel_id override, bad package_type, error paths
    TestChannelTemplateSystem — save, get, list, render, missing template, overwrite
    TestChannelDuplicator    — clone copies all profiles, overrides applied, no episode copy,
                               idempotent on existing new_id, source not found returns None
    TestCloneFactory         — one-click clone, brand/model/publishing overrides, no source → None
    TestCreateChannel        — alias for bootstrap_channel, creates full profile set
    TestNonBlocking          — all public functions return safe defaults on bad db
    TestNoNetworkCalls       — no sockets opened during any operation
    TestNoProductionSideEffects — no ep_000, no episode state written
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import (
    bootstrap_channel,
    create_brand_profile,
    get_brand_profile,
    get_channel_profile,
    load_channel_config,
)
from hypogeum.factory_clone import (
    ChannelTemplate,
    _deep_substitute,
    clone_factory,
    create_channel,
    duplicate_channel,
    export_brand_package,
    export_identity_package,
    get_channel_template,
    import_brand_package,
    import_identity_package,
    list_channel_templates,
    render_channel_template,
    save_channel_template,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "factory_clone_test.db"
    initialize_schema(p)
    return p


_FULL_CONFIG = {
    "channel_id":  "src_ch_001",
    "name":        "Source Channel",
    "description": "A template channel for cloning",
    "brand": {
        "persona_name":    "Sam Doe",
        "tone":            "authoritative",
        "target_audience": "developers 25-40",
        "visual_style":    "dark minimal",
        "language":        "en",
        "hashtags":        ["#tech", "#ai"],
        "color_palette":   {"primary": "#111111"},
        "typography":      {"headline": "Inter"},
        "voice_guidelines":"Speak clearly.",
        "content_pillars": ["AI", "SWE"],
    },
    "publishing": {
        "enabled_platforms":     ["youtube"],
        "schedule":              {"cadence": "weekly"},
        "default_tags":          ["tech"],
        "ai_disclosure_required": True,
        "auto_thumbnail":        True,
        "auto_captions":         True,
    },
    "models": {
        "episode_producer":   "anthropic/claude-sonnet-4.6",
        "research_director":  "anthropic/claude-opus-4.6",
        "thumbnail_planner":  "anthropic/claude-sonnet-4.6",
        "brand_guardian_p1":  "anthropic/claude-sonnet-4.6",
        "brand_guardian_p2":  "openai/gpt-5.5-pro",
        "narration_voice_id": "voice_abc",
        "image_provider":     "muapi",
        "video_provider":     "muapi",
    },
}


@pytest.fixture()
def source_channel(db_path):
    """Bootstrap the source channel for cloning tests."""
    return bootstrap_channel(_FULL_CONFIG, db_path=db_path)


# ===========================================================================
# TestDeepSubstitute
# ===========================================================================

class TestDeepSubstitute:
    def test_simple_string_substitution(self):
        result = _deep_substitute("Hello {name}!", {"name": "World"})
        assert result == "Hello World!"

    def test_multiple_vars_in_one_string(self):
        result = _deep_substitute("{greeting} {name}!", {"greeting": "Hi", "name": "Alice"})
        assert result == "Hi Alice!"

    def test_missing_key_left_as_is(self):
        result = _deep_substitute("Hello {missing}!", {})
        assert result == "Hello {missing}!"

    def test_dict_values_substituted(self):
        obj = {"audience": "engineers in {region}"}
        result = _deep_substitute(obj, {"region": "Europe"})
        assert result["audience"] == "engineers in Europe"

    def test_dict_keys_not_substituted(self):
        obj = {"{key}": "value"}
        result = _deep_substitute(obj, {"key": "replaced"})
        # Keys are not substituted — only values
        assert "{key}" in result

    def test_list_items_substituted(self):
        obj = ["#tech_{lang}", "#ai"]
        result = _deep_substitute(obj, {"lang": "en"})
        assert result[0] == "#tech_en"
        assert result[1] == "#ai"

    def test_nested_dict_and_list(self):
        obj = {"brand": {"tags": ["#{tag}", "#ai"], "tone": "{tone}"}}
        result = _deep_substitute(obj, {"tag": "code", "tone": "casual"})
        assert result["brand"]["tags"][0] == "#code"
        assert result["brand"]["tone"] == "casual"

    def test_non_string_values_untouched(self):
        obj = {"count": 42, "flag": True, "ratio": 3.14, "none": None}
        result = _deep_substitute(obj, {"count": "X"})
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["ratio"] == 3.14
        assert result["none"] is None

    def test_empty_variables_leaves_placeholders(self):
        result = _deep_substitute("{a} and {b}", {})
        assert result == "{a} and {b}"

    def test_no_placeholders_string_unchanged(self):
        result = _deep_substitute("no placeholders here", {"a": "b"})
        assert result == "no placeholders here"

    def test_partial_substitution(self):
        result = _deep_substitute("{a} and {b}", {"a": "one"})
        assert result == "one and {b}"

    def test_numeric_value_substitution(self):
        result = _deep_substitute("{n}", {"n": 99})
        assert result == "99"


# ===========================================================================
# TestBrandPackageSystem
# ===========================================================================

class TestBrandPackageSystem:
    def test_export_brand_package_returns_dict(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert isinstance(pkg, dict)

    def test_export_package_type_is_brand(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert pkg.get("package_type") == "brand"

    def test_export_package_version(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert pkg.get("version") == "v1"

    def test_export_package_contains_channel_id(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert pkg.get("channel_id") == "src_ch_001"

    def test_export_package_contains_brand_data(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert "brand" in pkg
        assert isinstance(pkg["brand"], dict)

    def test_export_brand_persona_name_preserved(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert pkg["brand"]["persona_name"] == "Sam Doe"

    def test_export_brand_tone_preserved(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        assert pkg["brand"]["tone"] == "authoritative"

    def test_export_missing_channel_returns_empty(self, db_path):
        pkg = export_brand_package("nonexistent_ch", db_path=db_path)
        assert pkg == {}

    def test_export_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        pkg = export_brand_package("x", db_path=bad)
        assert pkg == {}

    def test_import_brand_package_returns_brand_profile(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        bp = import_brand_package(pkg, db_path=db_path)
        assert bp is not None
        assert bp.brand_profile_id

    def test_import_brand_package_data_correct(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        bp = import_brand_package(pkg, db_path=db_path)
        assert bp.persona_name == "Sam Doe"
        assert bp.tone == "authoritative"

    def test_import_brand_package_creates_independent_record(self, db_path, source_channel):
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        bp = import_brand_package(pkg, db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert bp.brand_profile_id != src_cfg.brand.brand_profile_id

    def test_import_attaches_to_channel_when_channel_id_given(self, db_path, source_channel):
        # Create a new channel with required fields
        new_cfg = {
            "channel_id": "bare_ch",
            "name": "Bare",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {},
        }
        bootstrap_channel(new_cfg, db_path=db_path)
        pkg = export_brand_package("src_ch_001", db_path=db_path)
        bp = import_brand_package(pkg, channel_id="bare_ch", db_path=db_path)
        loaded = load_channel_config("bare_ch", db_path=db_path)
        assert loaded.channel.brand_profile_id == bp.brand_profile_id

    def test_import_wrong_package_type_returns_none(self, db_path):
        pkg = {"package_type": "identity", "brand": {}}
        bp = import_brand_package(pkg, db_path=db_path)
        assert bp is None

    def test_import_missing_package_type_returns_none(self, db_path):
        bp = import_brand_package({}, db_path=db_path)
        assert bp is None

    def test_import_bad_db_is_nonblocking(self, tmp_path):
        # create_brand_profile is non-blocking — returns a profile even if DB write fails;
        # import_brand_package inherits that contract and must not raise
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        pkg = {"package_type": "brand", "brand": {"language": "en"}}
        result = import_brand_package(pkg, db_path=bad)
        assert result is None or hasattr(result, "brand_profile_id")


# ===========================================================================
# TestIdentityPackageSystem
# ===========================================================================

class TestIdentityPackageSystem:
    def test_export_identity_package_returns_dict(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert isinstance(pkg, dict)

    def test_export_package_type_is_identity(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert pkg.get("package_type") == "identity"

    def test_export_package_version(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert pkg.get("version") == "v1"

    def test_export_identity_has_channel_section(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert "channel" in pkg
        assert pkg["channel"]["name"] == "Source Channel"

    def test_export_identity_has_brand_section(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert "brand" in pkg
        assert pkg["brand"]["persona_name"] == "Sam Doe"

    def test_export_identity_has_publishing_section(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert "publishing" in pkg
        assert "youtube" in pkg["publishing"]["enabled_platforms"]

    def test_export_identity_has_models_section(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        assert "models" in pkg
        assert pkg["models"]["image_provider"] == "muapi"

    def test_export_missing_channel_returns_empty(self, db_path):
        pkg = export_identity_package("nonexistent_ch", db_path=db_path)
        assert pkg == {}

    def test_export_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        pkg = export_identity_package("x", db_path=bad)
        assert pkg == {}

    def test_import_identity_package_returns_channel_profile(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        ch = import_identity_package(pkg, channel_id="imported_ch", db_path=db_path)
        assert ch is not None
        assert ch.channel_id == "imported_ch"

    def test_import_identity_uses_package_channel_id_if_not_overridden(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        # Modify channel_id in the package so we don't collide with source
        pkg["channel_id"] = "from_package_id"
        ch = import_identity_package(pkg, db_path=db_path)
        assert ch is not None
        assert ch.channel_id == "from_package_id"

    def test_import_identity_channel_id_override_takes_precedence(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        ch = import_identity_package(pkg, channel_id="override_ch", db_path=db_path)
        assert ch.channel_id == "override_ch"

    def test_import_identity_creates_independent_channel(self, db_path, source_channel):
        pkg = export_identity_package("src_ch_001", db_path=db_path)
        ch = import_identity_package(pkg, channel_id="clone_ch", db_path=db_path)
        assert ch.channel_id != "src_ch_001"

    def test_import_wrong_package_type_returns_none(self, db_path):
        pkg = {"package_type": "brand", "channel": {}}
        ch = import_identity_package(pkg, db_path=db_path)
        assert ch is None

    def test_import_missing_package_type_returns_none(self, db_path):
        ch = import_identity_package({}, db_path=db_path)
        assert ch is None

    def test_import_bad_db_is_nonblocking(self, tmp_path):
        # Underlying profile functions are non-blocking (absorb DB errors);
        # import_identity_package must not raise regardless of DB state
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        pkg = {"package_type": "identity", "channel": {"name": "X"}, "channel_id": "z"}
        result = import_identity_package(pkg, db_path=bad)
        assert result is None or hasattr(result, "channel_id")


# ===========================================================================
# TestChannelTemplateSystem
# ===========================================================================

class TestChannelTemplateSystem:
    def test_save_returns_channel_template(self, db_path):
        tmpl = save_channel_template("t1", {"name": "{channel_name}"}, db_path=db_path)
        assert isinstance(tmpl, ChannelTemplate)

    def test_save_template_has_id(self, db_path):
        tmpl = save_channel_template("t1", {}, db_path=db_path)
        assert tmpl.template_id

    def test_save_template_name_stored(self, db_path):
        save_channel_template("my_template", {}, db_path=db_path)
        tmpl = get_channel_template("my_template", db_path=db_path)
        assert tmpl is not None
        assert tmpl.name == "my_template"

    def test_save_template_description_stored(self, db_path):
        save_channel_template("t2", {}, description="A test template", db_path=db_path)
        tmpl = get_channel_template("t2", db_path=db_path)
        assert tmpl.description == "A test template"

    def test_save_template_config_round_trip(self, db_path):
        cfg = {"name": "{channel_name}", "brand": {"tone": "{tone}"}}
        save_channel_template("t3", cfg, db_path=db_path)
        tmpl = get_channel_template("t3", db_path=db_path)
        assert tmpl.config() == cfg

    def test_get_missing_template_returns_none(self, db_path):
        assert get_channel_template("doesnotexist", db_path=db_path) is None

    def test_get_template_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert get_channel_template("x", db_path=bad) is None

    def test_list_templates_empty_returns_empty_list(self, db_path):
        result = list_channel_templates(db_path=db_path)
        assert result == []

    def test_list_templates_returns_all(self, db_path):
        save_channel_template("alpha", {}, db_path=db_path)
        save_channel_template("beta", {}, db_path=db_path)
        result = list_channel_templates(db_path=db_path)
        names = [t.name for t in result]
        assert "alpha" in names
        assert "beta" in names

    def test_list_templates_ordered_by_name(self, db_path):
        save_channel_template("zzz", {}, db_path=db_path)
        save_channel_template("aaa", {}, db_path=db_path)
        result = list_channel_templates(db_path=db_path)
        assert result[0].name == "aaa"
        assert result[1].name == "zzz"

    def test_list_templates_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert list_channel_templates(db_path=bad) == []

    def test_save_overwrites_existing_name(self, db_path):
        save_channel_template("overwrite_me", {"v": 1}, db_path=db_path)
        save_channel_template("overwrite_me", {"v": 2}, db_path=db_path)
        tmpl = get_channel_template("overwrite_me", db_path=db_path)
        assert tmpl.config()["v"] == 2

    def test_render_substitutes_variables(self, db_path):
        cfg = {"name": "{channel_name}", "brand": {"tone": "{tone}"}}
        save_channel_template("render_tmpl", cfg, db_path=db_path)
        rendered = render_channel_template(
            "render_tmpl", {"channel_name": "Tech Show", "tone": "casual"}, db_path=db_path
        )
        assert rendered["name"] == "Tech Show"
        assert rendered["brand"]["tone"] == "casual"

    def test_render_missing_variable_left_as_is(self, db_path):
        cfg = {"name": "{channel_name}", "brand": {"tone": "{tone}"}}
        save_channel_template("partial_tmpl", cfg, db_path=db_path)
        rendered = render_channel_template(
            "partial_tmpl", {"channel_name": "Tech Show"}, db_path=db_path
        )
        assert rendered["name"] == "Tech Show"
        assert rendered["brand"]["tone"] == "{tone}"

    def test_render_missing_template_returns_empty(self, db_path):
        rendered = render_channel_template("no_such_template", {}, db_path=db_path)
        assert rendered == {}

    def test_channel_template_config_method(self, db_path):
        tmpl = save_channel_template("cfg_test", {"k": "v"}, db_path=db_path)
        assert tmpl.config() == {"k": "v"}

    def test_channel_template_bad_json_config_returns_empty_dict(self):
        tmpl = ChannelTemplate(template_id="x", name="x", config_json="INVALID JSON")
        assert tmpl.config() == {}


# ===========================================================================
# TestChannelDuplicator
# ===========================================================================

class TestChannelDuplicator:
    def test_duplicate_channel_returns_channel_profile(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_001", "Clone One", db_path=db_path)
        assert new_ch is not None
        assert new_ch.channel_id == "clone_001"

    def test_duplicate_channel_name_set(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_002", "Clone Two", db_path=db_path)
        assert new_ch.name == "Clone Two"

    def test_duplicate_creates_independent_brand(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_003", "Clone Three", db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert new_ch.brand_profile_id != src_cfg.brand.brand_profile_id

    def test_duplicate_brand_data_copied(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_004", "Clone Four", db_path=db_path)
        cfg = load_channel_config("clone_004", db_path=db_path)
        assert cfg.brand.persona_name == "Sam Doe"
        assert cfg.brand.tone == "authoritative"

    def test_duplicate_creates_independent_publishing(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_005", "Clone Five", db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert new_ch.publishing_profile_id != src_cfg.publishing.publishing_profile_id

    def test_duplicate_publishing_data_copied(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_006", "Clone Six", db_path=db_path)
        cfg = load_channel_config("clone_006", db_path=db_path)
        assert "youtube" in cfg.publishing.enabled_platforms

    def test_duplicate_creates_independent_models(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_007", "Clone Seven", db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert new_ch.model_profile_id != src_cfg.models.model_profile_id

    def test_duplicate_model_data_copied(self, db_path, source_channel):
        new_ch = duplicate_channel("src_ch_001", "clone_008", "Clone Eight", db_path=db_path)
        cfg = load_channel_config("clone_008", db_path=db_path)
        assert cfg.models.narration_voice_id == "voice_abc"
        assert cfg.models.image_provider == "muapi"

    def test_duplicate_source_channel_unchanged(self, db_path, source_channel):
        duplicate_channel("src_ch_001", "clone_009", "Clone Nine", db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert src_cfg.brand.persona_name == "Sam Doe"
        assert src_cfg.channel.name == "Source Channel"

    def test_duplicate_with_brand_override(self, db_path, source_channel):
        new_ch = duplicate_channel(
            "src_ch_001", "clone_010", "Clone Ten",
            overrides={"brand": {"tone": "casual", "persona_name": "New Host"}},
            db_path=db_path,
        )
        cfg = load_channel_config("clone_010", db_path=db_path)
        assert cfg.brand.tone == "casual"
        assert cfg.brand.persona_name == "New Host"
        # Non-overridden brand fields preserved
        assert cfg.brand.target_audience == "developers 25-40"

    def test_duplicate_with_publishing_override(self, db_path, source_channel):
        new_ch = duplicate_channel(
            "src_ch_001", "clone_011", "Clone Eleven",
            overrides={"publishing": {"enabled_platforms": ["tiktok", "instagram"]}},
            db_path=db_path,
        )
        cfg = load_channel_config("clone_011", db_path=db_path)
        assert "tiktok" in cfg.publishing.enabled_platforms
        assert "youtube" not in cfg.publishing.enabled_platforms

    def test_duplicate_with_model_override(self, db_path, source_channel):
        new_ch = duplicate_channel(
            "src_ch_001", "clone_012", "Clone Twelve",
            overrides={"models": {"narration_voice_id": "new_voice_xyz"}},
            db_path=db_path,
        )
        cfg = load_channel_config("clone_012", db_path=db_path)
        assert cfg.models.narration_voice_id == "new_voice_xyz"

    def test_duplicate_nonexistent_source_returns_none(self, db_path):
        result = duplicate_channel("does_not_exist", "clone_x", "Clone X", db_path=db_path)
        assert result is None

    def test_duplicate_existing_new_id_returns_existing(self, db_path, source_channel):
        # First clone
        first = duplicate_channel("src_ch_001", "clone_idem", "Clone Idem", db_path=db_path)
        # Second call with same new_channel_id → idempotent, returns existing
        second = duplicate_channel("src_ch_001", "clone_idem", "Clone Idem 2", db_path=db_path)
        assert second is not None
        assert second.channel_id == "clone_idem"

    def test_duplicate_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        result = duplicate_channel("src", "dst", "Dst", db_path=bad)
        assert result is None

    def test_duplicate_no_episode_history_copied(self, db_path, source_channel):
        """Episode assignments must NOT be copied during duplication."""
        new_ch = duplicate_channel("src_ch_001", "clone_ep", "Clone Ep", db_path=db_path)
        conn = get_connection(db_path)
        try:
            # channel_episodes table may not exist yet; just verify clone exists
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_episodes'"
            ).fetchone()
            if rows:
                ep_rows = conn.execute(
                    "SELECT * FROM channel_episodes WHERE channel_id=?", ["clone_ep"]
                ).fetchall()
                assert len(ep_rows) == 0
        finally:
            conn.close()


# ===========================================================================
# TestCloneFactory
# ===========================================================================

class TestCloneFactory:
    def test_clone_factory_returns_channel_profile(self, db_path, source_channel):
        ch = clone_factory("src_ch_001", "factory_clone_001", "Factory Clone", db_path=db_path)
        assert ch is not None
        assert ch.channel_id == "factory_clone_001"

    def test_clone_factory_name_set(self, db_path, source_channel):
        ch = clone_factory("src_ch_001", "factory_clone_002", "My Clone", db_path=db_path)
        assert ch.name == "My Clone"

    def test_clone_factory_no_overrides_copies_all(self, db_path, source_channel):
        ch = clone_factory("src_ch_001", "factory_clone_003", "Full Clone", db_path=db_path)
        cfg = load_channel_config("factory_clone_003", db_path=db_path)
        assert cfg.brand.persona_name == "Sam Doe"
        assert cfg.models.narration_voice_id == "voice_abc"
        assert "youtube" in cfg.publishing.enabled_platforms

    def test_clone_factory_brand_overrides_applied(self, db_path, source_channel):
        ch = clone_factory(
            "src_ch_001", "factory_clone_004", "Brand Override Clone",
            brand_overrides={"persona_name": "New Host", "tone": "playful"},
            db_path=db_path,
        )
        cfg = load_channel_config("factory_clone_004", db_path=db_path)
        assert cfg.brand.persona_name == "New Host"
        assert cfg.brand.tone == "playful"

    def test_clone_factory_model_overrides_applied(self, db_path, source_channel):
        ch = clone_factory(
            "src_ch_001", "factory_clone_005", "Model Override Clone",
            model_overrides={"narration_voice_id": "override_voice"},
            db_path=db_path,
        )
        cfg = load_channel_config("factory_clone_005", db_path=db_path)
        assert cfg.models.narration_voice_id == "override_voice"

    def test_clone_factory_publishing_overrides_applied(self, db_path, source_channel):
        ch = clone_factory(
            "src_ch_001", "factory_clone_006", "Publishing Override Clone",
            publishing_overrides={"enabled_platforms": ["tiktok"]},
            db_path=db_path,
        )
        cfg = load_channel_config("factory_clone_006", db_path=db_path)
        assert "tiktok" in cfg.publishing.enabled_platforms
        assert "youtube" not in cfg.publishing.enabled_platforms

    def test_clone_factory_all_overrides_combined(self, db_path, source_channel):
        ch = clone_factory(
            "src_ch_001", "factory_clone_007", "Full Override Clone",
            brand_overrides={"tone": "casual"},
            model_overrides={"narration_voice_id": "v2"},
            publishing_overrides={"enabled_platforms": ["instagram"]},
            db_path=db_path,
        )
        cfg = load_channel_config("factory_clone_007", db_path=db_path)
        assert cfg.brand.tone == "casual"
        assert cfg.models.narration_voice_id == "v2"
        assert "instagram" in cfg.publishing.enabled_platforms

    def test_clone_factory_creates_independent_profiles(self, db_path, source_channel):
        ch = clone_factory("src_ch_001", "factory_clone_008", "Ind Clone", db_path=db_path)
        src_cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert ch.brand_profile_id != src_cfg.brand.brand_profile_id
        assert ch.model_profile_id != src_cfg.models.model_profile_id
        assert ch.publishing_profile_id != src_cfg.publishing.publishing_profile_id

    def test_clone_factory_source_not_found_returns_none(self, db_path):
        ch = clone_factory("no_such_source", "factory_clone_x", "X", db_path=db_path)
        assert ch is None

    def test_clone_factory_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        ch = clone_factory("src", "dst", "Dst", db_path=bad)
        assert ch is None


# ===========================================================================
# TestCreateChannel
# ===========================================================================

_MINIMAL_CONFIG_BASE = {
    "brand": {"language": "en"},
    "publishing": {"enabled_platforms": ["youtube"]},
    "models": {},
}


class TestCreateChannel:
    def test_create_channel_returns_channel_profile(self, db_path):
        cfg = {"channel_id": "cc_001", "name": "Created Channel", **_MINIMAL_CONFIG_BASE}
        ch = create_channel(cfg, db_path=db_path)
        assert ch is not None
        assert ch.channel_id == "cc_001"

    def test_create_channel_persists(self, db_path):
        cfg = {"channel_id": "cc_002", "name": "Persisted Channel", **_MINIMAL_CONFIG_BASE}
        create_channel(cfg, db_path=db_path)
        fetched = get_channel_profile("cc_002", db_path=db_path)
        assert fetched is not None
        assert fetched.name == "Persisted Channel"

    def test_create_channel_idempotent(self, db_path):
        cfg = {"channel_id": "cc_003", "name": "Idempotent Channel", **_MINIMAL_CONFIG_BASE}
        ch1 = create_channel(cfg, db_path=db_path)
        ch2 = create_channel(cfg, db_path=db_path)
        assert ch1.channel_id == ch2.channel_id

    def test_create_channel_with_full_config(self, db_path):
        full = dict(_FULL_CONFIG)
        full["channel_id"] = "cc_full"
        full["name"] = "Full Created Channel"
        ch = create_channel(full, db_path=db_path)
        cfg = load_channel_config("cc_full", db_path=db_path)
        assert cfg.brand.persona_name == "Sam Doe"
        assert cfg.models.image_provider == "muapi"


# ===========================================================================
# TestNonBlocking
# ===========================================================================

class TestNonBlocking:
    def _bad_db(self, tmp_path):
        p = tmp_path / "bad_dir"
        p.mkdir()
        return p

    def test_export_brand_package_bad_db_returns_empty(self, tmp_path):
        assert export_brand_package("x", db_path=self._bad_db(tmp_path)) == {}

    def test_export_identity_package_bad_db_returns_empty(self, tmp_path):
        assert export_identity_package("x", db_path=self._bad_db(tmp_path)) == {}

    def test_import_brand_package_bad_db_nonblocking(self, tmp_path):
        # Underlying create_brand_profile absorbs DB errors; must not raise
        pkg = {"package_type": "brand", "brand": {"language": "en"}}
        result = import_brand_package(pkg, db_path=self._bad_db(tmp_path))
        assert result is None or hasattr(result, "brand_profile_id")

    def test_import_identity_package_bad_db_nonblocking(self, tmp_path):
        # Underlying profile functions absorb DB errors; must not raise
        pkg = {"package_type": "identity", "channel": {"name": "X"}, "channel_id": "z"}
        result = import_identity_package(pkg, db_path=self._bad_db(tmp_path))
        assert result is None or hasattr(result, "channel_id")

    def test_save_channel_template_bad_db_returns_template(self, tmp_path):
        # Non-blocking: returns template object even if DB write failed
        tmpl = save_channel_template("t", {}, db_path=self._bad_db(tmp_path))
        assert isinstance(tmpl, ChannelTemplate)
        assert tmpl.name == "t"

    def test_get_channel_template_bad_db_returns_none(self, tmp_path):
        assert get_channel_template("x", db_path=self._bad_db(tmp_path)) is None

    def test_list_channel_templates_bad_db_returns_empty(self, tmp_path):
        assert list_channel_templates(db_path=self._bad_db(tmp_path)) == []

    def test_render_channel_template_bad_db_returns_empty(self, tmp_path):
        assert render_channel_template("x", {}, db_path=self._bad_db(tmp_path)) == {}

    def test_duplicate_channel_bad_db_returns_none(self, tmp_path):
        assert duplicate_channel("src", "dst", "Dst", db_path=self._bad_db(tmp_path)) is None

    def test_clone_factory_bad_db_returns_none(self, tmp_path):
        assert clone_factory("src", "dst", "Dst", db_path=self._bad_db(tmp_path)) is None


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    """Block socket creation — any network call will raise an error."""

    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        original = socket.socket

        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected in factory_clone tests")

        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_export_brand_package_no_network(self, db_path, source_channel):
        export_brand_package("src_ch_001", db_path=db_path)

    def test_import_brand_package_no_network(self, db_path, source_channel):
        pkg = {"package_type": "brand", "brand": {"language": "en"}}
        import_brand_package(pkg, db_path=db_path)

    def test_export_identity_package_no_network(self, db_path, source_channel):
        export_identity_package("src_ch_001", db_path=db_path)

    def test_import_identity_package_no_network(self, db_path, source_channel):
        pkg = {"package_type": "identity", "channel": {"name": "X"}, "channel_id": "nw_ch"}
        import_identity_package(pkg, db_path=db_path)

    def test_save_channel_template_no_network(self, db_path):
        save_channel_template("nw_tmpl", {}, db_path=db_path)

    def test_get_channel_template_no_network(self, db_path):
        get_channel_template("nw_tmpl", db_path=db_path)

    def test_list_channel_templates_no_network(self, db_path):
        list_channel_templates(db_path=db_path)

    def test_render_channel_template_no_network(self, db_path):
        render_channel_template("nw_tmpl", {}, db_path=db_path)

    def test_duplicate_channel_no_network(self, db_path, source_channel):
        duplicate_channel("src_ch_001", "nw_clone", "NW Clone", db_path=db_path)

    def test_clone_factory_no_network(self, db_path, source_channel):
        clone_factory("src_ch_001", "nw_factory_clone", "NW Factory", db_path=db_path)

    def test_create_channel_no_network(self, db_path):
        cfg = {"channel_id": "nw_create", "name": "NW Create",
               "brand": {"language": "en"},
               "publishing": {"enabled_platforms": ["youtube"]},
               "models": {}}
        create_channel(cfg, db_path=db_path)

    def test_deep_substitute_no_network(self):
        _deep_substitute({"k": "{v}"}, {"v": "x"})


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path, source_channel, tmp_path):
        clone_factory("src_ch_001", "side_effect_ch", "Side Effect", db_path=db_path)
        # ep_000 must not exist in tmp_path or project root
        ep_in_tmp = list(tmp_path.rglob("ep_000"))
        assert ep_in_tmp == []

    def test_no_episode_state_written(self, db_path, source_channel):
        duplicate_channel("src_ch_001", "ep_state_ch", "EP State", db_path=db_path)
        conn = get_connection(db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "stage_executions" in tables:
                rows = conn.execute("SELECT * FROM stage_executions").fetchall()
                assert len(rows) == 0
            if "asset_versions" in tables:
                rows = conn.execute("SELECT * FROM asset_versions").fetchall()
                assert len(rows) == 0
        finally:
            conn.close()

    def test_multiple_clones_independent(self, db_path, source_channel):
        ch_a = clone_factory("src_ch_001", "ind_ch_a", "Channel A", db_path=db_path)
        ch_b = clone_factory("src_ch_001", "ind_ch_b", "Channel B", db_path=db_path)
        assert ch_a.channel_id != ch_b.channel_id
        assert ch_a.brand_profile_id != ch_b.brand_profile_id
        assert ch_a.model_profile_id != ch_b.model_profile_id
        assert ch_a.publishing_profile_id != ch_b.publishing_profile_id

    def test_source_channel_unchanged_after_operations(self, db_path, source_channel):
        export_brand_package("src_ch_001", db_path=db_path)
        export_identity_package("src_ch_001", db_path=db_path)
        clone_factory("src_ch_001", "src_unchanged_clone", "Clone", db_path=db_path)
        cfg = load_channel_config("src_ch_001", db_path=db_path)
        assert cfg.channel.name == "Source Channel"
        assert cfg.brand.persona_name == "Sam Doe"
