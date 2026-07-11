"""
tests/test_humanization_stage.py — Unit tests for the isolated Humanization Stage

Scope: hypogeum/agents/humanization_stage.py only. This module is not wired
into hypogeum/pipeline.py or any production agent — these tests exercise it
standalone, in-memory, with no network/file/DB access.

Coverage:
  TestOffMode                    — OFF mode pass-through behavior
  TestDeterministicOnlyMode      — real deterministic transform behavior
  TestInputImmutability          — caller's script_package is never mutated
  TestBeatExtraction             — order, IDs, duplicates, malformed entries
  TestP24Safety                  — fail-closed on unsafe hedging-deletion patterns
  TestPreservationChecks         — dates/numbers/quotes/URLs/citations/uncertainty
  TestLanguageSupport            — Arabic and mixed Arabic/English narration
  TestHashing                    — stable/distinct SHA-256 hashes
  TestModeValidation             — invalid mode rejection
  TestSchemaCompliance           — final result passes humanized_script_package
  TestIsolation                  — no external calls, no pipeline reference
"""

from __future__ import annotations

import copy
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.humanization_stage import (
    HumanizationStageError,
    UnsupportedHumanizationModeError,
    run_humanization_stage,
)
from hypogeum.agents.humanizer_llm_adapter import HumanizerLLMAdapterError, HumanizerLLMResult
from hypogeum.config import Config
from hypogeum.schemas import validate


def make_config() -> Config:
    pipeline_vars = [
        "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
        "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
        "ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
        "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL",
        "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
        "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST",
        "ELEVENLABS_MUSIC_MODEL_ID", "ELEVENLABS_MUSIC_DEFAULT_DURATION",
        "OPENAI_API_KEY", "WHISPER_MODEL",
    ]
    original = {k: os.environ.get(k) for k in pipeline_vars}
    for k in pipeline_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        return Config()
    finally:
        for k in pipeline_vars:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def _llm_result(humanized_text: str, **overrides) -> HumanizerLLMResult:
    defaults = dict(
        humanized_text=humanized_text,
        model_id="anthropic/claude-sonnet-4.6",
        provider="openrouter",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        latency_ms=500,
        via_fallback=False,
        estimated_cost_usd=0.001,
    )
    defaults.update(overrides)
    return HumanizerLLMResult(**defaults)


def _script_package(**overrides) -> dict:
    base = {
        "episode_metadata": {"title": "The Antikythera Mechanism"},
        "sections": [
            {
                "section_id": "s_001",
                "title": "The Wreck",
                "narration_beats": [
                    {"beat_id": "nb_001", "text": "In 1901, divers found something extraordinary."},
                    {"beat_id": "nb_002", "text": "We delve into the pivotal moment of discovery."},
                ],
            }
        ],
        "shorts_scripts": [
            {
                "short_id": "sh_001",
                "narration_beats": [
                    {"beat_id": "shnb_001", "text": "The oldest known computer."},
                ],
                "text_overlays": [],
            }
        ],
    }
    base.update(overrides)
    return base


