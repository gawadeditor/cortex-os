"""
tests/test_publishing_readiness.py — Publish-Readiness Check.

Covers hypogeum.publishing.readiness.check_publish_readiness(): the
mandatory CHANNEL_SCOPE check (required channel_id, no default, no global
fallback, short-circuits every other check on failure so no cross-channel
metadata/render/binding/staging evidence is ever returned), every other
blocking reason (A_METADATA missing/conflicting/resolved-by-override,
B_LONG_RENDER, C_SHORTS_RENDERS, D_PUBLISHING_BINDING, E_MEDIA_STAGING_READY),
and the full-pass case. All state is built under tmp_path (fake episode
assets dir) and an isolated tmp_path SQLite database — never the production
.hypogeum-assets tree. No network, no real Zernio call.
"""

from __future__ import annotations

import json

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.readiness import (
    MetadataOverride,
    MetadataResolutionError,
    check_publish_readiness,
    resolve_canonical_metadata,
)

EPISODE_ID = "ep_test01"
CHANNEL_ID = "channel_a"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "readiness_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def assets_root(tmp_path):
    root = tmp_path / "assets"
    (root / "episodes" / EPISODE_ID).mkdir(parents=True)
    return root


@pytest.fixture()
def episode_dir(assets_root):
    return assets_root / "episodes" / EPISODE_ID


def _attribute_to_channel(db_path, episode_id=EPISODE_ID, channel_id=CHANNEL_ID):
    """Seed the episode_topics row that attributes episode_id to
    channel_id — the only real channel-attribution mechanism the current
    schema supports (see readiness.py's CHANNEL_SCOPE check)."""
    conn = get_connection(db_path)
    try:
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00", episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()


def _write_render_package(episode_dir, *, metadata_source=None, shorts_renders=None):
    pkg = {
        "metadata_source": metadata_source if metadata_source is not None else {
            "episode_title": "The Real Title",
            "description": "A real description of the episode.",
            "shorts_titles": [],
        },
        "shorts_renders": shorts_renders if shorts_renders is not None else [],
    }
    (episode_dir / f"final_render_package_{EPISODE_ID}.json").write_text(json.dumps(pkg), encoding="utf-8")
    return pkg


def _write_main_render(episode_dir, size=1024):
    (episode_dir / f"main_{EPISODE_ID}.mp4").write_bytes(b"x" * size)


def _write_short_render(episode_dir, index, size=512):
    (episode_dir / f"short_{index}_{EPISODE_ID}.mp4").write_bytes(b"x" * size)


def _make_fully_ready(episode_dir, db_path, *, n_shorts=2, platforms=(), channel_id=CHANNEL_ID, attribute=True):
    if attribute:
        _attribute_to_channel(db_path, channel_id=channel_id)
    _write_render_package(
        episode_dir,
        metadata_source={
            "episode_title": "The Real Title",
            "description": "A real description of the episode.",
            "shorts_titles": [f"short title {i}" for i in range(n_shorts)],
        },
        shorts_renders=[{"short_id": f"sh_{i}"} for i in range(n_shorts)],
    )
    _write_main_render(episode_dir)
    for i in range(n_shorts):
        _write_short_render(episode_dir, i)
    create_secret_profile(
        provider="zernio", label="test key", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    if platforms:
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform=platforms[0], external_account_id="acct_1",
            provider_profile_id=profile["provider_profile_id"],
        )
        bind_channel_provider(
            db_path, channel_id=channel_id, capability="publishing", platform=platforms[0],
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=account["provider_account_id"],
        )


