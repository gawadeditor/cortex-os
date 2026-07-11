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

Scope: installation provenance and wiring-surface-area only. Does not test
Humanizer's prose-rewriting behavior (Claude applies the skill at generation
time — there is no separate deterministic code path to unit test here).
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
        Checks specifically for the third_party/skills/humanizer path, not
        the generic word "humanizer" — hypogeum/agents/script_engine.py
        already imports the pre-existing, unrelated hypogeum/humanizer.py
        module (a deterministic regex utility explicitly documented as "a
        subset of humanizer v2.7.0", predating and independent of this
        upstream skill entirely). That existing reference is legitimate,
        separate production code and out of scope here.
        """
        agents_dir = _REPO_ROOT / "hypogeum" / "agents"
        referencing = []
        for py_file in agents_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if "third_party" in content:
                referencing.append(py_file.name)
        assert referencing == ["script_engine.py"], (
            f"Expected only script_engine.py to reference the pinned upstream "
            f"skill, got: {referencing}"
        )

    def test_no_other_runtime_module_imports_third_party(self):
        hypogeum_dir = _REPO_ROOT / "hypogeum"
        offenders = []
        for py_file in hypogeum_dir.rglob("*.py"):
            if py_file.name == "script_engine.py":
                continue
            if "third_party" in py_file.read_text(encoding="utf-8"):
                offenders.append(str(py_file.relative_to(_REPO_ROOT)))
        assert not offenders, (
            f"No runtime module besides script_engine.py may reference "
            f"third_party/: {offenders}"
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
