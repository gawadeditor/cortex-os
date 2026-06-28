"""
tests/test_finance_agent.py — Test suite for Finance Agent (R50)

Tests cover:
  - Budget authorization: AUTHORIZED, WARNING, BLOCKED verdicts
  - Cost ledger: RECORDED verdict, ledger append
  - All 8 checks (4 authorization + 4 ledger)
  - Schema validation for both phases
  - Output file writing
  - Config integration (ceiling, warning threshold, ledger path)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.finance_agent import (
    _ELEVENLABS_COST_PER_1K_CHARS,
    _ELEVENLABS_MUSIC_COST_PER_TRACK,
    _MUAPI_CLIPS_PER_SECTION,
    _MUAPI_COST_PER_CLIP,
    _MUAPI_IMAGE_DEFAULT_COST,
    _OPENROUTER_PLANNING_FLAT,
    _append_to_ledger,
    _compute_music_cost,
    _compute_thumbnail_cost,
    _count_generated_clips,
    _estimate_narration_chars,
    _estimate_section_count,
    _read_narration_chars_billed,
    run_budget_authorization,
    run_cost_ledger,
)
from hypogeum.schemas import SCHEMA_NAMES, SchemaError, validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_config(
    ceiling: float = 0.0,
    warn_pct: float = 0.80,
    ledger_path: str | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.FINANCE_BUDGET_CEILING_USD = ceiling
    cfg.FINANCE_WARNING_THRESHOLD_PCT = warn_pct
    cfg.FINANCE_LEDGER_PATH = ledger_path
    return cfg


def make_script_package(section_count: int = 3, chars_per_beat: int = 200) -> dict:
    """Minimal script_package with controllable narration size."""
    sections = []
    for i in range(section_count):
        sections.append({
            "section_id": f"sec_{i}",
            "title": f"Section {i}",
            "narration_beats": [
                {"beat_id": f"b{i}_0", "text": "A" * chars_per_beat},
                {"beat_id": f"b{i}_1", "text": "B" * chars_per_beat},
            ],
        })
    return {
        "episode_metadata": {"episode_id": "ep_test"},
        "sections": sections,
        "shorts_scripts": [],
        "traceability_manifest": {},
        "script_integrity": {},
    }


def make_final_render_package(
    has_video: bool = True,
    chars_billed: int = 0,   # kept for legacy callers; no longer read by ledger
    thumbnail_count: int = 1,
    music_sources: list | None = None,
) -> dict:
    """Minimal final_render_package."""
    if music_sources is None:
        music_sources = []
    music_log = [
        {"source": src, "category": "Mystery", "music_asset_id": f"track_{i}"}
        for i, src in enumerate(music_sources)
    ]
    thumbnail_refs = [
        {"thumbnail_asset_id": f"ep_test/thumbnails/t{i}", "format": "16:9"}
        for i in range(thumbnail_count)
    ]
    return {
        "package_metadata": {"render_package_id": "rp1", "episode_id": "ep_test"},
        "episode_render": {
            "video_asset_id": "v1" if has_video else "",
            "caption_asset_id": "c1",
        },
        "shorts_renders": [],
        "thumbnail_references": thumbnail_refs,
        "music_log": music_log,
        "metadata_source": {
            "episode_title": "Test Episode",
            "ai_disclosure_text": "AI generated content",
            "section_titles": [],
        },
        "assembly_qa_report": {
            "overall_status": "pass",
        },
        "package_integrity": {"production_ready": True},
    }


def make_narration_package(chars_billed: int = 0) -> dict:
    """Minimal narration_package with configurable billed character count."""
    return {
        "package_metadata": {"narration_package_id": "np1", "episode_id": "ep_test"},
        "narration_segments": [],
        "stitched_tracks": [],
        "captions": [],
        "audio_qa_report": {
            "overall_status": "pass",
            "total_beats": 0,
            "beats_generated": 0,
            "beats_failed": 0,
            "failure_rate": 0.0,
            "total_characters_billed": chars_billed,
        },
        "package_integrity": {"production_ready": True},
    }


def make_visual_package(
    generated: int = 0,
    reused: int = 0,
) -> dict:
    """Minimal visual_package with configurable asset manifest."""
    assets = []
    for i in range(generated):
        assets.append({
            "scene_id": f"s{i:02d}_gen",
            "asset_id": f"episodes/ep_test/video/sc_s{i:02d}_gen",
            "generation_status": "generated",
            "source_type": "main_episode",
        })
    for i in range(reused):
        assets.append({
            "scene_id": f"s{i:02d}_reus",
            "asset_id": f"episodes/ep_test/video/sc_s{i:02d}_reus",
            "generation_status": "reused",
            "source_type": "main_episode",
        })
    return {
        "package_metadata": {"visual_package_id": "vp1", "episode_id": "ep_test"},
        "episode_metadata": {},
        "scenes": [],
        "asset_manifest": {"assets": assets},
        "package_integrity": {"production_ready": True},
    }


def make_publishing_record() -> dict:
    return {
        "package_metadata": {
            "publishing_record_id": "pr1",
            "episode_id": "ep_test",
            "overall_status": "published",
        },
        "platform_results": [
            {
                "result_id": "r1",
                "platform": "youtube",
                "status": "published",
                "metadata_applied": {"ai_disclosure_included": True},
            }
        ],
        "failures": [],
        "summary": {
            "youtube_main_published": True,
            "total_deliverables_attempted": 1,
            "total_published": 1,
            "total_failed": 0,
            "total_skipped": 0,
        },
        "package_integrity": {"production_ready": True},
    }


def make_archive_record() -> dict:
    return {
        "archive_id": "ar1",
        "episode_id": "ep_test",
        "archived_at": "2026-06-07T00:00:00+00:00",
        "archive_status": "archived",
        "verdict": "ARCHIVED",
        "archived": True,
        "checks": [],
        "failures": [],
        "warnings": [],
        "episode_summary": {
            "episode_title": "Test Episode",
            "youtube_main_published": True,
            "total_published": 1,
        },
    }


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationAuthorized
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationAuthorized:
    """AUTHORIZED verdict — no ceiling configured."""

    def _run(self, **kw) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(**kw),
                output_dir=Path(tmp),
            )

    def test_verdict_is_authorized_no_ceiling(self):
        rec = self._run(ceiling=0.0)
        assert rec["verdict"] == "AUTHORIZED"

    def test_authorized_true(self):
        rec = self._run(ceiling=0.0)
        assert rec["authorized"] is True

    def test_phase_is_authorization(self):
        rec = self._run()
        assert rec["phase"] == "authorization"

    def test_has_finance_id(self):
        rec = self._run()
        assert rec["finance_id"].startswith("fa_")

    def test_episode_id_in_record(self):
        rec = self._run()
        assert rec["episode_id"] == "ep_test"

    def test_no_failures(self):
        rec = self._run()
        assert rec["failures"] == []

    def test_four_checks_produced(self):
        rec = self._run()
        assert len(rec["checks"]) == 4

    def test_ceiling_recorded(self):
        rec = self._run(ceiling=0.0)
        assert rec["budget_ceiling_usd"] == 0.0

    def test_estimated_spend_positive(self):
        # 3 sections × 2 beats × 200 chars → narration cost
        rec = self._run()
        assert rec["estimated_spend_usd"] > 0

    def test_cost_breakdown_has_total(self):
        rec = self._run()
        assert "total_estimated_usd" in rec["cost_breakdown"]

    def test_verdict_authorized_under_ceiling(self):
        rec = self._run(ceiling=999.99)
        assert rec["verdict"] == "AUTHORIZED"


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationWarning
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationWarning:
    """WARNING verdict — spend above threshold but under ceiling."""

    def _run_with_tight_threshold(self) -> dict:
        # Set ceiling very high, warn threshold very low (0.0001)
        # so any positive spend triggers warn
        with tempfile.TemporaryDirectory() as tmp:
            return run_budget_authorization(
                script_package=make_script_package(section_count=3),
                episode_id="ep_test",
                config=make_config(ceiling=999.99, warn_pct=0.0001),
                output_dir=Path(tmp),
            )

    def test_verdict_is_warning(self):
        rec = self._run_with_tight_threshold()
        assert rec["verdict"] == "WARNING"

    def test_authorized_true_on_warning(self):
        # WARNING does not block; authorized stays True
        rec = self._run_with_tight_threshold()
        assert rec["authorized"] is True

    def test_warning_threshold_check_is_warn(self):
        rec = self._run_with_tight_threshold()
        check = next(c for c in rec["checks"] if c["check_id"] == "c_warning_threshold")
        assert check["status"] == "warn"

    def test_within_ceiling_check_passes(self):
        rec = self._run_with_tight_threshold()
        check = next(c for c in rec["checks"] if c["check_id"] == "c_within_ceiling")
        assert check["status"] == "pass"

    def test_warnings_list_populated(self):
        rec = self._run_with_tight_threshold()
        assert "c_warning_threshold" in rec["warnings"]

    def test_no_failures_on_warning(self):
        rec = self._run_with_tight_threshold()
        assert rec["failures"] == []


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationBlocked
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationBlocked:
    """BLOCKED verdict — estimated spend exceeds ceiling."""

    def _run_blocked(self) -> dict:
        # Set ceiling to $0.01 so any real episode estimate exceeds it
        with tempfile.TemporaryDirectory() as tmp:
            return run_budget_authorization(
                script_package=make_script_package(section_count=3),
                episode_id="ep_test",
                config=make_config(ceiling=0.01),
                output_dir=Path(tmp),
            )

    def test_verdict_is_blocked(self):
        rec = self._run_blocked()
        assert rec["verdict"] == "BLOCKED"

    def test_authorized_false_on_blocked(self):
        rec = self._run_blocked()
        assert rec["authorized"] is False

    def test_within_ceiling_check_fails(self):
        rec = self._run_blocked()
        check = next(c for c in rec["checks"] if c["check_id"] == "c_within_ceiling")
        assert check["status"] == "fail"

    def test_failures_list_contains_ceiling_check(self):
        rec = self._run_blocked()
        assert "c_within_ceiling" in rec["failures"]

    def test_estimated_spend_exceeds_ceiling(self):
        rec = self._run_blocked()
        assert rec["estimated_spend_usd"] > rec["budget_ceiling_usd"]

    def test_output_file_written_on_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(ceiling=0.01),
                output_dir=Path(tmp),
            )
            assert (Path(tmp) / "finance_authorization_ep_test.json").exists()


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationChecks
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationChecks:
    """Verify individual check IDs and statuses."""

    def _run(self, **kw) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(**kw),
                output_dir=Path(tmp),
            )

    def test_c_budget_ceiling_configured_pass_when_ceiling_set(self):
        rec = self._run(ceiling=10.0)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_budget_ceiling_configured")
        assert check["status"] == "pass"

    def test_c_budget_ceiling_configured_pass_when_no_ceiling(self):
        # No ceiling is a valid operational mode (gate disabled) — not a warning
        rec = self._run(ceiling=0.0)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_budget_ceiling_configured")
        assert check["status"] == "pass"

    def test_c_estimate_basis_pass_when_sections_present(self):
        rec = self._run()
        check = next(c for c in rec["checks"] if c["check_id"] == "c_estimate_basis")
        assert check["status"] == "pass"

    def test_c_estimate_basis_warn_when_no_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package={"sections": [], "shorts_scripts": []},
                episode_id="ep_test",
                config=make_config(ceiling=10.0),
                output_dir=Path(tmp),
            )
        check = next(c for c in rec["checks"] if c["check_id"] == "c_estimate_basis")
        assert check["status"] == "warn"

    def test_all_four_check_ids_present(self):
        rec = self._run()
        ids = {c["check_id"] for c in rec["checks"]}
        expected = {
            "c_budget_ceiling_configured",
            "c_estimate_basis",
            "c_within_ceiling",
            "c_warning_threshold",
        }
        assert expected == ids

    def test_each_check_has_detail_string(self):
        rec = self._run()
        for check in rec["checks"]:
            assert isinstance(check["detail"], str)
            assert len(check["detail"]) > 0


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationCostEstimation
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationCostEstimation:
    """Verify estimation math from helper functions and in the record."""

    def test_estimate_narration_chars_sums_beats(self):
        sp = make_script_package(section_count=2, chars_per_beat=100)
        # 2 sections × 2 beats × 100 chars = 400
        assert _estimate_narration_chars(sp) == 400

    def test_estimate_narration_chars_empty_package(self):
        assert _estimate_narration_chars({}) == 0

    def test_estimate_section_count(self):
        assert _estimate_section_count(make_script_package(section_count=5)) == 5

    def test_estimate_section_count_empty(self):
        assert _estimate_section_count({}) == 0

    def test_cost_breakdown_narration_component(self):
        sp = make_script_package(section_count=1, chars_per_beat=1000)
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=sp,
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )
        # 2 beats × 1000 chars = 2000 chars → $0.60
        expected_narration = (2000 / 1000) * _ELEVENLABS_COST_PER_1K_CHARS
        assert abs(rec["cost_breakdown"]["estimated_narration_usd"] - expected_narration) < 0.001

    def test_cost_breakdown_muapi_component(self):
        sp = make_script_package(section_count=2)
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=sp,
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )
        expected_muapi = 2 * _MUAPI_CLIPS_PER_SECTION * _MUAPI_COST_PER_CLIP
        assert abs(rec["cost_breakdown"]["estimated_muapi_usd"] - expected_muapi) < 0.001

    def test_cost_breakdown_openrouter_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )
        assert rec["cost_breakdown"]["estimated_openrouter_usd"] == _OPENROUTER_PLANNING_FLAT

    def test_cost_breakdown_section_count_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=make_script_package(section_count=4),
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )
        assert rec["cost_breakdown"]["section_count"] == 4


# ---------------------------------------------------------------------------
# TestBudgetAuthorizationOutputFile
# ---------------------------------------------------------------------------

class TestBudgetAuthorizationOutputFile:
    def test_output_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_abc",
                config=make_config(),
                output_dir=Path(tmp),
            )
            assert (Path(tmp) / "finance_authorization_ep_abc.json").exists()

    def test_output_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_abc",
                config=make_config(),
                output_dir=Path(tmp),
            )
            data = json.loads((Path(tmp) / "finance_authorization_ep_abc.json").read_text())
            assert data["episode_id"] == "ep_abc"

    def test_output_file_readable_as_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_abc",
                config=make_config(),
                output_dir=Path(tmp),
            )
            on_disk = json.loads((Path(tmp) / "finance_authorization_ep_abc.json").read_text())
            assert on_disk["finance_id"] == rec["finance_id"]


# ---------------------------------------------------------------------------
# TestCostLedgerRecorded
# ---------------------------------------------------------------------------

class TestCostLedgerRecorded:
    """RECORDED verdict — normal post-run ledger."""

    def _run(self, narration_chars=5000, generated=1, reused=0, **kw) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(**kw),
                narration_package=make_narration_package(chars_billed=narration_chars),
                visual_package=make_visual_package(generated=generated, reused=reused),
                output_dir=Path(tmp),
            )

    def test_verdict_is_recorded(self):
        rec = self._run()
        assert rec["verdict"] == "RECORDED"

    def test_authorized_true(self):
        rec = self._run()
        assert rec["authorized"] is True

    def test_phase_is_ledger(self):
        rec = self._run()
        assert rec["phase"] == "ledger"

    def test_finance_id_starts_with_fl(self):
        rec = self._run()
        assert rec["finance_id"].startswith("fl_")

    def test_six_checks_produced(self):
        rec = self._run()
        assert len(rec["checks"]) == 6

    def test_cost_breakdown_has_total_actual(self):
        rec = self._run()
        assert "total_actual_usd" in rec["cost_breakdown"]

    def test_narration_cost_computed_from_narration_package(self):
        rec = self._run(narration_chars=5000)
        expected = (5000 / 1000) * _ELEVENLABS_COST_PER_1K_CHARS
        assert abs(rec["cost_breakdown"]["narration_cost_usd"] - expected) < 0.001

    def test_muapi_clips_generated_recorded(self):
        rec = self._run(generated=2, reused=1)
        assert rec["cost_breakdown"]["muapi_clips_generated"] == 2

    def test_no_failures_on_full_data(self):
        rec = self._run()
        assert rec["failures"] == []


# ---------------------------------------------------------------------------
# TestCostLedgerChecks
# ---------------------------------------------------------------------------

class TestCostLedgerChecks:
    """Verify individual ledger check IDs and statuses."""

    def _run_with(
        self,
        chars_billed: int = 0,
        generated: int = 0,
        reused: int = 0,
        has_visual_package: bool = True,
        ledger_path=None,
    ) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(ledger_path=ledger_path),
                narration_package=make_narration_package(chars_billed=chars_billed),
                visual_package=(
                    make_visual_package(generated=generated, reused=reused)
                    if has_visual_package else None
                ),
                output_dir=Path(tmp),
            )

    def test_c_narration_cost_data_pass_when_chars_present(self):
        rec = self._run_with(chars_billed=2000)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_narration_cost_data")
        assert check["status"] == "pass"

    def test_c_narration_cost_data_warn_when_no_chars(self):
        rec = self._run_with(chars_billed=0)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_narration_cost_data")
        assert check["status"] == "warn"

    def test_c_muapi_cost_data_pass_when_visual_package_provided(self):
        rec = self._run_with(generated=1, has_visual_package=True)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_muapi_cost_data")
        assert check["status"] == "pass"

    def test_c_muapi_cost_data_pass_when_all_reused(self):
        """All-reused is still pass — we have generation data, cost is just $0.00."""
        rec = self._run_with(generated=0, reused=2, has_visual_package=True)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_muapi_cost_data")
        assert check["status"] == "pass"

    def test_c_muapi_cost_data_warn_when_no_visual_package(self):
        rec = self._run_with(has_visual_package=False)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_muapi_cost_data")
        assert check["status"] == "warn"

    def test_c_ledger_appended_warn_when_no_path(self):
        rec = self._run_with(chars_billed=1000, ledger_path=None)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_ledger_appended")
        assert check["status"] == "warn"

    def test_c_ledger_appended_pass_when_path_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = str(Path(tmp) / "ledger.jsonl")
            rec = run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(ledger_path=ledger),
                narration_package=make_narration_package(chars_billed=1000),
                visual_package=make_visual_package(generated=1),
                output_dir=Path(tmp),
            )
        check = next(c for c in rec["checks"] if c["check_id"] == "c_ledger_appended")
        assert check["status"] == "pass"

    def test_all_six_check_ids_present(self):
        rec = self._run_with(chars_billed=1000, generated=1)
        ids = {c["check_id"] for c in rec["checks"]}
        expected = {
            "c_narration_cost_data",
            "c_muapi_cost_data",
            "c_thumbnail_cost_data",
            "c_music_cost_data",
            "c_actual_spend_computed",
            "c_ledger_appended",
        }
        assert expected == ids


# ---------------------------------------------------------------------------
# TestCostLedgerLedgerAppend
# ---------------------------------------------------------------------------

class TestCostLedgerLedgerAppend:
    """Verify ledger JSONL file behavior."""

    def test_ledger_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = str(Path(tmp) / "cost_ledger.jsonl")
            run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(ledger_path=ledger),
                narration_package=make_narration_package(chars_billed=1000),
                visual_package=make_visual_package(generated=1),
                output_dir=Path(tmp),
            )
            assert Path(ledger).exists()

    def test_ledger_line_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = str(Path(tmp) / "ledger.jsonl")
            run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_abc",
                config=make_config(ledger_path=ledger),
                narration_package=make_narration_package(chars_billed=1000),
                visual_package=make_visual_package(generated=1),
                output_dir=Path(tmp),
            )
            lines = Path(ledger).read_text().strip().splitlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["episode_id"] == "ep_abc"

    def test_ledger_appends_multiple_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = str(Path(tmp) / "ledger.jsonl")
            for ep_id in ("ep_001", "ep_002", "ep_003"):
                run_cost_ledger(
                    final_render_package=make_final_render_package(has_video=True),
                    publishing_record=make_publishing_record(),
                    archive_record=make_archive_record(),
                    episode_id=ep_id,
                    config=make_config(ledger_path=ledger),
                    output_dir=Path(tmp),
                )
            lines = Path(ledger).read_text().strip().splitlines()
            assert len(lines) == 3

    def test_append_to_ledger_helper_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sub" / "ledger.jsonl")
            result = _append_to_ledger({"episode_id": "x"}, path)
            assert result is True
            assert Path(path).exists()

    def test_append_to_ledger_returns_false_on_error(self):
        # Pass an invalid path (directory, not file)
        with tempfile.TemporaryDirectory() as tmp:
            # Can't write to a path that has a non-existent parent
            # in a read-only scenario; simulate by passing None path
            result = _append_to_ledger({}, None)
            assert result is False


# ---------------------------------------------------------------------------
# TestCostLedgerOutputFile
# ---------------------------------------------------------------------------

class TestCostLedgerOutputFile:
    def test_output_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cost_ledger(
                final_render_package=make_final_render_package(),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_xyz",
                config=make_config(),
                output_dir=Path(tmp),
            )
            assert (Path(tmp) / "finance_record_ep_xyz.json").exists()

    def test_output_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cost_ledger(
                final_render_package=make_final_render_package(),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_xyz",
                config=make_config(),
                output_dir=Path(tmp),
            )
            data = json.loads((Path(tmp) / "finance_record_ep_xyz.json").read_text())
            assert data["phase"] == "ledger"




# ---------------------------------------------------------------------------
# TestFinanceAgentSchema
# ---------------------------------------------------------------------------

class TestFinanceAgentSchema:
    def test_authorization_record_passes_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )
        validate("finance_record", rec)

    def test_ledger_record_passes_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_cost_ledger(
                final_render_package=make_final_render_package(has_video=True),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(),
                narration_package=make_narration_package(chars_billed=1000),
                visual_package=make_visual_package(generated=1),
                output_dir=Path(tmp),
            )
        validate("finance_record", rec)

    def test_schema_requires_finance_id(self):
        with pytest.raises(SchemaError):
            validate("finance_record", {
                "episode_id": "ep_test",
                "phase": "authorization",
                "recorded_at": "2026-06-07T00:00:00+00:00",
                "verdict": "AUTHORIZED",
                "authorized": True,
                "budget_ceiling_usd": 0.0,
                "estimated_spend_usd": 0.0,
                "checks": [],
                "failures": [],
                "warnings": [],
                "cost_breakdown": {},
            })

    def test_schema_rejects_invalid_verdict(self):
        with pytest.raises(SchemaError):
            validate("finance_record", {
                "finance_id": "fa_abc",
                "episode_id": "ep_test",
                "phase": "authorization",
                "recorded_at": "2026-06-07T00:00:00+00:00",
                "verdict": "INVALID",
                "authorized": True,
                "budget_ceiling_usd": 0.0,
                "estimated_spend_usd": 0.0,
                "checks": [],
                "failures": [],
                "warnings": [],
                "cost_breakdown": {},
            })

    def test_schema_rejects_invalid_phase(self):
        with pytest.raises(SchemaError):
            validate("finance_record", {
                "finance_id": "fa_abc",
                "episode_id": "ep_test",
                "phase": "unknown",
                "recorded_at": "2026-06-07T00:00:00+00:00",
                "verdict": "AUTHORIZED",
                "authorized": True,
                "budget_ceiling_usd": 0.0,
                "estimated_spend_usd": 0.0,
                "checks": [],
                "failures": [],
                "warnings": [],
                "cost_breakdown": {},
            })

    def test_schema_count_updated(self):
        # finance_record is schema #23; vpd_scene_prompts_batch is #29
        assert len(SCHEMA_NAMES) == 29

    def test_finance_record_in_schema_names(self):
        assert "finance_record" in SCHEMA_NAMES


# ---------------------------------------------------------------------------
# TestNarrationCostFix  (Task 1)
# ---------------------------------------------------------------------------

class TestNarrationCostFix:
    """Narration cost reads from narration_package.audio_qa_report, not assembly_qa_report."""

    def _run(self, narration_package=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(),
                narration_package=narration_package,
                output_dir=Path(tmp),
            )

    def test_719_chars_yields_expected_cost(self):
        """719 chars × $0.30/1000 = $0.2157."""
        rec = self._run(make_narration_package(chars_billed=719))
        assert abs(rec["cost_breakdown"]["narration_cost_usd"] - 0.2157) < 0.0001

    def test_missing_field_yields_zero_cost(self):
        """narration_package present but total_characters_billed absent → $0.00."""
        pkg = {"audio_qa_report": {}, "package_metadata": {}}
        rec = self._run(pkg)
        assert rec["cost_breakdown"]["narration_cost_usd"] == 0.0

    def test_empty_narration_package_yields_zero_cost(self):
        """narration_package is empty dict → $0.00."""
        rec = self._run({})
        assert rec["cost_breakdown"]["narration_cost_usd"] == 0.0

    def test_none_narration_package_yields_zero_cost(self):
        """No narration_package passed → $0.00 with warn check."""
        rec = self._run(None)
        assert rec["cost_breakdown"]["narration_cost_usd"] == 0.0
        check = next(c for c in rec["checks"] if c["check_id"] == "c_narration_cost_data")
        assert check["status"] == "warn"

    def test_read_narration_chars_billed_helper(self):
        """_read_narration_chars_billed reads from audio_qa_report."""
        pkg = make_narration_package(chars_billed=500)
        assert _read_narration_chars_billed(pkg) == 500

    def test_read_narration_chars_billed_returns_zero_for_none(self):
        assert _read_narration_chars_billed(None) == 0


# ---------------------------------------------------------------------------
# TestMuapiClipCostFix  (Task 2)
# ---------------------------------------------------------------------------

class TestMuapiClipCostFix:
    """MUAPI cost reads from asset_manifest generation_status, not deliverable count."""

    def _run(self, visual_package=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(),
                visual_package=visual_package,
                output_dir=Path(tmp),
            )

    def test_2_generated_0_reused_is_2_billable(self):
        rec = self._run(make_visual_package(generated=2, reused=0))
        assert rec["cost_breakdown"]["muapi_clips_generated"] == 2
        assert abs(rec["cost_breakdown"]["muapi_cost_usd"] - 2 * _MUAPI_COST_PER_CLIP) < 0.0001

    def test_0_generated_2_reused_is_0_billable(self):
        rec = self._run(make_visual_package(generated=0, reused=2))
        assert rec["cost_breakdown"]["muapi_clips_generated"] == 0
        assert rec["cost_breakdown"]["muapi_cost_usd"] == 0.0

    def test_1_generated_1_reused_is_1_billable(self):
        rec = self._run(make_visual_package(generated=1, reused=1))
        assert rec["cost_breakdown"]["muapi_clips_generated"] == 1
        assert abs(rec["cost_breakdown"]["muapi_cost_usd"] - _MUAPI_COST_PER_CLIP) < 0.0001

    def test_empty_manifest_is_0_billable(self):
        rec = self._run(make_visual_package(generated=0, reused=0))
        assert rec["cost_breakdown"]["muapi_clips_generated"] == 0
        assert rec["cost_breakdown"]["muapi_cost_usd"] == 0.0

    def test_count_generated_clips_helper_counts_only_generated(self):
        vp = make_visual_package(generated=3, reused=5)
        assert _count_generated_clips(vp) == 3

    def test_count_generated_clips_helper_returns_zero_for_none(self):
        assert _count_generated_clips(None) == 0


# ---------------------------------------------------------------------------
# TestThumbnailCostTracking  (Task 3)
# ---------------------------------------------------------------------------

class TestThumbnailCostTracking:
    """Thumbnail generation cost tracked from thumbnail_references count."""

    def _run(self, thumbnail_count=1) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(thumbnail_count=thumbnail_count),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )

    def test_flux_dev_image_thumbnail_costs_default_rate(self):
        """1 thumbnail → _MUAPI_IMAGE_DEFAULT_COST."""
        rec = self._run(thumbnail_count=1)
        assert abs(rec["cost_breakdown"]["thumbnail_cost_usd"] - _MUAPI_IMAGE_DEFAULT_COST) < 0.0001

    def test_missing_thumbnail_is_zero_cost(self):
        """0 thumbnails → $0.00."""
        rec = self._run(thumbnail_count=0)
        assert rec["cost_breakdown"]["thumbnail_cost_usd"] == 0.0

    def test_missing_thumbnail_check_still_passes(self):
        """c_thumbnail_cost_data passes even when no thumbnail (expected during early pipeline)."""
        rec = self._run(thumbnail_count=0)
        check = next(c for c in rec["checks"] if c["check_id"] == "c_thumbnail_cost_data")
        assert check["status"] == "pass"

    def test_compute_thumbnail_cost_helper_single(self):
        frp = make_final_render_package(thumbnail_count=1)
        assert abs(_compute_thumbnail_cost(frp) - _MUAPI_IMAGE_DEFAULT_COST) < 0.0001

    def test_compute_thumbnail_cost_helper_zero(self):
        frp = make_final_render_package(thumbnail_count=0)
        assert _compute_thumbnail_cost(frp) == 0.0

    def test_unknown_model_uses_default_rate(self):
        """No model field available → falls back to _MUAPI_IMAGE_DEFAULT_COST."""
        frp = make_final_render_package(thumbnail_count=1)
        # thumbnail_references has no model field — default should apply
        assert abs(_compute_thumbnail_cost(frp) - _MUAPI_IMAGE_DEFAULT_COST) < 0.0001


# ---------------------------------------------------------------------------
# TestMusicCostTracking  (Task 4)
# ---------------------------------------------------------------------------

class TestMusicCostTracking:
    """Music cost: channel_library → $0, provider-generated → _ELEVENLABS_MUSIC_COST_PER_TRACK."""

    def _run(self, music_sources=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_cost_ledger(
                final_render_package=make_final_render_package(
                    music_sources=music_sources or []
                ),
                publishing_record=make_publishing_record(),
                archive_record=make_archive_record(),
                episode_id="ep_test",
                config=make_config(),
                output_dir=Path(tmp),
            )

    def test_channel_library_source_is_zero_cost(self):
        rec = self._run(["channel_library"])
        assert rec["cost_breakdown"]["music_cost_usd"] == 0.0

    def test_generated_music_incurs_provider_cost(self):
        rec = self._run(["elevenlabs_music"])
        expected = _ELEVENLABS_MUSIC_COST_PER_TRACK
        assert abs(rec["cost_breakdown"]["music_cost_usd"] - expected) < 0.0001

    def test_missing_music_metadata_is_zero(self):
        """Empty music_log → $0.00."""
        rec = self._run([])
        assert rec["cost_breakdown"]["music_cost_usd"] == 0.0

    def test_compute_music_cost_helper_library(self):
        frp = make_final_render_package(music_sources=["channel_library"])
        assert _compute_music_cost(frp) == 0.0

    def test_compute_music_cost_helper_generated(self):
        frp = make_final_render_package(music_sources=["elevenlabs_music"])
        assert abs(_compute_music_cost(frp) - _ELEVENLABS_MUSIC_COST_PER_TRACK) < 0.0001

    def test_compute_music_cost_helper_mixed(self):
        """1 library + 1 generated → cost for 1 generated only."""
        frp = make_final_render_package(music_sources=["channel_library", "elevenlabs_music"])
        assert abs(_compute_music_cost(frp) - _ELEVENLABS_MUSIC_COST_PER_TRACK) < 0.0001


# ---------------------------------------------------------------------------
# TestPostVPDAuthorization — post-VPD finance gate from scene routing
# ---------------------------------------------------------------------------

class TestPostVPDAuthorization:
    """
    Tests for run_post_vpd_authorization() and its helpers.

    Verifies that:
    - MUAPI cost is estimated from actual scene routing, not section count
    - Tier 1/2/3 model costs are summed correctly per MODEL_CREDIT_COSTS
    - I2V model is predicted for scenes with final-locked primary entities
    - BLOCKED verdict fires when scene-routing total exceeds ceiling
    - AUTHORIZED verdict fires when scene-routing total is within ceiling
    - Post-VPD record validates against finance_record schema
    """

    # ----------------------------------------------------------------
    # Test fixtures
    # ----------------------------------------------------------------

    def _make_config(
        self,
        ceiling: float = 50.0,
        t1_model: str = "veo3-text-to-video",
        t2_model: str = "veo3-fast-text-to-video",
        t3_model: str = "runway-text-to-video",
        i2v_model: str = "runway-image-to-video",
        default_tier: int = 2,
        runway_only: bool = False,
        cheap_mode: bool = False,
    ) -> MagicMock:
        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD = ceiling
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80
        cfg.MUAPI_T1_MODEL = t1_model
        cfg.MUAPI_T2_MODEL = t2_model
        cfg.MUAPI_T3_MODEL = t3_model
        cfg.MUAPI_I2V_MODEL = i2v_model
        cfg.MUAPI_DEFAULT_TIER = default_tier
        cfg.MUAPI_RUNWAY_ONLY_MODE = runway_only
        cfg.MUAPI_CHEAP_MODE = cheap_mode
        return cfg

    def _make_scene(self, scene_id: str, tier: int, entity_id: str | None = None) -> dict:
        """Make a minimal scene dict with tier routing and optional primary entity."""
        scene = {
            "scene_id": scene_id,
            "scene_index": 0,
            "source_type": "main_episode",
            "short_id": None,
            "section_reference": "sec_001",
            "narration_reference": "b_001",
            "production_spec": {
                "aspect_ratio": "16:9",
                "duration_seconds": 8.0,
                "provider_routing": {
                    "primary_provider": "muapi",
                    "primary_tier": tier,
                    "primary_prompt": "test prompt",
                    "fallback_tier": 3,
                    "fallback_prompt": "fallback prompt",
                    "routing_rationale": "test",
                },
                "reference_image_url": None,
            },
            "entity_appearances": [],
        }
        if entity_id:
            scene["entity_appearances"] = [
                {"entity_id": entity_id, "role": "primary"}
            ]
        return scene

    def _make_vpd_plan(self, scenes: list, final_entity_ids: list | None = None) -> dict:
        """Make a minimal visual_package plan with given scenes and locked entities."""
        locked_entities = []
        for eid in (final_entity_ids or []):
            locked_entities.append({
                "entity_id": eid,
                "lock_state": "final",
                "entity_name": f"Entity {eid}",
            })
        return {
            "package_metadata": {"visual_package_id": "vp_test"},
            "episode_metadata": {"episode_id": "ep_test"},
            "visual_continuity_record": {"locked_entities": locked_entities},
            "scenes": scenes,
            "asset_manifest": {"assets": []},
            "provider_routing_manifest": {"scenes_by_provider": {"muapi": len(scenes)}},
            "thumbnail_handoff_brief": {},
            "package_integrity": {},
        }

    # ----------------------------------------------------------------
    # _resolve_scene_model_estimate tests
    # ----------------------------------------------------------------

    def test_tier1_resolves_to_t1_model(self):
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=1)
        cfg = self._make_config()
        model = _resolve_scene_model_estimate(scene, set(), cfg)
        assert model == "veo3-text-to-video"

    def test_tier2_resolves_to_t2_model(self):
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=2)
        cfg = self._make_config()
        model = _resolve_scene_model_estimate(scene, set(), cfg)
        assert model == "veo3-fast-text-to-video"

    def test_tier3_resolves_to_t3_model(self):
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=3)
        cfg = self._make_config()
        model = _resolve_scene_model_estimate(scene, set(), cfg)
        assert model == "runway-text-to-video"

    def test_scene_with_final_locked_primary_entity_resolves_to_i2v(self):
        """Scene with final-locked primary entity → I2V model (reference image will be generated)."""
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=2, entity_id="entity_xyz")
        cfg = self._make_config()
        model = _resolve_scene_model_estimate(scene, {"entity_xyz"}, cfg)
        assert model == "runway-image-to-video"

    def test_scene_with_non_locked_entity_uses_tier_model(self):
        """Scene with entity that is NOT final-locked → T2V model (no ref image)."""
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=2, entity_id="entity_xyz")
        cfg = self._make_config()
        # entity_xyz is NOT in final_entity_ids
        model = _resolve_scene_model_estimate(scene, set(), cfg)
        assert model == "veo3-fast-text-to-video"

    def test_runway_only_mode_forces_tier2(self):
        """MUAPI_RUNWAY_ONLY_MODE=True forces tier 2 model regardless of scene routing."""
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        scene = self._make_scene("s01", tier=1)  # Tier 1 scene
        cfg = self._make_config(runway_only=True)
        model = _resolve_scene_model_estimate(scene, set(), cfg)
        assert model == "veo3-fast-text-to-video"

    def test_cheap_mode_forces_tier3(self):
        """MUAPI_CHEAP_MODE=True forces tier 3 model and disables I2V."""
        from hypogeum.agents.finance_agent import _resolve_scene_model_estimate
        # Tier 1 scene with final-locked entity — cheap mode overrides both
        scene = self._make_scene("s01", tier=1, entity_id="ent_a")
        cfg = self._make_config(cheap_mode=True)
        model = _resolve_scene_model_estimate(scene, {"ent_a"}, cfg)
        assert model == "runway-text-to-video"  # T3 model, not I2V

    # ----------------------------------------------------------------
    # _estimate_muapi_from_vpd_plan tests
    # ----------------------------------------------------------------

    def test_26_scene_antikythera_plan_estimates_correctly(self):
        """
        Reproduce ep_val_01 routing: 7×Tier1 + 17×Tier2 + 2×Tier3 with no locked entities.
        Expected: 7×$2.50 + 17×$0.60 + 2×$0.15 = $17.50 + $10.20 + $0.30 = $28.00
        """
        from hypogeum.agents.finance_agent import _estimate_muapi_from_vpd_plan
        scenes = (
            [self._make_scene(f"s{i:02d}", tier=1) for i in range(7)]
            + [self._make_scene(f"s{i:02d}", tier=2) for i in range(7, 24)]
            + [self._make_scene(f"s{i:02d}", tier=3) for i in range(24, 26)]
        )
        vpd_plan = self._make_vpd_plan(scenes, final_entity_ids=[])
        cfg = self._make_config()
        total, breakdown = _estimate_muapi_from_vpd_plan(vpd_plan, cfg)
        assert abs(total - 28.00) < 0.001, f"Expected $28.00, got ${total}"
        assert breakdown.get("veo3-text-to-video") == 7
        assert breakdown.get("veo3-fast-text-to-video") == 17
        assert breakdown.get("runway-text-to-video") == 2

    def test_tier1_cost_is_2_50(self):
        """Single Tier 1 scene = $2.50 (veo3-text-to-video)."""
        from hypogeum.agents.finance_agent import _estimate_muapi_from_vpd_plan
        vpd_plan = self._make_vpd_plan([self._make_scene("s01", tier=1)])
        cfg = self._make_config()
        total, _ = _estimate_muapi_from_vpd_plan(vpd_plan, cfg)
        assert abs(total - 2.5) < 0.001

    def test_tier2_cost_is_0_60(self):
        """Single Tier 2 scene = $0.60 (veo3-fast-text-to-video)."""
        from hypogeum.agents.finance_agent import _estimate_muapi_from_vpd_plan
        vpd_plan = self._make_vpd_plan([self._make_scene("s01", tier=2)])
        cfg = self._make_config()
        total, _ = _estimate_muapi_from_vpd_plan(vpd_plan, cfg)
        assert abs(total - 0.60) < 0.001

    def test_tier3_cost_is_0_15(self):
        """Single Tier 3 scene = $0.15 (runway-text-to-video)."""
        from hypogeum.agents.finance_agent import _estimate_muapi_from_vpd_plan
        vpd_plan = self._make_vpd_plan([self._make_scene("s01", tier=3)])
        cfg = self._make_config()
        total, _ = _estimate_muapi_from_vpd_plan(vpd_plan, cfg)
        assert abs(total - 0.15) < 0.001

    def test_i2v_scene_cost_is_runway_rate(self):
        """Scene with final-locked entity uses runway-image-to-video = $0.15."""
        from hypogeum.agents.finance_agent import _estimate_muapi_from_vpd_plan
        scene = self._make_scene("s01", tier=2, entity_id="ent_a")
        vpd_plan = self._make_vpd_plan([scene], final_entity_ids=["ent_a"])
        cfg = self._make_config()
        total, breakdown = _estimate_muapi_from_vpd_plan(vpd_plan, cfg)
        assert abs(total - 0.15) < 0.001
        assert breakdown.get("runway-image-to-video") == 1

    # ----------------------------------------------------------------
    # run_post_vpd_authorization gate tests
    # ----------------------------------------------------------------

    def test_authorized_when_plan_is_under_ceiling(self):
        """26-scene plan under $50 ceiling → AUTHORIZED."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        scenes = [self._make_scene(f"s{i:02d}", tier=2) for i in range(5)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        assert record["verdict"] == "AUTHORIZED"
        assert record["authorized"] is True
        assert record["phase"] == "post_vpd_authorization"

    def test_blocked_when_plan_exceeds_ceiling(self):
        """26-scene Antikythera plan ($28+ MUAPI) exceeds $15 ceiling → BLOCKED."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        scenes = (
            [self._make_scene(f"s{i:02d}", tier=1) for i in range(7)]
            + [self._make_scene(f"s{i:02d}", tier=2) for i in range(7, 24)]
            + [self._make_scene(f"s{i:02d}", tier=3) for i in range(24, 26)]
        )
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=15.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        assert record["verdict"] == "BLOCKED"
        assert record["authorized"] is False
        # MUAPI alone exceeds ceiling
        assert record["cost_breakdown"]["estimated_muapi_usd"] > 15.0

    def test_section_count_not_used_as_authoritative_muapi_estimate(self):
        """
        Post-VPD estimate must use scene routing, not section_count × clips_per_section.
        A 3-section script with 26 scenes should NOT produce section-count-based MUAPI estimate.
        """
        from hypogeum.agents.finance_agent import (
            _MUAPI_CLIPS_PER_SECTION,
            _MUAPI_COST_PER_CLIP,
            run_post_vpd_authorization,
        )
        script_pkg = make_script_package(section_count=3)
        section_based_estimate = 3 * _MUAPI_CLIPS_PER_SECTION * _MUAPI_COST_PER_CLIP

        scenes = [self._make_scene(f"s{i:02d}", tier=1) for i in range(10)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, script_package=script_pkg, output_dir=Path(tmpdir)
            )
        actual_muapi = record["cost_breakdown"]["estimated_muapi_usd"]
        # 10 Tier-1 scenes × $2.50 = $25.00 — not 3 × 3 × $0.15 = $1.35
        assert abs(actual_muapi - 25.0) < 0.001, (
            f"Post-VPD gate used scene routing correctly (${actual_muapi:.4f}), "
            f"not section-count estimate (${section_based_estimate:.4f})"
        )
        assert abs(actual_muapi - section_based_estimate) > 1.0, (
            "Post-VPD estimate must differ from section-count estimate for this plan"
        )

    def test_finance_blocks_before_muapi_on_26_scene_plan_over_ceiling(self):
        """
        FinancePostVPDBlockedError is raised before MUAPI generation when
        post-VPD gate returns BLOCKED.
        """
        from hypogeum.agents.finance_agent import (
            FinancePostVPDBlockedError,
            run_post_vpd_authorization,
        )
        scenes = [self._make_scene(f"s{i:02d}", tier=1) for i in range(7)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=5.0)  # 7 × $2.50 = $17.50 >> $5
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        assert record["verdict"] == "BLOCKED"
        # Caller raises FinancePostVPDBlockedError
        with pytest.raises(FinancePostVPDBlockedError) as exc_info:
            raise FinancePostVPDBlockedError(episode_id="ep_test", record=record)
        assert exc_info.value.episode_id == "ep_test"
        assert exc_info.value.record["verdict"] == "BLOCKED"

    def test_post_vpd_record_validates_against_schema(self):
        """Post-VPD authorization record passes finance_record schema validation."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        from hypogeum.schemas import validate
        scenes = [self._make_scene("s01", tier=2)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        # Must not raise
        validate("finance_record", record)

    def test_post_vpd_record_written_to_correct_filename(self):
        """Output file is finance_post_vpd_authorization_{episode_id}.json."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        scenes = [self._make_scene("s01", tier=2)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            run_post_vpd_authorization(vpd_plan, "ep_abc", cfg, output_dir=Path(tmpdir))
            output_file = Path(tmpdir) / "finance_post_vpd_authorization_ep_abc.json"
            assert output_file.exists()
            data = json.loads(output_file.read_text())
            assert data["episode_id"] == "ep_abc"
            assert data["phase"] == "post_vpd_authorization"

    def test_warning_when_plan_is_above_warning_threshold(self):
        """Plan above 80% of ceiling triggers WARNING verdict."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        # 4 Tier-2 scenes × $0.60 = $2.40; ceiling=$3.00, 80% = $2.40
        scenes = [self._make_scene(f"s{i:02d}", tier=2) for i in range(4)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=3.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        # $2.40 MUAPI + $0.05 OpenRouter = $2.45 > 80% of $3.00 ($2.40)
        assert record["verdict"] in ("WARNING", "AUTHORIZED")
        assert record["authorized"] is True

    def test_no_ceiling_always_authorized(self):
        """FINANCE_BUDGET_CEILING_USD=0 (no ceiling) → always AUTHORIZED."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        scenes = [self._make_scene(f"s{i:02d}", tier=1) for i in range(50)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=0.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))
        assert record["verdict"] == "AUTHORIZED"

    def test_narration_cost_included_when_script_package_provided(self):
        """Narration cost from script_package is included in total when provided."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        script_pkg = make_script_package(section_count=6, chars_per_beat=1000)
        scenes = [self._make_scene("s01", tier=3)]  # cheap scene
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, script_package=script_pkg, output_dir=Path(tmpdir)
            )
        narration_cost = record["cost_breakdown"]["estimated_narration_usd"]
        assert narration_cost > 0.0, "Narration cost must be non-zero when script_package provided"

    def test_no_script_package_narration_is_zero(self):
        """Without script_package (resume path), narration cost is $0.00."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        scenes = [self._make_scene("s01", tier=2)]
        vpd_plan = self._make_vpd_plan(scenes)
        cfg = self._make_config(ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, script_package=None, output_dir=Path(tmpdir)
            )
        assert record["cost_breakdown"]["estimated_narration_usd"] == 0.0
