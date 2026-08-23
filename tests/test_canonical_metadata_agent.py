"""
Focused unit coverage for hypogeum.agents.canonical_metadata_agent:

- _default_llm_call(): the single-shot, real production LLM-call wrapper.
- generate_canonical_platform_metadata_for_deliverable()'s own bounded
  automatic retry loop (JSON parse failures AND field-validation
  failures alike, fed back into the next attempt, max 3 attempts total)
  and its policy-completeness pre-check (a platform whose Channel
  Package lacks a contract-required field is never generated for, and
  the model is never called at all once no platform remains
  generatable).

Every other test of this module (see tests/test_zernio_channel_release_trigger.py,
tests/test_cross_platform_publishing.py) exercises
generate_canonical_platform_metadata_for_deliverable() with an injected
fake llm_call for its "happy path" wiring -- this file is where the
retry/pre-check behavior itself is proven directly.
"""
import json

import pytest

from hypogeum.agents.canonical_metadata_agent import (
    CanonicalMetadataGenerationError,
    _default_llm_call,
    generate_canonical_platform_metadata_for_deliverable,
)
from hypogeum.config import Config
from hypogeum.openrouter import CallResult
from hypogeum.publishing.canonical_metadata import (
    CanonicalPlatformMetadata,
    is_required_policy_field_present,
    materialize_youtube_main_episode_description,
    validate_canonical_platform_metadata,
)

EPISODE_ID = "ep_test"


def _result(content: str) -> CallResult:
    return CallResult(
        content=content, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model_used="anthropic/claude-sonnet-4.6", finish_reason="stop",
        response_time_ms=1, via_fallback=False, estimated_cost_usd=0.0,
    )


class TestDefaultLlmCallSingleShot:
    """_default_llm_call() itself no longer retries -- that responsibility
    moved to generate_canonical_platform_metadata_for_deliverable()'s own
    unified bounded-retry loop (2026-08-23), so max attempts across BOTH
    JSON-parse and validation failures is one real number (3), not two
    independently-bounded loops stacked on each other."""

    def test_valid_json_makes_exactly_one_call(self, monkeypatch):
        calls = []

        def fake_call_claude(**kwargs):
            calls.append(kwargs)
            return _result('{"platforms": {"youtube": {"description": "d"}}}')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        result = _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        assert result == {"platforms": {"youtube": {"description": "d"}}}
        assert len(calls) == 1

    def test_malformed_json_raises_immediately_no_internal_retry(self, monkeypatch):
        def fake_call_claude(**kwargs):
            return _result('{"platforms": {"youtube": {"description": "he said "hi" to me"}}}')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        with pytest.raises(json.JSONDecodeError):
            _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")

    def test_code_fence_wrapped_json_still_parses(self, monkeypatch):
        def fake_call_claude(**kwargs):
            return _result('```json\n{"platforms": {"youtube": {"description": "d"}}}\n```')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        result = _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        assert result == {"platforms": {"youtube": {"description": "d"}}}


# ---------------------------------------------------------------------------
# Fixtures for generate_canonical_platform_metadata_for_deliverable()
# ---------------------------------------------------------------------------

def _seed_short_grounding(episode_dir, deliverable_id="short_001"):
    (episode_dir / f"research_package_{EPISODE_ID}.json").write_text(json.dumps({
        "source_registry": [
            {"source_id": "src_001", "url": "https://example.com/a", "inferred_type": "academic"},
            {"source_id": "src_002", "url": "https://example.com/b", "inferred_type": "news_archive"},
        ],
        "claims": [{"claim_id": "clm_001", "sources": ["src_001"]}],
    }), encoding="utf-8")
    final_render_package = {
        "metadata_source": {
            "shorts_captions": [
                {"short_id": deliverable_id, "caption": "A real, script-grounded caption about the mystery tunnels."},
            ],
        },
    }
    return final_render_package


