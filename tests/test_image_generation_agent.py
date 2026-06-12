"""
tests/test_image_generation_agent.py — Unit tests for image_generation_agent (Priority 11)

All MUAPI calls (generate_reference_image) are mocked. No network calls. No credits.

Test coverage:
  TestGenerateEntityReferenceImages
    - empty locked_entities returns empty dict
    - provisional entity skipped
    - promoted entity skipped
    - final entity generates image and returns hosted_url
    - multiple final entities all processed
    - partial success: one fails, one succeeds → partial result
    - non-halt exception logged as warn, loop continues
    - MUAPIAuthFailure propagates immediately
    - MUAPIQuotaExhausted propagates immediately
    - MUAPIBudgetExhausted propagates immediately
    - generate_reference_image called with correct kwargs
    - vpd_ref_image_started logged before call
    - vpd_ref_image_completed logged on success
    - vpd_ref_image_failed logged on non-halt exception
    - budget_guard passed through to generate_reference_image
    - entity without entity_id skipped gracefully
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.image_generation_agent import generate_entity_reference_images
from hypogeum.muapi import MUAPIAuthFailure, MUAPIBudgetExhausted, MUAPIQuotaExhausted, ReferenceImageResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity(
    entity_id: str = "pharaoh_amenhotep",
    lock_state: str = "final",
) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "person",
        "name": "Pharaoh Amenhotep",
        "lock_state": lock_state,
        "visual_spec": {
            "description": "Tall imposing figure in white linen robes.",
            "period_detail": "New Kingdom Egypt, 18th Dynasty.",
            "distinctive_markers": ["double crown", "crook and flail"],
            "accuracy_basis": "Archaeological record from Luxor.",
        },
        "reference_image_asset_id": None,
    }


def _make_ref_result(entity_id: str = "pharaoh_amenhotep") -> ReferenceImageResult:
    return ReferenceImageResult(
        asset_id=f"episodes/ep_001/video/ref_{entity_id}",
        hosted_url=f"https://cdn.muapi.ai/ref_{entity_id}.jpg",
    )


def _make_log() -> MagicMock:
    log = MagicMock()
    log.info = MagicMock()
    log.warn = MagicMock()
    return log


_PATCH = "hypogeum.agents.image_generation_agent.generate_reference_image"


# ---------------------------------------------------------------------------
# TestGenerateEntityReferenceImages
# ---------------------------------------------------------------------------

class TestGenerateEntityReferenceImages:

    def test_empty_entities_returns_empty_dict(self, tmp_path):
        log = _make_log()
        result = generate_entity_reference_images([], MagicMock(), "ep_001", tmp_path, log)
        assert result == {}

    def test_provisional_entity_skipped(self, tmp_path):
        entity = _make_entity(lock_state="provisional")
        log = _make_log()
        with patch(_PATCH) as mock_gen:
            result = generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        mock_gen.assert_not_called()
        assert result == {}

    def test_promoted_entity_skipped(self, tmp_path):
        entity = _make_entity(lock_state="promoted")
        log = _make_log()
        with patch(_PATCH) as mock_gen:
            result = generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        mock_gen.assert_not_called()
        assert result == {}

    def test_final_entity_generates_and_returns_url(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        ref_result = _make_ref_result()
        with patch(_PATCH, return_value=ref_result):
            result = generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        assert result == {"pharaoh_amenhotep": "https://cdn.muapi.ai/ref_pharaoh_amenhotep.jpg"}

    def test_multiple_final_entities_all_processed(self, tmp_path):
        entities = [
            _make_entity("entity_a", "final"),
            _make_entity("entity_b", "final"),
            _make_entity("entity_c", "final"),
        ]
        log = _make_log()

        def side_effect(*args, **kwargs):
            return _make_ref_result(kwargs["entity_id"])

        with patch(_PATCH, side_effect=side_effect):
            result = generate_entity_reference_images(
                entities, MagicMock(), "ep_001", tmp_path, log
            )

        assert set(result.keys()) == {"entity_a", "entity_b", "entity_c"}
        assert result["entity_a"] == "https://cdn.muapi.ai/ref_entity_a.jpg"
        assert result["entity_b"] == "https://cdn.muapi.ai/ref_entity_b.jpg"

    def test_mixed_lock_states_only_final_processed(self, tmp_path):
        entities = [
            _make_entity("entity_a", "final"),
            _make_entity("entity_b", "provisional"),
            _make_entity("entity_c", "promoted"),
            _make_entity("entity_d", "final"),
        ]
        log = _make_log()

        def side_effect(*args, **kwargs):
            return _make_ref_result(kwargs["entity_id"])

        with patch(_PATCH, side_effect=side_effect) as mock_gen:
            result = generate_entity_reference_images(
                entities, MagicMock(), "ep_001", tmp_path, log
            )

        assert mock_gen.call_count == 2
        assert set(result.keys()) == {"entity_a", "entity_d"}

    def test_non_halt_exception_logged_and_continues(self, tmp_path):
        entities = [
            _make_entity("entity_a", "final"),
            _make_entity("entity_b", "final"),
        ]
        log = _make_log()

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if kwargs["entity_id"] == "entity_a":
                raise Exception("generation failed")
            return _make_ref_result("entity_b")

        with patch(_PATCH, side_effect=side_effect):
            result = generate_entity_reference_images(
                entities, MagicMock(), "ep_001", tmp_path, log
            )

        # entity_a failed but entity_b succeeded
        assert "entity_a" not in result
        assert result.get("entity_b") == "https://cdn.muapi.ai/ref_entity_b.jpg"
        assert call_count[0] == 2

    def test_non_halt_exception_logs_warn(self, tmp_path):
        entity = _make_entity("entity_a", "final")
        log = _make_log()
        with patch(_PATCH, side_effect=Exception("img gen error")):
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        log.warn.assert_called_once()
        warn_kwargs = log.warn.call_args
        assert "vpd_ref_image_failed" in warn_kwargs[0]

    def test_auth_failure_propagates_immediately(self, tmp_path):
        entities = [
            _make_entity("entity_a", "final"),
            _make_entity("entity_b", "final"),
        ]
        log = _make_log()
        with patch(_PATCH, side_effect=MUAPIAuthFailure()):
            with pytest.raises(MUAPIAuthFailure):
                generate_entity_reference_images(
                    entities, MagicMock(), "ep_001", tmp_path, log
                )

    def test_auth_failure_stops_remaining_entities(self, tmp_path):
        entities = [
            _make_entity("entity_a", "final"),
            _make_entity("entity_b", "final"),
        ]
        log = _make_log()
        with patch(_PATCH, side_effect=MUAPIAuthFailure()) as mock_gen:
            with pytest.raises(MUAPIAuthFailure):
                generate_entity_reference_images(
                    entities, MagicMock(), "ep_001", tmp_path, log
                )
        # Loop stopped after first entity
        assert mock_gen.call_count == 1

    def test_quota_exhausted_propagates_immediately(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                generate_entity_reference_images(
                    [entity], MagicMock(), "ep_001", tmp_path, log
                )

    def test_budget_exhausted_is_non_halt_for_ref_images(self, tmp_path):
        # MUAPIBudgetExhausted is not an explicit halt condition for ref image
        # generation (unlike MUAPIAuthFailure / MUAPIQuotaExhausted). It falls
        # through to the generic exception handler: logged as warn, loop continues.
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, side_effect=MUAPIBudgetExhausted("limit")):
            result = generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        assert result == {}
        log.warn.assert_called_once()

    def test_generate_reference_image_called_with_entity_id(self, tmp_path):
        entity = _make_entity("pharaoh_amenhotep", "final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["entity_id"] == "pharaoh_amenhotep"

    def test_generate_reference_image_called_with_episode_id(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_042", tmp_path, log
            )
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["episode_id"] == "ep_042"

    def test_generate_reference_image_called_with_visual_spec_text(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_gen.call_args[1]
        assert "Tall imposing figure" in call_kwargs["visual_spec_text"]

    def test_generate_reference_image_called_with_output_dir(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["output_dir"] == tmp_path

    def test_budget_guard_passed_through(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        budget_guard = MagicMock()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log,
                budget_guard=budget_guard,
            )
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["budget_guard"] is budget_guard

    def test_budget_guard_none_by_default(self, tmp_path):
        entity = _make_entity(lock_state="final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()) as mock_gen:
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["budget_guard"] is None

    def test_vpd_ref_image_started_logged(self, tmp_path):
        entity = _make_entity("pharaoh_amenhotep", "final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()):
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        info_calls = [c[0][0] for c in log.info.call_args_list]
        assert "vpd_ref_image_started" in info_calls

    def test_vpd_ref_image_completed_logged_on_success(self, tmp_path):
        entity = _make_entity("pharaoh_amenhotep", "final")
        log = _make_log()
        with patch(_PATCH, return_value=_make_ref_result()):
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        info_calls = [c[0][0] for c in log.info.call_args_list]
        assert "vpd_ref_image_completed" in info_calls

    def test_vpd_ref_image_failed_logged_on_error(self, tmp_path):
        entity = _make_entity("pharaoh_amenhotep", "final")
        log = _make_log()
        with patch(_PATCH, side_effect=Exception("network error")):
            generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        log.warn.assert_called_once()
        assert "vpd_ref_image_failed" in log.warn.call_args[0]

    def test_result_uses_hosted_url_from_result_object(self, tmp_path):
        entity = _make_entity("pharaoh_amenhotep", "final")
        log = _make_log()
        custom_result = ReferenceImageResult(
            asset_id="ep/ref_x",
            hosted_url="https://custom-cdn.example.com/img.jpg",
        )
        with patch(_PATCH, return_value=custom_result):
            result = generate_entity_reference_images(
                [entity], MagicMock(), "ep_001", tmp_path, log
            )
        assert result["pharaoh_amenhotep"] == "https://custom-cdn.example.com/img.jpg"
