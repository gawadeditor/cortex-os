"""
tests/test_canonical_metadata_zernio_mapping.py —
hypogeum/publishing/canonical_metadata.py::build_zernio_platform_specific_data()

Regression: the YouTube branch built categoryId/containsSyntheticMedia/
firstComment/madeForKids/playlistId/visibility but never mapped `title`,
even though title is one of the documented YouTube platformSpecificData
fields (zernio_platform_contracts._YOUTUBE_PLATFORM_SPECIFIC_DATA_FIELDS)
and Zernio's own post editor marks it *required* for YouTube. Confirmed
live 2026-09-24: every recently-published video on both channels carried
an AI-generated-sounding title on YouTube instead of the one Cortex
wrote and approved, because the field Zernio actually reads for YouTube's
title was submitted empty. The root-level `title`/`content` this
codebase also sends does not feed that field at all.
"""

from __future__ import annotations

from hypogeum.publishing.canonical_metadata import (
    CanonicalPlatformMetadata,
    build_zernio_platform_specific_data,
)


def _metadata(**overrides) -> CanonicalPlatformMetadata:
    base = dict(
        deliverable_id="main_episode",
        platform="youtube",
        description="A real description.",
        metadata_version="v1",
        source_artifact_ids=[],
        input_fingerprint={},
        generated_at="2026-09-24T00:00:00+00:00",
        title="Rosie's Goodnight Hug",
    )
    base.update(overrides)
    return CanonicalPlatformMetadata(**base)


class TestYouTubeTitleMapping:
    def test_title_is_mapped_into_platform_specific_data(self):
        data = build_zernio_platform_specific_data(_metadata(), "youtube")
        assert data is not None
        assert data["title"] == "Rosie's Goodnight Hug"

    def test_absent_title_omits_the_key(self):
        data = build_zernio_platform_specific_data(_metadata(title=None), "youtube")
        assert data is None or "title" not in data

    def test_title_coexists_with_other_youtube_fields(self):
        meta = _metadata(
            category_id="27", ai_disclosure=True, first_comment="hi",
            platform_policy_fields={"madeForKids": True},
        )
        data = build_zernio_platform_specific_data(meta, "youtube")
        assert data["title"] == "Rosie's Goodnight Hug"
        assert data["categoryId"] == "27"
        assert data["containsSyntheticMedia"] is True
        assert data["firstComment"] == "hi"
        assert data["madeForKids"] is True


class TestFacebookTitleMappingUnaffected:
    def test_facebook_title_still_maps(self):
        data = build_zernio_platform_specific_data(
            _metadata(platform="facebook"), "facebook",
        )
        assert data["title"] == "Rosie's Goodnight Hug"