class TestChannelScopeBlocking:
    def test_missing_channel_id_blocks_and_reads_nothing_else(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path)
        result = check_publish_readiness(EPISODE_ID, channel_id="", assets_root=episode_dir.parent.parent, db_path=db_path)
        assert result.passed is False
        assert [c.capability for c in result.checks] == ["CHANNEL_SCOPE"]
        assert "channel_id is required" in result.checks[0].reason

    def test_no_attributing_row_blocks_and_reads_nothing_else(self, episode_dir, db_path):
        # No _attribute_to_channel() call -- episode_topics has no row at all.
        _write_render_package(episode_dir)
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        assert result.passed is False
        assert [c.capability for c in result.checks] == ["CHANNEL_SCOPE"]
        assert "no episode_topics row attributes" in result.checks[0].reason

    def test_mismatched_channel_id_blocks_and_never_exposes_real_metadata(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, channel_id="channel_a")
        result = check_publish_readiness(
            EPISODE_ID, channel_id="channel_b", assets_root=episode_dir.parent.parent, db_path=db_path,
        )
        assert result.passed is False
        assert [c.capability for c in result.checks] == ["CHANNEL_SCOPE"]
        scope_check = result.checks[0]
        assert "channel_a" in scope_check.evidence["attributed_channel_ids"]
        assert scope_check.evidence["asserted_channel_id"] == "channel_b"
        # The real episode's title ("The Real Title") must never appear
        # anywhere in a result evaluated under the wrong channel_id.
        assert "The Real Title" not in json.dumps(result.to_dict())

    def test_correct_channel_id_proceeds_to_every_other_check(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, channel_id=CHANNEL_ID)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        assert "CHANNEL_SCOPE" in {c.capability for c in result.checks}
        assert len(result.checks) > 1


