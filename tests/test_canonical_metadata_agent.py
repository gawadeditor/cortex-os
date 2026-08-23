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


_HYPOGEUM_POLICY = {
    "youtube": {"shorts": {"enabled": True, "categoryId": "27", "ai_disclosure": True}},
}

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

    def test_one_incomplete_platform_does_not_block_a_complete_sibling_platform(self, tmp_path):
        # instagram has no required_policy_fields at all -- it must still
        # generate even though youtube's categoryId is missing.
        episode_dir = tmp_path
        final_render_package = _seed_short_grounding(episode_dir)
        policy = {
            "youtube": {"shorts": {"enabled": True}},  # missing categoryId
            "instagram": {"shorts": {"enabled": True}},
        }

        def fake_llm_call(system_prompt, input_package, **kw):
            assert input_package["platforms"] == ["instagram"]  # youtube was already dropped
            return {"platforms": {"instagram": {
                "description": "A real grounded description for instagram. #Mystery",
                "visible_hashtags": ["#Mystery"], "cta": "c",
            }}}

        entries = generate_canonical_platform_metadata_for_deliverable(
            EPISODE_ID, episode_dir, "short_001", "shorts", ["youtube", "instagram"], final_render_package,
            config=Config(), channel_id="hypogeum", platform_deliverable_policy=policy,
            llm_call=fake_llm_call,
        )
        assert [e["platform"] for e in entries] == ["instagram"]