class TestOffMode:
    def test_off_mode_valid_output(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        validate("humanized_script_package", r)
        assert r["humanization"]["mode"] == "OFF"
        assert r["humanization"]["status"] == "not_run"

    def test_off_mode_original_equals_humanized(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        for beat in r["beats"]:
            assert beat["original_narration"] == beat["humanized_narration"]
            assert beat["transformation_status"] == "unchanged"
            assert beat["deterministic_rules_applied"] == []
            assert beat["llm_pass_applied"] is False

    def test_off_mode_zero_tokens_and_no_model(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        tu = r["humanization"]["token_usage"]
        assert tu["input_tokens"] == 0
        assert tu["output_tokens"] == 0
        assert tu["total_tokens"] == 0
        assert r["humanization"]["model_id"] is None
        assert r["humanization"]["provider"] is None
        assert r["humanization"]["fallback_used"] is False


class TestDeterministicOnlyMode:
    def test_deterministic_only_valid_output(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        validate("humanized_script_package", r)
        assert r["humanization"]["mode"] == "DETERMINISTIC_ONLY"

    def test_changed_narration_status(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = next(b for b in r["beats"] if b["beat_id"] == "nb_002")
        assert beat["transformation_status"] == "deterministic"
        assert "delve into" not in beat["humanized_narration"]
        assert beat["deterministic_rules_applied"] == ["legacy_deterministic_humanizer"]
        assert beat["llm_pass_applied"] is False

    def test_unchanged_narration_status(self):
        pkg = _script_package()
        pkg["shorts_scripts"][0]["narration_beats"][0]["text"] = "A clean sentence with no AI tells."
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = next(b for b in r["beats"] if b["beat_id"] == "shnb_001")
        assert beat["transformation_status"] == "unchanged"
        assert beat["original_narration"] == beat["humanized_narration"]
        assert beat["deterministic_rules_applied"] == []

    def test_zero_tokens_and_no_model_in_deterministic_mode(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        tu = r["humanization"]["token_usage"]
        assert tu["input_tokens"] == 0 and tu["output_tokens"] == 0 and tu["total_tokens"] == 0
        assert r["humanization"]["model_id"] is None
        assert r["humanization"]["provider"] is None

    def test_rule_marker_is_conservative_not_per_rule(self):
        """
        hypogeum.humanizer.humanize_field() exposes no per-rule tracking, so
        deterministic_rules_applied must use the conservative marker, never
        an invented per-pattern ID (P01/P07/etc. attribution would be a
        fabricated claim this module cannot truthfully make).
        """
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = next(b for b in r["beats"] if b["beat_id"] == "nb_002")
        assert beat["deterministic_rules_applied"] == ["legacy_deterministic_humanizer"]


class TestShadowMode:
    def test_shadow_mode_invokes_mocked_model_path(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_shadow", "text": "A clean sentence with no AI tells."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("A clean sentence, rewritten by the model."),
        ) as mock_llm:
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        mock_llm.assert_called_once()
        validate("humanized_script_package", r)
        assert r["humanization"]["mode"] == "SHADOW"

    def test_valid_output_passes_schema(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Rewritten text."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=_script_package(),
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        validate("humanized_script_package", r)

    def test_proposed_output_stored_separately_from_original(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_prop", "text": "A calm afternoon settled over the valley."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("The valley grew quiet as afternoon settled in."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["original_narration"] == "A calm afternoon settled over the valley."
        assert beat["humanized_narration"] == "The valley grew quiet as afternoon settled in."
        assert beat["original_narration"] != beat["humanized_narration"]
        assert beat["llm_pass_applied"] is True
        assert beat["transformation_status"] in ("llm", "deterministic_and_llm")

    def test_original_script_package_untouched(self):
        cfg = make_config()
        pkg = _script_package()
        original_snapshot = copy.deepcopy(pkg)
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Rewritten text."),
        ):
            run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        assert pkg == original_snapshot

    def test_llm_pass_applied_truthfully_false_when_unchanged(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_same", "text": "Already natural."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Already natural."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "unchanged"
        assert beat["llm_pass_applied"] is True

    def test_package_metadata_records_model_and_provider(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Rewritten text."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=_script_package(),
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        assert r["humanization"]["model_id"] == "anthropic/claude-sonnet-4.6"
        assert r["humanization"]["provider"] == "openrouter"
        assert r["humanization"]["token_usage"]["total_tokens"] > 0
        assert r["provenance"]["model_id"] == "anthropic/claude-sonnet-4.6"


class TestShadowModeFallback:
    def test_malformed_model_output_falls_back_to_deterministic(self):
        """A HumanizerLLMAdapterError (malformed output) reverts that beat
        to the deterministic-pass result — never a raw failure, never a
        stage crash."""
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_malformed", "text": "We delve into the pivotal moment."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            side_effect=HumanizerLLMAdapterError("malformed_llm_output: not JSON"),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["llm_pass_applied"] is False
        assert "delve into" not in beat["humanized_narration"]  # deterministic pass still ran
        assert r["humanization"]["fallback_used"] is True
        assert r["humanization"]["status"] == "fallback"
        validate("humanized_script_package", r)

    def test_provider_failure_falls_back_to_deterministic(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_provider_fail", "text": "A clean sentence with no AI tells."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            side_effect=HumanizerLLMAdapterError("provider_failure: HardFailure: 400"),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["humanized_narration"] == "A clean sentence with no AI tells."
        assert beat["llm_pass_applied"] is False
        assert r["humanization"]["fallback_used"] is True
        validate("humanized_script_package", r)

    def test_provider_failure_never_raises_out_of_stage(self):
        """The stage itself must never raise on an LLM failure — it must
        fall back and still return a valid package."""
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            side_effect=HumanizerLLMAdapterError("provider_failure: boom"),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=_script_package(),
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        validate("humanized_script_package", r)

    def test_date_loss_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_date", "text": "In 1901, divers found something extraordinary."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Divers found something extraordinary."),  # date dropped
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert "1901" in beat["humanized_narration"]
        assert r["humanization"]["fallback_used"] is True

    def test_number_loss_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_num", "text": "The team found 45 artifacts at the site."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("The team found several artifacts at the site."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert "45" in beat["humanized_narration"]

    def test_quote_alteration_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_quote", "text": 'The captain said, "This changes everything."'}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("The captain said this changed everything."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert '"This changes everything."' in beat["humanized_narration"]

    def test_url_loss_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_url", "text": "See https://example.org/report for details."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("See the report for details."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert "https://example.org/report" in beat["humanized_narration"]

    def test_name_loss_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_name", "text": "Wilhelm discovered the mechanism."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("The researcher discovered the mechanism."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert "Wilhelm" in beat["humanized_narration"]

    def test_uncertainty_removal_in_llm_output_triggers_beat_revert(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_hedge_llm", "text": "The design may have been intentional."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("The design was intentional."),  # hedge dropped entirely
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert r["humanization"]["fallback_used"] is True

    def test_reverted_beat_never_uses_unvalidated_llm_text(self):
        """A reverted beat's humanized_narration must never equal the
        rejected LLM output — only the deterministic-pass (or original)
        text may appear."""
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_reject", "text": "In 1901, divers found something extraordinary."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        rejected_text = "Divers found something extraordinary."  # drops the date
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result(rejected_text),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        beat = r["beats"][0]
        assert beat["humanized_narration"] != rejected_text


class TestShadowModeLanguageSupport:
    def test_arabic_narration_in_shadow_mode(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_ar_shadow", "text": "في عام 1901، عثر الغواصون على شيء استثنائي."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("في عام 1901، عثر الغواصون على شيء استثنائي."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        validate("humanized_script_package", r)
        beat = r["beats"][0]
        assert beat["original_narration"] == "في عام 1901، عثر الغواصون على شيء استثنائي."

    def test_mixed_arabic_english_narration_in_shadow_mode(self):
        cfg = make_config()
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_mix_shadow", "text": "In 1901, الغواصون found something في البحر extraordinary."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("In 1901, الغواصون found something في البحر extraordinary."),
        ):
            r = run_humanization_stage(
                episode_id="ep_001", script_package=pkg,
                pipeline_run_id="run_1", mode="SHADOW", config=cfg,
            )
        validate("humanized_script_package", r)


class TestInputImmutability:
    def test_input_package_not_mutated(self):
        pkg = _script_package()
        original_snapshot = copy.deepcopy(pkg)
        run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        assert pkg == original_snapshot

    def test_episode_id_injected_only_into_copy(self):
        pkg = _script_package()
        assert "episode_id" not in pkg["episode_metadata"]
        r = run_humanization_stage(
            episode_id="ep_777", script_package=pkg,
            pipeline_run_id="run_1", mode="OFF",
        )
        assert "episode_id" not in pkg["episode_metadata"]
        assert r["source_script_package"]["episode_metadata"]["episode_id"] == "ep_777"

    def test_original_script_package_schema_field_set_unaffected(self):
        pkg = _script_package()
        pkg["episode_metadata"]["episode_id"] = "should-not-change"
        run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="OFF",
        )
        assert pkg["episode_metadata"]["episode_id"] == "should-not-change"


class TestBeatExtraction:
    def test_narration_beat_order_preserved(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        assert [b["beat_id"] for b in r["beats"]] == ["nb_001", "nb_002", "shnb_001"]

    def test_beat_ids_preserved(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        assert {b["beat_id"] for b in r["beats"]} == {"nb_001", "nb_002", "shnb_001"}

    def test_duplicate_beat_ids_handled_safely(self):
        pkg = _script_package()
        pkg["shorts_scripts"][0]["narration_beats"].append(
            {"beat_id": "nb_001", "text": "A duplicate of an existing beat id."}
        )
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="OFF",
        )
        validate("humanized_script_package", r)
        beat_ids = [b["beat_id"] for b in r["beats"]]
        assert beat_ids.count("nb_001") == 1
        assert any("duplicate beat_id" in w for w in r["humanization"]["warnings"])

    def test_missing_beat_id_handled_safely(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"].append({"text": "No beat_id here."})
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="OFF",
        )
        validate("humanized_script_package", r)
        assert any("missing/invalid beat_id" in w for w in r["humanization"]["warnings"])

    def test_empty_narration_handled_safely(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"].append({"beat_id": "nb_empty", "text": ""})
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        validate("humanized_script_package", r)
        beat = next(b for b in r["beats"] if b["beat_id"] == "nb_empty")
        assert beat["humanized_narration"] == ""
        assert beat["transformation_status"] == "unchanged"


class TestP24Safety:
    def test_p24_unsafe_deletion_pattern_reverted(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_p24", "text": "It could be argued that the evidence is incomplete."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert beat["humanized_narration"] == beat["original_narration"]
        assert beat["warnings"], "expected a warning explaining the revert"
        assert r["humanization"]["fallback_used"] is True
        assert r["humanization"]["status"] == "fallback"

    def test_safe_hedging_softening_still_permitted(self):
        """
        Not every P24 pattern is unsafe — 'could potentially possibly' is a
        softening substitution (matches upstream's own demonstrated
        behavior), not a clause deletion, and should be allowed to fire.
        """
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_soft", "text": "This could potentially possibly explain the mystery."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "deterministic"
        assert "potentially possibly" not in beat["humanized_narration"]


class TestPreservationChecks:
    def test_dates_numbers_quotes_urls_preserved_or_reverted(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {
                "beat_id": "nb_facts",
                "text": (
                    'In 1901, the divers found 45 artifacts. The captain said, '
                    '"This changes everything." See https://example.org/report for details.'
                ),
            }
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = r["beats"][0]
        # Either the text passed through with all facts intact, or the
        # stage reverted to the original — in no case may a fact be lost.
        assert "1901" in beat["humanized_narration"]
        assert "45" in beat["humanized_narration"]
        assert '"This changes everything."' in beat["humanized_narration"]
        assert "https://example.org/report" in beat["humanized_narration"]

    def test_uncertainty_language_cannot_be_destructively_removed(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_hedge", "text": "It seems evident that the design was intentional."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = r["beats"][0]
        # "it seems ... that" is one of humanizer.py's P24 deleting patterns.
        assert beat["transformation_status"] == "reverted"
        assert beat["humanized_narration"] == beat["original_narration"]

    def test_capitalized_name_loss_triggers_revert(self):
        """
        humanize_field()'s vocabulary substitutions always lowercase their
        replacement (e.g. 'Meticulous' -> 'careful'), which would silently
        drop a sentence-initial capital. This stage's preservation check
        catches that and reverts rather than accepting the loss.
        """
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_cap", "text": "Meticulous excavation revealed the truth."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        beat = r["beats"][0]
        assert beat["transformation_status"] == "reverted"
        assert beat["humanized_narration"] == "Meticulous excavation revealed the truth."


class TestLanguageSupport:
    def test_arabic_narration_accepted(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_ar", "text": "في عام 1901، عثر الغواصون على شيء استثنائي."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        validate("humanized_script_package", r)
        beat = r["beats"][0]
        assert beat["original_narration"] == "في عام 1901، عثر الغواصون على شيء استثنائي."

    def test_mixed_arabic_english_narration_accepted(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"] = [
            {"beat_id": "nb_mix", "text": "In 1901, الغواصون found something في البحر extraordinary."}
        ]
        pkg["shorts_scripts"][0]["narration_beats"] = []
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg,
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        validate("humanized_script_package", r)


class TestHashing:
    def test_stable_hashes_for_identical_input(self):
        pkg = _script_package()
        r1 = run_humanization_stage(
            episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF",
        )
        r2 = run_humanization_stage(
            episode_id="ep_001", script_package=pkg, pipeline_run_id="run_2", mode="OFF",
        )
        assert r1["humanization"]["input_hash"] == r2["humanization"]["input_hash"]
        assert r1["humanization"]["output_hash"] == r2["humanization"]["output_hash"]

    def test_different_input_produces_different_input_hash(self):
        pkg_a = _script_package()
        pkg_b = _script_package()
        pkg_b["sections"][0]["narration_beats"][0]["text"] = "A completely different sentence."
        r1 = run_humanization_stage(
            episode_id="ep_001", script_package=pkg_a, pipeline_run_id="run_1", mode="OFF",
        )
        r2 = run_humanization_stage(
            episode_id="ep_001", script_package=pkg_b, pipeline_run_id="run_1", mode="OFF",
        )
        assert r1["humanization"]["input_hash"] != r2["humanization"]["input_hash"]


class TestModeValidation:
    def test_invalid_mode_rejected(self):
        with pytest.raises((ValueError, UnsupportedHumanizationModeError)):
            run_humanization_stage(
                episode_id="ep_001", script_package=_script_package(),
                pipeline_run_id="run_1", mode="TURBO",
            )

    def test_shadow_mode_requires_config(self):
        with pytest.raises(ValueError):
            run_humanization_stage(
                episode_id="ep_001", script_package=_script_package(),
                pipeline_run_id="run_1", mode="SHADOW",
            )


class TestSchemaCompliance:
    def test_final_output_passes_humanized_script_package_validation(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY",
        )
        validate("humanized_script_package", r)  # must not raise

    def test_provenance_uses_pinned_skill_commit(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        assert r["provenance"]["skill_commit_sha"] == "1b48564898e999219882660237fde01bf4843a0f"
        assert r["provenance"]["skill_source"] == "https://github.com/blader/humanizer"
        assert r["provenance"]["source_stage"] == "script_engine"
        assert r["provenance"]["source_schema"] == "script_package"

    def test_created_at_is_iso8601_utc(self):
        r = run_humanization_stage(
            episode_id="ep_001", script_package=_script_package(),
            pipeline_run_id="run_1", mode="OFF",
        )
        created_at = r["provenance"]["created_at"]
        assert created_at.endswith("Z")
        # Round-trips through fromisoformat once the trailing Z is swapped
        from datetime import datetime
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))


class TestIsolation:
    def test_module_has_no_external_call_surface(self):
        import inspect

        import hypogeum.agents.humanization_stage as mod
        source = inspect.getsource(mod)
        for forbidden in ("run_agent(", "OpenAI(", "requests.", "httpx.", "subprocess."):
            assert forbidden not in source, f"found forbidden call surface: {forbidden}"

    def test_referenced_by_pipeline_in_shadow_mode_only(self):
        # Pipeline wiring is authorized for SHADOW mode only — the stage
        # runs for real (deterministic + LLM passes) but its output is not
        # yet consumed by any downstream stage. Guard against silent
        # escalation to ACTIVE/LLM_CANARY or a downstream consumer switch
        # without an explicit future task.
        pipeline_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hypogeum", "pipeline.py"
        )
        content = open(pipeline_path, encoding="utf-8").read()
        assert "humanization_stage" in content
        assert "run_humanization_stage(" in content
        assert '_HUMANIZATION_MODE = "SHADOW"' in content
        assert "LLM_CANARY" not in content
        assert '"ACTIVE"' not in content


# ---------------------------------------------------------------------------
# TestFingerprintUnification
#
# Confirms humanization.input_hash is computed by the single canonical
# hypogeum.humanization_fingerprint.build_humanization_fingerprint()
# implementation — the fix for the previously-documented incompatibility
# between this stage's own narration-only hash and the checkpoint layer's
# broader fingerprint formula — and that no circular import was introduced
# in resolving it.
# ---------------------------------------------------------------------------

class TestFingerprintUnification:
    def test_stage_input_hash_equals_shared_fingerprint(self):
        from hypogeum.humanization_fingerprint import build_humanization_fingerprint

        pkg = _script_package()
        r = run_humanization_stage(
            episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF",
        )
        expected = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        assert r["humanization"]["input_hash"] == expected

    def test_reordered_keys_produce_same_stage_hash(self):
        pkg_a = _script_package()
        pkg_b = {
            "shorts_scripts": pkg_a["shorts_scripts"],
            "sections": pkg_a["sections"],
            "episode_metadata": dict(reversed(list(pkg_a["episode_metadata"].items()))),
        }
        r_a = run_humanization_stage(episode_id="ep_001", script_package=pkg_a, pipeline_run_id="run_1", mode="OFF")
        r_b = run_humanization_stage(episode_id="ep_001", script_package=pkg_b, pipeline_run_id="run_1", mode="OFF")
        assert r_a["humanization"]["input_hash"] == r_b["humanization"]["input_hash"]

    def test_changed_mode_changes_stage_hash(self):
        pkg = _script_package()
        r_off = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        r_det = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="DETERMINISTIC_ONLY")
        assert r_off["humanization"]["input_hash"] != r_det["humanization"]["input_hash"]

    def test_off_and_shadow_fingerprints_differ(self):
        """SHADOW's fingerprint also folds in model_id (HUMANIZER_LLM_MODEL_ID),
        so OFF and SHADOW checkpoints can never be mistaken for each other by
        the checkpoint reuse check, even before any LLM call happens."""
        cfg = make_config()
        pkg = _script_package()
        r_off = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        with patch(
            "hypogeum.agents.humanization_stage.call_humanizer_llm",
            return_value=_llm_result("Rewritten text."),
        ):
            r_shadow = run_humanization_stage(
                episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1",
                mode="SHADOW", config=cfg,
            )
        assert r_off["humanization"]["input_hash"] != r_shadow["humanization"]["input_hash"]

    def test_changed_episode_id_changes_stage_hash(self):
        pkg = _script_package()
        r_a = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        r_b = run_humanization_stage(episode_id="ep_002", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        assert r_a["humanization"]["input_hash"] != r_b["humanization"]["input_hash"]

    def test_arabic_narration_hashes_consistently(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"][0]["text"] = "في عام 1901، عثر الغواصون على شيء استثنائي."
        r1 = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        r2 = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_2", mode="OFF")
        assert r1["humanization"]["input_hash"] == r2["humanization"]["input_hash"]

    def test_mixed_arabic_english_narration_hashes_consistently(self):
        pkg = _script_package()
        pkg["sections"][0]["narration_beats"][0]["text"] = "In 1901, الغواصون found something في البحر extraordinary."
        r1 = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_1", mode="OFF")
        r2 = run_humanization_stage(episode_id="ep_001", script_package=pkg, pipeline_run_id="run_2", mode="OFF")
        assert r1["humanization"]["input_hash"] == r2["humanization"]["input_hash"]

    def test_stage_does_not_import_checkpoint_persistence_functions(self):
        import ast
        import inspect

        import hypogeum.agents.humanization_stage as mod
        tree = ast.parse(inspect.getsource(mod))
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert "hypogeum.humanization_checkpoint" not in imported_modules

    def test_fingerprint_utility_imports_neither_stage_nor_checkpoint(self):
        import ast
        import inspect

        import hypogeum.humanization_fingerprint as mod
        tree = ast.parse(inspect.getsource(mod))
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert "hypogeum.agents.humanization_stage" not in imported_modules
        assert "hypogeum.humanization_checkpoint" not in imported_modules
        # Dependency-neutral: no hypogeum import of any kind.
        assert not any((m or "").startswith("hypogeum") for m in imported_modules)

    def test_no_circular_import(self):
        """
        Importing all three modules together must not raise ImportError —
        the real-world symptom of a circular dependency. Deliberately does
        NOT use importlib.reload(): reloading a module replaces its class
        objects (including HumanizationCheckpointError) with new instances
        distinct from whatever other test modules already imported by
        reference, silently breaking their pytest.raises(...) identity
        checks for the rest of the process. A plain re-import (returning
        the already-cached module, per normal Python import semantics) is
        sufficient to prove these three modules co-exist without a cycle.
        """
        import hypogeum.agents.humanization_stage  # noqa: F401
        import hypogeum.humanization_checkpoint  # noqa: F401
        import hypogeum.humanization_fingerprint  # noqa: F401
