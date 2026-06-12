"""
tests/test_channel_manager.py — Multi-Channel Architecture tests

Coverage:
    TestChannelSchema           — ChannelIdentity / Channel / ChannelTemplate fields
    TestCreateChannel           — persists row, defaults, returns Channel
    TestGetChannel              — found / not found / bad DB
    TestListChannels            — all, platform filter, order
    TestUpdateIdentity          — patch fields, unknown key → extra, no source = False
    TestChannelTemplateSystem   — create, get, filter by type
    TestChannelDuplicator       — copies identity + templates, no episode copy
    TestIdentityEditor          — non-destructive, only patched fields change
    TestPromptRegeneration      — injects identity, missing fields omitted,
                                  no channel = base prompt unchanged
    TestChannelIsolation        — assign_episode idempotent, get_channel_episodes
    TestEmptyDB                 — all functions tolerate empty tables
    TestNoNetworkCalls          — no sockets opened
    TestNoProductionSideEffects — no ep_000, read functions are read-only
"""

from __future__ import annotations

import socket
import uuid

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_manager import (
    Channel,
    ChannelIdentity,
    ChannelTemplate,
    assign_episode_to_channel,
    create_channel,
    create_template,
    duplicate_channel,
    get_channel,
    get_channel_episodes,
    get_templates,
    list_channels,
    regenerate_prompt,
    update_identity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "channel_test.db"
    initialize_schema(p)
    return p


# ===========================================================================
# TestChannelSchema
# ===========================================================================

class TestChannelSchema:
    def test_channel_identity_defaults(self):
        ci = ChannelIdentity()
        assert ci.language == "en"
        assert ci.hashtags == []
        assert ci.extra == {}
        assert ci.persona_name is None

    def test_channel_identity_fields(self):
        ci = ChannelIdentity(
            persona_name="Alex", tone="casual",
            target_audience="gen z", visual_style="bright",
            language="fr", hashtags=["#history"], extra={"k": "v"},
        )
        assert ci.persona_name == "Alex"
        assert ci.tone == "casual"
        assert ci.language == "fr"
        assert "#history" in ci.hashtags
        assert ci.extra["k"] == "v"

    def test_channel_defaults(self):
        ch = Channel(channel_id="c1", name="Test", platform="youtube")
        assert ch.active is True
        assert isinstance(ch.identity, ChannelIdentity)
        assert ch.metadata_version == "v1"

    def test_channel_template_defaults(self):
        t = ChannelTemplate(
            template_id="t1", channel_id="c1",
            template_type="hook", content="Start with...",
        )
        assert t.metadata_version == "v1"
        assert t.created_at == ""


# ===========================================================================
# TestCreateChannel
# ===========================================================================

class TestCreateChannel:
    def test_returns_channel(self, db_path):
        ch = create_channel("yt_main", "YouTube Main", "youtube", db_path=db_path)
        assert isinstance(ch, Channel)
        assert ch.channel_id == "yt_main"
        assert ch.name == "YouTube Main"
        assert ch.platform == "youtube"

    def test_persisted_to_db(self, db_path):
        create_channel("yt_main", "YouTube Main", "youtube", db_path=db_path)
        ch = get_channel("yt_main", db_path=db_path)
        assert ch is not None
        assert ch.name == "YouTube Main"

    def test_identity_persisted(self, db_path):
        identity = ChannelIdentity(persona_name="Dr. A", tone="academic")
        create_channel("ch1", "Chan 1", "youtube", identity=identity, db_path=db_path)
        ch = get_channel("ch1", db_path=db_path)
        assert ch.identity.persona_name == "Dr. A"
        assert ch.identity.tone == "academic"

    def test_default_identity_when_none(self, db_path):
        create_channel("ch2", "Chan 2", "tiktok", db_path=db_path)
        ch = get_channel("ch2", db_path=db_path)
        assert ch.identity.language == "en"
        assert ch.identity.persona_name is None

    def test_active_default_true(self, db_path):
        create_channel("ch3", "Chan 3", "instagram", db_path=db_path)
        ch = get_channel("ch3", db_path=db_path)
        assert ch.active is True

    def test_created_at_populated(self, db_path):
        ch = create_channel("ch4", "Chan 4", "youtube", db_path=db_path)
        assert "T" in ch.created_at

    def test_bad_db_still_returns_channel(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        ch = create_channel("cx", "X", "youtube", db_path=bad)
        assert isinstance(ch, Channel)
        assert ch.channel_id == "cx"


# ===========================================================================
# TestGetChannel
# ===========================================================================

class TestGetChannel:
    def test_not_found_returns_none(self, db_path):
        assert get_channel("nonexistent", db_path=db_path) is None

    def test_found_returns_channel(self, db_path):
        create_channel("yt_main", "YouTube Main", "youtube", db_path=db_path)
        ch = get_channel("yt_main", db_path=db_path)
        assert ch.channel_id == "yt_main"

    def test_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert get_channel("anything", db_path=bad) is None


# ===========================================================================
# TestListChannels
# ===========================================================================

class TestListChannels:
    def test_empty_returns_empty(self, db_path):
        assert list_channels(db_path=db_path) == []

    def test_returns_all_channels(self, db_path):
        create_channel("yt1", "YT1", "youtube",   db_path=db_path)
        create_channel("tt1", "TT1", "tiktok",    db_path=db_path)
        create_channel("ig1", "IG1", "instagram", db_path=db_path)
        channels = list_channels(db_path=db_path)
        assert len(channels) == 3

    def test_platform_filter(self, db_path):
        create_channel("yt1", "YT1", "youtube", db_path=db_path)
        create_channel("tt1", "TT1", "tiktok",  db_path=db_path)
        channels = list_channels(platform="youtube", db_path=db_path)
        assert len(channels) == 1
        assert channels[0].channel_id == "yt1"

    def test_ordered_by_created_at(self, db_path):
        create_channel("a1", "A1", "youtube", db_path=db_path)
        create_channel("a2", "A2", "youtube", db_path=db_path)
        channels = list_channels(db_path=db_path)
        assert channels[0].channel_id == "a1"
        assert channels[1].channel_id == "a2"

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert list_channels(db_path=bad) == []


# ===========================================================================
# TestUpdateIdentity (Identity Editor)
# ===========================================================================

class TestUpdateIdentity:
    def test_patch_updates_field(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(tone="casual"), db_path=db_path)
        ok = update_identity("ch1", {"tone": "serious"}, db_path=db_path)
        assert ok is True
        ch = get_channel("ch1", db_path=db_path)
        assert ch.identity.tone == "serious"

    def test_other_fields_unchanged(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(tone="casual", persona_name="Alex"),
                       db_path=db_path)
        update_identity("ch1", {"tone": "serious"}, db_path=db_path)
        ch = get_channel("ch1", db_path=db_path)
        assert ch.identity.persona_name == "Alex"

    def test_unknown_key_goes_to_extra(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        update_identity("ch1", {"custom_brand": "XYZ"}, db_path=db_path)
        ch = get_channel("ch1", db_path=db_path)
        assert ch.identity.extra.get("custom_brand") == "XYZ"

    def test_nonexistent_channel_returns_false(self, db_path):
        ok = update_identity("no_such", {"tone": "x"}, db_path=db_path)
        assert ok is False

    def test_bad_db_returns_false(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert update_identity("ch1", {"tone": "x"}, db_path=bad) is False

    def test_multi_field_patch(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        update_identity("ch1", {
            "tone": "friendly",
            "target_audience": "parents 30-50",
            "language": "es",
        }, db_path=db_path)
        ch = get_channel("ch1", db_path=db_path)
        assert ch.identity.tone == "friendly"
        assert ch.identity.target_audience == "parents 30-50"
        assert ch.identity.language == "es"


# ===========================================================================
# TestChannelTemplateSystem
# ===========================================================================

class TestChannelTemplateSystem:
    def test_create_template_returns_template(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        t = create_template("tmpl1", "ch1", "system_prompt", "Be concise.", db_path=db_path)
        assert isinstance(t, ChannelTemplate)
        assert t.template_id == "tmpl1"
        assert t.content == "Be concise."

    def test_template_persisted(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        create_template("tmpl1", "ch1", "system_prompt", "Be concise.", db_path=db_path)
        templates = get_templates("ch1", db_path=db_path)
        assert len(templates) == 1
        assert templates[0].content == "Be concise."

    def test_get_templates_empty(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        assert get_templates("ch1", db_path=db_path) == []

    def test_filter_by_template_type(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        create_template("t1", "ch1", "system_prompt", "Prompt A", db_path=db_path)
        create_template("t2", "ch1", "title_prompt",  "Prompt B", db_path=db_path)
        create_template("t3", "ch1", "system_prompt", "Prompt C", db_path=db_path)
        results = get_templates("ch1", template_type="system_prompt", db_path=db_path)
        assert len(results) == 2
        assert all(t.template_type == "system_prompt" for t in results)

    def test_templates_scoped_to_channel(self, db_path):
        create_channel("ch1", "C1", "youtube",   db_path=db_path)
        create_channel("ch2", "C2", "tiktok",    db_path=db_path)
        create_template("t1", "ch1", "hook", "Hook A", db_path=db_path)
        create_template("t2", "ch2", "hook", "Hook B", db_path=db_path)
        ch1_templates = get_templates("ch1", db_path=db_path)
        assert len(ch1_templates) == 1
        assert ch1_templates[0].template_id == "t1"

    def test_created_at_populated(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        t = create_template("t1", "ch1", "hook", "Hook", db_path=db_path)
        assert "T" in t.created_at

    def test_get_templates_bad_db(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert get_templates("ch1", db_path=bad) == []


# ===========================================================================
# TestChannelDuplicator
# ===========================================================================

class TestChannelDuplicator:
    def test_returns_new_channel(self, db_path):
        create_channel("src", "Source", "youtube",
                       identity=ChannelIdentity(tone="academic"), db_path=db_path)
        new = duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        assert isinstance(new, Channel)
        assert new.channel_id == "fork1"
        assert new.name == "Fork One"

    def test_fork_inherits_identity(self, db_path):
        create_channel("src", "Source", "youtube",
                       identity=ChannelIdentity(tone="academic", language="de"),
                       db_path=db_path)
        new = duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        assert new.identity.tone == "academic"
        assert new.identity.language == "de"

    def test_identity_overrides_applied(self, db_path):
        create_channel("src", "Source", "youtube",
                       identity=ChannelIdentity(tone="academic"), db_path=db_path)
        new = duplicate_channel("src", "fork1", "Fork One",
                                identity_overrides={"tone": "casual"},
                                db_path=db_path)
        assert new.identity.tone == "casual"

    def test_source_identity_unchanged(self, db_path):
        create_channel("src", "Source", "youtube",
                       identity=ChannelIdentity(tone="academic"), db_path=db_path)
        duplicate_channel("src", "fork1", "Fork One",
                          identity_overrides={"tone": "casual"}, db_path=db_path)
        src = get_channel("src", db_path=db_path)
        assert src.identity.tone == "academic"

    def test_templates_copied(self, db_path):
        create_channel("src", "Source", "youtube", db_path=db_path)
        create_template("t1", "src", "hook", "Original hook", db_path=db_path)
        duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        fork_templates = get_templates("fork1", db_path=db_path)
        assert len(fork_templates) == 1
        assert fork_templates[0].content == "Original hook"

    def test_template_ids_new_in_fork(self, db_path):
        create_channel("src", "Source", "youtube", db_path=db_path)
        create_template("t1", "src", "hook", "Hook", db_path=db_path)
        duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        fork_templates = get_templates("fork1", db_path=db_path)
        assert fork_templates[0].template_id != "t1"   # new UUID

    def test_episodes_not_copied(self, db_path):
        create_channel("src", "Source", "youtube", db_path=db_path)
        assign_episode_to_channel("ep_test", "src", db_path=db_path)
        duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        fork_episodes = get_channel_episodes("fork1", db_path=db_path)
        assert fork_episodes == []

    def test_missing_source_returns_none(self, db_path):
        result = duplicate_channel("no_such", "fork1", "Fork", db_path=db_path)
        assert result is None

    def test_platform_inherited(self, db_path):
        create_channel("src", "Source", "tiktok", db_path=db_path)
        new = duplicate_channel("src", "fork1", "Fork One", db_path=db_path)
        assert new.platform == "tiktok"


# ===========================================================================
# TestPromptRegeneration
# ===========================================================================

class TestPromptRegeneration:
    def test_no_channel_returns_base_prompt(self, db_path):
        result = regenerate_prompt("Write a story.", "no_such_channel", db_path=db_path)
        assert result == "Write a story."

    def test_identity_appended(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(persona_name="Alex", tone="casual"),
                       db_path=db_path)
        result = regenerate_prompt("Write a story.", "ch1", db_path=db_path)
        assert "Alex" in result
        assert "casual" in result
        assert result.startswith("Write a story.")

    def test_missing_fields_omitted(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(tone="academic"),
                       db_path=db_path)
        result = regenerate_prompt("Base prompt.", "ch1", db_path=db_path)
        assert "Persona:" not in result
        assert "Tone: academic" in result

    def test_empty_identity_returns_base(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        result = regenerate_prompt("Base.", "ch1", db_path=db_path)
        assert result == "Base."

    def test_hashtags_included(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(hashtags=["#history", "#science"]),
                       db_path=db_path)
        result = regenerate_prompt("Prompt.", "ch1", db_path=db_path)
        assert "#history" in result
        assert "#science" in result

    def test_non_english_language_included(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(language="fr"),
                       db_path=db_path)
        result = regenerate_prompt("Prompt.", "ch1", db_path=db_path)
        assert "fr" in result

    def test_english_language_omitted(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(language="en", tone="casual"),
                       db_path=db_path)
        result = regenerate_prompt("Prompt.", "ch1", db_path=db_path)
        # en is the default — not injected
        assert "Language:" not in result

    def test_visual_style_included(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(visual_style="dark cinematic"),
                       db_path=db_path)
        result = regenerate_prompt("Prompt.", "ch1", db_path=db_path)
        assert "dark cinematic" in result

    def test_identity_block_separator(self, db_path):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(tone="casual"), db_path=db_path)
        result = regenerate_prompt("Prompt.", "ch1", db_path=db_path)
        assert "[Channel identity]" in result

    def test_bad_db_returns_base(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        result = regenerate_prompt("Base.", "ch1", db_path=bad)
        assert result == "Base."


# ===========================================================================
# TestChannelIsolation
# ===========================================================================

class TestChannelIsolation:
    def test_assign_episode_success(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        ok = assign_episode_to_channel("ep_001", "ch1", db_path=db_path)
        assert ok is True

    def test_assign_idempotent(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        assign_episode_to_channel("ep_001", "ch1", db_path=db_path)
        assign_episode_to_channel("ep_001", "ch1", db_path=db_path)  # second call
        episodes = get_channel_episodes("ch1", db_path=db_path)
        assert episodes.count("ep_001") == 1

    def test_get_channel_episodes_empty(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        assert get_channel_episodes("ch1", db_path=db_path) == []

    def test_get_channel_episodes_returns_ids(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        assign_episode_to_channel("ep_001", "ch1", db_path=db_path)
        assign_episode_to_channel("ep_002", "ch1", db_path=db_path)
        episodes = get_channel_episodes("ch1", db_path=db_path)
        assert "ep_001" in episodes
        assert "ep_002" in episodes

    def test_episodes_scoped_per_channel(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        create_channel("ch2", "C2", "tiktok",  db_path=db_path)
        assign_episode_to_channel("ep_001", "ch1", db_path=db_path)
        assign_episode_to_channel("ep_002", "ch2", db_path=db_path)
        assert "ep_002" not in get_channel_episodes("ch1", db_path=db_path)

    def test_assign_bad_db_returns_false(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert assign_episode_to_channel("ep_001", "ch1", db_path=bad) is False

    def test_get_episodes_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        assert get_channel_episodes("ch1", db_path=bad) == []


# ===========================================================================
# TestEmptyDB
# ===========================================================================

class TestEmptyDB:
    @pytest.fixture()
    def empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        return p

    def test_get_channel_none(self, empty_db):
        assert get_channel("any", db_path=empty_db) is None

    def test_list_channels_empty(self, empty_db):
        assert list_channels(db_path=empty_db) == []

    def test_get_templates_empty(self, empty_db):
        assert get_templates("any", db_path=empty_db) == []

    def test_get_channel_episodes_empty(self, empty_db):
        assert get_channel_episodes("any", db_path=empty_db) == []

    def test_update_identity_false(self, empty_db):
        assert update_identity("any", {"tone": "x"}, db_path=empty_db) is False

    def test_duplicate_channel_none(self, empty_db):
        assert duplicate_channel("src", "fork", "Fork", db_path=empty_db) is None

    def test_regenerate_base_only(self, empty_db):
        assert regenerate_prompt("Base.", "any", db_path=empty_db) == "Base."


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block_socket(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise AssertionError("channel_manager opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_create_channel_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        create_channel("ch1", "C1", "youtube", db_path=db_path)

    def test_list_channels_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        list_channels(db_path=db_path)

    def test_update_identity_no_network(self, db_path, monkeypatch):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        self._block_socket(monkeypatch)
        update_identity("ch1", {"tone": "x"}, db_path=db_path)

    def test_regenerate_prompt_no_network(self, db_path, monkeypatch):
        create_channel("ch1", "C1", "youtube",
                       identity=ChannelIdentity(tone="casual"), db_path=db_path)
        self._block_socket(monkeypatch)
        regenerate_prompt("Prompt.", "ch1", db_path=db_path)

    def test_duplicate_channel_no_network(self, db_path, monkeypatch):
        create_channel("src", "Source", "youtube", db_path=db_path)
        self._block_socket(monkeypatch)
        duplicate_channel("src", "fork1", "Fork", db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        assign_episode_to_channel("ep_test", "ch1", db_path=db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM channel_episodes WHERE episode_id='ep_000'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_list_channels_read_only(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        conn = get_connection(db_path)
        try:
            before = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        finally:
            conn.close()
        list_channels(db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_get_templates_read_only(self, db_path):
        create_channel("ch1", "C1", "youtube", db_path=db_path)
        create_template("t1", "ch1", "hook", "Hook", db_path=db_path)
        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM channel_templates"
            ).fetchone()[0]
        finally:
            conn.close()
        get_templates("ch1", db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM channel_templates"
            ).fetchone()[0]
        finally:
            conn.close()
        assert before == after
