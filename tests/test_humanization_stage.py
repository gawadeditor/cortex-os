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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.humanization_stage import (
    HumanizationStageError,
    UnsupportedHumanizationModeError,
    run_humanization_stage,
)
from hypogeum.schemas import validate


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

    def test_shadow_mode_not_yet_supported(self):
        with pytest.raises(UnsupportedHumanizationModeError):
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

    def test_not_referenced_by_pipeline(self):
        pipeline_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hypogeum", "pipeline.py"
        )
        content = open(pipeline_path, encoding="utf-8").read()
        assert "humanization_stage" not in content
