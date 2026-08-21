"""
tests/test_publishing_readiness.py — Publish-Readiness Check.

Covers hypogeum.publishing.readiness.check_publish_readiness(): every
blocking reason (A_METADATA missing/conflicting/resolved-by-override,
B_LONG_RENDER, C_SHORTS_RENDERS, D_PUBLISHING_BINDING, E_MEDIA_STAGING_READY)
and the full-pass case. All state is built under tmp_path (fake episode
assets dir) and an isolated tmp_path SQLite database — never the production
.hypogeum-assets tree. No network, no real Zernio call.
"""

from __future__ import annotations

import json

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema, insert_episode
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.readiness import MetadataOverride, check_publish_readiness

EPISODE_ID = "ep_test01"


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


def _make_fully_ready(episode_dir, db_path, *, n_shorts=2, platforms=(), channel_id=None):
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
    if platforms and channel_id:
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


class TestFullPass:
    def test_all_checks_pass(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=2, platforms=("youtube",), channel_id="hypogeum")
        result = check_publish_readiness(
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path,
            channel_id="hypogeum", platforms=("youtube",),
        )
        assert result.passed is True
        assert {c.capability for c in result.checks} == {
            "A_METADATA", "B_LONG_RENDER", "C_SHORTS_RENDERS", "D_PUBLISHING_BINDING", "E_MEDIA_STAGING_READY",
        }
        assert all(c.status == "pass" for c in result.checks)

    def test_no_shorts_episode_passes_shorts_check(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        shorts_check = next(c for c in result.checks if c.capability == "C_SHORTS_RENDERS")
        assert shorts_check.status == "pass"


class TestMetadataBlocking:
    def test_missing_title_blocks(self, episode_dir, db_path):
        _write_render_package(episode_dir, metadata_source={"episode_title": "", "description": "d", "shorts_titles": []})
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "no title candidate" in meta.reason
        assert result.passed is False

    def test_conflicting_title_candidates_block_and_never_auto_select(self, episode_dir, db_path):
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

        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "conflicting title candidates" in meta.reason
        assert "Candidate A" in json.dumps(meta.evidence)
        assert "Candidate B" in json.dumps(meta.evidence)
        assert result.passed is False

    def test_metadata_override_resolves_title_without_choosing_one_itself(self, episode_dir, db_path):
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
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path, metadata_override=override,
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
        _write_render_package(episode_dir, metadata_source={"episode_title": "T", "description": "", "shorts_titles": []})
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "description" in meta.reason

    def test_incomplete_shorts_titles_blocks(self, episode_dir, db_path):
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "T", "description": "d", "shorts_titles": ["only one"]},
            shorts_renders=[{"short_id": "sh_0"}, {"short_id": "sh_1"}],
        )
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        meta = next(c for c in result.checks if c.capability == "A_METADATA")
        assert meta.status == "fail"
        assert "shorts_titles" in meta.reason


class TestRenderAvailabilityBlocking:
    def test_missing_main_render_blocks(self, episode_dir, db_path):
        _write_render_package(episode_dir)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        long_render = next(c for c in result.checks if c.capability == "B_LONG_RENDER")
        assert long_render.status == "fail"
        assert result.passed is False

    def test_empty_main_render_blocks(self, episode_dir, db_path):
        _write_render_package(episode_dir)
        _write_main_render(episode_dir, size=0)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        long_render = next(c for c in result.checks if c.capability == "B_LONG_RENDER")
        assert long_render.status == "fail"
        assert "empty" in long_render.reason

    def test_missing_short_render_blocks(self, episode_dir, db_path):
        _write_render_package(
            episode_dir,
            metadata_source={"episode_title": "T", "description": "d", "shorts_titles": ["a", "b"]},
            shorts_renders=[{"short_id": "sh_0"}, {"short_id": "sh_1"}],
        )
        _write_main_render(episode_dir)
        _write_short_render(episode_dir, 0)
        # short index 1 deliberately missing.
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        shorts_check = next(c for c in result.checks if c.capability == "C_SHORTS_RENDERS")
        assert shorts_check.status == "fail"
        assert "1/2" in shorts_check.reason
        assert result.passed is False

    def test_this_reproduces_the_real_ep_f3619022_shape(self, episode_dir, db_path):
        """metadata_source fields empty + shorts_renders present with empty
        video_asset_id placeholders (the exact real shape found in
        production, per the prior readiness audit) — physical files DO
        exist so C passes, but A must still block."""
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

        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        by_cap = {c.capability: c for c in result.checks}
        assert by_cap["A_METADATA"].status == "fail"
        assert by_cap["B_LONG_RENDER"].status == "pass"
        assert by_cap["C_SHORTS_RENDERS"].status == "pass"
        assert result.passed is False


class TestBindingBlocking:
    def test_no_platforms_requested_passes_trivially(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path, platforms=())
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "pass"

    def test_missing_binding_blocks(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path,
            channel_id="hypogeum", platforms=("youtube",),
        )
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "fail"
        assert result.passed is False

    def test_missing_channel_id_blocks_when_platforms_requested(self, episode_dir, db_path):
        _make_fully_ready(episode_dir, db_path, n_shorts=0)
        result = check_publish_readiness(
            EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path, platforms=("youtube",),
        )
        binding = next(c for c in result.checks if c.capability == "D_PUBLISHING_BINDING")
        assert binding.status == "fail"


class TestMediaStagingReadyBlocking:
    def test_no_credential_blocks(self, episode_dir, db_path):
        _write_render_package(episode_dir)
        _write_main_render(episode_dir)
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        staging = next(c for c in result.checks if c.capability == "E_MEDIA_STAGING_READY")
        assert staging.status == "fail"
        assert "credential" in staging.reason.lower()
        assert result.passed is False

    def test_credential_present_but_deliverable_empty_blocks(self, episode_dir, db_path):
        _write_render_package(episode_dir)
        _write_main_render(episode_dir, size=0)
        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test", scope="factory", db_path=db_path,
        )
        result = check_publish_readiness(EPISODE_ID, assets_root=episode_dir.parent.parent, db_path=db_path)
        staging = next(c for c in result.checks if c.capability == "E_MEDIA_STAGING_READY")
        assert staging.status == "fail"