def _seed_main_episode_grounding(episode_dir, *, sources=None):
    (episode_dir / f"script_package_{EPISODE_ID}.json").write_text(json.dumps({
        "sections": [
            {"section_id": "s1", "narration_beats": [{"text": "A real narrated fact about the tunnels."}]},
            {"section_id": "s2", "narration_beats": [{"text": "A second real narrated fact."}]},
            {"section_id": "s3", "narration_beats": [{"text": "A third real narrated fact."}]},
        ],
        "chapter_markers": [
            {"section_id": "s1", "title": "Intro"},
            {"section_id": "s2", "title": "Middle"},
            {"section_id": "s3", "title": "End"},
        ],
    }), encoding="utf-8")
    (episode_dir / f"research_package_{EPISODE_ID}.json").write_text(json.dumps({
        "source_registry": sources or [
            {"source_id": "src_001", "url": "https://example.com/source", "inferred_type": "academic"},
        ],
        "claims": [{"claim_id": "clm_001", "sources": ["src_001"]}],
    }), encoding="utf-8")
    return {
        "metadata_source": {
            "section_titles": [
                {"section_id": "s1", "start_seconds": 0.0},
                {"section_id": "s2", "start_seconds": 12.0},
                {"section_id": "s3", "start_seconds": 24.0},
            ],
        },
    }


_HYPOGEUM_POLICY = {
    "youtube": {"shorts": {"enabled": True, "categoryId": "27", "ai_disclosure": True}},
}
_MAIN_EPISODE_POLICY = {"youtube": {"main_episode": {"enabled": True, "categoryId": "27", "ai_disclosure": True}}}

_VALID_YOUTUBE_RESPONSE = {
    "platforms": {
        "youtube": {
            "title": "A Real Title",
            "description": "A real, grounded description about the mystery tunnels. #Mystery",
            "visible_hashtags": ["#Mystery"],
            "cta": "Subscribe for more.",
        },
    },
}


