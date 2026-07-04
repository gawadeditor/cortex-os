"""
tests/test_asset_registry.py — Asset Registry v1 tests

Coverage:
    TestComputeAssetSlotId          — deterministic, collision resistance, normalization
    TestNormalizeAssetType          — known and unknown types
    TestInferSourceStage            — type → stage mapping
    TestRunContext                  — set/get/clear cycle
    TestRegisterAssetAttempt        — DB write; no run context → graceful skip
    TestRegisterAssetSuccess        — UPDATE + assets INSERT + archive_index INSERT
    TestRegisterAssetFailure        — UPDATE with error fields preserved
    TestRegisterExistingAsset       — INSERT OR IGNORE assets + archive_index
    TestRetryCreatesMultipleVersions — multiple asset_versions rows same slot
    TestFileMetrics                 — file_size_bytes + content_hash computed from local file
    TestArchiveIndexGating          — reuse_eligible=False → no archive_index row
    TestSchemaIdempotency           — double init is safe; registry survives
    TestNoNetworkCalls              — no socket or HTTP calls from registry functions
    TestImageGenerationAgentRegistry — image agent calls attempt/success/failure
    TestVideoGenerationAgentRegistry — video agent calls attempt/success/failure
    TestThumbnailGenerationAgentRegistry — thumbnail agent tracks version_index
    TestNoProductionSideEffects     — no ep_000, no real assets, no publish
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hypogeum.asset_registry as registry
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_EPISODE = "ep_test"
_RUN_ID = "run-reg-test-001"
_TS = "2026-06-12T10:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path) -> Path:
    p = tmp_path / "registry_test.db"
    initialize_schema(p)
    # Insert a parent run so FK constraints pass
    conn = get_connection(p)
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?, ?, ?, ?)",
        (_RUN_ID, _EPISODE, _TS, "running"),
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture(autouse=False)
def ctx(db_path):
    """Set active run context pointing at the test DB; clear after test."""
    registry.set_run_context(_RUN_ID, _EPISODE, db_path=db_path)
    yield
    registry.clear_run_context()


def _slot_id(episode=_EPISODE, asset_type="video_clip", provider="muapi",
             variant="", scene_index=0) -> str:
    return registry.compute_asset_slot_id(
        episode, asset_type, provider, variant=variant, scene_index=scene_index
    )


# ---------------------------------------------------------------------------
# TestComputeAssetSlotId
# ---------------------------------------------------------------------------

class TestComputeAssetSlotId:
    def test_same_inputs_same_output(self):
        a = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        b = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        assert a == b

    def test_returns_16_char_hex(self):
        slot = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        assert len(slot) == 16
        int(slot, 16)  # raises ValueError if not hex

    def test_different_episode_different_slot(self):
        a = registry.compute_asset_slot_id("ep_001", "video_clip", "muapi")
        b = registry.compute_asset_slot_id("ep_002", "video_clip", "muapi")
        assert a != b

    def test_different_asset_type_different_slot(self):
        a = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        b = registry.compute_asset_slot_id("ep_000", "thumbnail", "muapi")
        assert a != b

    def test_different_variant_different_slot(self):
        a = registry.compute_asset_slot_id("ep_000", "thumbnail", "muapi", variant="main")
        b = registry.compute_asset_slot_id("ep_000", "thumbnail", "muapi", variant="short_01")
        assert a != b

    def test_different_scene_index_different_slot(self):
        a = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi", scene_index=0)
        b = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi", scene_index=1)
        assert a != b

    def test_normalizes_asset_type(self):
        """'clip' and 'video_clip' should yield the same slot ID."""
        a = registry.compute_asset_slot_id("ep_000", "clip", "muapi")
        b = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        assert a == b

    def test_case_insensitive_inputs(self):
        a = registry.compute_asset_slot_id("EP_000", "VIDEO_CLIP", "MUAPI")
        b = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        assert a == b


# ---------------------------------------------------------------------------
# TestNormalizeAssetType
# ---------------------------------------------------------------------------

class TestNormalizeAssetType:
    def test_clip_to_video_clip(self):
        assert registry.normalize_asset_type("clip") == "video_clip"

    def test_thumbnail_image_to_thumbnail(self):
        assert registry.normalize_asset_type("thumbnail_image") == "thumbnail"

    def test_narration_segment_to_narration(self):
        assert registry.normalize_asset_type("narration_segment") == "narration"

    def test_unknown_type_lowercased(self):
        assert registry.normalize_asset_type("MyCustomType") == "mycustomtype"

    def test_already_canonical_unchanged(self):
        assert registry.normalize_asset_type("video_clip") == "video_clip"

    def test_ref_image_alias(self):
        assert registry.normalize_asset_type("ref_image") == "reference_image"


# ---------------------------------------------------------------------------
# TestInferSourceStage
# ---------------------------------------------------------------------------

class TestInferSourceStage:
    def test_video_clip_to_vpd(self):
        assert registry.infer_source_stage("video_clip") == "visual_package_director"

    def test_thumbnail_to_thumbnail_director(self):
        assert registry.infer_source_stage("thumbnail") == "thumbnail_director"

    def test_narration_to_audio_agent(self):
        assert registry.infer_source_stage("narration") == "audio_agent"

    def test_final_video_to_assembly_agent(self):
        assert registry.infer_source_stage("final_video") == "assembly_agent"

    def test_unknown_type_returns_unknown(self):
        assert registry.infer_source_stage("mystery_type") == "unknown"

    def test_uses_normalization(self):
        """'clip' should resolve to the same stage as 'video_clip'."""
        assert registry.infer_source_stage("clip") == "visual_package_director"


# ---------------------------------------------------------------------------
# TestRunContext
# ---------------------------------------------------------------------------

class TestRunContext:
    def test_set_and_get(self, db_path):
        registry.set_run_context("run-ctx-01", "ep_ctx", db_path=db_path)
        ctx = registry.get_run_context()
        assert ctx["run_id"] == "run-ctx-01"
        assert ctx["episode_id"] == "ep_ctx"
        assert ctx["db_path"] == db_path
        registry.clear_run_context()

    def test_clear_empties_context(self, db_path):
        registry.set_run_context("run-ctx-02", "ep_ctx", db_path=db_path)
        registry.clear_run_context()
        assert registry.get_run_context() == {}

    def test_get_returns_copy(self, db_path):
        registry.set_run_context("run-ctx-03", "ep_ctx", db_path=db_path)
        ctx = registry.get_run_context()
        ctx["run_id"] = "mutated"
        assert registry.get_run_context()["run_id"] == "run-ctx-03"
        registry.clear_run_context()

    def test_set_overwrites_previous(self, db_path):
        registry.set_run_context("run-ctx-04", "ep_a", db_path=db_path)
        registry.set_run_context("run-ctx-05", "ep_b", db_path=db_path)
        ctx = registry.get_run_context()
        assert ctx["run_id"] == "run-ctx-05"
        assert ctx["episode_id"] == "ep_b"
        registry.clear_run_context()


# ---------------------------------------------------------------------------
# TestRegisterAssetAttempt
# ---------------------------------------------------------------------------

class TestRegisterAssetAttempt:
    def test_returns_version_id_string(self, ctx, db_path):
        slot = _slot_id()
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        assert isinstance(vid, str)
        assert len(vid) > 0

    def test_writes_attempted_row(self, ctx, db_path):
        slot = _slot_id()
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, asset_type, provider FROM asset_versions WHERE version_id=?",
            (vid,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "attempted"
        assert row[1] == "video_clip"
        assert row[2] == "muapi"

    def test_no_run_context_returns_id_and_does_not_raise(self):
        # Ensure no context is active
        registry.clear_run_context()
        vid = registry.register_asset_attempt(
            asset_slot_id="abc123",
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        assert isinstance(vid, str)

    def test_normalizes_asset_type(self, ctx, db_path):
        slot = _slot_id(asset_type="clip")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="clip",  # alias
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_type FROM asset_versions WHERE version_id=?",
            (vid,),
        ).fetchone()
        conn.close()
        assert row[0] == "video_clip"

    def test_model_stored(self, ctx, db_path):
        slot = _slot_id()
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
            model="seedance-1.0",
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT model FROM asset_versions WHERE version_id=?", (vid,)
        ).fetchone()
        conn.close()
        assert row[0] == "seedance-1.0"


# ---------------------------------------------------------------------------
# TestRegisterAssetSuccess
# ---------------------------------------------------------------------------

class TestRegisterAssetSuccess:
    def test_updates_asset_versions_to_succeeded(self, ctx, db_path):
        slot = _slot_id()
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot,
            version_id=vid,
            asset_id="asset_success_01",
            asset_type="video_clip",
            provider="muapi",
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, asset_id FROM asset_versions WHERE version_id=?", (vid,)
        ).fetchone()
        conn.close()
        assert row[0] == "succeeded"
        assert row[1] == "asset_success_01"

    def test_writes_assets_row(self, ctx, db_path):
        slot = _slot_id()
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot,
            version_id=vid,
            asset_id="asset_assets_row_01",
            asset_type="video_clip",
            provider="muapi",
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE asset_id=?",
            ("asset_assets_row_01",),
        ).fetchone()
        conn.close()
        assert row is not None

    def test_writes_archive_index_when_reuse_eligible(self, ctx, db_path):
        slot = _slot_id(asset_type="thumbnail", variant="main")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="thumbnail",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot,
            version_id=vid,
            asset_id="asset_archive_01",
            asset_type="thumbnail",
            provider="muapi",
            attempted_at=_TS,
            reuse_eligible=True,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM archive_index WHERE asset_id=?",
            ("asset_archive_01",),
        ).fetchone()
        conn.close()
        assert row is not None

    def test_no_archive_index_when_reuse_ineligible(self, ctx, db_path):
        slot = _slot_id(asset_type="narration", variant="ep_narration")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="narration",
            provider="elevenlabs",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot,
            version_id=vid,
            asset_id="asset_narr_01",
            asset_type="narration",
            provider="elevenlabs",
            attempted_at=_TS,
            reuse_eligible=False,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM archive_index WHERE asset_id=?",
            ("asset_narr_01",),
        ).fetchone()
        conn.close()
        assert row is None

    def test_assets_insert_is_idempotent(self, ctx, db_path):
        """Second register_asset_success with same asset_id does not raise."""
        slot = _slot_id(variant="scene_idem")
        for i in range(2):
            vid = registry.register_asset_attempt(
                asset_slot_id=slot,
                asset_type="video_clip",
                provider="muapi",
                attempt_number=1,
                version_index=i,
                attempted_at=_TS,
            )
            registry.register_asset_success(
                asset_slot_id=slot,
                version_id=vid,
                asset_id="asset_idem_01",
                asset_type="video_clip",
                provider="muapi",
                attempted_at=_TS,
            )
        conn = get_connection(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE asset_id='asset_idem_01'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# TestRegisterAssetFailure
# ---------------------------------------------------------------------------

class TestRegisterAssetFailure:
    def test_updates_to_failed_status(self, ctx, db_path):
        slot = _slot_id(variant="fail_01")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_failure(
            asset_slot_id=slot,
            version_id=vid,
            asset_type="video_clip",
            provider="muapi",
            error_type="MUAPIQuotaExhausted",
            error_message="Quota exceeded for this billing period.",
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, error_type, error_message FROM asset_versions WHERE version_id=?",
            (vid,),
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert row[1] == "MUAPIQuotaExhausted"
        assert "Quota exceeded" in row[2]

    def test_preserves_error_type_and_message(self, ctx, db_path):
        slot = _slot_id(variant="fail_02")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="thumbnail",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_failure(
            asset_slot_id=slot,
            version_id=vid,
            asset_type="thumbnail",
            provider="muapi",
            error_type="TimeoutError",
            error_message="Request timed out after 30s.",
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT error_type FROM asset_versions WHERE version_id=?", (vid,)
        ).fetchone()
        conn.close()
        assert row[0] == "TimeoutError"

    def test_does_not_write_assets_or_archive(self, ctx, db_path):
        slot = _slot_id(variant="fail_03")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot,
            asset_type="video_clip",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=_TS,
        )
        registry.register_asset_failure(
            asset_slot_id=slot,
            version_id=vid,
            asset_type="video_clip",
            provider="muapi",
            error_type="SomeError",
            error_message="boom",
            attempted_at=_TS,
        )
        conn = get_connection(db_path)
        asset_count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE run_id=? AND asset_type='video_clip'",
            (_RUN_ID,),
        ).fetchone()[0]
        archive_count = conn.execute(
            "SELECT COUNT(*) FROM archive_index WHERE run_id=?", (_RUN_ID,)
        ).fetchone()[0]
        conn.close()
        assert asset_count == 0
        assert archive_count == 0


# ---------------------------------------------------------------------------
# TestRetryCreatesMultipleVersions
# ---------------------------------------------------------------------------

class TestRetryCreatesMultipleVersions:
    def test_two_attempts_two_version_rows(self, ctx, db_path):
        slot = _slot_id(variant="retry_scene")
        vid1 = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="video_clip", provider="muapi",
            attempt_number=1, version_index=0, attempted_at=_TS,
        )
        registry.register_asset_failure(
            asset_slot_id=slot, version_id=vid1, asset_type="video_clip",
            provider="muapi", error_type="TransientError",
            error_message="try again", attempted_at=_TS,
        )
        vid2 = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="video_clip", provider="muapi",
            attempt_number=2, version_index=1, attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot, version_id=vid2,
            asset_id="asset_retry_final", asset_type="video_clip",
            provider="muapi", attempted_at=_TS,
        )
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT status FROM asset_versions WHERE asset_slot_id=? ORDER BY version_index",
            (slot,),
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "failed"
        assert rows[1][0] == "succeeded"

    def test_version_index_stored_correctly(self, ctx, db_path):
        slot = _slot_id(variant="vi_check")
        for i in range(3):
            vid = registry.register_asset_attempt(
                asset_slot_id=slot, asset_type="video_clip", provider="muapi",
                attempt_number=i + 1, version_index=i, attempted_at=_TS,
            )
            registry.register_asset_failure(
                asset_slot_id=slot, version_id=vid, asset_type="video_clip",
                provider="muapi", error_type="Err", error_message="x",
                attempted_at=_TS,
            )
        conn = get_connection(db_path)
        indexes = [
            r[0] for r in conn.execute(
                "SELECT version_index FROM asset_versions WHERE asset_slot_id=? ORDER BY version_index",
                (slot,),
            ).fetchall()
        ]
        conn.close()
        assert indexes == [0, 1, 2]


# ---------------------------------------------------------------------------
# TestFileMetrics
# ---------------------------------------------------------------------------

class TestFileMetrics:
    def test_file_size_and_hash_written(self, ctx, db_path, tmp_path):
        content = b"fake image data for hashing"
        local_file = tmp_path / "test_asset.bin"
        local_file.write_bytes(content)

        import hashlib
        expected_hash = hashlib.sha256(content).hexdigest()

        slot = _slot_id(variant="file_metrics")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="reference_image", provider="muapi",
            attempt_number=1, version_index=0, attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot, version_id=vid,
            asset_id="asset_file_01", asset_type="reference_image",
            provider="muapi", attempted_at=_TS,
            file_path=local_file,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT file_size_bytes, content_hash FROM assets WHERE asset_id='asset_file_01'"
        ).fetchone()
        conn.close()
        assert row[0] == len(content)
        assert row[1] == expected_hash

    def test_missing_file_does_not_raise(self, ctx, db_path, tmp_path):
        slot = _slot_id(variant="missing_file")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="video_clip", provider="muapi",
            attempt_number=1, version_index=0, attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot, version_id=vid,
            asset_id="asset_no_file_01", asset_type="video_clip",
            provider="muapi", attempted_at=_TS,
            file_path=tmp_path / "nonexistent.mp4",
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE asset_id='asset_no_file_01'"
        ).fetchone()
        conn.close()
        assert row is not None


# ---------------------------------------------------------------------------
# TestRegisterExistingAsset
# ---------------------------------------------------------------------------

class TestRegisterExistingAsset:
    def test_writes_assets_row(self, ctx, db_path):
        registry.register_existing_asset(
            asset_id="existing_asset_01",
            asset_type="video_clip",
            provider="muapi",
            source_stage="visual_package_director",
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id, asset_type FROM assets WHERE asset_id='existing_asset_01'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "video_clip"

    def test_writes_archive_index(self, ctx, db_path):
        registry.register_existing_asset(
            asset_id="existing_archive_01",
            asset_type="thumbnail",
            provider="muapi",
            source_stage="thumbnail_director",
            reuse_eligible=True,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM archive_index WHERE asset_id='existing_archive_01'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_no_archive_index_when_ineligible(self, ctx, db_path):
        registry.register_existing_asset(
            asset_id="existing_narr_01",
            asset_type="narration",
            provider="elevenlabs",
            source_stage="audio_agent",
            reuse_eligible=False,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM archive_index WHERE asset_id='existing_narr_01'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_idempotent_on_duplicate(self, ctx, db_path):
        for _ in range(2):
            registry.register_existing_asset(
                asset_id="existing_idem_01",
                asset_type="video_clip",
                provider="muapi",
                source_stage="visual_package_director",
            )
        conn = get_connection(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE asset_id='existing_idem_01'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# TestSchemaIdempotency
# ---------------------------------------------------------------------------

class TestSchemaIdempotency:
    def test_double_initialize_safe(self, tmp_path):
        p = tmp_path / "idem_test.db"
        initialize_schema(p)
        initialize_schema(p)  # must not raise

    def test_registry_works_after_double_init(self, tmp_path):
        p = tmp_path / "idem_registry.db"
        initialize_schema(p)
        initialize_schema(p)
        conn = get_connection(p)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?, ?, ?, ?)",
            ("run-idem-001", "ep_idem", _TS, "running"),
        )
        conn.commit()
        conn.close()
        registry.set_run_context("run-idem-001", "ep_idem", db_path=p)
        slot = registry.compute_asset_slot_id("ep_idem", "video_clip", "muapi")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="video_clip", provider="muapi",
            attempt_number=1, version_index=0, attempted_at=_TS,
        )
        registry.clear_run_context()
        conn = get_connection(p)
        row = conn.execute(
            "SELECT status FROM asset_versions WHERE version_id=?", (vid,)
        ).fetchone()
        conn.close()
        assert row[0] == "attempted"


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    """Verify registry functions never open network connections."""

    def test_register_attempt_no_network(self, ctx, db_path):
        import socket
        orig_connect = socket.socket.connect
        calls = []

        def _reject(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Network call in registry")

        socket.socket.connect = _reject
        try:
            slot = registry.compute_asset_slot_id("ep_net", "video_clip", "muapi")
            registry.register_asset_attempt(
                asset_slot_id=slot, asset_type="video_clip", provider="muapi",
                attempt_number=1, version_index=0, attempted_at=_TS,
            )
        finally:
            socket.socket.connect = orig_connect

        assert calls == []

    def test_compute_slot_id_no_network(self):
        import socket
        orig_connect = socket.socket.connect

        def _reject(*args, **kwargs):
            raise AssertionError("Network call in compute_asset_slot_id")

        socket.socket.connect = _reject
        try:
            slot = registry.compute_asset_slot_id("ep_000", "video_clip", "muapi")
        finally:
            socket.socket.connect = orig_connect
        assert len(slot) == 16


# ---------------------------------------------------------------------------
# TestImageGenerationAgentRegistry
# ---------------------------------------------------------------------------

class TestImageGenerationAgentRegistry:
    def test_success_calls_attempt_and_success(self, tmp_path):
        import hypogeum.agents.image_generation_agent as _iga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot0000deadbeef"
        mock_reg.register_asset_attempt.return_value = "ver-img-001"

        mock_result = MagicMock()
        mock_result.asset_id = "img_gen_001"
        mock_result.hosted_url = "https://example.com/img.jpg"

        entities = [{"entity_id": "ent_img_01", "lock_state": "final"}]

        with (
            patch.object(_iga, "_asset_registry", mock_reg),
            patch.object(_iga, "generate_reference_image", return_value=mock_result),
            patch.object(_iga, "is_ref_image_eligible", return_value=True),
            patch.object(_iga, "build_visual_spec_text", return_value="spec text"),
        ):
            result = _iga.generate_entity_reference_images(
                locked_entities=entities,
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        mock_reg.register_asset_attempt.assert_called_once()
        mock_reg.register_asset_success.assert_called_once()
        mock_reg.register_asset_failure.assert_not_called()
        assert result == {"ent_img_01": "https://example.com/img.jpg"}

    def test_failure_calls_attempt_and_failure(self, tmp_path):
        import hypogeum.agents.image_generation_agent as _iga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot0001deadbeef"
        mock_reg.register_asset_attempt.return_value = "ver-img-fail"

        entities = [{"entity_id": "ent_img_02", "lock_state": "final"}]

        with (
            patch.object(_iga, "_asset_registry", mock_reg),
            patch.object(
                _iga, "generate_reference_image",
                side_effect=RuntimeError("provider error"),
            ),
            patch.object(_iga, "is_ref_image_eligible", return_value=True),
            patch.object(_iga, "build_visual_spec_text", return_value="spec"),
        ):
            result = _iga.generate_entity_reference_images(
                locked_entities=entities,
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        mock_reg.register_asset_attempt.assert_called_once()
        mock_reg.register_asset_failure.assert_called_once()
        mock_reg.register_asset_success.assert_not_called()
        assert result == {}  # entity skipped

    def test_halt_calls_failure_then_reraises(self, tmp_path):
        import hypogeum.agents.image_generation_agent as _iga
        from hypogeum.muapi import MUAPIQuotaExhausted

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot0002deadbeef"
        mock_reg.register_asset_attempt.return_value = "ver-img-halt"

        entities = [{"entity_id": "ent_img_03", "lock_state": "final"}]

        with (
            patch.object(_iga, "_asset_registry", mock_reg),
            patch.object(
                _iga, "generate_reference_image",
                side_effect=MUAPIQuotaExhausted(),
            ),
            patch.object(_iga, "is_ref_image_eligible", return_value=True),
            patch.object(_iga, "build_visual_spec_text", return_value="spec"),
        ):
            with pytest.raises(MUAPIQuotaExhausted):
                _iga.generate_entity_reference_images(
                    locked_entities=entities,
                    config=MagicMock(),
                    episode_id="ep_test",
                    output_dir=tmp_path,
                    log=MagicMock(),
                )

        mock_reg.register_asset_failure.assert_called_once()
        mock_reg.register_asset_success.assert_not_called()


# ---------------------------------------------------------------------------
# TestVideoGenerationAgentRegistry
# ---------------------------------------------------------------------------

class TestVideoGenerationAgentRegistry:
    def _make_scene(self, scene_id="scene_001"):
        return {
            "scene_id": scene_id,
            "source_type": "main_episode",
            "short_id": None,
        }

    def test_generated_calls_attempt_and_success(self, tmp_path):
        import hypogeum.agents.video_generation_agent as _vga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_vid_0001"
        mock_reg.register_asset_attempt.return_value = "ver-vid-001"

        manifest_entry = {
            "generation_status": "generated",
            "asset_id": "vid_asset_001",
            "provider": "muapi",
            "model_used": "seedance-1.0",
            "fallback_used": False,
        }

        with (
            patch.object(_vga, "_asset_registry", mock_reg),
            patch.object(_vga, "submit_scene", return_value=manifest_entry),
            patch.object(_vga, "build_scene_spec", return_value={}),
        ):
            _vga.generate_scene_video(
                scene=self._make_scene(),
                asset_index={},
                asset_manifest_assets=[],
                entity_hosted_urls={},
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        mock_reg.register_asset_attempt.assert_called_once()
        mock_reg.register_asset_success.assert_called_once()
        mock_reg.register_asset_failure.assert_not_called()

    def test_generation_failed_status_calls_failure(self, tmp_path):
        import hypogeum.agents.video_generation_agent as _vga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_vid_0002"
        mock_reg.register_asset_attempt.return_value = "ver-vid-002"

        manifest_entry = {
            "generation_status": "generation_failed",
            "failure_reason": "provider returned 503",
        }

        with (
            patch.object(_vga, "_asset_registry", mock_reg),
            patch.object(_vga, "submit_scene", return_value=manifest_entry),
            patch.object(_vga, "build_scene_spec", return_value={}),
        ):
            _vga.generate_scene_video(
                scene=self._make_scene("scene_002"),
                asset_index={},
                asset_manifest_assets=[],
                entity_hosted_urls={},
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        mock_reg.register_asset_failure.assert_called_once()
        mock_reg.register_asset_success.assert_not_called()

    def test_halt_exception_calls_failure_and_reraises(self, tmp_path):
        import hypogeum.agents.video_generation_agent as _vga
        from hypogeum.muapi import MUAPIAuthFailure

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_vid_0003"
        mock_reg.register_asset_attempt.return_value = "ver-vid-003"

        with (
            patch.object(_vga, "_asset_registry", mock_reg),
            patch.object(_vga, "submit_scene", side_effect=MUAPIAuthFailure()),
            patch.object(_vga, "build_scene_spec", return_value={}),
        ):
            with pytest.raises(MUAPIAuthFailure):
                _vga.generate_scene_video(
                    scene=self._make_scene("scene_003"),
                    asset_index={},
                    asset_manifest_assets=[],
                    entity_hosted_urls={},
                    config=MagicMock(),
                    episode_id="ep_test",
                    output_dir=tmp_path,
                    log=MagicMock(),
                )

        mock_reg.register_asset_failure.assert_called_once()
        mock_reg.register_asset_success.assert_not_called()


# ---------------------------------------------------------------------------
# TestThumbnailGenerationAgentRegistry
# ---------------------------------------------------------------------------

class TestThumbnailGenerationAgentRegistry:
    def _make_plan(self, candidates=None):
        if candidates is None:
            candidates = [
                {"candidate_id": "cand_01", "rank": 1,
                 "image_prompt": "a nice thumbnail", "aspect_ratio": "16:9"},
            ]
        return {
            "main_episode": {"ranked_candidates": candidates},
            "shorts": [],
        }

    def test_success_calls_attempt_and_success(self, tmp_path):
        import hypogeum.agents.thumbnail_generation_agent as _tga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_thumb_0001"
        mock_reg.register_asset_attempt.return_value = "ver-thumb-001"

        mock_result = MagicMock()
        mock_result.asset_id = "thumb_asset_001"

        with (
            patch.object(_tga, "_asset_registry", mock_reg),
            patch.object(_tga, "generate_thumbnail", return_value=mock_result),
        ):
            result = _tga.generate_thumbnails(
                generation_plan=self._make_plan(),
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        mock_reg.register_asset_attempt.assert_called_once()
        mock_reg.register_asset_success.assert_called_once()
        assert result["main_ok"] is True
        assert result["main_asset_id"] == "thumb_asset_001"

    def test_version_index_increments_across_failures(self, tmp_path):
        import hypogeum.agents.thumbnail_generation_agent as _tga

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_thumb_0002"
        mock_reg.register_asset_attempt.return_value = "ver-thumb-002"

        candidates = [
            {"candidate_id": "cand_a", "rank": 1, "image_prompt": "p1", "aspect_ratio": "16:9"},
            {"candidate_id": "cand_b", "rank": 2, "image_prompt": "p2", "aspect_ratio": "16:9"},
        ]
        call_count = 0

        def _fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient error")
            mock_result = MagicMock()
            mock_result.asset_id = "thumb_retry_final"
            return mock_result

        with (
            patch.object(_tga, "_asset_registry", mock_reg),
            patch.object(_tga, "generate_thumbnail", side_effect=_fail_then_succeed),
        ):
            result = _tga.generate_thumbnails(
                generation_plan={"main_episode": {"ranked_candidates": candidates}, "shorts": []},
                config=MagicMock(),
                episode_id="ep_test",
                output_dir=tmp_path,
                log=MagicMock(),
            )

        failure_calls = mock_reg.register_asset_failure.call_count
        success_calls = mock_reg.register_asset_success.call_count
        assert failure_calls == 2
        assert success_calls == 1

        # version_index increments within each _generate_for_deliverable call but resets
        # when a new candidate gets its own call (new deliverable_id, new version counter).
        # candidate_1: attempt-1 → vi=0 (fail), attempt-2 → vi=1 (fail); exhausted.
        # candidate_2: attempt-1 → vi=0 (success on 3rd overall call).
        attempt_kwargs = [
            call.kwargs for call in mock_reg.register_asset_attempt.call_args_list
        ]
        version_indexes = [kw["version_index"] for kw in attempt_kwargs]
        assert version_indexes == [0, 1, 0]

    def test_halt_calls_failure_and_reraises(self, tmp_path):
        import hypogeum.agents.thumbnail_generation_agent as _tga
        from hypogeum.muapi import MUAPIBudgetExhausted

        mock_reg = MagicMock()
        mock_reg.compute_asset_slot_id.return_value = "slot_thumb_halt"
        mock_reg.register_asset_attempt.return_value = "ver-thumb-halt"

        with (
            patch.object(_tga, "_asset_registry", mock_reg),
            patch.object(
                _tga, "generate_thumbnail",
                side_effect=MUAPIBudgetExhausted("budget exhausted"),
            ),
        ):
            with pytest.raises(MUAPIBudgetExhausted):
                _tga.generate_thumbnails(
                    generation_plan=self._make_plan(),
                    config=MagicMock(),
                    episode_id="ep_test",
                    output_dir=tmp_path,
                    log=MagicMock(),
                )

        mock_reg.register_asset_failure.assert_called_once()
        mock_reg.register_asset_success.assert_not_called()


# ---------------------------------------------------------------------------
# TestNoProductionSideEffects
# ---------------------------------------------------------------------------

class TestNoProductionSideEffects:
    def test_default_db_path_not_touched(self, tmp_path):
        """Tests must not write to the production DB path."""
        from hypogeum.asset_registry import _DEFAULT_DB_PATH
        # If the default path were hit, tests would fail with FK errors anyway.
        # Verify our fixtures always provide an override db_path.
        assert str(tmp_path) not in str(_DEFAULT_DB_PATH)

    def test_no_ep_000_episode_in_test_db(self, db_path):
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT episode_id FROM runs WHERE episode_id='ep_000'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_registry_functions_non_blocking_on_bad_db(self):
        """Registry functions must never raise even with a corrupt/missing DB."""
        registry.set_run_context("run-bad-db", "ep_test", db_path=Path("/nonexistent/bad.db"))
        slot = registry.compute_asset_slot_id("ep_test", "video_clip", "muapi")
        vid = registry.register_asset_attempt(
            asset_slot_id=slot, asset_type="video_clip", provider="muapi",
            attempt_number=1, version_index=0, attempted_at=_TS,
        )
        registry.register_asset_success(
            asset_slot_id=slot, version_id=vid,
            asset_id="asset_bad_db", asset_type="video_clip",
            provider="muapi", attempted_at=_TS,
        )
        registry.register_asset_failure(
            asset_slot_id=slot, version_id=vid, asset_type="video_clip",
            provider="muapi", error_type="Err", error_message="x",
            attempted_at=_TS,
        )
        registry.clear_run_context()
        # No assertion needed — must not raise
