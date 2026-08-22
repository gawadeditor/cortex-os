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

    def test_tig09_violation_when_image_absent(self, tmp_path):
        # 2026-08-23: was TIG-05 (missing text overlay) -- moved to TIG-09
        # (placeholder/missing imagery), since TIG-05 is now conditionally
        # inapplicable for deliverable classes that never require a text
        # overlay (e.g. Shorts) and "file not found" must still block
        # regardless of deliverable class.
        result = run_thumbnail_identity_guardian(
            "ep_000", tmp_path / "nonexistent.jpg",
            tig_client=_no_violations_client,
            output_dir=tmp_path,
        )
        rule_ids = [v["rule_id"] for v in result["violations"]]
        assert "TIG-09" in rule_ids

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


# ---------------------------------------------------------------------------
# TestDeliverableClassAwareness — 2026-08-23: the guardian must evaluate a
# vertical Short cover against the Short's OWN contract (from Channel
# Package content, hypogeum.brand.thumbnail_identity_rules), never the
# main episode's horizontal wordmark/title-hierarchy contract.
# ---------------------------------------------------------------------------

class TestDeliverableClassAwareness:
    def test_short_cover_not_rejected_for_lacking_horizontal_hierarchy(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        # Simulates a vision model that (incorrectly) still flags the
        # main-episode-only rules -- the deterministic backstop
        # (_filter_inapplicable_violations) must strip these for a class
        # that never requires a wordmark or title hierarchy.
        client = _make_client([
            {"rule_id": "TIG-05", "severity": "BLOCKING", "description": "No text overlay present."},
            {"rule_id": "TIG-07", "severity": "BLOCKING", "description": "Wordmark absent."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "APPROVED"
        assert result["violations"] == []

    def test_valid_9x16_short_cover_passes(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=_no_violations_client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "APPROVED"

    def test_wrong_aspect_short_cover_fails(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        client = _make_client([
            {"rule_id": "TIG-06", "severity": "BLOCKING", "description": "Image is 1280x720, not 720x1280 (9:16)."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "BLOCKED"

    def test_placeholder_short_cover_fails(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        client = _make_client([
            {"rule_id": "TIG-09", "severity": "BLOCKING", "description": "Flat near-black placeholder fill."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "BLOCKED"

    def test_identity_breaking_short_cover_fails(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        client = _make_client([
            {"rule_id": "TIG-02", "severity": "BLOCKING", "description": "Bright high-key background, not the channel's dark palette."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "BLOCKED"

    def test_unrelated_imagery_short_cover_fails(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        client = _make_client([
            {"rule_id": "TIG-08", "severity": "BLOCKING", "description": "Imagery is unrelated to the episode's own subject matter."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "BLOCKED"

    def test_prohibited_human_figure_short_cover_fails(self, tmp_path):
        img = _make_fake_image(tmp_path, name="short.jpg")
        client = _make_client([
            {"rule_id": "TIG-01", "severity": "BLOCKING", "description": "Frontal human face is the primary subject."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="short",
        )
        assert result["verdict"] == "BLOCKED"

    def test_main_episode_default_still_requires_wordmark(self, tmp_path):
        # Long-form rules unchanged: TIG-07 still blocks when
        # deliverable_class defaults to "main_episode".
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-07", "severity": "BLOCKING", "description": "Wordmark absent from composition."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
        )
        assert result["verdict"] == "BLOCKED"
        assert result["deliverable_class"] == "main_episode"

    def test_main_episode_still_requires_title_hierarchy(self, tmp_path):
        img = _make_fake_image(tmp_path)
        client = _make_client([
            {"rule_id": "TIG-05", "severity": "BLOCKING", "description": "No headline/subtitle text present."},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path, deliverable_class="main_episode",
        )
        assert result["verdict"] == "BLOCKED"

    def test_real_channel_package_short_rules_omit_wordmark_and_hierarchy_requirements(self):
        from hypogeum.brand.thumbnail_identity_rules import SHORT_RULES, MAIN_EPISODE_RULES
        assert SHORT_RULES.wordmark_required is False
        assert SHORT_RULES.title_hierarchy_required is False
        assert SHORT_RULES.aspect_ratio == "9:16"
        assert MAIN_EPISODE_RULES.wordmark_required is True
        assert MAIN_EPISODE_RULES.title_hierarchy_required is True
        assert MAIN_EPISODE_RULES.aspect_ratio == "16:9"

    def test_rules_are_sourced_from_injected_resolver_not_hardcoded_in_factory_core(self, tmp_path):
        """
        Factory Core must have zero hardcoded per-channel content: an
        entirely custom deliverable_class/rules set, unrelated to
        anything Hypogeum-specific, must work identically -- proving the
        guardian only ever consumes whatever identity_rules_resolver
        supplies.
        """
        from hypogeum.brand.thumbnail_identity_rules import ThumbnailIdentityRules

        custom_rules = ThumbnailIdentityRules(
            deliverable_class="poster",
            aspect_ratio="1:1",
            expected_dimensions="1080x1080",
            wordmark_required=False,
            wordmark_text=None,
            title_hierarchy_required=False,
            human_figure_policy="unrestricted",
            palette_description="Bright pastel palette, high-key lighting encouraged.",
            typography_description="No mandatory typography.",
            forbidden_style_language=[],
            focal_hierarchy_description="A centered product shot.",
        )

        def _resolver(deliverable_class):
            assert deliverable_class == "poster"
            return custom_rules

        img = _make_fake_image(tmp_path, name="poster.jpg")
        client = _make_client([
            {"rule_id": "TIG-07", "severity": "BLOCKING", "description": "no wordmark (should be filtered -- not required)"},
        ])
        result = run_thumbnail_identity_guardian(
            "ep_000", img, tig_client=client, output_dir=tmp_path,
            deliverable_class="poster", identity_rules_resolver=_resolver,
        )
        assert result["verdict"] == "APPROVED"
        assert result["deliverable_class"] == "poster"

    def test_build_system_prompt_contains_no_hardcoded_hypogeum_wording_for_a_custom_channel(self):
        from hypogeum.agents.thumbnail_identity_guardian import _build_system_prompt
        from hypogeum.brand.thumbnail_identity_rules import ThumbnailIdentityRules

        other_channel_rules = ThumbnailIdentityRules(
            deliverable_class="main_episode",
            aspect_ratio="16:9",
            expected_dimensions="1280x720",
            wordmark_required=True,
            wordmark_text="SOME OTHER CHANNEL",
            title_hierarchy_required=True,
            human_figure_policy="unrestricted",
            palette_description="Bright, colorful palette.",
            typography_description="Comic Sans everywhere.",
            forbidden_style_language=["dark and moody"],
            focal_hierarchy_description="Whatever this other channel wants.",
        )
        prompt = _build_system_prompt(other_channel_rules)
        assert "SOME OTHER CHANNEL" in prompt
        assert "Hypogeum" not in prompt
        assert "Cinzel" not in prompt
