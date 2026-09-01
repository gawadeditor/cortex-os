"""channel_creative_brief + channel_thumbnail_identity: every content agent
reads the channel's own editorial identity instead of a hardcoded
'history and ancient mystery' genre."""

from __future__ import annotations

import pytest

from hypogeum.channel_creative_brief import build_creative_brief, channel_brief_for
from hypogeum.channel_config import create_brand_profile, create_channel_profile


def _seed(db, *, tone, audience, pillars, name="Twinkle Tales", genre_line="Genre: soothing bedtime stories for toddlers"):
    bp = create_brand_profile(
        "Brand",
        visual_style="soft pastel storybook, rounded characters, dreamy bokeh",
        tone=tone,
        target_audience=audience,
        color_palette={"night": "#2b1a4a", "glow": "#f4d9a0"},
        content_pillars=pillars,
        voice_guidelines="slow, hushed, musical; short sentences",
        db_path=db,
    )
    create_channel_profile(
        "twinkle", name,
        brand_profile_id=bp.brand_profile_id,
        description=genre_line,
        db_path=db,
    )


@pytest.fixture
def _route_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    import hypogeum.channel_config as cc
    monkeypatch.setattr(cc, "_resolve_db_path", lambda p: db)
    return db


def test_fallback_when_no_profile():
    brief = build_creative_brief("nonexistent-channel")
    assert brief["_source"].startswith("fallback")
    assert "history" not in brief["genre"].lower()
    assert brief["topic_taxonomy"] == []


def test_channel_brief_for_tolerates_none():
    brief = channel_brief_for(None)
    assert isinstance(brief, dict)
    assert "what_makes_a_good_topic" in brief


def test_gentle_channel_gets_soothing_standard(_route_db):
    _seed(
        _route_db,
        tone="gentle whimsical soothing",
        audience="toddlers aged 2-5 and their parents",
        pillars=["Sleepy animal adventures", "Magical dreamland journeys",
                 "Gentle moral moments", "Lullaby endings"],
    )
    brief = build_creative_brief("twinkle")
    assert brief["_source"] == "brand_profile"
    assert brief["channel_name"] == "Twinkle Tales"
    # standard of quality is bedtime-shaped, not mystery-shaped
    std = brief["what_makes_a_good_topic"].lower()
    assert "soothing" in std or "wind-down" in std
    assert "unresolved mystery" not in std or "over" in std  # gentle branch explicitly de-prioritises it
    # taxonomy comes from the channel's own pillars
    assert "sleepy_animal_adventures" in brief["topic_taxonomy"]
    assert "ancient_mystery" not in brief["topic_taxonomy"]
    assert brief["voice_notes"].startswith("slow, hushed")


def test_investigative_channel_gets_investigative_standard(_route_db):
    _seed(
        _route_db,
        tone="investigative documentary rigorous",
        audience="history enthusiasts",
        pillars=["Ancient engineering", "Lost civilisations"],
        name="Deep Dig",
        genre_line="Genre: archaeological investigation",
    )
    brief = build_creative_brief("twinkle")
    std = brief["what_makes_a_good_topic"].lower()
    assert "unresolved questions" in std
    assert "ancient_engineering" in brief["topic_taxonomy"]


def test_thumbnail_identity_derived_from_profile(_route_db):
    _seed(
        _route_db,
        tone="gentle whimsical soothing",
        audience="toddlers",
        pillars=["Sleepy animal adventures"],
    )
    from hypogeum.brand.channel_thumbnail_identity import channel_thumbnail_identity_rules

    rules = channel_thumbnail_identity_rules("twinkle", "main_episode")
    assert rules is not None
    assert rules.wordmark_required is False
    assert rules.human_figure_policy == "unrestricted"
    assert rules.forbidden_style_language == []
    # near-black is explicitly NOT required (it may only appear as a negation)
    assert "do not require a dark" in rules.palette_description.lower()
    assert "pastel" in rules.palette_description.lower()


def test_thumbnail_identity_none_without_profile():
    from hypogeum.brand.channel_thumbnail_identity import channel_thumbnail_identity_rules
    assert channel_thumbnail_identity_rules("nonexistent", "main_episode") is None
    assert channel_thumbnail_identity_rules("whatever", "unknown_class") is None