class TestBoundedAutomaticRegeneration:
    """
    Real bug found live against ep_f3619022's main_episode: the model's
    first-attempt description ran 5055 chars against YouTube's 5000-char
    limit. The old behavior required a human to manually rerun the
    generator -- a permanent autonomy defect. Fixed: up to 3 automatic
    attempts, the exact validation error fed back into the next attempt,
    never a silent truncation, only a fully validated package ever
    returned/persisted.
    """

    def test_json_parse_failure_then_valid_response_succeeds_automatically(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        calls = []

        def fake_llm_call(system_prompt, input_package, **kwargs):
            calls.append(system_prompt)
            if len(calls) == 1:
                raise json.JSONDecodeError("bad", "doc", 0)
            return _VALID_YOUTUBE_RESPONSE

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        assert len(calls) == 2
        assert entries[0]["description"].startswith("A real, grounded description")
        # No manual rerun needed -- the second call's prompt carries the
        # first failure's exact reason.
        assert "could not be parsed as valid JSON" in calls[1]

    def test_5055_char_description_then_valid_response_succeeds_automatically(self, tmp_path):
        # The exact real-world reproduction: attempt 1 exceeds YouTube's
        # 5000-char limit, attempt 2 is valid -- no manual rerun.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        oversized_description = ("x" * 5055) + " #Mystery"
        calls = []

        def fake_llm_call(system_prompt, input_package, **kwargs):
            calls.append(system_prompt)
            if len(calls) == 1:
                return {"platforms": {"youtube": {
                    "title": "A Real Title", "description": oversized_description,
                    "visible_hashtags": ["#Mystery"], "cta": "Subscribe.",
                }}}
            return _VALID_YOUTUBE_RESPONSE

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        assert len(calls) == 2
        assert len(entries[0]["description"]) <= 5000
        # The retry prompt names the exact problem, including the measured length.
        assert "5055" in calls[1] or "5000" in calls[1]
        assert "exceeding" in calls[1]

    def test_never_silently_truncates_persists_only_the_regenerated_valid_copy(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        oversized = ("y" * 6000) + " #Mystery"

        def fake_llm_call(system_prompt, input_package, **kwargs):
            if "exceeding" not in system_prompt:
                return {"platforms": {"youtube": {
                    "title": "T", "description": oversized, "visible_hashtags": ["#Mystery"], "cta": "c",
                }}}
            return _VALID_YOUTUBE_RESPONSE

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        # The persisted description is the SECOND (valid, regenerated) one,
        # never the first one truncated down to fit.
        assert entries[0]["description"] == _VALID_YOUTUBE_RESPONSE["platforms"]["youtube"]["description"]
        assert oversized not in entries[0]["description"]

    def test_three_invalid_attempts_fail_closed_with_zero_transport_calls(self, tmp_path, monkeypatch):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        calls = []

        def fake_llm_call(system_prompt, input_package, **kwargs):
            calls.append(system_prompt)
            # Always empty description -- never correctable, proves the
            # bound is real (3), not infinite.
            return {"platforms": {"youtube": {"title": "T", "description": "", "visible_hashtags": ["#x"], "cta": "c"}}}

        def _must_not_be_called(*a, **kw):
            raise AssertionError("Zernio must never be called when generation fails closed")

        monkeypatch.setattr("hypogeum.publishing.zernio.publish_multi_platform", _must_not_be_called, raising=False)

        with pytest.raises(CanonicalMetadataGenerationError, match="after 3 attempts"):
            generate_canonical_platform_metadata_for_deliverable(
                EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
                config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
                llm_call=fake_llm_call,
            )
        assert len(calls) == 3  # exactly the bound, never more
        # Every retry prompt after the first names the exact prior failure.
        assert "description is empty" in calls[1]
        assert "description is empty" in calls[2]

    def test_each_retry_prompt_carries_every_distinct_error_not_just_the_last(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        calls = []

        def fake_llm_call(system_prompt, input_package, **kwargs):
            calls.append(system_prompt)
            if len(calls) == 1:
                # Two distinct problems in one response: no hashtags AND
                # an overlong title.
                return {"platforms": {"youtube": {
                    "title": "T" * 150, "description": "A real grounded description here.",
                    "visible_hashtags": [], "cta": "c",
                }}}
            return _VALID_YOUTUBE_RESPONSE

        generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        assert "visible_hashtags is empty" in calls[1]
        assert "150 chars" in calls[1]

    def test_hashtag_substring_match_is_rejected_not_accepted_then_recovers(self, tmp_path):
        # Real bug: the old check was 'h.lower() in description.lower()',
        # a substring match -- '#Test' would incorrectly pass against a
        # description that only contains '#TestTagExtra'. Proves the real
        # token-boundary validator (hypogeum.publishing.canonical_metadata.
        # extract_hashtag_tokens(), the ONE shared validator with the
        # readiness-side check) rejects this, AND that the retry loop
        # recovers automatically once attempt 2 declares a real token.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        calls = []

        def fake_llm_call(system_prompt, input_package, **kwargs):
            calls.append(system_prompt)
            if len(calls) == 1:
                return {"platforms": {"youtube": {
                    "title": "T", "cta": "c",
                    "description": "A real grounded description mentioning #TestTagExtra only.",
                    "visible_hashtags": ["#Test"],
                }}}
            return {"platforms": {"youtube": {
                "title": "T", "cta": "c",
                "description": "A real grounded description mentioning #Test directly.",
                "visible_hashtags": ["#Test"],
            }}}

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        assert len(calls) == 2
        assert "#Test" in calls[1]  # the exact rejected hashtag reaches the retry prompt
        assert "complete hashtag tokens" in calls[1]
        assert entries[0]["visible_hashtags"] == ["#Test"]
        assert entries[0]["description"] == "A real grounded description mentioning #Test directly."


class TestPolicyCompletenessPreCheck:
    """
    2026-08-23 operator decision: categoryId is mandatory once youtube is
    enabled for a deliverable class -- the VALUE stays channel-owned
    (never hardcoded here), but Factory Core must refuse to even attempt
    generation when the Channel Package hasn't declared one, spending
    zero model calls on a platform it already knows is misconfigured.
    """

    def test_missing_required_policy_field_skips_generation_zero_calls(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        policy_without_category = {"youtube": {"shorts": {"enabled": True}}}  # no categoryId
        calls = []

        def fake_llm_call(*a, **kw):
            calls.append(1)
            return _VALID_YOUTUBE_RESPONSE

        with pytest.raises(CanonicalMetadataGenerationError, match="categoryId"):
            generate_canonical_platform_metadata_for_deliverable(
                EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
                config=Config(), channel_id="hypogeum", platform_deliverable_policy=policy_without_category,
                llm_call=fake_llm_call,
            )
        assert calls == []  # the model was never called

    def test_blank_categoryid_fails_and_skips_generation(self, tmp_path):
        # An explicit but blank/whitespace-only value is not a real
        # configured category any more than a missing key is.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        policy_blank_category = {"youtube": {"shorts": {"enabled": True, "categoryId": "   "}}}
        calls = []

        def fake_llm_call(*a, **kw):
            calls.append(1)
            return _VALID_YOUTUBE_RESPONSE

        with pytest.raises(CanonicalMetadataGenerationError, match="categoryId"):
            generate_canonical_platform_metadata_for_deliverable(
                EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
                config=Config(), channel_id="hypogeum", platform_deliverable_policy=policy_blank_category,
                llm_call=fake_llm_call,
            )
        assert calls == []

    def test_required_boolean_field_explicitly_false_is_present_not_missing(self, tmp_path):
        # Real bug this fixes: TikTok's required_policy_fields
        # (allow_comment/allow_duet/allow_stitch/content_preview_confirmed/
        # express_consent_given) are all booleans -- an operator setting
        # allow_duet=False (a deliberate, valid opt-out) was previously
        # treated as "missing" by 'not class_entry.get(field)', wrongly
        # blocking TikTok generation entirely. Every one of these fields
        # explicitly False here must NOT block generation.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        policy = {
            "tiktok": {"shorts": {
                "enabled": True,
                "content_preview_confirmed": True, "express_consent_given": True,
                "allow_comment": True, "allow_duet": False, "allow_stitch": False,
            }},
        }

        def fake_llm_call(system_prompt, input_package, **kw):
            return {"platforms": {"tiktok": {
                "description": "A real grounded description for tiktok. #Mystery",
                "visible_hashtags": ["#Mystery"], "cta": "c",
            }}}

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["tiktok"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=policy,
            llm_call=fake_llm_call,
        )
        assert entries[0]["platform"] == "tiktok"

    def test_present_required_policy_field_generates_normally(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)

        def fake_llm_call(*a, **kw):
            return _VALID_YOUTUBE_RESPONSE

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        assert entries[0]["category_id"] == "27"

    def test_one_incomplete_platform_fails_the_whole_deliverable_atomically(self, tmp_path):
        # Fixed 2026-08-23: the OLD behavior silently dropped youtube
        # (missing categoryId) and generated instagram anyway -- that
        # violates the publishing invariant "every enabled target
        # (deliverable_id, platform) pair must be ready before any
        # platform for that deliverable is submitted". An incomplete
        # sibling must fail the ENTIRE deliverable's generation, not just
        # exclude itself while a complete platform proceeds.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        policy = {
            "youtube": {"shorts": {"enabled": True}},  # missing categoryId
            "instagram": {"shorts": {"enabled": True}},
        }
        calls = []

        def fake_llm_call(*a, **kw):
            calls.append(1)
            return {"platforms": {"instagram": {
                "description": "A real grounded description for instagram. #Mystery",
                "visible_hashtags": ["#Mystery"], "cta": "c",
            }}}

        with pytest.raises(CanonicalMetadataGenerationError, match="youtube.*categoryId|categoryId.*youtube"):
            generate_canonical_platform_metadata_for_deliverable(
                EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube", "instagram"], final_render_package,
                config=Config(), channel_id="hypogeum", platform_deliverable_policy=policy,
                llm_call=fake_llm_call,
            )
        assert calls == []  # zero model calls -- not even for the policy-complete instagram


class TestRequiredPolicyFieldPresenceValidator:
    """
    Direct unit coverage for hypogeum.publishing.canonical_metadata.
    is_required_policy_field_present() -- the ONE shared validator used
    by both the pre-generation check above and the readiness-side checks
    in hypogeum.publishing.canonical_metadata.validate_canonical_platform_metadata()
    and hypogeum.publishing.cross_platform_readiness.check_cross_platform_readiness().

    Two real, opposite-direction bugs this replaces:
      - 'not class_entry.get(field)' treated boolean False and numeric 0
        as missing (too strict -- rejected a legitimate False).
      - 'field not in class_entry' treated an explicit None, an
        all-whitespace string, or an empty list/dict as present (too
        permissive -- accepted real absence of content).
    """

    def test_missing_key_is_missing(self):
        assert is_required_policy_field_present({}, "categoryId") is False

    def test_none_value_is_missing(self):
        assert is_required_policy_field_present({"categoryId": None}, "categoryId") is False

    def test_whitespace_only_string_is_missing(self):
        assert is_required_policy_field_present({"categoryId": "   "}, "categoryId") is False

    def test_empty_list_is_missing(self):
        assert is_required_policy_field_present({"allow_list": []}, "allow_list") is False

    def test_empty_dict_is_missing(self):
        assert is_required_policy_field_present({"settings": {}}, "settings") is False

    def test_boolean_false_is_present(self):
        assert is_required_policy_field_present({"ai_disclosure": False}, "ai_disclosure") is True

    def test_boolean_true_is_present(self):
        assert is_required_policy_field_present({"ai_disclosure": True}, "ai_disclosure") is True

    def test_numeric_zero_is_present(self):
        assert is_required_policy_field_present({"retry_count": 0}, "retry_count") is True

    def test_real_non_empty_string_is_present(self):
        assert is_required_policy_field_present({"categoryId": "27"}, "categoryId") is True

    def test_none_class_entry_is_missing(self):
        assert is_required_policy_field_present(None, "categoryId") is False


def _base_metadata(**overrides) -> CanonicalPlatformMetadata:
    kwargs = dict(
        deliverable_id="main_episode", platform="youtube", description="A real base description.",
        metadata_version="v1", source_artifact_ids=["research_package_ep_test.json"],
        input_fingerprint={}, generated_at="2026-08-23T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return CanonicalPlatformMetadata(**kwargs)


class TestMaterializeYoutubeMainEpisodeDescription:
    """
    Direct unit coverage for hypogeum.publishing.canonical_metadata.
    materialize_youtube_main_episode_description() -- the ONE
    deterministic function weaving canonical chapters/sources into the
    final visible description, used identically by readiness validation
    and the real Zernio serialization path.
    """

    def test_no_chapters_or_sources_returns_description_unchanged(self):
        metadata = _base_metadata()
        assert materialize_youtube_main_episode_description(metadata) == "A real base description."

    def test_chapters_are_appended_with_correct_timestamps(self):
        metadata = _base_metadata(chapters=[
            {"start_seconds": 0.0, "title": "Intro"},
            {"start_seconds": 65.479, "title": "The Middle"},
            {"start_seconds": 588.775, "title": "The End"},
        ])
        result = materialize_youtube_main_episode_description(metadata)
        assert "0:00 Intro" in result
        assert "1:05 The Middle" in result
        assert "9:49 The End" in result
        assert result.count("Chapters:") == 1

    def test_sources_are_appended(self):
        metadata = _base_metadata(sources=[
            {"title": "Academic", "url": "https://example.com/a"},
            {"title": "News", "url": "https://example.com/b"},
        ])
        result = materialize_youtube_main_episode_description(metadata)
        assert "https://example.com/a" in result
        assert "https://example.com/b" in result
        assert result.count("Sources & Further Reading:") == 1

    def test_calling_twice_never_duplicates_chapters_or_sources(self):
        metadata = _base_metadata(
            chapters=[{"start_seconds": 0.0, "title": "Intro"}, {"start_seconds": 12.0, "title": "End"}],
            sources=[{"title": "Academic", "url": "https://example.com/a"}],
        )
        once = materialize_youtube_main_episode_description(metadata)
        twice = materialize_youtube_main_episode_description(_base_metadata(
            description=once, chapters=metadata.chapters, sources=metadata.sources,
        ))
        assert once == twice
        assert twice.count("Chapters:") == 1
        assert twice.count("Sources & Further Reading:") == 1

    def test_a_source_the_model_already_cited_inline_is_not_duplicated(self):
        # The model is explicitly permitted (never guaranteed) to cite a
        # source inline -- materialization must not re-list it.
        metadata = _base_metadata(
            description="A description that already cites https://example.com/a inline.",
            sources=[
                {"title": "Academic", "url": "https://example.com/a"},
                {"title": "News", "url": "https://example.com/b"},
            ],
        )
        result = materialize_youtube_main_episode_description(metadata)
        assert result.count("https://example.com/a") == 1
        assert "https://example.com/b" in result

    def test_visible_hashtags_are_never_touched_or_duplicated(self):
        metadata = _base_metadata(
            description="A real description with #Erdstall embedded.",
            visible_hashtags=["#Erdstall"],
            chapters=[{"start_seconds": 0.0, "title": "Intro"}],
        )
        result = materialize_youtube_main_episode_description(metadata)
        assert result.count("#Erdstall") == 1

    def test_backend_tags_never_appear_in_the_materialized_description(self):
        metadata = _base_metadata(backend_tags=["a search phrase", "another search phrase"])
        result = materialize_youtube_main_episode_description(metadata)
        assert "a search phrase" not in result


class TestMaterializationWiredIntoGeneration:
    """
    Proves the materialized (not raw) length is what generation-time
    validation checks for a main_episode/youtube entry, and that
    exceeding the limit only after chapters/sources are appended feeds
    real shortening guidance back into the bounded retry loop -- never a
    silent truncation.
    """

    def test_materialized_overflow_retries_with_shortening_guidance(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_main_episode_grounding(episode_dir)
        # A base description safely under 5000 chars on its own, but
        # chapters + a long sources block push the MATERIALIZED result
        # over the limit.
        oversized_base = "x" * 4950 + " #Mystery"
        many_sources = [
            {"source_id": f"src_{i:03d}", "url": f"https://example.com/very/long/path/{i}", "inferred_type": "academic"}
            for i in range(30)
        ]
        (episode_dir / f"research_package_{EPISODE_ID}.json").write_text(json.dumps({
            "source_registry": many_sources,
            "claims": [{"claim_id": "clm_001", "sources": [many_sources[0]["source_id"]]}],
        }), encoding="utf-8")
        calls = []

        def fake_llm_call(system_prompt, input_package, **kw):
            calls.append(system_prompt)
            if len(calls) == 1:
                return {"platforms": {"youtube": {
                    "title": "T", "cta": "c",
                    "description": oversized_base, "visible_hashtags": ["#Mystery"],
                }}}
            return {"platforms": {"youtube": {
                "title": "T", "cta": "c",
                "description": "A real, short, grounded description. #Mystery",
                "visible_hashtags": ["#Mystery"],
            }}}

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "main_episode", "main_episode", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_MAIN_EPISODE_POLICY,
            llm_call=fake_llm_call,
        )
        assert len(calls) == 2
        assert "materialized description" in calls[1]
        assert "exceeding" in calls[1]
        # The final, accepted, persisted description is the short one --
        # never the oversized base truncated down to fit.
        assert entries[0]["description"] == "A real, short, grounded description. #Mystery"

    def test_shorts_are_never_materialized_even_with_a_short_base_description(self, tmp_path):
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)

        def fake_llm_call(*a, **kw):
            return _VALID_YOUTUBE_RESPONSE

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=_HYPOGEUM_POLICY,
            llm_call=fake_llm_call,
        )
        # A Short never computes chapters at all (deliverable_class !=
        # "main_episode") -- the stored/returned description is exactly
        # the model's own text, no Chapters/Sources block appended.
        assert entries[0]["chapters"] == []
        assert "Chapters:" not in entries[0]["description"]
        assert "Sources & Further Reading:" not in entries[0]["description"]

    def test_readiness_never_materializes_for_a_short_even_with_chapters_present(self, tmp_path):
        # Defense in depth: the real generator never populates chapters
        # for a Short, but validate_canonical_platform_metadata()'s own
        # length check must still gate materialization on
        # (deliverable_class, platform), not merely "chapters is
        # non-empty" -- an oversized-if-materialized Short must be judged
        # on its own raw description length, which here is well within
        # any real caption limit.
        from hypogeum.publishing.zernio_platform_contracts import get_platform_contract

        metadata = _base_metadata(
            deliverable_id="short_001", description="A short, real, grounded caption.",
            chapters=[{"start_seconds": 0.0, "title": "Intro"}],
            sources=[{"title": "Academic", "url": "https://example.com/a"}],
            visible_hashtags=["#Mystery"], cta="Subscribe.", ai_disclosure=True,
        )
        failures = validate_canonical_platform_metadata(
            metadata, deliverable_class="shorts", platform="youtube",
            class_entry={"categoryId": "27"}, final_duration_seconds=30.0,
            episode_dir=tmp_path, episode_id=EPISODE_ID,
            contract=get_platform_contract("youtube"),
        )
        assert not any("exceeding" in f for f in failures), failures
