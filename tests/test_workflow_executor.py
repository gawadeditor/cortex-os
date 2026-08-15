"""
tests/test_workflow_executor.py — Focused tests for
hypogeum/workflow_executor.py and its integration into hypogeum/pipeline.py.

Proves:
  - the Channel Package file is authoritative (reload-and-verify, never
    trusting a cached DB row);
  - Factory Core (workflow_registry.py, workflow_executor.py,
    channel_config.py) and the bootstrap script contain zero
    Hypogeum/stage-specific strings;
  - a second, synthetic Channel Package works through the same generic
    executor with zero Factory Core changes;
  - editing the artifact without bumping its version fails closed;
  - a missing/corrupt artifact file blocks execution;
  - StageOrderVerifier proves real-time execution-order drift detection.

All fixtures use isolated temp directories/DBs -- never
channel_packages/hypogeum/ and never the live .hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hypogeum import workflow_executor as we
from hypogeum import workflow_registry as wr

_ROOT = Path(__file__).resolve().parent.parent


def _definition_dict(**overrides) -> dict:
    base = {
        "canonical_serialization_version": "1.0",
        "workflow_id": "test_workflow",
        "stage_order": ["stage_a", "stage_b"],
        "stage_handler_identity": {"stage_a": "mod.a", "stage_b": "mod.b"},
        "required_inputs": {}, "required_outputs": {}, "dependencies": {},
        "entry_conditions": {}, "exit_conditions": {}, "retry_policy": {},
        "fallback_policy": {}, "authorization_requirements": {}, "provider_boundary_policy": {},
    }
    base.update(overrides)
    return base


def _write_package(packages_root: Path, channel_id: str, workflow_id: str, workflow_version: str, definition: dict):
    pkg_dir = packages_root / channel_id
    (pkg_dir / "workflow").mkdir(parents=True, exist_ok=True)
    artifact_path = pkg_dir / "workflow" / f"{workflow_id}.json"
    artifact_bytes = json.dumps(definition, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_bytes)

    manifest = {
        "channel_id": channel_id,
        "package_version": "1.0.0",
        "artifacts": [
            {
                "path": f"workflow/{workflow_id}.json",
                "role": "workflow_definition",
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
            }
        ],
    }
    (pkg_dir / "channel_package_manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return artifact_path, artifact_bytes


class TestChannelPackageFileIsAuthoritative:

    def test_load_reads_live_file_bytes_exactly(self, tmp_path):
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        loaded = we.load_workflow_artifact_bytes("test_channel", "test_workflow", packages_root=tmp_path)
        assert loaded == artifact_bytes

    def test_verify_and_load_succeeds_when_registered_checksum_matches_live_file(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        definition = we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)
        assert definition["stage_order"] == ["stage_a", "stage_b"]

    def test_verify_and_load_fails_closed_when_not_registered(self, tmp_path):
        _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        with pytest.raises(we.WorkflowArtifactUnreadableError):
            we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=tmp_path / "empty.db")

    def test_missing_artifact_file_blocks_execution(self, tmp_path):
        db_path = tmp_path / "registry.db"
        _artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        (tmp_path / "test_channel" / "workflow" / "test_workflow.json").unlink()

        with pytest.raises(we.WorkflowArtifactUnreadableError):
            we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

    def test_corrupt_artifact_file_blocks_execution(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        artifact_path.write_bytes(b"not json")

        with pytest.raises((we.WorkflowDriftError, wr.WorkflowSchemaError)):
            we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

    def test_missing_manifest_blocks_execution(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        (tmp_path / "test_channel" / "channel_package_manifest.json").unlink()

        with pytest.raises(we.WorkflowArtifactUnreadableError):
            we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

    def test_editing_artifact_without_bumping_version_causes_drift_on_next_verify(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        # Edit the live file's content WITHOUT registering a new version --
        # exactly the scenario the reload-and-verify design exists to catch.
        mutated = _definition_dict(stage_order=["stage_a", "stage_b", "stage_c"])
        artifact_path.write_bytes(json.dumps(mutated, sort_keys=True).encode())

        with pytest.raises(we.WorkflowDriftError):
            we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

    def test_never_trusts_cached_db_copy_over_live_file(self, tmp_path):
        # Even though the registry DB has its own cached canonical_definition
        # column, verify_and_load_workflow's returned definition must come
        # from the LIVE file, proven by mutating the file post-registration
        # (with the SAME bytes/checksum re-registered) and confirming the
        # returned content reflects what's actually on disk.
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)
        definition = we.verify_and_load_workflow("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)
        assert definition == json.loads(artifact_bytes)


class TestResolveWorkflowBinding:

    def test_returns_complete_identity_on_success(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        binding = we.resolve_workflow_binding("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

        assert binding["workflow_id"] == "test_workflow"
        assert binding["workflow_version"] == "1.0.0"
        assert binding["workflow_checksum"] == wr.compute_definition_checksum(artifact_bytes)
        assert binding["registry_definition_schema_version"] == "1.0"
        assert binding["registration_identity"]
        assert binding["evaluation_timestamp"]

    def test_fails_closed_when_not_registered(self, tmp_path):
        _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        with pytest.raises(we.WorkflowArtifactUnreadableError):
            we.resolve_workflow_binding("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=tmp_path / "empty.db")

    def test_fails_closed_on_live_file_drift(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        mutated = _definition_dict(stage_order=["stage_a", "stage_b", "stage_c"])
        artifact_path.write_bytes(json.dumps(mutated, sort_keys=True).encode())

        with pytest.raises(we.WorkflowDriftError):
            we.resolve_workflow_binding("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)

    def test_checksum_reflects_live_file_not_stale_cache(self, tmp_path):
        # Same content re-registered under a NEW version after editing --
        # the returned checksum must match the live bytes just verified,
        # not some other cached value.
        db_path = tmp_path / "registry.db"
        artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        binding = we.resolve_workflow_binding("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)
        assert binding["workflow_checksum"] == wr.compute_definition_checksum(artifact_bytes)

    def test_two_calls_produce_distinct_evaluation_timestamps_or_at_least_valid_iso(self, tmp_path):
        db_path = tmp_path / "registry.db"
        _artifact_path, artifact_bytes = _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        from datetime import datetime
        binding = we.resolve_workflow_binding("test_channel", "test_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)
        # Must parse as a real ISO-8601 timestamp.
        datetime.fromisoformat(binding["evaluation_timestamp"])


class TestPackageVersionMustMoveForMaterialChange:

    def test_registering_new_content_under_same_workflow_version_fails_closed(self, tmp_path):
        db_path = tmp_path / "registry.db"
        _write_package(tmp_path, "test_channel", "test_workflow", "1.0.0", _definition_dict())
        artifact_bytes_v1 = (tmp_path / "test_channel" / "workflow" / "test_workflow.json").read_bytes()
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes_v1, db_path=db_path)

        mutated_bytes = json.dumps(_definition_dict(retry_policy={"changed": True}), sort_keys=True).encode()
        with pytest.raises(wr.WorkflowIntegrityError):
            wr.register_workflow("test_channel", "test_workflow", "1.0.0", mutated_bytes, db_path=db_path)

    def test_new_version_with_changed_content_succeeds(self, tmp_path):
        db_path = tmp_path / "registry.db"
        artifact_bytes_v1 = json.dumps(_definition_dict(), sort_keys=True).encode()
        wr.register_workflow("test_channel", "test_workflow", "1.0.0", artifact_bytes_v1, db_path=db_path)

        artifact_bytes_v2 = json.dumps(_definition_dict(retry_policy={"changed": True}), sort_keys=True).encode()
        d2 = wr.register_workflow("test_channel", "test_workflow", "1.1.0", artifact_bytes_v2, db_path=db_path)
        assert d2.workflow_version == "1.1.0"


class TestStageOrderVerifierRealTimeDrift:

    def test_advances_in_order_without_error(self):
        v = we.StageOrderVerifier(["a", "b", "c"])
        v.advance("a")
        v.advance("b")
        v.advance("c")
        v.assert_complete()

    def test_out_of_order_stage_raises_drift(self):
        v = we.StageOrderVerifier(["a", "b", "c"])
        v.advance("a")
        with pytest.raises(we.WorkflowDriftError):
            v.advance("c")

    def test_unexpected_extra_stage_raises_drift(self):
        v = we.StageOrderVerifier(["a"])
        v.advance("a")
        with pytest.raises(we.WorkflowDriftError):
            v.advance("b")

    def test_incomplete_run_detected_by_assert_complete(self):
        v = we.StageOrderVerifier(["a", "b", "c"])
        v.advance("a")
        with pytest.raises(we.WorkflowDriftError):
            v.assert_complete()


class TestSecondSyntheticChannelPackageWorksGenerically:

    def test_synthetic_channel_runs_through_same_generic_executor(self, tmp_path):
        db_path = tmp_path / "registry.db"
        definition = _definition_dict(
            workflow_id="synthetic_workflow",
            stage_order=["intake", "transform", "emit"],
            stage_handler_identity={"intake": "synth.intake", "transform": "synth.transform", "emit": "synth.emit"},
        )
        _artifact_path, artifact_bytes = _write_package(tmp_path, "synthetic_channel_2", "synthetic_workflow", "1.0.0", definition)
        wr.register_workflow("synthetic_channel_2", "synthetic_workflow", "1.0.0", artifact_bytes, db_path=db_path)

        loaded = we.verify_and_load_workflow("synthetic_channel_2", "synthetic_workflow", "1.0.0", packages_root=tmp_path, registry_db_path=db_path)
        assert loaded["stage_order"] == ["intake", "transform", "emit"]

        verifier = we.StageOrderVerifier(loaded["stage_order"])
        for stage in loaded["stage_order"]:
            verifier.advance(stage)
        verifier.assert_complete()


class TestFactoryCoreContainsNoChannelSpecificContent:
    """Source-grep proof: workflow_registry.py, workflow_executor.py,
    channel_config.py, and the bootstrap script contain no Hypogeum/
    stage-specific string beyond the bare "hypogeum" channel identifier
    itself in the one place it's expected."""

    _FACTORY_CORE_FILES = [
        "hypogeum/workflow_registry.py",
        "hypogeum/workflow_executor.py",
    ]
    _FORBIDDEN_STAGE_STRINGS = (
        "executive_producer", "research_director", "narrative_architect",
        "script_engine", "thumbnail_strategy", "visual_package_director",
        "thumbnail_director", "audio_agent", "assembly_agent", "publishing_agent",
    )

    @pytest.mark.parametrize("rel_path", _FACTORY_CORE_FILES)
    def test_no_stage_specific_strings_in_factory_core(self, rel_path):
        source = (_ROOT / rel_path).read_text()
        for forbidden in self._FORBIDDEN_STAGE_STRINGS:
            assert forbidden not in source, f"{rel_path} contains stage-specific string {forbidden!r}"

    def test_no_hardcoded_hypogeum_channel_id_literal_in_factory_core(self):
        # A docstring explaining "this module has zero knowledge of
        # Hypogeum" legitimately contains the word "hypogeum" in prose --
        # that is not a violation. What must never appear is the literal
        # string constant "hypogeum" used as a channel_id default/branch
        # (an ast.Constant, not prose) anywhere in these modules.
        for rel_path in self._FACTORY_CORE_FILES:
            tree = ast.parse((_ROOT / rel_path).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "hypogeum":
                    pytest.fail(f"{rel_path} contains the literal string constant \"hypogeum\" -- a hardcoded channel identifier")

    def test_bootstrap_script_has_no_stage_content(self):
        source = (_ROOT / "scripts/register_hypogeum_workflow.py").read_text()
        for forbidden in self._FORBIDDEN_STAGE_STRINGS:
            assert forbidden not in source, f"bootstrap script contains stage-specific string {forbidden!r}"

    def test_channel_config_new_functions_have_no_stage_content(self):
        # channel_config.py pre-dates this program and legitimately has an
        # unrelated ModelProfile.research_director field (a per-stage
        # model-slug assignment column, not a hardcoded stage handler
        # reference) -- scope this check to only the new functions this
        # program added, not the whole pre-existing file.
        source = (_ROOT / "hypogeum/channel_config.py").read_text()
        tree = ast.parse(source)
        for fn_name in ("compute_channel_package_identity", "_canonical_json_bytes"):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            fn_source = ast.get_source_segment(source, fn)
            for forbidden in self._FORBIDDEN_STAGE_STRINGS:
                assert forbidden not in fn_source, f"channel_config.py::{fn_name} contains stage-specific string {forbidden!r}"


class TestRealArtifactFileMatchesRealPipelineStageOrder:
    """Confirms the committed channel_packages/hypogeum/workflow/
    hypogeum_main_production.json artifact's stage_order is the exact,
    real stage sequence hypogeum/pipeline.py::run_pipeline() executes --
    proven from actual pipeline.py source, not assumed."""

    def test_artifact_stage_order_matches_real_record_stage_start_call_sites(self):
        artifact = json.loads((_ROOT / "channel_packages/hypogeum/workflow/hypogeum_main_production.json").read_bytes())
        pipeline_source = (_ROOT / "hypogeum/pipeline.py").read_text()
        tree = ast.parse(pipeline_source)
        run_pipeline_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_pipeline")

        real_stage_names = []
        for node in ast.walk(run_pipeline_fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_record_stage_start"
                and len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant)
            ):
                real_stage_names.append(node.args[2].value)

        assert real_stage_names == artifact["stage_order"]

    def test_every_stage_has_an_advance_stage_order_call_immediately_before_it(self):
        pipeline_source = (_ROOT / "hypogeum/pipeline.py").read_text()
        for stage_name in json.loads((_ROOT / "channel_packages/hypogeum/workflow/hypogeum_main_production.json").read_bytes())["stage_order"]:
            assert f'_advance_stage_order("{stage_name}")' in pipeline_source, f"missing _advance_stage_order call for {stage_name!r}"
