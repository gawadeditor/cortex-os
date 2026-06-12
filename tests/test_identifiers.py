"""
tests/test_identifiers.py — Identifier generation test suite

Coverage:
    TestMetadataVersion              — METADATA_VERSION constant value and type
    TestGenerateRunId                — UUID4 format, uniqueness, type
    TestGenerateAssetId              — determinism, normalization, length, hex
    TestGenerateTitleId              — determinism, normalization, episode binding
    TestGenerateThumbnailCandidateId — determinism, normalization, stability
    TestCollisionSafety              — practical collision resistance at scale
    TestBackwardCompatibility        — existing episode_id formats accepted
"""

from __future__ import annotations

import re
import uuid

import pytest

from hypogeum.identifiers import (
    METADATA_VERSION,
    generate_asset_id,
    generate_run_id,
    generate_thumbnail_candidate_id,
    generate_title_id,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_EP = "ep_000"
_EP_ALT = "ep_001"

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HEX_RE = re.compile(r"^[0-9a-f]+$")


# ---------------------------------------------------------------------------
# TestMetadataVersion
# ---------------------------------------------------------------------------

class TestMetadataVersion:
    def test_value_is_v1(self):
        assert METADATA_VERSION == "v1"

    def test_is_string(self):
        assert isinstance(METADATA_VERSION, str)


# ---------------------------------------------------------------------------
# TestGenerateRunId
# ---------------------------------------------------------------------------

class TestGenerateRunId:
    def test_returns_string(self):
        assert isinstance(generate_run_id(), str)

    def test_uuid4_format(self):
        run_id = generate_run_id()
        assert _UUID4_RE.match(run_id), f"Not a valid UUID4: {run_id!r}"

    def test_unique_across_calls(self):
        ids = {generate_run_id() for _ in range(20)}
        assert len(ids) == 20

    def test_not_empty(self):
        assert generate_run_id() != ""

    def test_parses_as_uuid(self):
        # Confirm Python's uuid module agrees with the format
        run_id = generate_run_id()
        parsed = uuid.UUID(run_id)
        assert parsed.version == 4


# ---------------------------------------------------------------------------
# TestGenerateAssetId
# ---------------------------------------------------------------------------

class TestGenerateAssetId:
    def test_returns_string(self):
        result = generate_asset_id(_EP, "video_clip", "muapi")
        assert isinstance(result, str)

    def test_length_is_16(self):
        result = generate_asset_id(_EP, "video_clip", "muapi")
        assert len(result) == 16

    def test_is_lowercase_hex(self):
        result = generate_asset_id(_EP, "video_clip", "muapi", "a prompt", 0)
        assert _HEX_RE.match(result), f"Not lowercase hex: {result!r}"

    def test_deterministic(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "some prompt", 2)
        b = generate_asset_id(_EP, "video_clip", "muapi", "some prompt", 2)
        assert a == b

    def test_different_episode_id(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "prompt")
        b = generate_asset_id(_EP_ALT, "video_clip", "muapi", "prompt")
        assert a != b

    def test_different_asset_type(self):
        a = generate_asset_id(_EP, "video_clip", "muapi")
        b = generate_asset_id(_EP, "narration_segment", "muapi")
        assert a != b

    def test_different_provider(self):
        a = generate_asset_id(_EP, "thumbnail", "muapi")
        b = generate_asset_id(_EP, "thumbnail", "elevenlabs")
        assert a != b

    def test_different_prompt(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "prompt A")
        b = generate_asset_id(_EP, "video_clip", "muapi", "prompt B")
        assert a != b

    def test_different_sequence_index(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "same prompt", 0)
        b = generate_asset_id(_EP, "video_clip", "muapi", "same prompt", 1)
        assert a != b

    def test_asset_type_case_normalized(self):
        a = generate_asset_id(_EP, "Video_Clip", "muapi")
        b = generate_asset_id(_EP, "video_clip", "muapi")
        assert a == b

    def test_provider_case_normalized(self):
        a = generate_asset_id(_EP, "thumbnail", "MUAPI")
        b = generate_asset_id(_EP, "thumbnail", "muapi")
        assert a == b

    def test_episode_id_stripped(self):
        a = generate_asset_id("  ep_000  ", "video_clip", "muapi")
        b = generate_asset_id("ep_000", "video_clip", "muapi")
        assert a == b

    def test_prompt_stripped(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "  corroded bronze gears  ")
        b = generate_asset_id(_EP, "video_clip", "muapi", "corroded bronze gears")
        assert a == b

    def test_prompt_case_preserved(self):
        # Prompts are passed directly to image models; internal case is significant
        a = generate_asset_id(_EP, "video_clip", "muapi", "Corroded Bronze Gears")
        b = generate_asset_id(_EP, "video_clip", "muapi", "corroded bronze gears")
        assert a != b

    def test_prompt_internal_whitespace_preserved(self):
        # Internal whitespace in prompts is significant (different prompts)
        a = generate_asset_id(_EP, "video_clip", "muapi", "corroded  bronze")
        b = generate_asset_id(_EP, "video_clip", "muapi", "corroded bronze")
        assert a != b

    def test_empty_prompt_is_valid(self):
        result = generate_asset_id(_EP, "final_video", "assembly")
        assert len(result) == 16

    def test_default_sequence_index_is_zero(self):
        a = generate_asset_id(_EP, "video_clip", "muapi", "prompt")
        b = generate_asset_id(_EP, "video_clip", "muapi", "prompt", 0)
        assert a == b


# ---------------------------------------------------------------------------
# TestGenerateTitleId
# ---------------------------------------------------------------------------

class TestGenerateTitleId:
    def test_returns_string(self):
        assert isinstance(generate_title_id("The Computer That Vanished", _EP), str)

    def test_length_is_12(self):
        result = generate_title_id("The Computer That Vanished", _EP)
        assert len(result) == 12

    def test_is_lowercase_hex(self):
        result = generate_title_id("The Computer That Vanished", _EP)
        assert _HEX_RE.match(result)

    def test_deterministic(self):
        a = generate_title_id("The Computer That Vanished", _EP)
        b = generate_title_id("The Computer That Vanished", _EP)
        assert a == b

    def test_case_normalized(self):
        a = generate_title_id("THE COMPUTER THAT VANISHED", _EP)
        b = generate_title_id("the computer that vanished", _EP)
        c = generate_title_id("The Computer That Vanished", _EP)
        assert a == b == c

    def test_internal_whitespace_collapsed(self):
        a = generate_title_id("The  Computer   That  Vanished", _EP)
        b = generate_title_id("The Computer That Vanished", _EP)
        assert a == b

    def test_leading_trailing_whitespace_stripped(self):
        a = generate_title_id("  The Computer That Vanished  ", _EP)
        b = generate_title_id("The Computer That Vanished", _EP)
        assert a == b

    def test_different_titles_produce_different_ids(self):
        a = generate_title_id("The Computer That Vanished", _EP)
        b = generate_title_id("Then They Forgot", _EP)
        assert a != b

    def test_different_episodes_produce_different_ids(self):
        a = generate_title_id("The Computer That Vanished", _EP)
        b = generate_title_id("The Computer That Vanished", _EP_ALT)
        assert a != b

    def test_episode_id_stripped(self):
        a = generate_title_id("The Title", "  ep_000  ")
        b = generate_title_id("The Title", "ep_000")
        assert a == b


# ---------------------------------------------------------------------------
# TestGenerateThumbnailCandidateId
# ---------------------------------------------------------------------------

class TestGenerateThumbnailCandidateId:
    _PROMPT = (
        "Corroded bronze gear fragments with green-brown marine patina, "
        "partial wooden housing, dark museum background, 4K macro photography"
    )
    _OVERLAY = "THE COMPUTER THAT VANISHED"

    def test_returns_string(self):
        assert isinstance(
            generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY),
            str,
        )

    def test_length_is_14(self):
        result = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        assert len(result) == 14

    def test_is_lowercase_hex(self):
        result = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        assert _HEX_RE.match(result)

    def test_deterministic(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        assert a == b

    def test_stable_across_simulated_reruns(self):
        # Simulates two pipeline runs with identical TD output for the same candidate
        run1 = generate_thumbnail_candidate_id("ep_000", 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        run2 = generate_thumbnail_candidate_id("ep_000", 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        assert run1 == run2

    def test_different_ranks_produce_different_ids(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP, 2, self._PROMPT, self._OVERLAY)
        assert a != b

    def test_different_prompts_produce_different_ids(self):
        a = generate_thumbnail_candidate_id(_EP, 1, "Prompt A", self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP, 1, "Prompt B", self._OVERLAY)
        assert a != b

    def test_different_overlays_produce_different_ids(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THEN IT DISAPPEARED")
        assert a != b

    def test_different_episodes_produce_different_ids(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP_ALT, 1, self._PROMPT, self._OVERLAY)
        assert a != b

    def test_text_overlay_case_normalized(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "the computer that vanished")
        c = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "The Computer That Vanished")
        assert a == b == c

    def test_text_overlay_whitespace_normalized(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THE COMPUTER  THAT  VANISHED")
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        assert a == b

    def test_text_overlay_stripped(self):
        a = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "  THE COMPUTER THAT VANISHED  ")
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, "THE COMPUTER THAT VANISHED")
        assert a == b

    def test_visual_prompt_case_preserved(self):
        # Prompts are semantically case-sensitive for image generation models
        a = generate_thumbnail_candidate_id(_EP, 1, "Corroded Bronze Gears", self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP, 1, "corroded bronze gears", self._OVERLAY)
        assert a != b

    def test_visual_prompt_stripped(self):
        a = generate_thumbnail_candidate_id(_EP, 1, "  " + self._PROMPT + "  ", self._OVERLAY)
        b = generate_thumbnail_candidate_id(_EP, 1, self._PROMPT, self._OVERLAY)
        assert a == b

    def test_episode_id_stripped(self):
        a = generate_thumbnail_candidate_id("  ep_000  ", 1, self._PROMPT, self._OVERLAY)
        b = generate_thumbnail_candidate_id("ep_000", 1, self._PROMPT, self._OVERLAY)
        assert a == b


# ---------------------------------------------------------------------------
# TestCollisionSafety
# ---------------------------------------------------------------------------

class TestCollisionSafety:
    def test_asset_ids_no_collision_across_sequence_indices(self):
        ids = [
            generate_asset_id(_EP, "video_clip", "muapi", "same prompt", i)
            for i in range(500)
        ]
        assert len(set(ids)) == 500

    def test_title_ids_no_collision_across_titles(self):
        titles = [f"Episode Title Number {i}" for i in range(200)]
        ids = [generate_title_id(t, _EP) for t in titles]
        assert len(set(ids)) == 200

    def test_thumbnail_candidate_ids_no_collision_across_ranks_and_episodes(self):
        ids = []
        for ep in [f"ep_{i:03d}" for i in range(20)]:
            for rank in range(1, 6):
                ids.append(
                    generate_thumbnail_candidate_id(ep, rank, "prompt", "overlay")
                )
        assert len(set(ids)) == len(ids)

    def test_asset_ids_no_collision_across_asset_types(self):
        asset_types = [
            "video_clip", "narration_segment", "thumbnail", "music",
            "reference_image", "final_video",
        ]
        ids = [generate_asset_id(_EP, t, "muapi", "prompt") for t in asset_types]
        assert len(set(ids)) == len(asset_types)


# ---------------------------------------------------------------------------
# TestBackwardCompatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_ep_000_format_accepted(self):
        result = generate_asset_id("ep_000", "video_clip", "muapi")
        assert len(result) == 16

    def test_ep_smoke_01_format_accepted(self):
        result = generate_asset_id("ep_smoke_01", "video_clip", "muapi")
        assert len(result) == 16

    def test_ep_000_lite_format_accepted(self):
        result = generate_asset_id("ep_000_lite", "thumbnail", "muapi")
        assert len(result) == 16

    def test_episode_ids_with_hyphens_accepted(self):
        result = generate_asset_id("ep-val-01", "video_clip", "muapi")
        assert len(result) == 16

    def test_existing_title_ids_stable(self):
        # Reference value computed from the first ep_000 TD output.
        # Changing normalization logic must not silently break this.
        title = "THE COMPUTER THAT VANISHED"
        ep = "ep_000"
        result = generate_title_id(title, ep)
        # Re-derive expected: any change to normalization logic will fail this test.
        import hashlib, re
        normalized_title = re.sub(r"\s+", " ", title.strip().lower())
        normalized_ep = ep.strip().lower()
        raw = f"{normalized_ep}|{normalized_title}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        assert result == expected

    def test_existing_thumbnail_candidate_ids_stable(self):
        # Reference value for tc_001 from ep_000 run.
        ep = "ep_000"
        rank = 1
        prompt = "Corroded bronze gear fragments"
        overlay = "THE COMPUTER THAT VANISHED"
        result = generate_thumbnail_candidate_id(ep, rank, prompt, overlay)
        import hashlib, re
        normalized = "|".join([
            ep.strip().lower(),
            str(rank),
            prompt.strip(),
            re.sub(r"\s+", " ", overlay.strip().lower()),
        ])
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:14]
        assert result == expected
