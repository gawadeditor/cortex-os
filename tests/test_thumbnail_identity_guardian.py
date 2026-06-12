"""
tests/test_thumbnail_identity_guardian.py — Phase 4B: Thumbnail Identity Guardian v1

Coverage:
    TestVerdictComputation     — APPROVED/WARNING/BLOCKED from violations
    TestReportFields           — required fields in returned report dict
    TestReportFileWritten      — JSON file written to output_dir
    TestMissingImage           — BLOCKED when image does not exist
    TestClientInjection        — tig_client callable is used instead of Claude
    TestViolationNormalization — malformed violations are dropped
    TestBlockingViolations     — each BLOCKING rule_id causes BLOCKED verdict
    TestWarningViolations      — WARNING-only violations produce WARNING verdict
    TestIdempotency            — second call overwrites report file cleanly
    TestAPIError               — API error returns "error" verdict, writes file
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hypogeum.agents.thumbnail_identity_guardian import run_thumbnail_identity_guardian
from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_image(tmp_path: Path, name: str = "main.jpg") -> Path:
    """Write a minimal valid JPEG stub (enough for path existence checks)."""
    p = tmp_path / name
    # Minimal JPEG header bytes — enough to satisfy path.exists()
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
    return p


def _make_client(violations: list[dict]):
    """Return a tig_client callable that returns the given violations as JSON."""
    def _client(image_path, episode_id):
        return json.dumps({"violations": violations})
    return _client


def _no_violations_client(image_path, episode_id):
    return json.dumps({"violations": []})


# ---------------------------------------------------------------------------
# TestVerdictComputation
# ---------------------------------------------------------------------------

class TestVerdictComputation:
    def test_approved_when_no_violations(self, tmp_path):
        img = _make_fake_image(tmp_path)
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert result["verdict"] == "APPROVED"

    def test_warning_when_only_warnings(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-W01", "severity": "WARNING", "description": "Slightly elevated brightness."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "WARNING"

    def test_blocked_when_any_blocking(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-01", "severity": "BLOCKING", "description": "Human face visible."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"

    def test_blocked_overrides_warnings(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-W01", "severity": "WARNING", "description": "Slightly warm."},
            {"rule_id": "TIG-02", "severity": "BLOCKING", "description": "Bright background."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"


# ---------------------------------------------------------------------------
# TestReportFields
# ---------------------------------------------------------------------------

class TestReportFields:
    REQUIRED_FIELDS = [
        "episode_id", "image_path", "verdict", "violations",
        "blocking_count", "warning_count", "checked_at",
        "tig_version", "metadata_version",
    ]

    def test_all_required_fields_present(self, tmp_path):
        img = _make_fake_image(tmp_path)
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        for field in self.REQUIRED_FIELDS:
            assert field in result, f"Missing field: {field}"

    def test_metadata_version_is_v1(self, tmp_path):
        img = _make_fake_image(tmp_path)
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert result["metadata_version"] == METADATA_VERSION

    def test_episode_id_in_report(self, tmp_path):
        img = _make_fake_image(tmp_path)
        result = run_thumbnail_identity_guardian(
            "ep_999", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert result["episode_id"] == "ep_999"

    def test_blocking_count_correct(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-01", "severity": "BLOCKING", "description": "Face present."},
            {"rule_id": "TIG-02", "severity": "BLOCKING", "description": "Bright background."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["blocking_count"] == 2

    def test_warning_count_correct(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-W01", "severity": "WARNING", "description": "Warm tones."},
            {"rule_id": "TIG-W02", "severity": "WARNING", "description": "Low contrast subtitle."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["warning_count"] == 2

    def test_image_path_in_report(self, tmp_path):
        img = _make_fake_image(tmp_path)
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert str(img) in result["image_path"]


# ---------------------------------------------------------------------------
# TestReportFileWritten
# ---------------------------------------------------------------------------

class TestReportFileWritten:
    def test_report_file_created(self, tmp_path):
        img = _make_fake_image(tmp_path)
        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert (tmp_path / "thumbnail_identity_ep_000.json").exists()

    def test_report_file_parseable(self, tmp_path):
        img = _make_fake_image(tmp_path)
        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        content = json.loads((tmp_path / "thumbnail_identity_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"
        assert content["verdict"] == "APPROVED"

    def test_report_file_named_by_episode_id(self, tmp_path):
        img = _make_fake_image(tmp_path)
        run_thumbnail_identity_guardian(
            "ep_042", img, tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert (tmp_path / "thumbnail_identity_ep_042.json").exists()


# ---------------------------------------------------------------------------
# TestMissingImage
# ---------------------------------------------------------------------------

class TestMissingImage:
    def test_blocked_when_image_absent(self, tmp_path):
        result = run_thumbnail_identity_guardian(
            "ep_000", tmp_path / "nonexistent.jpg",
            tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"

    def test_blocking_count_one_when_image_absent(self, tmp_path):
        result = run_thumbnail_identity_guardian(
            "ep_000", tmp_path / "nonexistent.jpg",
            tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert result["blocking_count"] == 1

    def test_tig05_violation_when_image_absent(self, tmp_path):
        result = run_thumbnail_identity_guardian(
            "ep_000", tmp_path / "nonexistent.jpg",
            tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        rule_ids = [v["rule_id"] for v in result["violations"]]
        assert "TIG-05" in rule_ids

    def test_report_written_when_image_absent(self, tmp_path):
        run_thumbnail_identity_guardian(
            "ep_000", tmp_path / "nonexistent.jpg",
            tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        assert (tmp_path / "thumbnail_identity_ep_000.json").exists()


# ---------------------------------------------------------------------------
# TestClientInjection
# ---------------------------------------------------------------------------

class TestClientInjection:
    def test_tig_client_called_with_image_path(self, tmp_path):
        img = _make_fake_image(tmp_path)
        received = {}

        def _capturing_client(image_path, episode_id):
            received["image_path"] = image_path
            received["episode_id"] = episode_id
            return json.dumps({"violations": []})

        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_capturing_client, output_dir=tmp_path,
        )
        assert received["image_path"] == img
        assert received["episode_id"] == "ep_000"

    def test_tig_client_response_used(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-03", "severity": "BLOCKING", "description": "Neon colors present."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"
        assert result["violations"][0]["rule_id"] == "TIG-03"


# ---------------------------------------------------------------------------
# TestViolationNormalization
# ---------------------------------------------------------------------------

class TestViolationNormalization:
    def test_missing_rule_id_dropped(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"severity": "BLOCKING", "description": "No rule_id here."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["violations"] == []
        assert result["verdict"] == "APPROVED"

    def test_invalid_severity_dropped(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-01", "severity": "UNKNOWN", "description": "Bad severity."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["violations"] == []

    def test_missing_description_dropped(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-01", "severity": "BLOCKING"},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["violations"] == []

    def test_non_dict_entry_dropped(self, tmp_path):
        img = _make_fake_image(tmp_path)

        def _bad_client(image_path, episode_id):
            return json.dumps({"violations": ["not a dict", 42, None]})

        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_bad_client, output_dir=tmp_path,
        )
        assert result["violations"] == []


# ---------------------------------------------------------------------------
# TestBlockingViolations
# ---------------------------------------------------------------------------

class TestBlockingViolations:
    @pytest.mark.parametrize("rule_id", [
        "TIG-01", "TIG-02", "TIG-03", "TIG-04", "TIG-05", "TIG-06", "TIG-07",
    ])
    def test_each_blocking_rule_causes_blocked(self, rule_id, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": rule_id, "severity": "BLOCKING", "description": f"Violation {rule_id}."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"


# ---------------------------------------------------------------------------
# TestWarningViolations
# ---------------------------------------------------------------------------

class TestWarningViolations:
    @pytest.mark.parametrize("rule_id", ["TIG-W01", "TIG-W02", "TIG-W03", "TIG-W04"])
    def test_warning_only_produces_warning_verdict(self, rule_id, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": rule_id, "severity": "WARNING", "description": f"Advisory {rule_id}."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "WARNING"


# ---------------------------------------------------------------------------
# TestIdempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_call_overwrites_report(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client1 = _make_client([
            {"rule_id": "TIG-01", "severity": "BLOCKING", "description": "Face."},
        ])
        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client1, output_dir=tmp_path,
        )
        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client, output_dir=tmp_path,
        )
        content = json.loads((tmp_path / "thumbnail_identity_ep_000.json").read_text())
        assert content["verdict"] == "APPROVED"

    def test_second_call_returns_latest_verdict(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-W01", "severity": "WARNING", "description": "Warm."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "WARNING"
        result2 = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client, output_dir=tmp_path,
        )
        assert result2["verdict"] == "APPROVED"


# ---------------------------------------------------------------------------
# TestAPIError
# ---------------------------------------------------------------------------

class TestAPIError:
    def test_api_error_returns_error_verdict(self, tmp_path):
        img = _make_fake_image(tmp_path)

        def _failing_client(image_path, episode_id):
            raise RuntimeError("API quota exceeded")

        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_failing_client, output_dir=tmp_path,
        )
        assert result["verdict"] == "error"

    def test_api_error_has_error_field(self, tmp_path):
        img = _make_fake_image(tmp_path)

        def _failing_client(image_path, episode_id):
            raise RuntimeError("API quota exceeded")

        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_failing_client, output_dir=tmp_path,
        )
        assert "error" in result

    def test_api_error_writes_report(self, tmp_path):
        img = _make_fake_image(tmp_path)

        def _failing_client(image_path, episode_id):
            raise RuntimeError("connection timeout")

        run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_failing_client, output_dir=tmp_path,
        )
        assert (tmp_path / "thumbnail_identity_ep_000.json").exists()