class TestFullPass:
    def test_all_checks_pass(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=2, platforms=("youtube",), channel_id=CHANNEL_ID)
        result = check_publish_readiness(
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path,
            channel_id=CHANNEL_ID, platforms=("youtube",),
        )
        assert result.passed is True
        assert {c.capability for c in result.checks} == {
            "CHANNEL_SCOPE", "A_METADATA", "B_LONG_RENDER", "C_SHORTS_RENDERS", "D_PUBLISHING_BINDING", "E_MEDIA_STAGING_READY",
        }
        assert all(c.status == "pass" for c in result.checks)

    def test_no_shorts_episode_passes_shorts_check(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        shorts_check = next(c for c in result.checks if c.capability == "C_SHORTS_RENDERS")
        assert shorts_check.status == "pass"


class TestMetadataBlocking:
    def test_missing_title_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "", "description": "d", "shorts_titles": []})
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "no canonical publication title found" in meta.reason
        assert result.passed is False

    def test_discovery_topic_conflict_does_not_block_reproduces_real_ep_f3619022_shape(self, episode_dir, db_path):
        """The exact real ep_f3619022 shape: topic_brief.episode_title
        populated (the canonical, historically-published title) vs.
        episodes.topic holding a DIFFERENT discovery-stage label. Per the
        operator-approved rule, episodes.topic is provenance only and must
        never conflict-block a real canonical title -- the title check
        must resolve to topic_brief's value with no override needed."""
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "", "description": "d", "shorts_titles": []})
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"episode_title": "The Erdstall Tunnels: Europe's Secret Underground Mystery"}), encoding="utf-8",
        )
        conn = get_connection(db_path)
        try:
            insert_episode(
                conn, episode_id=EPISODE_ID, created_at="2026-01-01T00:00:00+00:00",
                topic="The Erdstall Tunnels: Europe's Most Baffling Medieval Underground Network", status="complete",
            )
            conn.commit()
        finally:
            conn.close()
        _write_main_render(episode_dir)

        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "pass", meta.reason
        assert meta.evidence["title"] == "The Erdstall Tunnels: Europe's Secret Underground Mystery"
        assert meta.evidence["resolved_by"] == "single_consistent_canonical_candidate"
        # The discovery-topic conflict is retained as provenance evidence,
        # never as a blocking candidate.
        assert meta.evidence["discovery_topic_provenance"] == "The Erdstall Tunnels: Europe's Most Baffling Medieval Underground Network"

    def test_genuine_canonical_conflict_still_blocks(self, episode_dir, db_path):
        """metadata_source.episode_title and topic_brief.episode_title
        disagreeing IS a real canonical conflict and must still block --
        this rule only exempts episodes.topic, not the two canonical
        release-package sources from each other."""
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "Canonical A", "description": "d", "shorts_titles": []})
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"episode_title": "Canonical B"}), encoding="utf-8",
        )
        _write_main_render(episode_dir)

        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "conflicting CANONICAL title candidates" in meta.reason
        assert "Canonical A" in json.dumps(meta.evidence)
        assert "Canonical B" in json.dumps(meta.evidence)
        assert result.passed is False

    def test_metadata_override_resolves_title_without_choosing_one_itself(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "", "description": "d", "shorts_titles": []})
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"episode_title": "Candidate A"}), encoding="utf-8",
        )
        conn = get_connection(db_path)
        try:
            insert_episode(conn, episode_id=EPISODE_ID, created_at="2026-01-01T00:00:00+00:00", topic="Candidate B", status="complete")
            conn.commit()
        finally:
            conn.close()
        _write_main_render(episode_dir)

        override = MetadataOverride(title="Operator-Confirmed Title")
        result = check_publish_readiness(
            EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path, metadata_override=override,
        )
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "pass"
        assert meta.evidence["title"] == "Operator-Confirmed Title"
        assert result.metadata_override_applied is True

        # The DB row itself must remain untouched — this check never mutates.
        conn = get_connection(db_path)
        try:
            row = conn.execute("SELECT topic FROM episodes WHERE episode_id = ?", (EPISODE_ID,)).fetchone()
        finally:
            conn.close()
        assert row["topic"] == "Candidate B"

    def test_missing_description_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "T", "description": "", "shorts_titles": []})
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "description" in meta.reason

    def test_incomplete_shorts_titles_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "T", "description": "d", "shorts_titles": ["only one"]},
            shorts_renders=[{"short_id": "sh_0"}, {"short_id": "sh_1"}],
        )
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "shorts_titles" in meta.reason

    def test_description_resolves_from_topic_brief_logline_reproduces_real_ep_f3619022_shape(self, episode_dir, db_path):
        """The exact real ep_f3619022 shape: metadata_source.description and
        metadata_source.logline are both empty (confirmed via the sealed
        publishing_record and final_render_package on disk), but
        topic_brief.logline holds the Executive Producer's real,
        already-generated logline. This is the same completed-package
        text that every other real production episode copies verbatim into
        metadata_source.description/.logline -- reusing it here is not
        inventing copy, it is resolving the canonical value from where it
        already lives."""
        _attribute_to_channel(db_path)
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "T", "logline": "", "description": "", "shorts_titles": []},
            shorts_renders=[{"short_id": "short_001"}, {"short_id": "short_002"}, {"short_id": "short_003"}],
        )
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({
                "episode_title": "T",
                "logline": (
                    "Thousands of hand-carved medieval tunnels too narrow for normal human "
                    "movement honeycomb Europe from Bavaria to Ireland, yet no tools, no "
                    "artefacts, no written records, and no historical mention explain who "
                    "built them, why, or how they share identical construction signatures "
                    "across an entire continent."
                ),
            }),
            encoding="utf-8",
        )
        _write_main_render(episode_dir)

        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        # description itself resolves cleanly from topic_brief.logline...
        assert "no canonical description found" not in meta.reason
        assert "conflicting CANONICAL description" not in meta.reason
        # ...but shorts_titles is a genuine, unresolved content gap in this
        # real episode -- no artifact anywhere contains a real Shorts title
        # (every episode in production history that ever populated
        # shorts_titles used the short_id itself as a placeholder), so
        # A_METADATA still fails closed, reported exactly for that reason,
        # never fabricated.
        assert meta.status == "fail"
        assert "shorts_titles" in meta.reason

    def test_missing_description_blocks_when_no_candidate_source_exists(self, episode_dir, db_path):
        """Absence of ALL three description sources (metadata_source.
        description, metadata_source.logline, topic_brief.logline) must
        report the exact absence, never fabricate a value."""
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "T", "logline": "", "description": "", "shorts_titles": []})
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "no canonical description found" in meta.reason
        assert meta.evidence["description_candidates"] == {}

    def test_conflicting_description_candidates_still_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "T", "logline": "", "description": "Description A", "shorts_titles": []})
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"episode_title": "T", "logline": "Description B"}), encoding="utf-8",
        )
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "conflicting CANONICAL description candidates" in meta.reason
        assert "Description A" in json.dumps(meta.evidence)
        assert "Description B" in json.dumps(meta.evidence)

    def test_metadata_override_resolves_description_without_choosing_one_itself(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir, metadata_source={"episode_title": "T", "logline": "", "description": "", "shorts_titles": []})
        _write_main_render(episode_dir)
        override = MetadataOverride(description="Operator-confirmed description.")
        result = check_publish_readiness(
            EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path, metadata_override=override,
        )
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert "no canonical description found" not in meta.reason
        assert "conflicting CANONICAL description" not in meta.reason


