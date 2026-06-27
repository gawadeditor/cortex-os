"""
tests/test_production_asset_protection.py

Regression tests for production asset overwrite protection.

Ensures:
  - protect_production_path() registers a path as protected
  - _write_package() raises ProductionAssetProtectionError for protected paths
  - _write_package() writes normally to unprotected paths
  - The protection set is isolated per test (no cross-test contamination)
  - Validation output may go to experiments/ or a *_validation.json path
  - No provider calls are made by any of these tests
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import (
    ProductionAssetProtectionError,
    _PROTECTED_PRODUCTION_PATHS,
    _write_package,
    protect_production_path,
)
from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_protection_set():
    """Isolate each test — clear the global protection set before and after."""
    _PROTECTED_PRODUCTION_PATHS.clear()
    yield
    _PROTECTED_PRODUCTION_PATHS.clear()


def _fake_log():
    return get_logger(episode_id="test_ep", agent="test_agent")


# ---------------------------------------------------------------------------
# Core protection behaviour
# ---------------------------------------------------------------------------

class TestProtectProductionPath:

    def test_registers_resolved_path(self, tmp_path):
        production_file = tmp_path / "visual_package_ep_000.json"
        protect_production_path(production_file)
        assert str(production_file.resolve()) in _PROTECTED_PRODUCTION_PATHS

    def test_multiple_paths_registered(self, tmp_path):
        f1 = tmp_path / "visual_package_ep_000.json"
        f2 = tmp_path / "script_package_ep_000.json"
        protect_production_path(f1)
        protect_production_path(f2)
        assert str(f1.resolve()) in _PROTECTED_PRODUCTION_PATHS
        assert str(f2.resolve()) in _PROTECTED_PRODUCTION_PATHS

    def test_protection_set_empty_by_default(self):
        assert _PROTECTED_PRODUCTION_PATHS == set()


class TestWritePackageProtection:

    def test_raises_for_protected_path(self, tmp_path):
        """_write_package must raise ProductionAssetProtectionError on protected paths."""
        production_file = tmp_path / "visual_package_ep_000.json"
        protect_production_path(production_file)

        package = {"episode_id": "ep_000"}
        with pytest.raises(ProductionAssetProtectionError) as exc_info:
            _write_package(package, production_file, "visual_package", _fake_log())

        assert str(production_file.resolve()) in str(exc_info.value)
        assert not production_file.exists(), "Protected file must not have been written"

    def test_error_message_names_experiments_alternative(self, tmp_path):
        """Error message must direct caller to experiments/ directory."""
        production_file = tmp_path / "visual_package_ep_000.json"
        protect_production_path(production_file)

        with pytest.raises(ProductionAssetProtectionError) as exc_info:
            _write_package({}, production_file, "visual_package", _fake_log())

        assert "experiments" in str(exc_info.value)

    def test_unprotected_path_writes_normally(self, tmp_path):
        """_write_package must write normally to unprotected paths."""
        output_file = tmp_path / "visual_package_ep_001.json"
        package = {"episode_id": "ep_001", "scenes": []}

        _write_package(package, output_file, "visual_package", _fake_log())

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["episode_id"] == "ep_001"

    def test_protection_does_not_affect_different_path(self, tmp_path):
        """Protecting one path must not block writes to other paths."""
        production_file = tmp_path / "visual_package_ep_000.json"
        validation_file = tmp_path / "visual_package_ep_000_validation.json"

        protect_production_path(production_file)

        package = {"episode_id": "ep_000", "note": "validation run"}
        _write_package(package, validation_file, "visual_package", _fake_log())

        assert validation_file.exists()
        assert not production_file.exists()

    def test_protection_does_not_affect_experiments_dir(self, tmp_path):
        """Writes to an experiments/ subdirectory must succeed even when the
        production file is protected."""
        production_file = tmp_path / "episodes" / "ep_000" / "visual_package_ep_000.json"
        experiments_file = tmp_path / "experiments" / "skill_v1" / "visual_package_ep_000_skill_v1.json"

        protect_production_path(production_file)

        package = {"episode_id": "ep_000", "note": "experiment"}
        _write_package(package, experiments_file, "visual_package", _fake_log())

        assert experiments_file.exists()
        assert not production_file.exists()

    def test_protected_file_not_written_even_if_it_does_not_exist(self, tmp_path):
        """Protection must block writes regardless of whether the file currently exists."""
        production_file = tmp_path / "visual_package_ep_000.json"
        assert not production_file.exists()

        protect_production_path(production_file)

        with pytest.raises(ProductionAssetProtectionError):
            _write_package({"x": 1}, production_file, "visual_package", _fake_log())

        assert not production_file.exists()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestProtectionSetIsolation:

    def test_protection_set_cleared_between_tests(self):
        """The autouse fixture must ensure no leakage between tests."""
        assert _PROTECTED_PRODUCTION_PATHS == set()

    def test_protection_set_cleared_after_registration(self, tmp_path):
        f = tmp_path / "visual_package_ep_000.json"
        protect_production_path(f)
        assert str(f.resolve()) in _PROTECTED_PRODUCTION_PATHS
        # After this test, the autouse fixture clears the set

    def test_protection_set_still_empty_despite_prior_test(self):
        """Verifies the previous test's registration did not leak."""
        assert _PROTECTED_PRODUCTION_PATHS == set()


# ---------------------------------------------------------------------------
# No provider calls
# ---------------------------------------------------------------------------

class TestNoProviderCalls:

    def test_protect_production_path_is_pure_python(self):
        """protect_production_path() must not import or call any network library."""
        import inspect
        src = inspect.getsource(protect_production_path)
        assert "requests" not in src
        assert "urllib" not in src
        assert "httpx" not in src
        assert "call_claude" not in src

    def test_write_package_protection_check_is_pure_python(self):
        """The protection check in _write_package() must not invoke any provider."""
        import inspect
        src = inspect.getsource(_write_package)
        assert "call_claude" not in src
        assert "submit_scene" not in src
        assert "muapi" not in src.lower()
