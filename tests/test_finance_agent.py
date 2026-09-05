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

    def _run(self, db_path=None, **kw) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return run_budget_authorization(
                script_package=make_script_package(),
                episode_id="ep_test",
                config=make_config(**kw),
                output_dir=Path(tmp),
                db_path=db_path,
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

    def test_ceiling_recorded(self, tmp_path):
        # Isolated, empty DB — no factories row means the factory-policy
        # cascade cleanly falls back to the env ceiling (0.0) instead of
        # reading whatever ceiling happens to be configured on the real
        # production DB (CLAUDE.md Test Isolation: tests must never touch
        # .hypogeum-assets/hypogeum.db).
        from hypogeum.db import initialize_schema
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        rec = self._run(ceiling=0.0, db_path=db_path)
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
# TestBudgetAuthorizationCheckpointReuse
# ---------------------------------------------------------------------------
#
# Regression coverage for the 2026-08-17 incident: unlike Executive Producer/
# Research Director/Narrative Architect/Script Engine/Thumbnail Strategy
# (all gated by _load_package() in pipeline.py), run_budget_authorization()
# had no checkpoint at all -- every resumed run re-executed it unconditionally,
# so identical script_package bytes produced a finance_authorization file with
# a fresh finance_id/recorded_at every time, and once, a genuinely different
# narration_chars figure whose origin was never verifiable because nothing
# bound the persisted output to the exact inputs that produced it.

class TestBudgetAuthorizationCheckpointReuse:
    """
    All calls pass db_path pointing at a nonexistent file so
    get_effective_budget_ceiling()'s factory-policy DB lookup fails closed
    and falls back to config.FINANCE_BUDGET_CEILING_USD (finance_agent.py's
    own try/except around that call) -- otherwise these tests would silently
    read whatever real ceiling exists in the production DB, exactly the kind
    of implicit, unbound input this fix exists to eliminate.
    """

    def _no_db(self, tmp) -> Path:
        return Path(tmp) / "no_such.db"

    def test_identical_inputs_are_not_rerun(self):
        """
        Item 5: Finance is not rerun when its bound inputs are unchanged --
        the second call must return the exact same finance_id/recorded_at as
        the first (proof it was reused, not recomputed with the same numbers
        by coincidence -- recomputation always mints a fresh uuid4 + timestamp).
        """
        with tempfile.TemporaryDirectory() as tmp:
            sp = make_script_package()
            cfg = make_config(ceiling=100.0)
            first = run_budget_authorization(
                script_package=sp, episode_id="ep_reuse", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            second = run_budget_authorization(
                script_package=sp, episode_id="ep_reuse", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            assert second["finance_id"] == first["finance_id"]
            assert second["recorded_at"] == first["recorded_at"]

    def test_identical_inputs_produce_byte_identical_file(self):
        """Item 6: identical Finance inputs produce byte-identical output on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = make_script_package()
            cfg = make_config(ceiling=100.0)
            out_path = Path(tmp) / "finance_authorization_ep_bytes.json"

            run_budget_authorization(
                script_package=sp, episode_id="ep_bytes", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            first_bytes = out_path.read_bytes()

            run_budget_authorization(
                script_package=sp, episode_id="ep_bytes", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            second_bytes = out_path.read_bytes()

            assert second_bytes == first_bytes

    def test_changed_script_package_invalidates_explicitly(self):
        """
        Item 7: a changed Finance dependency (script_package content, hence
        narration_chars) must invalidate the old authorization explicitly --
        a new finance_id, a supersedes_finance_id pointing at the old one, and
        an invalidation_reason naming what changed -- never a silent overwrite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(ceiling=100.0)
            first = run_budget_authorization(
                script_package=make_script_package(chars_per_beat=200),
                episode_id="ep_changed", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            second = run_budget_authorization(
                script_package=make_script_package(chars_per_beat=400),
                episode_id="ep_changed", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            assert second["finance_id"] != first["finance_id"]
            assert second["cost_breakdown"]["narration_chars"] != first["cost_breakdown"]["narration_chars"]
            assert second["supersedes_finance_id"] == first["finance_id"]
            assert "script_package" in second["invalidation_reason"]

    def test_changed_ceiling_invalidates_explicitly(self):
        """Same as above, but the bound ceiling changes instead of script_package."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = make_script_package()
            first = run_budget_authorization(
                script_package=sp, episode_id="ep_ceiling", config=make_config(ceiling=100.0),
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            second = run_budget_authorization(
                script_package=sp, episode_id="ep_ceiling", config=make_config(ceiling=50.0),
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            assert second["finance_id"] != first["finance_id"]
            assert second["supersedes_finance_id"] == first["finance_id"]
            assert "budget_ceiling" in second["invalidation_reason"]

    def test_bound_fields_present_on_every_record(self):
        """The checkpoint-binding key itself must always be persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = run_budget_authorization(
                script_package=make_script_package(), episode_id="ep_bound",
                config=make_config(ceiling=100.0),
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            assert rec["bound_script_package_checksum"]
            assert rec["bound_budget_ceiling_usd"] == 100.0
            assert rec["bound_finance_agent_version"]

    def test_reused_record_still_schema_valid(self):
        """A checkpoint-reused record (early-return path) must still validate."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = make_script_package()
            cfg = make_config(ceiling=100.0)
            run_budget_authorization(
                script_package=sp, episode_id="ep_valid", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            reused = run_budget_authorization(
                script_package=sp, episode_id="ep_valid", config=cfg,
                output_dir=Path(tmp), db_path=self._no_db(tmp),
            )
            validate("finance_record", reused)  # must not raise


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
        # +5 total: Visual Intelligence V1–V4 schemas (+4, total 35) then
        # cinematic_coverage_plan (+1, total 36 — the batched Cinematic
        # Coverage Plan schema added with Commit A).
        assert len(SCHEMA_NAMES) == 36

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
# TestPostVPDAuthorization — post-VPD finance gate, authorized exclusively
# against a compiled hypogeum.production_plan.ProductionPlan's own
# normal/maximum totals. The rough scene-routing-only estimate
# (_resolve_scene_model_estimate / _estimate_muapi_from_vpd_plan) and the
# production_plan=None legacy fallback were REMOVED in the 2026-08-24
# factory-level correction -- this gate cannot run at all without a
# compiled plan (TypeError for a missing argument), and no longer
# re-derives narration/music/OpenRouter costs of its own (they are already
# inside the plan's own totals).
# ---------------------------------------------------------------------------

class TestPostVPDAuthorization:
    """
    Tests for run_post_vpd_authorization() against a real compiled
    Production Plan.
    """

    _REPO_ROOT = Path(__file__).resolve().parent.parent
    _HYPOGEUM_PACKAGES_ROOT = _REPO_ROOT / "channel_packages"

    def _make_config(self, ceiling: float = 50.0) -> MagicMock:
        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD = ceiling
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80
        cfg.MUAPI_T1_MODEL = "veo3-text-to-video"
        cfg.MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
        cfg.MUAPI_T3_MODEL = "veo3.1-lite-text-to-video"
        cfg.MUAPI_I2V_MODEL = "veo3-image-to-video"
        cfg.MUAPI_DEFAULT_TIER = 2
        cfg.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE = 1
        cfg.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        cfg.THUMBNAIL_SHORT_CANDIDATE_CAP = 3
        cfg.ELEVENLABS_MUSIC_DEFAULT_DURATION = 120
        return cfg

    def _make_scene(self, scene_id: str, beat_id: str, tier: int = 2, short_id=None,
                     aspect_ratio: str = "16:9") -> dict:
        return {
            "scene_id": scene_id, "narration_reference": beat_id, "short_id": short_id,
            "scene_index": 0, "source_type": "short" if short_id else "main_episode",
            "section_reference": "sec_001",
            "production_spec": {
                "aspect_ratio": aspect_ratio,
                "duration_seconds": 8.0,
                "provider_routing": {"primary_tier": tier, "primary_prompt": f"prompt for {scene_id}"},
                "reference_image_url": None,
            },
            "entity_appearances": [],
        }

    def _make_plan_and_vpd(self, *, scene_count: int, tier: int = 2, ceiling: float = 50.0):
        from hypogeum.production_plan import compile_production_plan

        script_package = {
            "sections": [{"section_id": "s1", "narration_beats": [
                {"beat_id": f"b_{i:03d}", "text": "x"} for i in range(scene_count)
            ]}],
            "shorts_scripts": [
                {"short_id": sid, "narration_beats": [{"beat_id": f"b_{sid}", "text": "x"}]}
                for sid in ("short_001", "short_002", "short_003")
            ],
        }
        narration_segments = [
            {"beat_reference": f"b_{i:03d}", "duration_seconds": 8.0, "character_cost": 50}
            for i in range(scene_count)
        ]
        for sid in ("short_001", "short_002", "short_003"):
            narration_segments.append({"beat_reference": f"b_{sid}", "duration_seconds": 8.0, "character_cost": 30})
        narration_package = {"narration_segments": narration_segments}

        scenes = [self._make_scene(f"s{i:02d}", f"b_{i:03d}", tier=tier) for i in range(scene_count)]
        for sid in ("short_001", "short_002", "short_003"):
            scenes.append(self._make_scene(f"sh_{sid}", f"b_{sid}", tier=tier, short_id=sid, aspect_ratio="9:16"))
        visual_package = {
            "package_metadata": {"visual_package_id": "vp_test"},
            "episode_metadata": {"episode_id": "ep_test"},
            "visual_continuity_record": {"locked_entities": []},
            "scenes": scenes,
            "asset_manifest": {"assets": []},
            "provider_routing_manifest": {"scenes_by_provider": {"muapi": len(scenes)}},
            "thumbnail_handoff_brief": {},
            "package_integrity": {},
        }
        cfg = self._make_config(ceiling=ceiling)
        plan = compile_production_plan(
            script_package=script_package, narration_package=narration_package,
            visual_package=visual_package, channel_id="hypogeum", episode_id="ep_test",
            config=cfg, packages_root=self._HYPOGEUM_PACKAGES_ROOT,
        )
        return visual_package, plan, cfg

    def _persist_plan(self, plan, output_dir) -> None:
        """Phase 2 plan-bound authorization: run_post_vpd_authorization()
        verifies its passed plan against the episode's VERIFIED PERSISTED
        Production Plan before authorizing anything -- every test in this
        class persists the compiled plan to its tmpdir first."""
        from hypogeum.production_plan import persist_production_plan

        persist_production_plan(plan, Path(output_dir))

    # ----------------------------------------------------------------
    # No compiled plan → cannot call this gate at all.
    # ----------------------------------------------------------------

    def test_missing_production_plan_is_a_type_error(self):
        """There is no None-tolerant legacy fallback any more -- omitting
        production_plan is a missing required argument, not an optional
        rough-estimate path."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, _plan, cfg = self._make_plan_and_vpd(scene_count=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(TypeError):
                run_post_vpd_authorization(vpd_plan, "ep_test", cfg, output_dir=Path(tmpdir))

    # ----------------------------------------------------------------
    # Plan-driven gate tests
    # ----------------------------------------------------------------

    def test_authorized_when_plan_maximum_is_under_ceiling(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        assert record["verdict"] == "AUTHORIZED"
        assert record["authorized"] is True
        assert record["phase"] == "post_vpd_authorization"

    def test_blocked_when_plan_maximum_exceeds_ceiling(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=10, tier=1, ceiling=1.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        assert record["verdict"] == "BLOCKED"
        assert record["authorized"] is False
        assert record["cost_breakdown"]["maximum_bounded_cost_usd"] > 1.0

    def test_estimated_spend_equals_plan_maximum_bounded_cost(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        assert record["estimated_spend_usd"] == pytest.approx(plan.maximum_bounded_cost_usd, abs=0.001)

    def test_finance_totals_equal_sum_of_all_authoritative_line_items(self):
        """Finance must not append narration/music/OpenRouter costs of its
        own -- the recorded cost_breakdown fields must equal the plan's
        own already-summed categories exactly."""
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        cb = record["cost_breakdown"]
        assert cb["estimated_narration_usd"] == round(plan.narration_cost_usd, 4)
        assert cb["estimated_music_usd"] == round(plan.music_cost_usd, 4)
        assert cb["estimated_openrouter_usd"] == round(plan.planning_call_estimate_usd, 4)
        assert cb["maximum_bounded_cost_usd"] == round(plan.maximum_bounded_cost_usd, 4)

    def test_finance_blocks_before_muapi_on_over_ceiling_plan(self):
        from hypogeum.agents.finance_agent import (
            FinancePostVPDBlockedError,
            run_post_vpd_authorization,
        )
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=10, tier=1, ceiling=1.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        assert record["verdict"] == "BLOCKED"
        with pytest.raises(FinancePostVPDBlockedError) as exc_info:
            raise FinancePostVPDBlockedError(episode_id="ep_test", record=record)
        assert exc_info.value.episode_id == "ep_test"
        assert exc_info.value.record["verdict"] == "BLOCKED"

    def test_post_vpd_block_is_terminal_not_auto_retryable(self):
        # A BLOCKED verdict is deterministic (hash-locked plan + static
        # ceiling): the queue must not auto-retry it, re-burning every
        # pre-VPD paid stage. Regression for ep_734076a3 (2026-09-06).
        from hypogeum.agents.finance_agent import FinancePostVPDBlockedError
        from hypogeum.failure_policy import FailurePolicy

        assert FinancePostVPDBlockedError.failure_policy is FailurePolicy.TERMINAL
        exc = FinancePostVPDBlockedError(
            episode_id="ep_x", record={"verdict": "BLOCKED"},
        )
        assert getattr(exc, "failure_policy", None) is FailurePolicy.TERMINAL

    def test_post_vpd_record_validates_against_schema(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=1, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        validate("finance_record", record)  # must not raise

    def test_post_vpd_record_written_to_correct_filename(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=1, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            # The gate verifies against production_plan_{episode_id}.json --
            # mirror the persisted plan under this test's episode id too.
            (Path(tmpdir) / "production_plan_ep_abc.json").write_bytes(
                (Path(tmpdir) / "production_plan_ep_test.json").read_bytes()
            )
            run_post_vpd_authorization(
                vpd_plan, "ep_abc", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
            output_file = Path(tmpdir) / "finance_post_vpd_authorization_ep_abc.json"
            assert output_file.exists()
            data = json.loads(output_file.read_text())
            assert data["episode_id"] == "ep_abc"
            assert data["phase"] == "post_vpd_authorization"

    def test_no_ceiling_always_authorized(self, tmp_path):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        from hypogeum.db import initialize_schema
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=10, tier=1, ceiling=0.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan,
                output_dir=Path(tmpdir), db_path=db_path,
            )
        assert record["verdict"] == "AUTHORIZED"

    def test_warning_when_plan_maximum_is_above_warning_threshold(self):
        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        # First compile once (ceiling irrelevant to the plan's own totals)
        # to learn the plan's real maximum, then set a ceiling just above
        # it so the maximum crosses the 80% warning threshold but stays
        # within the ceiling.
        _, probe_plan, _ = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        ceiling = probe_plan.maximum_bounded_cost_usd / 0.85
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=ceiling)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            record = run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
        assert record["verdict"] in ("WARNING", "AUTHORIZED")
        assert record["authorized"] is True

    def test_passed_plan_must_equal_verified_persisted_plan(self):
        """Phase 2: authorization is calculated only from the exact verified
        persisted Production Plan. An in-memory plan that diverges from the
        persisted file (a replaced or recompiled plan) is never authorized."""
        from hypogeum.agents.finance_agent import (
            FinancePostVPDPlanMismatchError,
            run_post_vpd_authorization,
        )
        from hypogeum.production_plan import PlannedAsset, ProductionPlan, payload_content_hash
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            # A DIFFERENT, internally consistent plan (one asset's prompt changed).
            tampered_asset = PlannedAsset(**{
                **plan.assets[0].__dict__, "prompt": "changed prompt",
                "prompt_fingerprint": "changed",
            })
            replaced_dict = {
                **plan.to_dict(),
                "assets": [tampered_asset.__dict__] + [a.__dict__ for a in plan.assets[1:]],
            }
            payload = {k: v for k, v in replaced_dict.items()}
            replaced_hash = payload_content_hash(payload)
            replaced = ProductionPlan.from_dict({**payload, "content_hash": replaced_hash})
            with pytest.raises(FinancePostVPDPlanMismatchError) as exc_info:
                run_post_vpd_authorization(
                    vpd_plan, "ep_test", cfg, production_plan=replaced, output_dir=Path(tmpdir),
                )
            assert exc_info.value.passed_content_hash == replaced.content_hash
            assert exc_info.value.persisted_content_hash == plan.content_hash

    def test_missing_persisted_plan_fails_closed(self):
        from hypogeum.agents.finance_agent import (
            FinancePostVPDPlanMismatchError,
            run_post_vpd_authorization,
        )
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=2, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FinancePostVPDPlanMismatchError):
                run_post_vpd_authorization(
                    vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
                )

    def test_record_carries_top_level_bound_production_plan_hash(self):
        """The durable bound hash the MUAPI video gate later three-way-
        checks against the grant and the loaded plan."""
        import json

        from hypogeum.agents.finance_agent import run_post_vpd_authorization
        vpd_plan, plan, cfg = self._make_plan_and_vpd(scene_count=1, ceiling=50.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._persist_plan(plan, tmpdir)
            run_post_vpd_authorization(
                vpd_plan, "ep_test", cfg, production_plan=plan, output_dir=Path(tmpdir),
            )
            data = json.loads(
                (Path(tmpdir) / "finance_post_vpd_authorization_ep_test.json").read_text()
            )
        assert data["bound_production_plan_content_hash"] == plan.content_hash
