"""
tests/test_shorts_metadata.py — hypogeum.agents.shorts_metadata.generate_shorts_titles()

Covers the module that fixes the historical placeholder-title defect
(every episode in production history that ever populated
metadata_source.shorts_titles used {"short_id": sid, "title": sid} — the
short_id repeated as its own "title"). Every test here uses a FAKE model
fixture (a plain Python callable injected via llm_call=) — no real
OpenRouter/Claude call, no network, no cost.
"""

from __future__ import annotations

import pytest

from hypogeum.agents.shorts_metadata import (
    DEFAULT_CAPTION_RULES,
    DEFAULT_EDITORIAL_RULES,
    ShortsMetadataError,
    generate_shorts_captions,
    generate_shorts_titles,
)
from hypogeum.config import Config


@pytest.fixture()
def config():
    return Config()


REAL_SHORTS_SCRIPTS = [
    {
        "short_id": "short_001",
        "narration_beats": [
            {"beat_id": "b_030", "text": "There is a passage in these tunnels called a schlupf. It narrows to thirty or forty centimetres."},
            {"beat_id": "b_034", "text": "The schlupf has no structural purpose. It exists, consistently, across every known system."},
        ],
        "text_overlays": [{"text": "30 cm wide. Narrower than your shoulders."}],
    },
    {
        "short_id": "short_002",
        "narration_beats": [
            {"beat_id": "b_036", "text": "In 2026, archaeologists excavating near Reinstedt found a medieval tunnel entrance cut directly into the wall of a Stone Age tomb."},
        ],
        "text_overlays": [{"text": "2026: medieval tunnel found inside a Stone Age tomb"}],
    },
]


def _fake_llm_call_factory(titles_by_id: dict):
    def _fake(system_prompt, input_package, *, config, episode_id, channel_id):
        return {"titles": [{"short_id": sid, "title": t} for sid, t in titles_by_id.items()]}
    return _fake


