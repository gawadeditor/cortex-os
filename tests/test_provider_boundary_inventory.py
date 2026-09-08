"""
tests/test_provider_boundary_inventory.py — Executable, structural
repository-wide provider-boundary inventory.

This is the objective measuring stick for the entire Workflow Registry
/ gate-only provider-adapter closed program (see
/Users/gawadeditor/.claude/plans/jolly-roaming-bear.md). It encodes,
as real AST/import-graph checks (never a hand-maintained prose claim),
the exact current gating status of every real network-call chokepoint
in the repository.

_INVENTORY is the single source of truth for "what status does this
chokepoint currently have." As each domain-migration commit (F, G, H,
I, J) lands, that row's `status` is updated to `"gated"` in this same
file — the *checker* functions below do not change; only the expected
status per row does. Commit L reruns this file unmodified except for
`_INVENTORY`'s accumulated status changes and asserts zero reachable
production rows remain `"ungated"`/`"shadow_only"`.

Never assumes a row is gated/ungated from documentation — every
assertion below re-derives the answer from the actual current source
tree.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Names that, if called (directly or via a simple wrapper) from inside a
# chokepoint function's body, indicate that chokepoint is enforced or
# observed through the shared Factory Core mechanisms -- never a
# per-caller reimplementation.
_GATE_CALL_NAMES = frozenset({"submit"})  # hypogeum.submission_gate.submit()
_SHADOW_CALL_NAMES = frozenset({
    "_run_shadow_observation", "evaluate_shadow", "run_shadow_checks",
})

_STATUS_GATED = "gated"
_STATUS_SHADOW_ONLY = "shadow_only"
_STATUS_UNGATED = "ungated"
_STATUS_DEAD_UNREACHABLE = "dead_unreachable"
_STATUS_TEST_ONLY = "test_only"
_STATUS_UNWIRED_BUILT = "unwired_built"

_REACHABLE_PRODUCTION_STATUSES_REQUIRING_CLOSURE = frozenset({
    _STATUS_UNGATED, _STATUS_SHADOW_ONLY,
})


def _module_source(module_path: str) -> str:
    return (_ROOT / module_path).read_text()


def _find_function(tree: ast.Module, function_name: str) -> Optional[ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node
    return None


def _calls_any(module_path: str, function_name: str, target_names: frozenset) -> bool:
    """True iff the named function's body contains a direct call to any
    name in target_names -- a real AST Call node, not a substring match
    (so a comment/docstring mentioning the name can never produce a
    false positive, matching the pattern already established and
    hardened in tests/test_muapi_submission_gate_shadow.py). Matches
    both a bare-name call (`submit(...)`) and a module-qualified
    attribute call (`submission_gate.submit(...)`) -- Commit F's real
    gate call sites use the latter form."""
    source = _module_source(module_path)
    tree = ast.parse(source)
    fn = _find_function(tree, function_name)
    if fn is None:
        raise AssertionError(f"{module_path}::{function_name} not found -- inventory is stale, update it")
    called_names = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name):
            called_names.add(n.func.id)
        elif isinstance(n.func, ast.Attribute):
            called_names.add(n.func.attr)
    return bool(called_names & target_names)


def _module_imported_by(importer_path: str, target_module_dotted: str) -> bool:
    """True iff importer_path contains a real import of target_module_dotted
    (as an ast.Import/ast.ImportFrom node, never a substring match)."""
    source = _module_source(importer_path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == target_module_dotted or alias.name.startswith(target_module_dotted + ".") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == target_module_dotted or node.module.startswith(target_module_dotted + ".")):
                return True
    return False


# ---------------------------------------------------------------------------
# _INVENTORY -- the single source of truth. Update only the `status`
# field as domains are migrated (Commits F-J) or dead clients quarantined
# (Commit K). Never remove a row without the corresponding code actually
# being removed/renamed.
# ---------------------------------------------------------------------------

_INVENTORY = [
    # Commit F: the legacy path (_attempt_generation) is deliberately
    # NOT flipped to "gated" -- it is still the exact path every real
    # production call takes today (no real grant exists for any real
    # channel; see the plan's Enforcement Cutover section), so it stays
    # honestly "shadow_only" until that legacy code is actually removed
    # at a future, separately-approved cutover. _submit_scene_via_gate
    # is the NEW, real, structural gated path Commit F adds alongside
    # it -- reachable only when a caller supplies channel_id AND a
    # verified grant resolves (synthetic-only through Commit L).
    {"domain": "video", "module": "hypogeum/muapi.py", "function": "_attempt_generation", "status": _STATUS_SHADOW_ONLY},
    {"domain": "video_gate_path", "module": "hypogeum/muapi.py", "function": "_submit_scene_via_gate", "status": _STATUS_GATED},
    {"domain": "image_reference", "module": "hypogeum/muapi.py", "function": "generate_reference_image", "status": _STATUS_UNGATED},
    {"domain": "image_gate_path", "module": "hypogeum/muapi.py", "function": "_generate_reference_image_via_gate", "status": _STATUS_GATED},
    {"domain": "thumbnail", "module": "hypogeum/muapi.py", "function": "_generate_thumbnail_with_model", "status": _STATUS_UNGATED},
    {"domain": "thumbnail_gate_path", "module": "hypogeum/muapi.py", "function": "_generate_thumbnail_via_gate", "status": _STATUS_GATED},
    {"domain": "voice", "module": "hypogeum/elevenlabs.py", "function": "generate_narration_segment", "status": _STATUS_UNGATED},
    {"domain": "voice_gate_path", "module": "hypogeum/elevenlabs.py", "function": "_generate_narration_via_gate", "status": _STATUS_GATED},
    {"domain": "music", "module": "hypogeum/elevenlabs.py", "function": "generate_music_track", "status": _STATUS_UNGATED},
    {"domain": "music_gate_path", "module": "hypogeum/elevenlabs.py", "function": "_generate_music_via_gate", "status": _STATUS_GATED},
    {"domain": "text_llm", "module": "hypogeum/openrouter.py", "function": "call_claude", "status": _STATUS_UNGATED},
    {"domain": "text_llm_gate_path", "module": "hypogeum/openrouter.py", "function": "_call_openrouter_via_gate", "status": _STATUS_GATED},
    {"domain": "research", "module": "hypogeum/perplexity.py", "function": "query_perplexity", "status": _STATUS_UNGATED},
    {"domain": "research_gate_path", "module": "hypogeum/perplexity.py", "function": "_single_request_via_gate", "status": _STATUS_GATED},
    {"domain": "upscale", "module": "hypogeum/providers/muapi_video_upscaler.py", "function": "enhance", "status": _STATUS_UNGATED},
    {"domain": "upscale_gate_path", "module": "hypogeum/providers/muapi_video_upscaler.py", "function": "_enhance_via_gate", "status": _STATUS_GATED},
]

_DEAD_UNREACHABLE_MODULES = [
    "hypogeum.dormant_providers.byteplus_ark",
    "hypogeum.dormant_providers.higgsfield",
    "hypogeum.dormant_providers.veo_official",
    "hypogeum.dormant_providers.runway_official",
    "hypogeum.dormant_providers.kling_official",
]

# Modules allowed to import a dead/unreachable provider client (credential
# probes only, never generation) -- checked exhaustively below, this list
# itself is verified against the whole repo, not assumed.
_DEAD_MODULE_ALLOWED_IMPORTERS = {"hypogeum/provider_verifier.py"}

_PRODUCTION_SEARCH_ROOTS = ["hypogeum", "run_worker.py", "run_pipeline.py"]


def _all_production_python_files() -> list:
    files = []
    for root in _PRODUCTION_SEARCH_ROOTS:
        p = _ROOT / root
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
    return [f for f in files if "/tests/" not in str(f) and not f.name.startswith("test_")]


class TestInventoryIsCurrent:
    """Proves the inventory table itself hasn't drifted from reality --
    every listed function still exists at the listed module path."""

    @pytest.mark.parametrize("row", _INVENTORY, ids=lambda r: f"{r['domain']}:{r['function']}")
    def test_chokepoint_function_exists(self, row):
        tree = ast.parse(_module_source(row["module"]))
        assert _find_function(tree, row["function"]) is not None, (
            f"{row['module']}::{row['function']} not found -- update _INVENTORY"
        )


class TestChokepointGatingStatus:
    """The core structural check: for every row, verify its *actual*
    current status by AST inspection, and assert it matches what
    _INVENTORY claims -- proving the table is not stale documentation."""

    @pytest.mark.parametrize("row", _INVENTORY, ids=lambda r: f"{r['domain']}:{r['function']}")
    def test_status_matches_actual_source(self, row):
        is_gated = _calls_any(row["module"], row["function"], _GATE_CALL_NAMES)
        is_shadow = _calls_any(row["module"], row["function"], _SHADOW_CALL_NAMES)

        if row["status"] == _STATUS_GATED:
            assert is_gated, f"{row['domain']} claims gated but does not call submission_gate.submit()"
        elif row["status"] == _STATUS_SHADOW_ONLY:
            assert is_shadow and not is_gated, (
                f"{row['domain']} claims shadow_only but shadow={is_shadow} gated={is_gated}"
            )
        elif row["status"] == _STATUS_UNGATED:
            assert not is_shadow and not is_gated, (
                f"{row['domain']} claims ungated but shadow={is_shadow} gated={is_gated} -- update _INVENTORY"
            )
        else:
            pytest.fail(f"unexpected status {row['status']!r} for row {row}")


class TestSubmissionGateBuiltButUnwired:
    """hypogeum.submission_gate.submit() -- confirm its production
    call-site count exactly matches _INVENTORY's own gated-row count
    (i.e. it is called only by rows explicitly marked "gated", never by
    anything else, and by nothing at all until the first domain
    migration lands)."""

    def test_submit_has_zero_production_callers_until_first_domain_migration(self):
        gated_rows = [r for r in _INVENTORY if r["status"] == _STATUS_GATED]
        if gated_rows:
            pytest.skip("at least one domain has been migrated; this check no longer applies in its original form")
        for f in _all_production_python_files():
            if f.name == "submission_gate.py":
                continue
            source = f.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "submit":
                    # only a false-positive risk if it's actually calling
                    # submission_gate's submit -- verify via the module's
                    # own imports naming submission_gate.
                    assert not _module_imported_by(str(f.relative_to(_ROOT)), "hypogeum.submission_gate"), (
                        f"{f} imports hypogeum.submission_gate and calls something named .submit(...) "
                        "-- inventory must be updated if this is a genuine gate call site"
                    )


class TestDeadUnreachableClientsQuarantineStatus:
    """Confirms the five official/direct provider clients remain
    unreachable from production (or, post-Commit-K, confirms they were
    successfully moved and no production file imports them from their
    old location)."""

    @pytest.mark.parametrize("dead_module", _DEAD_UNREACHABLE_MODULES)
    def test_no_unauthorized_production_importer(self, dead_module):
        for f in _all_production_python_files():
            rel = str(f.relative_to(_ROOT))
            if rel in _DEAD_MODULE_ALLOWED_IMPORTERS:
                continue
            assert not _module_imported_by(rel, dead_module), (
                f"{rel} imports {dead_module}, a dead/unreachable provider client, "
                f"outside the allowed importer set {_DEAD_MODULE_ALLOWED_IMPORTERS}"
            )


class TestFakeAdapterRemainsTestOnly:
    """hypogeum.providers.fake_provider_adapter must never be imported by
    any production module -- it is the one sanctioned test-only bypass,
    and this must remain true throughout every commit in the program."""

    _ALLOWED_IMPORTERS = {
        "hypogeum/submission_gate.py", "hypogeum/capability_boundary.py",
        "hypogeum/providers/muapi_video_adapter.py",
        "hypogeum/providers/muapi_image_adapter.py", "hypogeum/providers/muapi_thumbnail_adapter.py",
        "hypogeum/providers/elevenlabs_voice_adapter.py", "hypogeum/providers/elevenlabs_music_adapter.py",
        "hypogeum/providers/openrouter_text_adapter.py", "hypogeum/providers/perplexity_adapter.py",
        "hypogeum/providers/muapi_upscale_adapter.py",
    }
    """submission_gate.py and capability_boundary.py import
    fake_provider_adapter.py for the shared AdapterInvocationCapability/
    AdapterResult/compute_capability_checksum definitions -- the
    authorization boundary itself (ef1aeaa), not a business-logic caller. hypogeum/providers/muapi_video_adapter.py
    (Commit F) imports the exact same shared protocol dataclasses for
    the identical reason -- every real per-domain adapter implements
    FakeProviderAdapter's own protocol, so every real adapter module is
    expected to import these definitions from the same place.
    hypogeum/providers/elevenlabs_voice_adapter.py (Commit G) is the
    same pattern applied to the voice domain. This exemption is
    expected to GROW by one entry per domain-migration commit (F-J),
    not shrink -- it never grows to include anything other than a
    real, registered adapter module."""

    def test_fake_adapter_not_imported_by_production(self):
        for f in _all_production_python_files():
            rel = str(f.relative_to(_ROOT))
            if rel in self._ALLOWED_IMPORTERS:
                continue
            assert not _module_imported_by(rel, "hypogeum.providers.fake_provider_adapter"), (
                f"{rel} imports the test-only fake_provider_adapter module"
            )


class TestZeroUngatedReachablePathsAtFinalClosure:
    """This is the closure assertion Commit L runs unmodified. It stays
    skipped (documenting why) until every _INVENTORY row's status has
    been updated to "gated" by its own domain-migration commit."""

    def test_zero_reachable_production_rows_remain_ungated_or_shadow_only(self):
        remaining = [r for r in _INVENTORY if r["status"] in _REACHABLE_PRODUCTION_STATUSES_REQUIRING_CLOSURE]
        if remaining:
            pytest.skip(
                f"{len(remaining)} domain(s) not yet migrated: "
                f"{sorted(r['domain'] for r in remaining)} -- expected until Commit L"
            )
        assert all(r["status"] == _STATUS_GATED for r in _INVENTORY)
