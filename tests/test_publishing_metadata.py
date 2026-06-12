"""
tests/test_publishing_metadata.py — Unit tests for hypogeum/publishing/metadata.py

Tests:
  - YouTube description construction (logline + chapters + disclosure)
  - Chapter block construction from section_titles
  - ai_disclosure_text included/missing (MissingDisclosureError)
  - Short title lookup (dict and list forms)
  - Facebook description construction
  - Instagram caption construction
  - TikTok title construction
"""

from __future__ import annotations

import pytest

from hypogeum.publishing.metadata import (
    MissingDisclosureError,
    _build_chapter_block,
    _build_sources_block,
    _format_timestamp,
    build_facebook_description,
    build_instagram_caption,
    build_tiktok_title,
    build_youtube_description,
    build_youtube_short_description,
    get_ai_disclosure_text,
    get_episode_title,
    get_short_title,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_metadata_source():
    return {
        "episode_title": "The Lost City of Atlantis",
        "logline": "Historians uncover evidence pointing to a real city beneath the waves.",
        "section_titles": [
            {"title": "Introduction", "start_seconds": 0.0},
            {"title": "The Discovery", "start_seconds": 120.0},
            {"title": "The Evidence", "start_seconds": 300.0},
        ],
        "shorts_titles": {
            "sh_001": "3 Signs Atlantis Was Real",
            "sh_002": "The Most Shocking Find",
            "sh_003": "What Historians Won't Tell You",
        },
        "ai_disclosure_text": "This video was produced with AI assistance.",
    }


@pytest.fixture
def metadata_missing_disclosure():
    return {
        "episode_title": "Test Episode",
        "logline": "A test logline.",
        "section_titles": [],
        "shorts_titles": {},
        "ai_disclosure_text": "",
    }


@pytest.fixture
def metadata_list_shorts_titles():
    return {
        "episode_title": "Episode A",
        "logline": "Logline A.",
        "section_titles": [],
        "shorts_titles": [
            {"short_id": "sh_001", "title": "Short One Title"},
            {"short_id": "sh_002", "title": "Short Two Title"},
        ],
        "ai_disclosure_text": "AI generated content.",
    }


# ---------------------------------------------------------------------------
# TestFormatTimestamp
# ---------------------------------------------------------------------------

class TestFormatTimestamp:
    def test_seconds_only(self):
        assert _format_timestamp(45) == "0:45"

    def test_minutes_and_seconds(self):
        assert _format_timestamp(125) == "2:05"

    def test_hours(self):
        assert _format_timestamp(3665) == "1:01:05"

    def test_zero(self):
        assert _format_timestamp(0) == "0:00"


# ---------------------------------------------------------------------------
# TestBuildChapterBlock
# ---------------------------------------------------------------------------

class TestBuildChapterBlock:
    def test_basic_chapter_block(self):
        section_titles = [
            {"title": "Introduction", "start_seconds": 0.0},
            {"title": "The Discovery", "start_seconds": 120.5},
            {"title": "The Evidence", "start_seconds": 300.0},
        ]
        block = _build_chapter_block(section_titles)
        assert "0:00 Introduction" in block
        assert "2:00 The Discovery" in block
        assert "5:00 The Evidence" in block

    def test_empty_section_titles(self):
        block = _build_chapter_block([])
        assert block == ""

    def test_section_titles_without_start_seconds(self):
        section_titles = [
            {"title": "Intro"},
            {"title": "Main"},
        ]
        block = _build_chapter_block(section_titles)
        assert "0:00 Intro" in block
        assert "0:00 Main" in block

    def test_section_titles_skips_empty_titles(self):
        section_titles = [
            {"title": "Real Section", "start_seconds": 0.0},
            {"title": "", "start_seconds": 60.0},
        ]
        block = _build_chapter_block(section_titles)
        assert "Real Section" in block
        # Empty title should not appear
        lines = block.splitlines()
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# TestGetAiDisclosureText
# ---------------------------------------------------------------------------

class TestGetAiDisclosureText:
    def test_returns_disclosure_when_present(self, minimal_metadata_source):
        text = get_ai_disclosure_text(minimal_metadata_source)
        assert text == "This video was produced with AI assistance."

    def test_raises_when_missing(self, metadata_missing_disclosure):
        with pytest.raises(MissingDisclosureError):
            get_ai_disclosure_text(metadata_missing_disclosure)

    def test_raises_when_whitespace_only(self):
        ms = {"ai_disclosure_text": "   "}
        with pytest.raises(MissingDisclosureError):
            get_ai_disclosure_text(ms)

    def test_raises_when_key_absent(self):
        ms = {}
        with pytest.raises(MissingDisclosureError):
            get_ai_disclosure_text(ms)

    def test_strips_surrounding_whitespace(self):
        ms = {"ai_disclosure_text": "  AI made this.  "}
        text = get_ai_disclosure_text(ms)
        assert text == "AI made this."


# ---------------------------------------------------------------------------
# TestBuildYoutubeDescription
# ---------------------------------------------------------------------------

class TestBuildYoutubeDescription:
    def test_includes_logline(self, minimal_metadata_source):
        desc = build_youtube_description(minimal_metadata_source)
        assert "Historians uncover evidence" in desc

    def test_includes_chapter_block(self, minimal_metadata_source):
        desc = build_youtube_description(minimal_metadata_source)
        assert "0:00 Introduction" in desc
        assert "2:00 The Discovery" in desc

    def test_includes_ai_disclosure(self, minimal_metadata_source):
        desc = build_youtube_description(minimal_metadata_source)
        assert "This video was produced with AI assistance." in desc

    def test_disclosure_is_last_section(self, minimal_metadata_source):
        desc = build_youtube_description(minimal_metadata_source)
        disclosure_pos = desc.rfind("This video was produced with AI assistance.")
        assert disclosure_pos == len(desc) - len("This video was produced with AI assistance.")

    def test_raises_when_disclosure_missing(self, metadata_missing_disclosure):
        with pytest.raises(MissingDisclosureError):
            build_youtube_description(metadata_missing_disclosure)

    def test_no_chapters_when_section_titles_empty(self):
        ms = {
            "episode_title": "Test",
            "logline": "A logline.",
            "section_titles": [],
            "ai_disclosure_text": "AI disclosure.",
        }
        desc = build_youtube_description(ms)
        assert "0:00" not in desc
        assert "AI disclosure." in desc

    def test_truncates_to_5000_chars_keeping_disclosure(self):
        long_logline = "x" * 6000
        ms = {
            "episode_title": "Test",
            "logline": long_logline,
            "section_titles": [],
            "ai_disclosure_text": "DISCLOSURE_TEXT",
        }
        desc = build_youtube_description(ms)
        assert len(desc) <= 5000
        assert desc.endswith("DISCLOSURE_TEXT")


# ---------------------------------------------------------------------------
# TestBuildYoutubeShortDescription
# ---------------------------------------------------------------------------

class TestBuildYoutubeShortDescription:
    def test_includes_short_title(self, minimal_metadata_source):
        desc = build_youtube_short_description(minimal_metadata_source, "sh_001")
        assert "3 Signs Atlantis Was Real" in desc

    def test_includes_disclosure(self, minimal_metadata_source):
        desc = build_youtube_short_description(minimal_metadata_source, "sh_001")
        assert "This video was produced with AI assistance." in desc

    def test_raises_when_disclosure_missing(self, metadata_missing_disclosure):
        with pytest.raises(MissingDisclosureError):
            build_youtube_short_description(metadata_missing_disclosure, "sh_001")

    def test_unknown_short_id_returns_disclosure_only(self, minimal_metadata_source):
        desc = build_youtube_short_description(minimal_metadata_source, "sh_999")
        assert "This video was produced with AI assistance." in desc


# ---------------------------------------------------------------------------
# TestGetShortTitle
# ---------------------------------------------------------------------------

class TestGetShortTitle:
    def test_dict_shorts_titles(self, minimal_metadata_source):
        title = get_short_title(minimal_metadata_source, "sh_001")
        assert title == "3 Signs Atlantis Was Real"

    def test_list_shorts_titles(self, metadata_list_shorts_titles):
        title = get_short_title(metadata_list_shorts_titles, "sh_001")
        assert title == "Short One Title"

    def test_list_shorts_titles_second_entry(self, metadata_list_shorts_titles):
        title = get_short_title(metadata_list_shorts_titles, "sh_002")
        assert title == "Short Two Title"

    def test_unknown_short_id_returns_empty(self, minimal_metadata_source):
        title = get_short_title(minimal_metadata_source, "sh_999")
        assert title == ""

    def test_empty_shorts_titles_returns_empty(self):
        ms = {"shorts_titles": {}}
        assert get_short_title(ms, "sh_001") == ""

    def test_missing_shorts_titles_returns_empty(self):
        ms = {}
        assert get_short_title(ms, "sh_001") == ""


# ---------------------------------------------------------------------------
# TestGetEpisodeTitle
# ---------------------------------------------------------------------------

class TestGetEpisodeTitle:
    def test_returns_title(self, minimal_metadata_source):
        title = get_episode_title(minimal_metadata_source)
        assert title == "The Lost City of Atlantis"

    def test_returns_empty_when_missing(self):
        ms = {}
        assert get_episode_title(ms) == ""

    def test_strips_whitespace(self):
        ms = {"episode_title": "  Padded Title  "}
        assert get_episode_title(ms) == "Padded Title"


# ---------------------------------------------------------------------------
# TestBuildFacebookDescription
# ---------------------------------------------------------------------------

class TestBuildFacebookDescription:
    def test_includes_logline(self, minimal_metadata_source):
        desc = build_facebook_description(minimal_metadata_source)
        assert "Historians uncover evidence" in desc

    def test_includes_disclosure(self, minimal_metadata_source):
        desc = build_facebook_description(minimal_metadata_source)
        assert "This video was produced with AI assistance." in desc

    def test_no_chapter_block(self, minimal_metadata_source):
        desc = build_facebook_description(minimal_metadata_source)
        # Facebook description should not include chapter timestamps
        assert "0:00 Introduction" not in desc

    def test_raises_when_disclosure_missing(self, metadata_missing_disclosure):
        with pytest.raises(MissingDisclosureError):
            build_facebook_description(metadata_missing_disclosure)


# ---------------------------------------------------------------------------
# TestBuildInstagramCaption
# ---------------------------------------------------------------------------

class TestBuildInstagramCaption:
    def test_includes_short_title(self, minimal_metadata_source):
        caption = build_instagram_caption(minimal_metadata_source, "sh_001")
        assert "3 Signs Atlantis Was Real" in caption

    def test_includes_disclosure(self, minimal_metadata_source):
        caption = build_instagram_caption(minimal_metadata_source, "sh_001")
        assert "This video was produced with AI assistance." in caption

    def test_raises_when_disclosure_missing(self, metadata_missing_disclosure):
        with pytest.raises(MissingDisclosureError):
            build_instagram_caption(metadata_missing_disclosure, "sh_001")


# ---------------------------------------------------------------------------
# TestBuildTiktokTitle
# ---------------------------------------------------------------------------

class TestBuildTiktokTitle:
    def test_returns_short_title(self, minimal_metadata_source):
        title = build_tiktok_title(minimal_metadata_source, "sh_002")
        assert title == "The Most Shocking Find"

    def test_falls_back_to_episode_title_when_short_not_found(self, minimal_metadata_source):
        title = build_tiktok_title(minimal_metadata_source, "sh_999")
        assert title == "The Lost City of Atlantis"

    def test_empty_shorts_titles_returns_episode_title(self):
        ms = {
            "episode_title": "Episode Title",
            "shorts_titles": {},
        }
        title = build_tiktok_title(ms, "sh_001")
        assert title == "Episode Title"


# ---------------------------------------------------------------------------
# TestBuildSourcesBlock
# ---------------------------------------------------------------------------

class TestBuildSourcesBlock:
    def test_empty_sources_returns_empty_string(self):
        assert _build_sources_block([]) == ""

    def test_title_and_url(self):
        sources = [{"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Foo"}]
        block = _build_sources_block(sources)
        assert "Sources:" in block
        assert "Wikipedia" in block
        assert "https://en.wikipedia.org/wiki/Foo" in block

    def test_url_only(self):
        sources = [{"title": "", "url": "https://example.com"}]
        block = _build_sources_block(sources)
        assert "https://example.com" in block

    def test_title_only(self):
        sources = [{"title": "Only Title", "url": ""}]
        block = _build_sources_block(sources)
        assert "Only Title" in block

    def test_all_empty_entries_returns_empty_string(self):
        sources = [{"title": "", "url": ""}]
        assert _build_sources_block(sources) == ""

    def test_multiple_sources_all_listed(self):
        sources = [
            {"title": "Source A", "url": "https://a.com"},
            {"title": "Source B", "url": "https://b.com"},
        ]
        block = _build_sources_block(sources)
        assert "Source A" in block
        assert "Source B" in block

    def test_non_dict_entries_skipped(self):
        sources = ["not_a_dict", {"title": "Valid", "url": "https://v.com"}]
        block = _build_sources_block(sources)
        assert "Valid" in block
        assert "not_a_dict" not in block


# ---------------------------------------------------------------------------
# TestBuildYoutubeDescriptionWithSources
# ---------------------------------------------------------------------------

class TestBuildYoutubeDescriptionWithSources:
    def test_includes_sources_block_when_present(self):
        ms = {
            "episode_title": "Test",
            "logline": "A logline.",
            "section_titles": [],
            "sources": [
                {"title": "Ancient Source", "url": "https://ancient.example.com"},
            ],
            "ai_disclosure_text": "AI disclosure.",
        }
        desc = build_youtube_description(ms)
        assert "Sources:" in desc
        assert "Ancient Source" in desc
        assert "https://ancient.example.com" in desc

    def test_no_sources_block_when_sources_empty(self):
        ms = {
            "episode_title": "Test",
            "logline": "A logline.",
            "section_titles": [],
            "sources": [],
            "ai_disclosure_text": "AI disclosure.",
        }
        desc = build_youtube_description(ms)
        assert "Sources:" not in desc

    def test_no_sources_block_when_sources_absent(self):
        ms = {
            "episode_title": "Test",
            "logline": "A logline.",
            "section_titles": [],
            "ai_disclosure_text": "AI disclosure.",
        }
        desc = build_youtube_description(ms)
        assert "Sources:" not in desc

    def test_disclosure_is_last_section_even_with_sources(self):
        ms = {
            "episode_title": "Test",
            "logline": "A logline.",
            "section_titles": [],
            "sources": [{"title": "Src", "url": "https://x.com"}],
            "ai_disclosure_text": "FINAL_DISCLOSURE",
        }
        desc = build_youtube_description(ms)
        assert desc.endswith("FINAL_DISCLOSURE")

    def test_sources_appear_before_disclosure(self):
        ms = {
            "episode_title": "Test",
            "logline": "Logline.",
            "section_titles": [],
            "sources": [{"title": "MySrc", "url": "https://src.com"}],
            "ai_disclosure_text": "Disclosure.",
        }
        desc = build_youtube_description(ms)
        sources_pos = desc.find("MySrc")
        disclosure_pos = desc.find("Disclosure.")
        assert sources_pos < disclosure_pos

    def test_multiple_sources_all_in_description(self):
        ms = {
            "episode_title": "Test",
            "logline": "Logline.",
            "section_titles": [],
            "sources": [
                {"title": "Src1", "url": "https://s1.com"},
                {"title": "Src2", "url": "https://s2.com"},
                {"title": "Src3", "url": "https://s3.com"},
            ],
            "ai_disclosure_text": "AI disclosure.",
        }
        desc = build_youtube_description(ms)
        for i in range(1, 4):
            assert f"Src{i}" in desc