class TestGenerateShortsTitles:
    def test_real_grounded_titles_pass_through(self, config):
        fake = _fake_llm_call_factory({
            "short_001": "Europe's Erdstall Tunnels Hide a Passage No One Can Explain",
            "short_002": "The Erdstall Tunnel Cut Into a 6,000-Year-Old Stone Age Tomb",
        })
        result = generate_shorts_titles(
            REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake,
        )
        assert {r["short_id"] for r in result} == {"short_001", "short_002"}
        by_id = {r["short_id"]: r["title"] for r in result}
        assert by_id["short_001"] == "Europe's Erdstall Tunnels Hide a Passage No One Can Explain"
        assert by_id["short_002"] == "The Erdstall Tunnel Cut Into a 6,000-Year-Old Stone Age Tomb"

    def test_count_and_deliverable_ids_match_bundle_definition(self, config):
        fake = _fake_llm_call_factory({
            "short_001": "A Real Title About The First Short",
            "short_002": "A Different Real Title About The Second Short",
        })
        result = generate_shorts_titles(
            REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake,
        )
        assert len(result) == len(REAL_SHORTS_SCRIPTS)
        assert {r["short_id"] for r in result} == {s["short_id"] for s in REAL_SHORTS_SCRIPTS}

    def test_placeholder_title_equal_to_short_id_is_rejected(self, config):
        fake = _fake_llm_call_factory({
            "short_001": "short_001",  # the historical placeholder defect
            "short_002": "A Real Title About The Second Short",
        })
        with pytest.raises(ShortsMetadataError, match="placeholder"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_missing_title_for_a_requested_short_is_rejected(self, config):
        fake = _fake_llm_call_factory({"short_001": "A Real Title About The First Short"})
        with pytest.raises(ShortsMetadataError, match="missing title"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_empty_title_is_rejected(self, config):
        fake = _fake_llm_call_factory({"short_001": "", "short_002": "A Real Title About The Second Short"})
        with pytest.raises(ShortsMetadataError, match="empty title"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_duplicate_titles_across_distinct_shorts_rejected(self, config):
        fake = _fake_llm_call_factory({
            "short_001": "The Same Exact Title Text",
            "short_002": "The Same Exact Title Text",
        })
        with pytest.raises(ShortsMetadataError, match="identical"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_title_too_long_rejected(self, config):
        too_long = "X" * (DEFAULT_EDITORIAL_RULES["max_length"] + 1)
        fake = _fake_llm_call_factory({"short_001": too_long, "short_002": "A Real Title About The Second Short"})
        with pytest.raises(ShortsMetadataError, match="outside the allowed"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_title_too_short_rejected(self, config):
        fake = _fake_llm_call_factory({"short_001": "Hi", "short_002": "A Real Title About The Second Short"})
        with pytest.raises(ShortsMetadataError, match="outside the allowed"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_channel_editorial_rules_override_defaults(self, config):
        # A Channel Package can loosen/tighten the generic Factory Core
        # defaults -- e.g. a channel that wants very short titles.
        fake = _fake_llm_call_factory({"short_001": "Short Title", "short_002": "Another Short Title"})
        result = generate_shorts_titles(
            REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake,
            editorial_rules={"min_length": 5, "max_length": 20},
        )
        assert len(result) == 2

    def test_llm_call_failure_raises_never_falls_back_to_placeholder(self, config):
        def _raising(*a, **k):
            raise RuntimeError("simulated provider failure")
        with pytest.raises(ShortsMetadataError, match="shorts title generation failed"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_raising)

    def test_malformed_response_missing_titles_key_raises(self, config):
        def _bad(system_prompt, input_package, *, config, episode_id, channel_id):
            return {"not_titles": []}
        with pytest.raises(ShortsMetadataError, match="'titles' list"):
            generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_bad)

    def test_empty_shorts_scripts_raises(self, config):
        with pytest.raises(ShortsMetadataError, match="nothing to title"):
            generate_shorts_titles([], config=config, episode_id="ep_test", llm_call=lambda *a, **k: {})

    def test_duplicate_short_id_in_input_raises(self, config):
        dup = [REAL_SHORTS_SCRIPTS[0], REAL_SHORTS_SCRIPTS[0]]
        with pytest.raises(ShortsMetadataError, match="duplicate"):
            generate_shorts_titles(dup, config=config, episode_id="ep_test", llm_call=lambda *a, **k: {})

    def test_grounding_only_passes_each_shorts_own_content_to_the_model(self, config):
        """The model call must receive ONLY each Short's own narration_beats/
        text_overlays -- never another Short's content, never episode-wide
        research -- so a title cannot reference an out-of-scope fact."""
        captured = {}

        def _capturing(system_prompt, input_package, *, config, episode_id, channel_id):
            captured["input_package"] = input_package
            return {"titles": [
                {"short_id": "short_001", "title": "A Real Title About The First Short"},
                {"short_id": "short_002", "title": "A Different Real Title About The Second"},
            ]}

        generate_shorts_titles(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_capturing)
        shorts_sent = captured["input_package"]["shorts"]
        assert len(shorts_sent) == 2
        sent_001 = next(s for s in shorts_sent if s["short_id"] == "short_001")
        assert "schlupf" in " ".join(sent_001["narration_beats"])
        assert "Reinstedt" not in " ".join(sent_001["narration_beats"])
        assert "Reinstedt" not in " ".join(sent_001["text_overlays"])


def _fake_caption_llm_call_factory(captions_by_id: dict):
    def _fake(system_prompt, input_package, *, config, episode_id, channel_id):
        return {"captions": [{"short_id": sid, "caption": c} for sid, c in captions_by_id.items()]}
    return _fake


class TestGenerateShortsCaptions:
    """
    2026-08-22 fix: the social-platform publication caption (Zernio's
    top-level `content` field) -- fixes ep_f3619022's "No content" defect
    (content="", 0 chars) the same way generate_shorts_titles() already
    fixed the historical placeholder-title defect. Every test uses a FAKE
    model fixture -- no real OpenRouter/Claude call, no network, no cost.
    """

    def test_real_grounded_captions_pass_through(self, config):
        fake = _fake_caption_llm_call_factory({
            "short_001": "A medieval passage in Europe narrows to just 30cm -- barely wider than your shoulders.",
            "short_002": "In 2026, archaeologists found a medieval tunnel cut directly through a Stone Age tomb.",
        })
        result = generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)
        assert {r["short_id"] for r in result} == {"short_001", "short_002"}
        by_id = {r["short_id"]: r["caption"] for r in result}
        assert by_id["short_001"].startswith("A medieval passage")
        assert by_id["short_002"].startswith("In 2026")

    def test_placeholder_caption_equal_to_short_id_is_rejected(self, config):
        fake = _fake_caption_llm_call_factory({"short_001": "short_001", "short_002": "A real distinct caption about the tomb entrance."})
        with pytest.raises(ShortsMetadataError, match="placeholder"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_empty_caption_is_rejected(self, config):
        fake = _fake_caption_llm_call_factory({"short_001": "", "short_002": "A real distinct caption about the tomb entrance."})
        with pytest.raises(ShortsMetadataError, match="empty"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_missing_caption_for_a_requested_short_is_rejected(self, config):
        fake = _fake_caption_llm_call_factory({"short_001": "A real, distinct caption about the schlupf passage."})
        with pytest.raises(ShortsMetadataError, match="missing"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_caption_too_long_rejected(self, config):
        fake = _fake_caption_llm_call_factory({
            "short_001": "x" * 3000,
            "short_002": "A real, distinct caption about the tomb entrance and its medieval passage.",
        })
        with pytest.raises(ShortsMetadataError, match="outside the allowed"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_caption_too_short_rejected(self, config):
        fake = _fake_caption_llm_call_factory({
            "short_001": "Short.",
            "short_002": "A real, distinct caption about the tomb entrance and its medieval passage.",
        })
        with pytest.raises(ShortsMetadataError, match="outside the allowed"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)

    def test_channel_caption_rules_override_defaults(self, config):
        fake = _fake_caption_llm_call_factory({
            "short_001": "Tiny.",
            "short_002": "A real, distinct caption about the tomb entrance and its medieval passage.",
        })
        result = generate_shorts_captions(
            REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test",
            caption_rules={"min_length": 3}, llm_call=fake,
        )
        assert next(r for r in result if r["short_id"] == "short_001")["caption"] == "Tiny."

    def test_llm_call_failure_raises_never_falls_back_to_empty_content(self, config):
        def _raising(*a, **k):
            raise RuntimeError("upstream model call failed")
        with pytest.raises(ShortsMetadataError):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_raising)

    def test_malformed_response_missing_captions_key_raises(self, config):
        def _malformed(*a, **k):
            return {"not_captions": []}
        with pytest.raises(ShortsMetadataError, match="captions"):
            generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_malformed)

    def test_empty_shorts_scripts_raises(self, config):
        with pytest.raises(ShortsMetadataError):
            generate_shorts_captions([], config=config, episode_id="ep_test", llm_call=lambda *a, **k: {})

    def test_duplicate_short_id_in_input_raises(self, config):
        dup = [REAL_SHORTS_SCRIPTS[0], REAL_SHORTS_SCRIPTS[0]]
        with pytest.raises(ShortsMetadataError, match="duplicate"):
            generate_shorts_captions(dup, config=config, episode_id="ep_test", llm_call=lambda *a, **k: {})

    def test_grounding_only_passes_each_shorts_own_content_to_the_model(self, config):
        captured = {}

        def _capturing(system_prompt, input_package, *, config, episode_id, channel_id):
            captured["input_package"] = input_package
            return {"captions": [
                {"short_id": "short_001", "caption": "A real caption grounded in the schlupf passage only."},
                {"short_id": "short_002", "caption": "A different real caption grounded in the tomb entrance only."},
            ]}

        generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=_capturing)
        shorts_sent = captured["input_package"]["shorts"]
        sent_001 = next(s for s in shorts_sent if s["short_id"] == "short_001")
        assert "schlupf" in " ".join(sent_001["narration_beats"])
        assert "Reinstedt" not in " ".join(sent_001["narration_beats"])

    def test_caption_never_reused_as_burned_in_or_title_field(self, config):
        """Sanity: generate_shorts_captions() returns a distinct 'caption'
        key, never 'title' or a burned-in-caption field -- proves the three
        concerns (YouTube title, social caption, burned pixels) stay
        independently keyed and can never be silently swapped."""
        fake = _fake_caption_llm_call_factory({
            "short_001": "A real caption about the schlupf passage in these tunnels.",
            "short_002": "A real caption about the tomb entrance found in 2026.",
        })
        result = generate_shorts_captions(REAL_SHORTS_SCRIPTS, config=config, episode_id="ep_test", llm_call=fake)
        for entry in result:
            assert set(entry.keys()) == {"short_id", "caption"}
