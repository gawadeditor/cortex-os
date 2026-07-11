"""
tests/test_upstream_skill_provenance.py — Upstream skill installation integrity

Coverage:
    TestHumanizerInstallation          — pinned directory/files exist, checksums
                                          match, no local modification, no
                                          executable content
    TestHumanizerWiredIntoScriptEngine — exactly one agent (script_engine.py)
                                          references the pinned upstream skill;
                                          it is composed into the system prompt,
                                          not registered in skill_registry.py
                                          (that registry is for internal skills
                                          only); manifest state is ACTIVE
    TestHumanizerRollback              — rollback scope is exactly the pinned
                                          directory, the manifest entry, and
                                          script_engine.py's composition call
    TestGPTImage2Installation                    — pinned directory/files exist,
                                                    checksums match, byte-identical
                                                    to the pinned upstream commit,
                                                    no executable content
    TestGPTImage2WiredIntoThumbnailPromptWriter  — exactly one agent
                                                    (thumbnail_prompt_writer.py)
                                                    references the pinned upstream
                                                    skill; composed into the system
                                                    prompt, not registered in
                                                    skill_registry.py; manifest
                                                    state is ACTIVE; no new stage/
                                                    adapter/checkpoint/schema exists
    TestGPTImage2Rollback                        — rollback scope is exactly the
                                                    pinned directory, the manifest
                                                    entry, and
                                                    thumbnail_prompt_writer.py's
                                                    composition call plus
                                                    thumbnail_director.txt's bridge
    TestSeedance2Installation                    — pinned directory/files exist,
                                                    checksums match, byte-identical
                                                    to the pinned upstream commit,
                                                    no executable content
    TestSeedance2NotWired                        — installed but not yet attached
                                                    to any agent; no runtime module
                                                    references the pinned path;
                                                    not registered in
                                                    skill_registry.py; existing
                                                    internal Seedance skill files
                                                    (hypogeum/skills/seedance_translation_skill.md
                                                    and its compact variant) and
                                                    visual_package_director.py are
                                                    unchanged; manifest state is
                                                    INSTALLED / NOT WIRED / NOT ACTIVE
    TestSeedance2Rollback                        — rollback scope is exactly the
                                                    pinned directory and the
                                                    manifest entry

Scope: installation provenance and wiring-surface-area only. Does not test
Humanizer's, GPT-Image2's, or Seedance2's prompt-writing behavior (Claude
applies a skill at generation time — there is no separate deterministic
code path to unit test here, and Seedance2 is not wired to any agent yet
regardless).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HUMANIZER_PINNED_SHA = "1b48564898e999219882660237fde01bf4843a0f"
_HUMANIZER_DIR = _REPO_ROOT / "third_party" / "skills" / "humanizer" / _HUMANIZER_PINNED_SHA
_MANIFEST_JSON = _REPO_ROOT / "config" / "upstream_skills_manifest.json"

_EXPECTED_CHECKSUMS = {
    "SKILL.md": "243aecdafecb5e11c2d45e2e088b7876e3f6eee34aa50c53f624d8468039afa8",
    "LICENSE": "4ac4810254ab36d45419141aeb8e69bf50652cfafe5b2dab947d06d44e5cbf96",
}

_GPT_IMAGE2_PINNED_SHA = "74a665173a19e461fa08f84ae76d800f85d2dd1e"
_GPT_IMAGE2_DIR = _REPO_ROOT / "third_party" / "skills" / "gpt-image2" / _GPT_IMAGE2_PINNED_SHA

_GPT_IMAGE2_EXPECTED_CHECKSUMS = {
    "SKILL.md": "b9a8226006433441b905141976a13943bffb813fd8443c7ec3470d08c381b6b8",
    "LICENSE": "9fc35d3971ef0358bfee870305c757c4e3482b8a184a1c3b038dce9579d40f06",
}

_SEEDANCE2_PINNED_SHA = "e06c7c63a766d623004a2807881c30685ce517af"
_SEEDANCE2_DIR = _REPO_ROOT / "third_party" / "skills" / "seedance2" / _SEEDANCE2_PINNED_SHA

_SEEDANCE2_EXPECTED_CHECKSUMS = {
    "SKILL.md": "107dd9fa5ef46f64d3d5183957244fd42f32a91df57e615ba63338c1344cf442",
    "LICENSE": "56abf51bba5502dcc1cd014c9adea5787cd9eec67421858484b660ec97922978",
}

_SEEDANCE_TRANSLATION_SKILL_FILE = (
    _REPO_ROOT / "hypogeum" / "skills" / "seedance_translation_skill.md"
)
_SEEDANCE_TRANSLATION_SKILL_COMPACT_FILE = (
    _REPO_ROOT / "hypogeum" / "skills" / "compiled" / "seedance_translation_skill.compact.md"
)

_EXECUTABLE_EXTENSIONS = {
    ".py", ".sh", ".ts", ".js", ".cjs", ".mjs", ".rb", ".pl", ".exe", ".bin",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestHumanizerInstallation:
    def test_pinned_directory_exists(self):
        assert _HUMANIZER_DIR.is_dir(), (
            f"Expected pinned Humanizer directory at {_HUMANIZER_DIR}"
        )

    def test_directory_name_matches_pinned_commit(self):
        assert _HUMANIZER_DIR.name == _HUMANIZER_PINNED_SHA
        assert len(_HUMANIZER_PINNED_SHA) == 40, "must be a full commit SHA, not a short SHA"

    def test_required_files_exist(self):
        for filename in _EXPECTED_CHECKSUMS:
            assert (_HUMANIZER_DIR / filename).is_file(), (
                f"Missing required upstream file: {filename}"
            )

    def test_no_additional_files_present(self):
        actual_files = {p.name for p in _HUMANIZER_DIR.iterdir() if p.is_file()}
        assert actual_files == set(_EXPECTED_CHECKSUMS), (
            f"Unexpected files in pinned Humanizer directory: "
            f"{actual_files - set(_EXPECTED_CHECKSUMS)}"
        )

    def test_checksums_match_recorded_manifest(self):
        for filename, expected_sha256 in _EXPECTED_CHECKSUMS.items():
            actual = _sha256(_HUMANIZER_DIR / filename)
            assert actual == expected_sha256, (
                f"{filename} checksum mismatch — file may have been locally "
                f"modified. Expected {expected_sha256}, got {actual}"
            )

    def test_no_executable_content_in_installed_tree(self):
        for path in _HUMANIZER_DIR.rglob("*"):
            if path.is_file():
                assert path.suffix.lower() not in _EXECUTABLE_EXTENSIONS, (
                    f"Executable file found in Humanizer's pure-markdown tree: {path}"
                )

    def test_manifest_json_records_this_installation(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entries = [s for s in data["skills"] if s["name"] == "humanizer"]
        assert len(entries) == 1, "expected exactly one humanizer entry"
        entry = entries[0]
        assert entry["pinned_commit_sha"] == _HUMANIZER_PINNED_SHA
        assert entry["owner"] == "blader"
        assert entry["repository"] == "humanizer"
        assert entry["license_identifier"] == "MIT"
        recorded_checksums = {
            Path(f["path"]).name: f["sha256"] for f in entry["file_inventory"]
        }
        assert recorded_checksums == _EXPECTED_CHECKSUMS


class TestHumanizerWiredIntoScriptEngine:
    """
    Humanizer is a skill attached to Script Engine's own single LLM
    invocation — not a separate agent, pipeline stage, adapter, or
    checkpoint layer. These tests confirm the wiring surface area is
    exactly script_engine.py and nothing else.
    """

    def test_exactly_one_agent_references_pinned_upstream_skill(self):
        """
        Checks specifically for the third_party/skills/humanizer path or the
        pinned Humanizer commit SHA, not the generic word "humanizer" or the
        generic word "third_party" — hypogeum/agents/script_engine.py
        already imports the pre-existing, unrelated hypogeum/humanizer.py
        module (a deterministic regex utility explicitly documented as "a
        subset of humanizer v2.7.0", predating and independent of this
        upstream skill entirely), and other agents (e.g. thumbnail_prompt_writer.py)
        legitimately reference a different pinned skill under third_party/
        of their own. A generic "third_party" substring check would
        false-positive on both.
        """
        agents_dir = _REPO_ROOT / "hypogeum" / "agents"
        referencing = []
        for py_file in agents_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if "skills/humanizer" in content or _HUMANIZER_PINNED_SHA in content:
                referencing.append(py_file.name)
        assert referencing == ["script_engine.py"], (
            f"Expected only script_engine.py to reference the pinned Humanizer "
            f"skill, got: {referencing}"
        )

    def test_no_other_runtime_module_imports_humanizer_path(self):
        hypogeum_dir = _REPO_ROOT / "hypogeum"
        offenders = []
        for py_file in hypogeum_dir.rglob("*.py"):
            if py_file.name == "script_engine.py":
                continue
            content = py_file.read_text(encoding="utf-8")
            if "skills/humanizer" in content or _HUMANIZER_PINNED_SHA in content:
                offenders.append(str(py_file.relative_to(_REPO_ROOT)))
        assert not offenders, (
            f"No runtime module besides script_engine.py may reference the "
            f"pinned Humanizer path: {offenders}"
        )

    def test_skill_registry_does_not_register_humanizer(self):
        """
        hypogeum/skill_registry.py is the registry for internal skills under
        hypogeum/skills/ only (per its own module docstring) — a pinned,
        commit-SHA-scoped third-party skill under third_party/skills/ is a
        deliberately different, simpler pattern (a direct path constant in
        the owning agent, exactly like script_engine.py's own
        _HUMANIZER_SKILL_PATH), not a registry entry.
        """
        registry_content = (_REPO_ROOT / "hypogeum" / "skill_registry.py").read_text(
            encoding="utf-8"
        )
        assert "humanizer" not in registry_content.lower()

    def test_script_engine_composes_pinned_skill_path(self):
        content = (_REPO_ROOT / "hypogeum" / "agents" / "script_engine.py").read_text(
            encoding="utf-8"
        )
        assert _HUMANIZER_PINNED_SHA in content
        assert "compose_system_prompt" in content

    def test_manifest_state_is_active(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entry = next(s for s in data["skills"] if s["name"] == "humanizer")
        assert "ACTIVE" in entry["state"].split(" / "), (
            f"Humanizer must be marked ACTIVE now that it is wired, got: {entry['state']}"
        )


class TestHumanizerRollback:
    def test_rollback_scope_is_script_engine_plus_pinned_directory(self):
        """
        Rollback requires reverting exactly two things: script_engine.py's
        composition call (back to load_prompt(_PROMPT_FILE) alone) and,
        optionally, deleting the pinned directory + manifest entry. No
        adapter, checkpoint, or stage directory exists for this to depend
        on — Humanizer's only wiring surface is the single agent it is
        attached to.
        """
        assert not (_REPO_ROOT / "hypogeum" / "skill_adapters").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "humanization_stage.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "humanizer_llm_adapter.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "humanization_checkpoint.py").exists()


class TestGPTImage2Installation:
    def test_pinned_directory_exists(self):
        assert _GPT_IMAGE2_DIR.is_dir(), (
            f"Expected pinned GPT-Image2 directory at {_GPT_IMAGE2_DIR}"
        )

    def test_directory_name_matches_pinned_commit(self):
        assert _GPT_IMAGE2_DIR.name == _GPT_IMAGE2_PINNED_SHA
        assert len(_GPT_IMAGE2_PINNED_SHA) == 40, "must be a full commit SHA, not a short SHA"

    def test_required_files_exist(self):
        for filename in _GPT_IMAGE2_EXPECTED_CHECKSUMS:
            assert (_GPT_IMAGE2_DIR / filename).is_file(), (
                f"Missing required upstream file: {filename}"
            )

    def test_no_additional_files_present(self):
        """
        Only SKILL.md and LICENSE are installed — no reference galleries
        (references/), no CLI scripts (scripts/, src/), no plugin/repo
        scaffolding. SKILL.md's own "Scale references" section describes
        the reference files as optional, load-on-demand material, not a
        hard dependency, so excluding them does not leave SKILL.md
        non-functional as an installed, readable document.
        """
        actual_files = {p.name for p in _GPT_IMAGE2_DIR.iterdir() if p.is_file()}
        assert actual_files == set(_GPT_IMAGE2_EXPECTED_CHECKSUMS), (
            f"Unexpected files in pinned GPT-Image2 directory: "
            f"{actual_files - set(_GPT_IMAGE2_EXPECTED_CHECKSUMS)}"
        )
        actual_dirs = {p.name for p in _GPT_IMAGE2_DIR.iterdir() if p.is_dir()}
        assert not actual_dirs, (
            f"No subdirectories (references/, scripts/, agents/) may be installed: {actual_dirs}"
        )

    def test_checksums_match_recorded_manifest(self):
        for filename, expected_sha256 in _GPT_IMAGE2_EXPECTED_CHECKSUMS.items():
            actual = _sha256(_GPT_IMAGE2_DIR / filename)
            assert actual == expected_sha256, (
                f"{filename} checksum mismatch — file may have been locally "
                f"modified. Expected {expected_sha256}, got {actual}"
            )

    def test_no_executable_content_in_installed_tree(self):
        for path in _GPT_IMAGE2_DIR.rglob("*"):
            if path.is_file():
                assert path.suffix.lower() not in _EXECUTABLE_EXTENSIONS, (
                    f"Executable file found in GPT-Image2's pinned tree: {path}"
                )

    def test_manifest_json_records_this_installation(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entries = [s for s in data["skills"] if s["name"] == "gpt-image2"]
        assert len(entries) == 1, "expected exactly one gpt-image2 entry"
        entry = entries[0]
        assert entry["pinned_commit_sha"] == _GPT_IMAGE2_PINNED_SHA
        assert entry["owner"] == "wuyoscar"
        assert entry["repository"] == "GPT-Image2-Skill"
        assert entry["canonical_url"] == "https://github.com/wuyoscar/GPT-Image2-Skill"
        recorded_checksums = {
            Path(f["path"]).name: f["sha256"] for f in entry["file_inventory"]
        }
        assert recorded_checksums == _GPT_IMAGE2_EXPECTED_CHECKSUMS

    def test_manifest_records_owning_agent(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entry = next(s for s in data["skills"] if s["name"] == "gpt-image2")
        assert "thumbnail_prompt_writer.py" in entry["intended_owning_agent"]
        assert "compose_system_prompt" in entry["attachment_mechanism"]
        assert "thumbnail_prompt_writer.py" in entry["wired_to"]


class TestGPTImage2WiredIntoThumbnailPromptWriter:
    """
    GPT-Image2 is a skill attached directly to thumbnail_prompt_writer.py's
    own single LLM invocation — not a separate agent, pipeline stage,
    adapter, or checkpoint layer. These tests confirm the wiring surface
    area is exactly thumbnail_prompt_writer.py and nothing else, mirroring
    TestHumanizerWiredIntoScriptEngine above.
    """

    def test_exactly_one_agent_references_pinned_upstream_skill(self):
        agents_dir = _REPO_ROOT / "hypogeum" / "agents"
        referencing = []
        for py_file in agents_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if "gpt-image2" in content or _GPT_IMAGE2_PINNED_SHA in content:
                referencing.append(py_file.name)
        assert referencing == ["thumbnail_prompt_writer.py"], (
            f"Expected only thumbnail_prompt_writer.py to reference the pinned "
            f"GPT-Image2 skill, got: {referencing}"
        )

    def test_no_other_runtime_module_imports_gpt_image2_path(self):
        hypogeum_dir = _REPO_ROOT / "hypogeum"
        offenders = []
        for py_file in hypogeum_dir.rglob("*.py"):
            if py_file.name == "thumbnail_prompt_writer.py":
                continue
            content = py_file.read_text(encoding="utf-8")
            if "gpt-image2" in content or _GPT_IMAGE2_PINNED_SHA in content:
                offenders.append(str(py_file.relative_to(_REPO_ROOT)))
        assert not offenders, (
            f"No runtime module besides thumbnail_prompt_writer.py may reference "
            f"the pinned GPT-Image2 path: {offenders}"
        )

    def test_thumbnail_prompt_writer_composes_pinned_skill_path(self):
        content = (
            _REPO_ROOT / "hypogeum" / "agents" / "thumbnail_prompt_writer.py"
        ).read_text(encoding="utf-8")
        assert _GPT_IMAGE2_PINNED_SHA in content
        assert "compose_system_prompt" in content

    def test_skill_registry_does_not_register_gpt_image2(self):
        """
        hypogeum/skill_registry.py is the registry for internal skills under
        hypogeum/skills/ only — a pinned third-party skill under
        third_party/skills/ is a deliberately different, simpler pattern
        (a direct path constant in the owning agent), not a registry entry.
        """
        registry_content = (_REPO_ROOT / "hypogeum" / "skill_registry.py").read_text(
            encoding="utf-8"
        )
        assert "gpt-image2" not in registry_content.lower()
        assert "gpt_image2" not in registry_content.lower()

    def test_manifest_state_is_installed_wired_active(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entry = next(s for s in data["skills"] if s["name"] == "gpt-image2")
        assert entry["state"] == "INSTALLED / WIRED / ACTIVE"

    def test_no_new_stage_adapter_checkpoint_or_provider_module_exists(self):
        assert not (_REPO_ROOT / "hypogeum" / "skill_adapters").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "gpt_image2_stage.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "gpt_image2_adapter.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "gpt_image2_checkpoint.py").exists()

    def test_no_gpt_image2_schema_exists(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert not any("gpt_image" in name.lower() for name in SCHEMA_NAMES)


class TestGPTImage2Rollback:
    def test_rollback_scope_is_isolated_to_pinned_directory(self):
        """
        Rollback requires reverting exactly two things:
        thumbnail_prompt_writer.py's composition call (drop
        _GPT_IMAGE2_SKILL_PATH) and thumbnail_director.txt's SECTION 12B
        bridge; deleting the pinned directory + manifest entry is optional
        beyond that. No adapter, checkpoint, or stage directory exists for
        this to depend on.
        """
        assert not (_REPO_ROOT / "hypogeum" / "skill_adapters").exists()


class TestSeedance2Installation:
    def test_pinned_directory_exists(self):
        assert _SEEDANCE2_DIR.is_dir(), (
            f"Expected pinned Seedance2 directory at {_SEEDANCE2_DIR}"
        )

    def test_directory_name_matches_pinned_commit(self):
        assert _SEEDANCE2_DIR.name == _SEEDANCE2_PINNED_SHA
        assert len(_SEEDANCE2_PINNED_SHA) == 40, "must be a full commit SHA, not a short SHA"

    def test_required_files_exist(self):
        for filename in _SEEDANCE2_EXPECTED_CHECKSUMS:
            assert (_SEEDANCE2_DIR / filename).is_file(), (
                f"Missing required upstream file: {filename}"
            )

    def test_no_additional_files_present(self):
        """
        Only SKILL.md and LICENSE are installed — no zh/SKILL.md (Chinese
        variant, no English-only pipeline consumer), no README files, no
        repository scaffolding. SKILL.md references no other file in the
        repository (no "references/" directory, no relative-path links to
        any other document), so excluding everything else does not leave
        it non-functional as an installed, readable document.
        """
        actual_files = {p.name for p in _SEEDANCE2_DIR.iterdir() if p.is_file()}
        assert actual_files == set(_SEEDANCE2_EXPECTED_CHECKSUMS), (
            f"Unexpected files in pinned Seedance2 directory: "
            f"{actual_files - set(_SEEDANCE2_EXPECTED_CHECKSUMS)}"
        )
        actual_dirs = {p.name for p in _SEEDANCE2_DIR.iterdir() if p.is_dir()}
        assert not actual_dirs, (
            f"No subdirectories (e.g. zh/) may be installed: {actual_dirs}"
        )

    def test_checksums_match_recorded_manifest(self):
        for filename, expected_sha256 in _SEEDANCE2_EXPECTED_CHECKSUMS.items():
            actual = _sha256(_SEEDANCE2_DIR / filename)
            assert actual == expected_sha256, (
                f"{filename} checksum mismatch — file may have been locally "
                f"modified. Expected {expected_sha256}, got {actual}"
            )

    def test_no_executable_content_in_installed_tree(self):
        for path in _SEEDANCE2_DIR.rglob("*"):
            if path.is_file():
                assert path.suffix.lower() not in _EXECUTABLE_EXTENSIONS, (
                    f"Executable file found in Seedance2's pinned tree: {path}"
                )

    def test_manifest_json_records_exactly_one_entry(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entries = [s for s in data["skills"] if s["name"] == "seedance2"]
        assert len(entries) == 1, "expected exactly one seedance2 entry"
        entry = entries[0]
        assert entry["pinned_commit_sha"] == _SEEDANCE2_PINNED_SHA
        assert entry["owner"] == "dexhunter"
        assert entry["repository"] == "seedance2-skill"
        assert entry["canonical_url"] == "https://github.com/dexhunter/seedance2-skill"
        recorded_checksums = {
            Path(f["path"]).name: f["sha256"] for f in entry["file_inventory"]
        }
        assert recorded_checksums == _SEEDANCE2_EXPECTED_CHECKSUMS

    def test_manifest_records_visual_package_director_stage_b_as_owner(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entry = next(s for s in data["skills"] if s["name"] == "seedance2")
        assert "visual_package_director.py" in entry["intended_owning_agent"]
        assert "Stage B" in entry["intended_owning_agent"]


class TestSeedance2NotWired:
    """
    Seedance2 is installed only — not attached to any agent. These tests
    confirm zero runtime wiring exists yet, and that the existing internal
    Seedance skill and its owning agent are completely untouched, mirroring
    the same guard GPT-Image2's own installation task once had before its
    later, separate attachment task.
    """

    def test_no_agent_references_pinned_upstream_skill(self):
        agents_dir = _REPO_ROOT / "hypogeum" / "agents"
        referencing = []
        for py_file in agents_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if "seedance2" in content or _SEEDANCE2_PINNED_SHA in content:
                referencing.append(py_file.name)
        assert not referencing, (
            f"No agent may reference the pinned Seedance2 skill yet: {referencing}"
        )

    def test_no_runtime_module_imports_seedance2_path(self):
        hypogeum_dir = _REPO_ROOT / "hypogeum"
        offenders = []
        for py_file in hypogeum_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if "seedance2" in content or _SEEDANCE2_PINNED_SHA in content:
                offenders.append(str(py_file.relative_to(_REPO_ROOT)))
        assert not offenders, (
            f"No runtime module may reference the pinned Seedance2 path yet: {offenders}"
        )

    def test_visual_package_director_unchanged_by_this_task(self):
        """
        The intended future owning agent must not yet reference the pinned
        skill in any way — this installation task is provenance-only.
        """
        content = (
            _REPO_ROOT / "hypogeum" / "agents" / "visual_package_director.py"
        ).read_text(encoding="utf-8")
        assert "seedance2" not in content
        assert _SEEDANCE2_PINNED_SHA not in content

    def test_existing_internal_seedance_translation_skill_unchanged(self):
        """
        This installation task must not reconcile, merge, rewrite,
        summarize, extract, or copy Seedance2 content into the existing
        internal Seedance translation skill or its compact runtime variant.

        A literal-string check for the pinned SHA or "dexhunter/seedance2-skill"
        is not a valid signal here: the existing skill file's own evidence/
        citation table already references https://github.com/dexhunter/seedance2-skill
        as a pre-existing research source (evidence tag seed-comm-XX,
        predating this installation task entirely) — that citation is
        legitimate content this task must not disturb, not evidence of
        reconciliation. The authoritative "unchanged" proof for this task
        is `git diff` on both files (see the task's own required
        verification command), which must produce no output; this test
        only confirms the files still exist and are non-empty, since a
        content diff without a pre-task snapshot cannot be asserted here.
        """
        for skill_file in (_SEEDANCE_TRANSLATION_SKILL_FILE, _SEEDANCE_TRANSLATION_SKILL_COMPACT_FILE):
            assert skill_file.exists(), f"Expected existing internal skill file at {skill_file}"
            assert skill_file.read_text(encoding="utf-8").strip(), (
                f"Expected non-empty existing internal skill file at {skill_file}"
            )

    def test_skill_registry_does_not_register_seedance2(self):
        """
        hypogeum/skill_registry.py is the registry for internal skills under
        hypogeum/skills/ only — a pinned third-party skill under
        third_party/skills/ is a deliberately different, simpler pattern
        (a direct path constant in the owning agent), not a registry entry.
        The registry's own existing seedance_translation_skill entry
        (an internal skill) is unaffected by this assertion.
        """
        registry_content = (_REPO_ROOT / "hypogeum" / "skill_registry.py").read_text(
            encoding="utf-8"
        )
        assert "seedance2" not in registry_content.lower()
        assert _SEEDANCE2_PINNED_SHA not in registry_content

    def test_manifest_state_is_installed_not_wired_not_active(self):
        data = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
        entry = next(s for s in data["skills"] if s["name"] == "seedance2")
        assert entry["state"] == "INSTALLED / NOT WIRED / NOT ACTIVE"

    def test_no_new_stage_adapter_checkpoint_or_provider_module_exists(self):
        assert not (_REPO_ROOT / "hypogeum" / "skill_adapters").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "seedance2_stage.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "seedance2_adapter.py").exists()
        assert not (_REPO_ROOT / "hypogeum" / "seedance2_checkpoint.py").exists()

    def test_no_seedance2_schema_exists(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert not any("seedance2" in name.lower() for name in SCHEMA_NAMES)


class TestSeedance2Rollback:
    def test_rollback_scope_is_isolated_to_pinned_directory(self):
        """
        Deleting third_party/skills/seedance2/<sha>/ and removing its
        config/upstream_skills_manifest.json entry must be sufficient to
        fully roll back this installation — no other file depends on the
        pinned directory's contents while the skill is unwired.
        """
        assert not (_REPO_ROOT / "hypogeum" / "skill_adapters").exists()
        assert not (_REPO_ROOT / "hypogeum" / "agents" / "seedance2_stage.py").exists()
