"""
tests/test_humanization_checkpoint.py — Unit tests for
hypogeum/humanization_checkpoint.py

Scope: the checkpoint persistence/reuse layer only. All tests use tmp_path —
none write to .hypogeum-assets, the real project tree, or any user
directory. This module is not wired into hypogeum/pipeline.py.

Coverage:
  TestFingerprint          — stability, key-order independence, sensitivity
                              to every documented input field
  TestCheckpointPath       — filename convention, traversal rejection
  TestSaveBehavior         — schema gate, atomic write, no mutation
  TestLoadBehavior         — miss/hit/corruption handling
  TestReuseBehavior        — every reuse condition, staleness, corruption
  TestLanguageAndContent   — Arabic, mixed-language, fact-preserving round-trip
  TestIsolation            — no DB/network/pipeline reference
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from hypogeum.agents.humanization_stage import run_humanization_stage
from hypogeum.humanization_checkpoint import (
    HumanizationCheckpointError,
    build_humanization_fingerprint,
    humanization_checkpoint_path,
    load_humanization_checkpoint,
    load_reusable_humanization_checkpoint,
    save_humanization_checkpoint,
)
from hypogeum.schemas import validate


def _script_package(text: str = "In 1901, divers found something extraordinary.") -> dict:
    return {
        "episode_metadata": {"title": "The Antikythera Mechanism"},
        "sections": [
            {
                "section_id": "s_001",
                "title": "The Wreck",
                "narration_beats": [{"beat_id": "nb_001", "text": text}],
            }
        ],
        "shorts_scripts": [],
    }


def _off_result(episode_id: str = "ep_001", script_package: dict = None) -> dict:
    return run_humanization_stage(
        episode_id=episode_id,
        script_package=script_package or _script_package(),
        pipeline_run_id="run_1",
        mode="OFF",
    )


class TestFingerprint:
    def test_stable_for_identical_input(self):
        pkg = _script_package()
        fp1 = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        fp2 = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        assert fp1 == fp2

    def test_key_order_does_not_change_fingerprint(self):
        pkg_a = _script_package()
        pkg_b = {
            "shorts_scripts": pkg_a["shorts_scripts"],
            "sections": pkg_a["sections"],
            "episode_metadata": dict(reversed(list(pkg_a["episode_metadata"].items()))),
        }
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg_a, mode="OFF")
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg_b, mode="OFF")
        assert fp_a == fp_b

    def test_changed_narration_changes_fingerprint(self):
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=_script_package("A"), mode="OFF")
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=_script_package("B"), mode="OFF")
        assert fp_a != fp_b

    def test_changed_episode_id_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        fp_b = build_humanization_fingerprint(episode_id="ep_002", script_package=pkg, mode="OFF")
        assert fp_a != fp_b

    def test_changed_mode_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="DETERMINISTIC_ONLY")
        assert fp_a != fp_b

    def test_changed_stage_version_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", stage_version="0.1.0")
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", stage_version="0.2.0")
        assert fp_a != fp_b

    def test_changed_wrapper_version_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", wrapper_version="0.1.0")
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", wrapper_version="0.2.0")
        assert fp_a != fp_b

    def test_changed_skill_sha_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(
            episode_id="ep_001", script_package=pkg, mode="OFF",
            skill_commit_sha="1b48564898e999219882660237fde01bf4843a0f",
        )
        fp_b = build_humanization_fingerprint(
            episode_id="ep_001", script_package=pkg, mode="OFF",
            skill_commit_sha="0000000000000000000000000000000000000f",
        )
        assert fp_a != fp_b

    def test_changed_model_id_changes_fingerprint(self):
        pkg = _script_package()
        fp_a = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", model_id=None)
        fp_b = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF", model_id="anthropic/claude-sonnet-4.6")
        assert fp_a != fp_b


class TestCheckpointPath:
    def test_uses_exact_required_filename(self, tmp_path):
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_042")
        assert path.name == "humanized_script_package_ep_042.json"
        assert path.parent == (tmp_path / "episodes" / "ep_042")

    def test_unsafe_episode_id_with_slash_rejected(self, tmp_path):
        with pytest.raises(HumanizationCheckpointError):
            humanization_checkpoint_path(assets_root=tmp_path, episode_id="../../etc/passwd")

    def test_unsafe_episode_id_empty_rejected(self, tmp_path):
        with pytest.raises(HumanizationCheckpointError):
            humanization_checkpoint_path(assets_root=tmp_path, episode_id="")

    def test_unsafe_episode_id_dotdot_rejected(self, tmp_path):
        with pytest.raises(HumanizationCheckpointError):
            humanization_checkpoint_path(assets_root=tmp_path, episode_id="..")


class TestSaveBehavior:
    def test_valid_package_saves_successfully(self, tmp_path):
        result = _off_result()
        path = save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        assert path.exists()

    def test_saved_json_is_valid_utf8_json(self, tmp_path):
        result = _off_result()
        path = save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_saved_package_passes_schema_validation(self, tmp_path):
        result = _off_result()
        path = save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        data = json.loads(path.read_text(encoding="utf-8"))
        validate("humanized_script_package", data)  # must not raise

    def test_input_package_not_mutated_during_save(self, tmp_path):
        result = _off_result()
        snapshot = copy.deepcopy(result)
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        assert result == snapshot

    def test_no_temp_file_remains_after_success(self, tmp_path):
        result = _off_result()
        path = save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        assert not path.with_suffix(".tmp").exists()

    def test_failed_validation_writes_no_final_checkpoint(self, tmp_path):
        invalid = {"not": "a valid humanized_script_package"}
        with pytest.raises(HumanizationCheckpointError):
            save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=invalid)
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        assert not path.exists()
        assert not path.with_suffix(".tmp").exists()


class TestLoadBehavior:
    def test_no_checkpoint_returns_none(self, tmp_path):
        assert load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_missing") is None

    def test_valid_checkpoint_loads_successfully(self, tmp_path):
        result = _off_result()
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        loaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        assert loaded == result

    def test_malformed_json_raises(self, tmp_path):
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(HumanizationCheckpointError):
            load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")

    def test_non_dict_json_raises(self, tmp_path):
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["a", "list", "not", "a", "dict"]), encoding="utf-8")
        with pytest.raises(HumanizationCheckpointError):
            load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")

    def test_schema_invalid_json_raises(self, tmp_path):
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"episode_id": "ep_001"}), encoding="utf-8")
        with pytest.raises(HumanizationCheckpointError):
            load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")


class TestReuseBehavior:
    def test_checkpoint_episode_mismatch_is_stale(self, tmp_path):
        """
        A self-consistent checkpoint (its own top-level episode_id and
        embedded source_script_package.episode_metadata.episode_id agree —
        genuinely schema-valid on its own terms) that ends up written at a
        *different* episode's expected path — e.g. a misplaced or copied
        file. load_reusable_humanization_checkpoint() must recognize this
        as a mismatch (via the top-level episode_id check) and report a
        clean miss, not raise, since the file itself is not corrupt.
        """
        other_result = _off_result(episode_id="ep_other", script_package=_script_package())
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(other_result), encoding="utf-8")

        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001",
            script_package=_script_package(), mode="OFF",
        )
        assert reused is None

    def test_matching_fingerprint_and_metadata_is_cache_hit(self, tmp_path, monkeypatch):
        """
        run_humanization_stage()'s current input_hash formula does not equal
        build_humanization_fingerprint() (documented incompatibility in
        hypogeum/humanization_checkpoint.py's module docstring). To test the
        genuine cache-hit path independent of that unresolved gap, this test
        writes a checkpoint whose humanization.input_hash is deliberately
        set to the fingerprint this module itself would compute.
        """
        pkg = _script_package()
        result = _off_result(script_package=pkg)
        fingerprint = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode="OFF")
        result["humanization"]["input_hash"] = fingerprint
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)

        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="OFF",
        )
        assert reused is not None
        assert reused["humanization"]["input_hash"] == fingerprint

    def _saved_with_matching_fingerprint(self, tmp_path, pkg, mode="OFF", **overrides):
        result = _off_result(script_package=pkg)
        fingerprint = build_humanization_fingerprint(episode_id="ep_001", script_package=pkg, mode=mode)
        result["humanization"]["input_hash"] = fingerprint
        result["humanization"]["mode"] = mode
        for key, value in overrides.items():
            result["humanization"][key] = value
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        return fingerprint

    def test_changed_input_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg)
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001",
            script_package=_script_package("A different sentence entirely."), mode="OFF",
        )
        assert reused is None

    def test_changed_mode_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg, mode="OFF")
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="DETERMINISTIC_ONLY",
        )
        assert reused is None

    def test_changed_stage_version_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg, stage_version="0.1.0")
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="OFF",
            stage_version="0.2.0",
        )
        assert reused is None

    def test_changed_wrapper_version_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg, wrapper_version="0.1.0")
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="OFF",
            wrapper_version="0.2.0",
        )
        assert reused is None

    def test_changed_skill_sha_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(
            tmp_path, pkg, skill_commit_sha="1b48564898e999219882660237fde01bf4843a0f"
        )
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="OFF",
            skill_commit_sha="0000000000000000000000000000000000000f",
        )
        assert reused is None

    def test_changed_model_id_produces_cache_miss(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg, model_id=None)
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001", script_package=pkg, mode="OFF",
            model_id="anthropic/claude-sonnet-4.6",
        )
        assert reused is None

    def test_corrupt_checkpoint_raises_instead_of_cache_miss(self, tmp_path):
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(HumanizationCheckpointError):
            load_reusable_humanization_checkpoint(
                assets_root=tmp_path, episode_id="ep_001",
                script_package=_script_package(), mode="OFF",
            )

    def test_no_checkpoint_is_reuse_miss_not_error(self, tmp_path):
        reused = load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_missing",
            script_package=_script_package(), mode="OFF",
        )
        assert reused is None

    def test_stale_checkpoint_not_deleted_or_overwritten(self, tmp_path):
        pkg = _script_package()
        self._saved_with_matching_fingerprint(tmp_path, pkg)
        path = humanization_checkpoint_path(assets_root=tmp_path, episode_id="ep_001")
        before = path.read_text(encoding="utf-8")
        load_reusable_humanization_checkpoint(
            assets_root=tmp_path, episode_id="ep_001",
            script_package=_script_package("changed text"), mode="OFF",
        )
        after = path.read_text(encoding="utf-8")
        assert before == after


class TestLanguageAndContent:
    def test_arabic_narration_round_trips(self, tmp_path):
        pkg = _script_package("في عام 1901، عثر الغواصون على شيء استثنائي.")
        result = _off_result(script_package=pkg)
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        loaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        assert loaded["beats"][0]["original_narration"] == "في عام 1901، عثر الغواصون على شيء استثنائي."

    def test_mixed_arabic_english_round_trips(self, tmp_path):
        text = "In 1901, الغواصون found something في البحر extraordinary."
        pkg = _script_package(text)
        result = _off_result(script_package=pkg)
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        loaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        assert loaded["beats"][0]["original_narration"] == text

    def test_quotes_dates_numbers_urls_citations_unchanged_by_round_trip(self, tmp_path):
        text = (
            'The captain said, "This changes everything," in 1901. '
            "45 artifacts were found. See https://example.org/report (Smith, 2019)."
        )
        pkg = _script_package(text)
        result = _off_result(script_package=pkg)
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        loaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        assert loaded["beats"][0]["original_narration"] == text

    def test_loaded_object_independent_of_saved_object(self, tmp_path):
        result = _off_result()
        save_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001", package=result)
        loaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        loaded["beats"][0]["humanized_narration"] = "mutated after load"
        reloaded = load_humanization_checkpoint(assets_root=tmp_path, episode_id="ep_001")
        assert reloaded["beats"][0]["humanized_narration"] != "mutated after load"


class TestIsolation:
    def test_module_has_no_external_or_db_call_surface(self):
        import ast
        import inspect

        import hypogeum.humanization_checkpoint as mod
        source = inspect.getsource(mod)

        # Substring checks for call-shaped forbidden patterns (none of these
        # legitimately appear as call syntax anywhere in this module,
        # including its own docstring prose).
        for forbidden in ("OpenAI(", "requests.", "httpx.", "subprocess.", "sqlite", "get_connection"):
            assert forbidden not in source, f"found forbidden reference: {forbidden}"

        # run_agent()/run_humanization_stage() are both named in this
        # module's own docstring (explaining what it deliberately does not
        # do), so a plain substring check would false-positive on the
        # prose. Parse the AST instead and confirm neither name is ever
        # imported or called as actual code.
        tree = ast.parse(source)
        called_or_imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_or_imported_names.add(node.func.id)
            if isinstance(node, ast.ImportFrom):
                called_or_imported_names.update(alias.name for alias in node.names)
        assert "run_agent" not in called_or_imported_names
        assert "run_humanization_stage" not in called_or_imported_names

    def test_not_referenced_by_pipeline(self):
        pipeline_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hypogeum", "pipeline.py"
        )
        content = open(pipeline_path, encoding="utf-8").read()
        assert "humanization_checkpoint" not in content
        assert "humanization_stage" not in content
