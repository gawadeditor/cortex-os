"""
tests/test_authorization_authority_trust_boundary.py — Static,
AST-based proof of the three trust boundaries (plan §5.5): only
hypogeum/authorization_authority.py may mint or activate authorization
or a grant; hypogeum/submission_gate.py may only revalidate/settle an
already-issued reservation, never mint or broaden anything, and never
imports hypogeum.authorization_authority at all.

AST-based (never substring/grep) throughout, to avoid false positives
on docstring mentions -- the recurring pitfall documented elsewhere in
this test suite (see tests/test_workflow_executor.py's own history).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HYPOGEUM_DIR = _ROOT / "hypogeum"

_MINTING_FUNCTION_NAMES = frozenset({
    "register_run_grant", "issue_child_authorization", "issue_authorization_from_explicit_approval",
})


def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _calls_transition_to_authorized(tree: ast.Module) -> bool:
    """True if the module contains a call of the shape
    `<something>.transition(AuthorizationState.AUTHORIZED, ...)` or
    `<something>.transition(to_state=AuthorizationState.AUTHORIZED, ...)`
    -- checked by AST node shape, not by any string search."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "transition"):
            continue
        candidates = list(node.args) + [kw.value for kw in node.keywords]
        for arg in candidates:
            if isinstance(arg, ast.Attribute) and arg.attr == "AUTHORIZED":
                return True
    return False


def _calls_any_minting_function(tree: ast.Module) -> bool:
    """True if the module calls any of _MINTING_FUNCTION_NAMES, whether
    as a bare name (`issue_child_authorization(...)`) or via attribute
    access (`authority.issue_child_authorization(...)`,
    `authorization_authority.register_run_grant(...)`)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in _MINTING_FUNCTION_NAMES:
            return True
    return False


def _module_imports(tree: ast.Module) -> "list[str]":
    """Every dotted module path this file imports, from both `import
    x.y.z` and `from x.y.z import ...` statements."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


class TestOnlyAuthorizationAuthorityMintsOrActivates:

    def test_only_authorization_authority_calls_transition_to_authorized(self):
        offenders = []
        for path in _iter_python_files(_HYPOGEUM_DIR):
            if path.name == "authorization_authority.py":
                continue
            if path.name in ("authorization_lifecycle.py",):
                # authorization_lifecycle.py DEFINES transition() and
                # AuthorizationState -- it never calls its own
                # transition() with a literal AUTHORIZED target itself.
                pass
            tree = _parse(path)
            if _calls_transition_to_authorized(tree):
                offenders.append(str(path.relative_to(_ROOT)))
        assert offenders == [], f"only authorization_authority.py may transition a record to AUTHORIZED: {offenders}"

    def test_only_authorization_authority_defines_and_calls_minting_functions_in_production_code(self):
        offenders = []
        for path in _iter_python_files(_HYPOGEUM_DIR):
            if path.name in ("authorization_authority.py",):
                continue
            tree = _parse(path)
            if _calls_any_minting_function(tree):
                offenders.append(str(path.relative_to(_ROOT)))
        assert offenders == [], (
            f"no production module other than authorization_authority.py may call "
            f"register_run_grant()/issue_child_authorization()/issue_authorization_from_explicit_approval(): {offenders}"
        )

    def test_authorization_authority_is_the_only_repo_wide_caller_including_scripts_and_tools(self):
        offenders = []
        for extra_dir in (_ROOT / "scripts", _ROOT / "tools"):
            if not extra_dir.exists():
                continue
            for path in _iter_python_files(extra_dir):
                tree = _parse(path)
                if _calls_any_minting_function(tree):
                    offenders.append(str(path.relative_to(_ROOT)))
        # scripts/approve_run_grant.py and scripts/approve_authorization_proposal.py
        # are the offline callers of register_run_grant()/issue_authorization_from_
        # explicit_approval() -- expected and allowed. Everything else is not.
        allowed = {"scripts/approve_run_grant.py", "scripts/approve_authorization_proposal.py"}
        unexpected = [o for o in offenders if o not in allowed]
        assert unexpected == [], f"unexpected minting-function caller(s) outside the offline approval scripts: {unexpected}"


class TestSubmissionGateNeverMintsOrBroadensAuthorization:

    def _submission_gate_tree(self) -> ast.Module:
        return _parse(_HYPOGEUM_DIR / "submission_gate.py")

    def test_submission_gate_never_calls_transition_to_authorized(self):
        assert _calls_transition_to_authorized(self._submission_gate_tree()) is False

    def test_submission_gate_never_calls_any_minting_function(self):
        assert _calls_any_minting_function(self._submission_gate_tree()) is False

    def test_submission_gate_never_imports_authorization_authority(self):
        imports = _module_imports(self._submission_gate_tree())
        assert not any(name == "hypogeum.authorization_authority" or name.startswith("hypogeum.authorization_authority.") for name in imports)

    def test_submission_gate_imports_authorization_store_only_for_the_narrow_revalidate_function(self):
        tree = self._submission_gate_tree()
        imports = _module_imports(tree)
        assert any(name == "hypogeum.authorization_store" for name in imports), (
            "submission_gate.py is expected to import hypogeum.authorization_store "
            "(for the narrow boundary-revalidation/settlement functions)"
        )
        # It must call revalidate_grant_reservation()/transition_ledger_state()
        # (settlement) -- both read-and-settle-only -- but never any minting
        # function, and never register_run_grant/insert_grant/insert_ledger_reservation.
        forbidden_store_calls = {"insert_grant", "insert_ledger_reservation", "persist_authorization_record"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_store_calls:
                raise AssertionError(f"submission_gate.py must never call authorization_store.{node.func.attr}()")


class TestNoTestOnlyVerificationBypassSeam:

    def test_authorization_authority_public_api_has_no_public_key_override_parameter(self):
        """No function on authorization_authority's public API accepts a
        caller-suppliable trust-anchor override -- the only way to
        change what key is trusted is monkeypatching
        _load_trusted_public_key() itself (a test-only technique, not a
        production parameter)."""
        import inspect
        import hypogeum.authorization_authority as authority

        for name in authority.__all__:
            obj = getattr(authority, name)
            if not callable(obj) or isinstance(obj, type):
                continue
            sig = inspect.signature(obj)
            forbidden = {"public_key", "trusted_public_key", "verification_key"}
            assert not (forbidden & set(sig.parameters)), f"{name}() must not accept a trust-anchor override parameter"