class TestRenderAvailabilityBlocking:
    def test_missing_main_render_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        long_render = next(c for c in result.checks if c.capability == "B_LONG_RENDER")
        assert long_render.status == "fail"
        assert result.passed is False

    def test_empty_main_render_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir)
        _write_main_render(episode_dir, size=0)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        long_render = next(c for c in result.checks if c.capability == "B_LONG_RENDER")
        assert long_render.status == "fail"
        assert "empty" in long_render.reason

    def test_missing_short_render_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "T", "description": "d", "shorts_titles": ["a", "b"]},
            shorts_renders=[{"short_id": "sh_0"}, {"short_id": "sh_1"}],
        )
        _write_main_render(episode_dir)
        _write_short_render(episode_dir, 0)
        # short index 1 deliberately missing.
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        shorts_check = next(c for c in result.checks if c.capability == "C_SHORTS_RENDERS")
        assert shorts_check.status == "fail"
        assert "1/2" in shorts_check.reason
        assert result.passed is False

    def test_this_reproduces_the_real_ep_f3619022_shape(self, episode_dir, db_path):
        """metadata_source fields empty + shorts_renders present with empty
        video_asset_id placeholders (the exact real shape found in
        production, per the prior readiness audit) — physical files DO
        exist so C passes, but A must still block."""
        _attribute_to_channel(db_path)
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "", "logline": "", "description": "", "shorts_titles": [], "sources": []},
            shorts_renders=[
                {"short_id": "sh_000_placeholder", "video_asset_id": "", "caption_asset_id": ""},
                {"short_id": "sh_001_placeholder", "video_asset_id": "", "caption_asset_id": ""},
                {"short_id": "sh_002_placeholder", "video_asset_id": "", "caption_asset_id": ""},
            ],
        )
        _write_main_render(episode_dir)
        for i in range(3):
            _write_short_render(episode_dir, i)

        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        by_cap = {c.capability: c for c in result.checks}
        assert by_cap["A_METADATA"].status == "fail"
        assert by_cap["B_LONG_RENDER"].status == "pass"
        assert by_cap["C_SHORTS_RENDERS"].status == "pass"
        assert result.passed is False


class TestBindingBlocking:
    def test_no_platforms_requested_passes_trivially(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path, platforms=())
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "pass"

    def test_missing_binding_blocks(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path,
            channel_id=CHANNEL_ID, platforms=("youtube",),
        )
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "fail"
        assert result.passed is False

    def test_binding_for_a_different_channel_does_not_satisfy_this_one(self, episode_dir, db_path):
        # Bind 'youtube' for channel_b, but evaluate readiness for channel_a
        # (this episode's own real channel) -- must still fail.
        _make_fully_ready(episode_dir, db_path, n_shorts=0, channel_id=CHANNEL_ID)
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_2")
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="acct_2",
            provider_profile_id=profile["provider_profile_id"],
        )
        bind_channel_provider(
            db_path, channel_id="channel_b", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=account["provider_account_id"],
        )
        result = check_publish_readiness(
            EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path, platforms=("youtube",),
        )
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "fail"


