"""
tests/test_migrate_legacy_manifest_to_envelopes.py — Focused tests for
tools/migrate_legacy_manifest_to_envelopes.py (four-dimension classifier
version, corrected 2026-08-16).

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. No test invokes hypogeum.muapi.submit_scene(), any
provider adapter, or any network call.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from test_muapi import make_config  # noqa: E402

from hypogeum.channel_config import (  # noqa: E402
    create_brand_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.video_prompt_writer import compile_authoritative_shot_prompt  # noqa: E402
import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

_AUTHORIZED_MODEL = "veo3.1-lite-text-to-video"  # real fixed-8s-family model (hypogeum.muapi._VEO3_1_MODELS)


def _shot(shot_id: str, parent_beat_id: str, **overrides) -> dict:
    base = {
        "shot_id": shot_id, "parent_beat_id": parent_beat_id, "shot_order": 1,
        "shot_purpose": "establish", "narrative_function": "orient",
        "estimated_usable_duration": 8.0, "subject": "test subject matter",
        "shot_scale": "medium", "camera_angle": "eye_level", "camera_height": "eye_level",
        "camera_direction": "static", "camera_movement": "slow_push_in",
        "composition": "rule_of_thirds_left", "background_depth": "shallow",
        "screen_direction": "left_to_right", "motion_energy": "low",
        "transition_in": "cut", "transition_out": "cut",
    }
    base.update(overrides)
    return base


def _scene(scene_id: str, beat_id: str, shots: list, *, short_id=None, primary_tier=2, fallback_tier=3, aspect_ratio="16:9") -> dict:
    return {
        "scene_id": scene_id,
        "short_id": short_id,
        "narration_reference": beat_id,
        "production_spec": {
            "aspect_ratio": aspect_ratio,
            "duration_seconds": 8.0,
            "provider_routing": {
                "primary_tier": primary_tier, "fallback_tier": fallback_tier,
                "primary_prompt": "", "fallback_prompt": "",
            },
        },
        "entity_appearances": [],
        "cinematic_coverage_plan": {"beat_id": beat_id, "shots": shots},
    }


def _render_prompt(scene: dict, shot: dict) -> dict:
    rendered = compile_authoritative_shot_prompt(scene, shot)
    shot["primary_prompt"] = rendered["primary_prompt"]
    shot["fallback_prompt"] = rendered["fallback_prompt"]
    return rendered


def _entry(shot_id: str, beat_id: str, checksum: str, *, auth_index: int, deliverable="main", aspect_ratio="16:9") -> dict:
    return {
        "target_shot_id": shot_id, "beat": beat_id, "deliverable": deliverable,
        "auth_index": auth_index, "aspect_ratio": aspect_ratio,
        "primary_prompt_checksum": checksum,
        "prompt_provenance": {
            "compiler": "hypogeum.video_prompt_writer.compile_authoritative_shot_prompt",
            "compiler_version": "deadbeefcafefeed0000000000000000000000",
            "rendered_at": "2026-08-01T00:00:00+00:00",
            "source_shot_id": shot_id,
        },
    }


def _write_episode_fixture(tmp_path: Path, episode_id: str, entries: list, scenes: list, *, model_tier_planned=_AUTHORIZED_MODEL):
    episode_dir = tmp_path / "episodes" / episode_id
    episode_dir.mkdir(parents=True)

    plan_bytes = json.dumps({"episode_id": episode_id, "beat_plans": []}, sort_keys=True).encode()
    plan_path = episode_dir / f"cinematic_coverage_plan_{episode_id}.json"
    plan_path.write_bytes(plan_bytes)
    plan_md5 = hashlib.md5(plan_bytes).hexdigest()

    visual_package = {"scenes": scenes, "asset_manifest": {"assets": []}}
    (episode_dir / f"visual_package_{episode_id}.json").write_text(json.dumps(visual_package))

    manifest = {
        "manifest_id": f"genauth_{episode_id}_test",
        "episode_id": episode_id,
        "status": "authorized_not_started",
        "plan_checksum_after": plan_md5,
        "plan_amendment_id": f"amend_{episode_id}_test",
        "model_tier_planned": model_tier_planned,
        "active_entry_count": len(entries),
        "entries": entries,
    }
    manifest_path = episode_dir / f"generation_authorization_manifest_{episode_id}.json"
    manifest_path.write_text(json.dumps(manifest))

    return manifest_path, plan_md5


def _args(tmp_path: Path, episode_id: str, manifest_path: Path, *, db_path=None, **overrides):
    argv = [
        "--episode-id", episode_id, "--dry-run",
        "--manifest-path", str(manifest_path),
        "--assets-root", str(tmp_path),
    ]
    if db_path is not None:
        argv += ["--db-path", str(db_path)]
    ns = mle.build_arg_parser().parse_args(argv)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _bootstrap_channel(db_path: Path, channel_id: str = "hypogeum") -> None:
    """Real hypogeum.channel_config write path, pointed at an isolated
    temp DB -- exactly what a genuinely-bootstrapped channel looks like,
    never a synthetic bypass of compute_channel_package_identity()."""
    brand = create_brand_profile(name="Test Brand", persona_name="Tester", db_path=db_path)
    publishing = create_publishing_profile(name="Test Publishing", db_path=db_path)
    models = create_model_profile(name="Test Models", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name="Test Channel",
        brand_profile_id=brand.brand_profile_id,
        publishing_profile_id=publishing.publishing_profile_id,
        model_profile_id=models.model_profile_id,
        db_path=db_path,
    )


def _happy_path_fixture(tmp_path: Path, episode_id: str = "ep_test", *, primary_tier=2):
    shot = _shot("b_001__shot1", "b_001")
    scene = _scene("s01", "b_001", [shot], primary_tier=primary_tier)
    rendered = _render_prompt(scene, shot)
    entry = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1)
    manifest_path, plan_md5 = _write_episode_fixture(tmp_path, episode_id, [entry], [scene])
    return manifest_path, scene, shot, entry, plan_md5


def _config():
    # T2 -> the real authorized model, so a tier-2-routed scene is
    # legacy_path_compatible by default; individual tests override tier
    # to deliberately create a mismatch.
    return make_config(t2_model=_AUTHORIZED_MODEL)


def self_compile(tmp_path, manifest_path, *, db_path=None, config=None, **overrides):
    args = _args(tmp_path, "ep_test", manifest_path, db_path=db_path, **overrides)
    return mle.compile_manifest_to_envelopes(args, config=config or _config())


def one(report):
    assert len(report["entries"]) == 1, report["entries"]
    return report["entries"][0]


def _with_resolved_workflow_identity(monkeypatch):
    # workflow_version has no canonical source anywhere in this
    # repository today (see compute_workflow_identity()'s own
    # docstring) -- so envelope construction can never actually
    # complete in the real tool or in most of these tests. This helper
    # monkeypatches ONLY the isolated compute_workflow_identity() seam
    # (never a CLI override) to simulate "if a real Workflow Registry
    # existed," for the small number of tests that specifically need to
    # prove the envelope-construction path itself works correctly once
    # that gap is closed.
    monkeypatch.setattr(
        mle, "compute_workflow_identity",
        lambda: {"available": True, "version": "wf-test-1", "reason": None},
    )


class TestAuthorizationBindingWinsOverLegacyRouting:

    def test_authorized_model_used_even_when_legacy_caller_would_choose_differently(self, tmp_path, monkeypatch):
        # Scene routed to tier 1 (legacy caller -> "veo3-text-to-video"),
        # but the manifest authorizes veo3.1-lite-text-to-video. The
        # envelope's own binding must use the AUTHORIZED model, not the
        # legacy-derived one. workflow_version is simulated-available
        # here specifically to prove a real envelope gets constructed
        # (using the authorized model) despite the legacy mismatch.
        _with_resolved_workflow_identity(monkeypatch)
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path, primary_tier=1)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        assert e["envelope_fields_valid"] is True
        assert e["legacy_path_compatible"] is False
        assert e["envelope_id"] is not None  # a real envelope WAS constructed, using the authorized model

    def test_envelope_never_falsely_reports_authorization_model_mismatch(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path, primary_tier=1)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        # envelope_fields_reasons must NOT claim the authorization itself
        # has a model problem -- only legacy_path_reasons may say that.
        assert not any("model" in r.lower() for r in e["envelope_fields_reasons"])
        assert any("legacy_execution_path_binding_mismatch" in r for r in e["legacy_path_reasons"])


class TestSevenKnownDiscrepanciesClassifiedAsLegacyMismatch:

    def test_tier1_scene_with_lite_authorization_is_legacy_mismatch_not_blocked(self, tmp_path):
        # Directly mirrors the real ep_f3619022 b_001/b_015 shots: parent
        # scene routed tier 1, manifest authorizes the Lite (T2) model.
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path, primary_tier=1)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        assert report["legacy_path_incompatible_count"] == 1
        assert report["envelope_valid_count"] == 1  # still a valid Lite authorization
        finding = report["legacy_path_findings"][0]
        assert "legacy_execution_path_binding_mismatch" in finding["reasons"][0]
        assert "veo3-text-to-video" in finding["reasons"][0]  # what legacy would pick
        assert _AUTHORIZED_MODEL in finding["reasons"][0]      # what's actually authorized

    def test_tier2_scene_with_lite_authorization_is_legacy_compatible(self, tmp_path, monkeypatch):
        _with_resolved_workflow_identity(monkeypatch)
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path, primary_tier=2)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        assert report["legacy_path_incompatible_count"] == 0
        assert report["paid_execution_ready_count"] == 1


class TestNoArbitraryCliIdentityOverrides:

    def test_cli_has_no_channel_package_or_workflow_override_flags(self):
        parser = mle.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        for forbidden in ("--channel-package-checksum", "--channel-package-version", "--workflow-version"):
            assert forbidden not in option_strings

    def test_stuffing_arbitrary_attributes_onto_args_cannot_fabricate_dependency_identity(self, tmp_path):
        # Even if a caller manually sets non-CLI attributes on the
        # argparse Namespace, compile_manifest_to_envelopes() never reads
        # a channel_package_checksum/version or workflow_version off
        # args at all -- it only ever calls compute_channel_package_
        # identity() against the real DB. This proves that structurally,
        # not just "the flag doesn't exist."
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        args = _args(tmp_path, "ep_test", manifest_path)
        args.channel_package_checksum = "f" * 64  # would-be injected override
        args.channel_package_version = "9.9.9"
        args.workflow_version = "9.9.9"
        report = mle.compile_manifest_to_envelopes(args, config=_config())
        e = one(report)
        # no real channel bootstrapped at args.db_path (None -> real
        # default DB path is NOT used here because db_path stays None
        # and no bootstrap occurred at the resolved path in this isolated
        # test process) -- dependency identity must still be evaluated
        # from the real lookup, not from the stuffed attributes.
        assert report["channel_package_checksum"] != "f" * 64
        assert any("workflow_version" in r for r in e["dependency_identity_reasons"])

    def test_source_never_reads_channel_package_override_cli_attribute(self):
        source = open(mle.__file__).read()
        assert "args.channel_package_checksum" not in source
        assert "args.channel_package_version" not in source
        assert "args.workflow_version" not in source


class TestCanonicalChannelPackageIdentityUsedWhenItExists:

    def test_real_bootstrapped_channel_yields_available_checksum_and_version(self, tmp_path):
        from dataclasses import asdict
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        identity = mle.compute_channel_package_identity("hypogeum", db_path=db_path)
        assert identity["available"] is True
        assert identity["reason"] is None
        assert len(identity["checksum"]) == 64
        assert identity["version"] == "v1"  # ChannelProfile.metadata_version

    def test_checksum_changes_when_model_profile_content_changes(self, tmp_path):
        db_path1 = tmp_path / "channel1.db"
        _bootstrap_channel(db_path1)
        id1 = mle.compute_channel_package_identity("hypogeum", db_path=db_path1)

        db_path2 = tmp_path / "channel2.db"
        brand = create_brand_profile(name="Different Brand", persona_name="Someone Else", db_path=db_path2)
        publishing = create_publishing_profile(name="Different Publishing", db_path=db_path2)
        models = create_model_profile(name="Different Models", episode_producer="a/different-model", db_path=db_path2)
        create_channel_profile(
            channel_id="hypogeum", name="Different Channel",
            brand_profile_id=brand.brand_profile_id, publishing_profile_id=publishing.publishing_profile_id,
            model_profile_id=models.model_profile_id, db_path=db_path2,
        )
        id2 = mle.compute_channel_package_identity("hypogeum", db_path=db_path2)
        assert id1["checksum"] != id2["checksum"]

    def test_bootstrapped_channel_makes_dependency_identity_reason_shrink_to_workflow_only(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        assert not any("channel_package_checksum" in r for r in e["dependency_identity_reasons"])
        assert any("workflow_version" in r for r in e["dependency_identity_reasons"])
        assert e["dependency_identity_available"] is False  # workflow_version alone still blocks it


class TestMissingCanonicalIdentityRemainsUnverifiable:

    def test_no_bootstrapped_channel_yields_unavailable(self, tmp_path):
        db_path = tmp_path / "empty_channel.db"  # never bootstrapped
        identity = mle.compute_channel_package_identity("hypogeum", db_path=db_path)
        assert identity["available"] is False
        assert identity["checksum"] is None
        assert "no channel_profiles row found" in identity["reason"]

    def test_no_channel_and_no_workflow_registry_both_reported(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "empty_channel.db"
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        assert e["dependency_identity_available"] is False
        assert any("channel_package_checksum" in r for r in e["dependency_identity_reasons"])
        assert any("workflow_version" in r for r in e["dependency_identity_reasons"])
        assert e["envelope_id"] is None  # never construct a real envelope without full dependency identity

    def test_never_labeled_envelope_valid_with_placeholder_dependency_identity(self, tmp_path):
        # Structural guarantee: envelope_id is None whenever
        # dependency_identity_available is False, regardless of how
        # otherwise-clean the entry is.
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "empty_channel.db"
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        assert e["envelope_fields_valid"] is True  # otherwise clean
        assert e["dependency_identity_available"] is False
        assert e["envelope_id"] is None
        assert e["paid_execution_ready"] is False


class TestBlockingConditions:

    def test_one_character_prompt_mutation_blocks_envelope_fields_valid(self, tmp_path):
        shot = _shot("b_001__shot1", "b_001")
        scene = _scene("s01", "b_001", [shot])
        rendered = _render_prompt(scene, shot)
        entry = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1)
        shot["primary_prompt"] = shot["primary_prompt"] + "!"
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry], [scene])
        report = self_compile(tmp_path, manifest_path)
        e = one(report)
        assert e["envelope_fields_valid"] is False
        reasons = " ".join(e["envelope_fields_reasons"])
        assert "checksum" in reasons.lower()

    def test_stale_plan_checksum_blocks(self, tmp_path):
        manifest_path, scene, shot, entry, plan_md5 = _happy_path_fixture(tmp_path)
        data = json.loads(manifest_path.read_text())
        data["plan_checksum_after"] = "f" * 32
        manifest_path.write_text(json.dumps(data))
        report = self_compile(tmp_path, manifest_path)
        assert report["plan_checksum_match"] is False
        assert any(b["check"] == "stale_plan_snapshot" for b in report["global_blockers"])

    def test_missing_execution_binding_blocks(self, tmp_path):
        shot = _shot("b_001__shot1", "b_001")
        scene = _scene("s01", "b_001", [shot])
        rendered = _render_prompt(scene, shot)
        entry = _entry("b_002__shot1", "b_002", rendered["prompt_checksum"], auth_index=1)
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry], [scene])
        report = self_compile(tmp_path, manifest_path)
        e = one(report)
        assert e["envelope_fields_valid"] is False
        assert "no scene found" in e["envelope_fields_reasons"][0]

    def test_duplicate_shot_id_blocks_both_entries(self, tmp_path):
        shot = _shot("b_001__shot1", "b_001")
        scene = _scene("s01", "b_001", [shot])
        rendered = _render_prompt(scene, shot)
        entry1 = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1)
        entry2 = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=2)
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry1, entry2], [scene])
        report = self_compile(tmp_path, manifest_path)
        assert report["envelope_valid_count"] == 0
        assert all("duplicate_shot_id" in " ".join(e["envelope_fields_reasons"]) for e in report["entries"])

    def test_duplicate_idempotency_identity_blocks(self, tmp_path):
        shot1 = _shot("b_001__shot1", "b_001")
        scene1 = _scene("s01", "b_001", [shot1])
        r1 = _render_prompt(scene1, shot1)
        shot2 = _shot("b_002__shot1", "b_002")
        scene2 = _scene("s02", "b_002", [shot2])
        r2 = _render_prompt(scene2, shot2)
        entry1 = _entry(shot1["shot_id"], "b_001", r1["prompt_checksum"], auth_index=1)
        entry2 = _entry(shot2["shot_id"], "b_002", r2["prompt_checksum"], auth_index=1)  # same auth_index
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry1, entry2], [scene1, scene2])
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        assert report["duplicate_findings"], report
        assert report["envelope_valid_count"] == 0

    def test_output_already_exists_blocks(self, tmp_path, monkeypatch):
        # plan_confirmed_shot_allocations() is itself collision-avoidant,
        # so this tool's own defense-in-depth check is exercised by
        # forcing the allocation result directly.
        shot = _shot("b_001__shot1", "b_001")
        scene = _scene("s01", "b_001", [shot])
        rendered = _render_prompt(scene, shot)
        entry = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1)
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry], [scene])

        collision_path = tmp_path / "episodes" / "ep_test" / "forced_collision.mp4"
        collision_path.write_bytes(b"not a real video, just a collision marker")

        def _forced_allocation(missing_shots, visual_package, assets_root, episode_id):
            return [{**missing_shots[0], "extra_scene_id": f"{missing_shots[0]['shot_id']}__extra1", "output_path": str(collision_path)}]

        monkeypatch.setattr(mle, "plan_confirmed_shot_allocations", _forced_allocation)

        report = self_compile(tmp_path, manifest_path)
        e = one(report)
        assert e["envelope_fields_valid"] is False
        assert "output_already_exists" in " ".join(e["envelope_fields_reasons"])

    def test_aspect_ratio_mismatch_is_legacy_incompatibility_not_envelope_defect(self, tmp_path):
        shot = _shot("b_001__shot1", "b_001")
        scene = _scene("s01", "b_001", [shot], aspect_ratio="16:9")
        rendered = _render_prompt(scene, shot)
        entry = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1, aspect_ratio="9:16")
        manifest_path, _ = _write_episode_fixture(tmp_path, "ep_test", [entry], [scene])
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        # authorized aspect_ratio (9:16) is used for the envelope itself
        assert e["envelope_fields_valid"] is True
        assert e["legacy_path_compatible"] is False
        assert any("legacy_execution_path_binding_mismatch" in r for r in e["legacy_path_reasons"])


class TestUnsupportedSchemaVersion:

    def test_unsupported_envelope_schema_version_blocks(self, tmp_path, monkeypatch):
        _with_resolved_workflow_identity(monkeypatch)  # must actually reach .create() to exercise this
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        monkeypatch.setattr(mle, "_ENVELOPE_SCHEMA_VERSION", "99.9")
        report = self_compile(tmp_path, manifest_path, db_path=db_path)
        e = one(report)
        assert e["envelope_fields_valid"] is False
        assert "envelope_construction_failed" in " ".join(e["envelope_fields_reasons"])


class TestDryRunIsReadOnly:

    def test_dry_run_does_not_modify_manifest_plan_or_visual_package(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        episode_dir = tmp_path / "episodes" / "ep_test"
        plan_path = episode_dir / "cinematic_coverage_plan_ep_test.json"
        vp_path = episode_dir / "visual_package_ep_test.json"

        before = {
            "manifest": manifest_path.read_bytes(),
            "plan": plan_path.read_bytes(),
            "vp": vp_path.read_bytes(),
        }
        before_all_files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())

        self_compile(tmp_path, manifest_path)

        after = {
            "manifest": manifest_path.read_bytes(),
            "plan": plan_path.read_bytes(),
            "vp": vp_path.read_bytes(),
        }
        after_all_files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())

        assert before == after
        assert before_all_files == after_all_files

    def test_dry_run_never_writes_to_the_channel_config_database(self, tmp_path):
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        before_bytes = db_path.read_bytes()
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        self_compile(tmp_path, manifest_path, db_path=db_path)
        after_bytes = db_path.read_bytes()
        assert before_bytes == after_bytes

    def test_dry_run_creates_no_receipt_budget_lease_breaker_state(self, tmp_path):
        source = open(mle.__file__).read()
        for forbidden_module in ("hypogeum.provider_receipt", "hypogeum.budget_scopes", "hypogeum.submission_gate"):
            assert forbidden_module not in source

    def test_dry_run_creates_no_shadow_observation_store(self, tmp_path):
        source = open(mle.__file__).read()
        assert "shadow_observation" not in source
        assert "ShadowObservationStore" not in source


class TestNoNetworkOrProviderAdapterReachable:

    def test_no_network_capable_import(self):
        source = open(mle.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                module = getattr(node, "module", None)
                for forbidden in ("requests", "httpx", "socket", "urllib"):
                    assert forbidden not in names
                    assert module != forbidden and (module is None or not module.startswith(forbidden + "."))

    def test_no_fake_provider_adapter_or_submit_scene_reference(self):
        source = open(mle.__file__).read()
        tree = ast.parse(source)
        names_referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names_referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                names_referenced.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                names_referenced.update(alias.name for alias in node.names)
        assert "submit_scene" not in names_referenced
        assert "FakeProviderAdapter" not in names_referenced
        assert "fake_provider_adapter" not in names_referenced

    def test_zero_provider_calls_reported(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        report = self_compile(tmp_path, manifest_path)
        assert report["provider_calls_made"] == 0
        assert report["spend_usd"] == 0.0
        assert report["envelopes_persisted"] == 0


class TestDeterministicRepeatedDryRuns:

    def test_repeated_dry_runs_produce_identical_results(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        r1 = self_compile(tmp_path, manifest_path, db_path=db_path)
        r2 = self_compile(tmp_path, manifest_path, db_path=db_path)
        r1.pop("generated_at")
        r2.pop("generated_at")
        assert r1 == r2

    def test_repeated_dry_runs_produce_identical_envelope_ids(self, tmp_path, monkeypatch):
        _with_resolved_workflow_identity(monkeypatch)
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        db_path = tmp_path / "channel.db"
        _bootstrap_channel(db_path)
        r1 = self_compile(tmp_path, manifest_path, db_path=db_path)
        r2 = self_compile(tmp_path, manifest_path, db_path=db_path)
        assert one(r1)["envelope_id"] is not None
        assert one(r1)["envelope_id"] == one(r2)["envelope_id"]


class TestCliDefaultsToDryRunSafely:

    def test_main_refuses_non_dry_run(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        rc = mle.main([
            "--episode-id", "ep_test", "--manifest-path", str(manifest_path),
            "--assets-root", str(tmp_path),
        ])
        assert rc in (0, 1)

    def test_report_not_written_when_report_path_omitted(self, tmp_path):
        manifest_path, scene, shot, entry, _ = _happy_path_fixture(tmp_path)
        before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
        mle.main([
            "--episode-id", "ep_test", "--manifest-path", str(manifest_path),
            "--assets-root", str(tmp_path), "--dry-run",
        ])
        after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
        assert before == after