class TestMediaStagingReadyBlocking:
    def test_no_credential_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir)
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        staging = next(c for c in result.checks if c.capability == "E_MEDIA_STAGING_READY")
        assert staging.status == "fail"
        assert "credential" in staging.reason.lower()
        assert result.passed is False

    def test_credential_present_but_deliverable_empty_blocks(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(episode_dir)
        _write_main_render(episode_dir, size=0)
        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )
        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        staging = next(c for c in result.checks if c.capability == "E_MEDIA_STAGING_READY")
        assert staging.status == "fail"


class TestResolveCanonicalMetadata:
    """
    hypogeum.publishing.readiness.resolve_canonical_metadata() — the
    public resolver _check_metadata() itself now calls internally.
    Factored out so a real publish call (hypogeum.publishing.
    zernio_channel_release_trigger.build_publish_package_from_episode_dir())
    can use the EXACT SAME resolution readiness already validated, instead
    of risking a second, independently-written implementation.
    """

    def test_resolves_the_same_values_check_publish_readiness_validated(self, episode_dir, db_path):
        _attribute_to_channel(db_path)
        _write_render_package(
            episode_dir,
            metadata_source={
                "episode_title": "T", "logline": "", "description": "",
                "shorts_titles": [{"short_id": "short_001", "title": "Real Short Title"}],
            },
            shorts_renders=[{"short_id": "001"}],
        )
        (episode_dir / f"topic_brief_{EPISODE_ID}.json").write_text(
            json.dumps({"episode_title": "T", "logline": "A real description."}), encoding="utf-8",
        )
        _write_main_render(episode_dir)
        _write_short_render(episode_dir, 0)

        result = check_publish_readiness(EPISODE_ID, channel_id=CHANNEL_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta_check = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta_check.status == "pass", meta_check.reason

        resolved = resolve_canonical_metadata(EPISODE_ID, episode_dir, db_path, None, {
            "metadata_source": {"episode_title": "T", "logline": "", "description": "", "shorts_titles": [{"short_id": "short_001", "title": "Real Short Title"}]},
            "shorts_renders": [{"short_id": "001"}],
        })
        assert resolved.title == "T"
        assert resolved.description == "A real description."
        assert resolved.shorts_titles == {"short_001": "Real Short Title"}

    def test_raises_on_missing_title_never_returns_a_partial_result(self, episode_dir, db_path):
        _write_render_package(episode_dir, metadata_source={"episode_title": "", "description": "d", "shorts_titles": []})
        with pytest.raises(MetadataResolutionError, match="no canonical publication title found"):
            resolve_canonical_metadata(EPISODE_ID, episode_dir, db_path, None, {
                "metadata_source": {"episode_title": "", "description": "d", "shorts_titles": []},
            })

    def test_raises_on_placeholder_shorts_title(self, episode_dir, db_path):
        pkg = {
            "metadata_source": {
                "episode_title": "T", "description": "d",
                "shorts_titles": [{"short_id": "short_001", "title": "short_001"}],
            },
            "shorts_renders": [{"short_id": "001"}],
        }
        with pytest.raises(MetadataResolutionError, match="placeholder"):
            resolve_canonical_metadata(EPISODE_ID, episode_dir, db_path, None, pkg)

    def test_no_shorts_expected_returns_empty_shorts_titles_dict(self, episode_dir, db_path):
        pkg = {"metadata_source": {"episode_title": "T", "description": "d", "shorts_titles": []}, "shorts_renders": []}
        resolved = resolve_canonical_metadata(EPISODE_ID, episode_dir, db_path, None, pkg)
        assert resolved.shorts_titles == {}

    def test_legacy_flat_string_shape_resolves_to_deliverable_id_keyed_dict(self, episode_dir, db_path):
        pkg = {
            "metadata_source": {"episode_title": "T", "description": "d", "shorts_titles": ["First Real Title", "Second Real Title"]},
            "shorts_renders": [{"short_id": "sh_0"}, {"short_id": "sh_1"}],
        }
        resolved = resolve_canonical_metadata(EPISODE_ID, episode_dir, db_path, None, pkg)
        assert resolved.shorts_titles == {"short_sh_0": "First Real Title", "short_sh_1": "Second Real Title"}
