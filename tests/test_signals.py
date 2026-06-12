"""
tests/test_signals.py — Phase 5B: Signal Ingestion Schemas

Coverage:
    TestIngestSignalFields          — required fields in returned signal_record
    TestIngestSignalMetrics         — metrics extracted from analytics_snapshot
    TestIngestSignalNormalization   — normalized_metrics computed correctly
    TestIngestSignalDataQuality     — data_quality inferred correctly
    TestIngestSignalFileWritten     — signal_record file written to output_dir
    TestIngestSignalIdempotency     — second ingestion overwrites cleanly
    TestIngestSignalEmpty           — works with no analytics_snapshot
    TestListSignalRecords           — list_signal_records returns correct records
    TestSignalId                    — generate_signal_id is deterministic
    TestSignalSchema                — signal_record passes schema validation
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.identifiers import (
    METADATA_VERSION,
    generate_signal_id,
)
from hypogeum.signals import ingest_signal, list_signal_records
from hypogeum.schemas import validate, SchemaError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot(
    episode_id: str = "ep_000",
    run_id: str = "run-abc-001",
    youtube_video_id: str = "vid_abc123",
    status: str = "captured",
    window_days: int = 7,
    views: int = 1500,
    watch_minutes: float = 3200.5,
    avg_duration: float = 128.2,
    likes: int = 42,
    comments: int = 7,
    shares: int = 3,
    subs_gained: int = 12,
) -> dict:
    return {
        "episode_id": episode_id,
        "youtube_video_id": youtube_video_id,
        "run_id": run_id,
        "published_at": "2026-06-10T12:00:00+00:00",
        "window_days": window_days,
        "fetched_at": "2026-06-12T09:00:00+00:00",
        "status": status,
        "metrics": {
            "views": views,
            "estimatedMinutesWatched": watch_minutes,
            "averageViewDuration": avg_duration,
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "subscribersGained": subs_gained,
        },
        "metadata_version": "v1",
    }


def _error_snapshot(run_id: str = "run-abc-001") -> dict:
    return {
        "episode_id": "ep_000",
        "youtube_video_id": "vid_abc123",
        "run_id": run_id,
        "published_at": "",
        "window_days": 7,
        "status": "error",
        "error": "quota_exceeded",
        "metrics": {},
        "metadata_version": "v1",
    }


# ---------------------------------------------------------------------------
# TestIngestSignalFields
# ---------------------------------------------------------------------------

class TestIngestSignalFields:
    REQUIRED_FIELDS = [
        "signal_id", "episode_id", "run_id", "ingested_at",
        "metadata_version", "youtube_video_id", "published_at",
        "window_days", "data_quality", "metrics",
        "normalized_metrics", "signal_summary",
    ]

    def test_all_required_fields_present(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        for f in self.REQUIRED_FIELDS:
            assert f in record, f"Missing field: {f}"

    def test_metadata_version_is_v1(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        assert record["metadata_version"] == METADATA_VERSION

    def test_episode_id_correct(self, tmp_path):
        record = ingest_signal("ep_042", _snapshot(), output_dir=tmp_path)
        assert record["episode_id"] == "ep_042"

    def test_run_id_from_snapshot(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(run_id="run-xyz-999"), output_dir=tmp_path)
        assert record["run_id"] == "run-xyz-999"

    def test_youtube_video_id_correct(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(youtube_video_id="abc123"), output_dir=tmp_path)
        assert record["youtube_video_id"] == "abc123"

    def test_window_days_correct(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(window_days=14), output_dir=tmp_path)
        assert record["window_days"] == 14

    def test_signal_summary_has_required_keys(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        summary = record["signal_summary"]
        assert "metric_count" in summary
        assert "data_quality" in summary
        assert "has_views" in summary


# ---------------------------------------------------------------------------
# TestIngestSignalMetrics
# ---------------------------------------------------------------------------

class TestIngestSignalMetrics:
    def test_views_extracted(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(views=2000), output_dir=tmp_path)
        assert record["metrics"]["views"] == 2000

    def test_all_known_metrics_extracted(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        expected_keys = {
            "views", "estimatedMinutesWatched", "averageViewDuration",
            "likes", "comments", "shares", "subscribersGained",
        }
        assert set(record["metrics"].keys()) == expected_keys

    def test_unknown_metrics_excluded(self, tmp_path):
        snap = _snapshot()
        snap["metrics"]["unknownMetric"] = 999
        record = ingest_signal("ep_000", snap, output_dir=tmp_path)
        assert "unknownMetric" not in record["metrics"]

    def test_empty_metrics_snapshot(self, tmp_path):
        snap = _snapshot()
        snap["metrics"] = {}
        record = ingest_signal("ep_000", snap, output_dir=tmp_path)
        assert record["metrics"] == {}

    def test_metric_count_in_summary(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        assert record["signal_summary"]["metric_count"] == 7

    def test_has_views_true_when_views_positive(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(views=100), output_dir=tmp_path)
        assert record["signal_summary"]["has_views"] is True

    def test_has_views_false_when_views_zero(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(views=0), output_dir=tmp_path)
        assert record["signal_summary"]["has_views"] is False


# ---------------------------------------------------------------------------
# TestIngestSignalNormalization
# ---------------------------------------------------------------------------

class TestIngestSignalNormalization:
    def test_views_per_day_computed(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(views=700, window_days=7), output_dir=tmp_path)
        assert record["normalized_metrics"]["views_per_day"] == pytest.approx(100.0)

    def test_watch_minutes_per_day_computed(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(watch_minutes=350.0, window_days=7), output_dir=tmp_path)
        assert record["normalized_metrics"]["watch_minutes_per_day"] == pytest.approx(50.0)

    def test_normalization_empty_when_window_zero(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(window_days=0), output_dir=tmp_path)
        assert record["normalized_metrics"] == {}

    def test_normalization_empty_when_no_metrics(self, tmp_path):
        snap = _snapshot()
        snap["metrics"] = {}
        snap["status"] = "error"
        record = ingest_signal("ep_000", snap, output_dir=tmp_path)
        assert record["normalized_metrics"] == {}

    def test_views_per_day_14_day_window(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(views=1400, window_days=14), output_dir=tmp_path)
        assert record["normalized_metrics"]["views_per_day"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TestIngestSignalDataQuality
# ---------------------------------------------------------------------------

class TestIngestSignalDataQuality:
    def test_captured_when_status_captured_with_metrics(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(status="captured"), output_dir=tmp_path)
        assert record["data_quality"] == "captured"

    def test_error_when_status_error(self, tmp_path):
        record = ingest_signal("ep_000", _error_snapshot(), output_dir=tmp_path)
        assert record["data_quality"] == "error"

    def test_no_data_when_captured_but_empty_metrics(self, tmp_path):
        snap = _snapshot(status="captured")
        snap["metrics"] = {}
        record = ingest_signal("ep_000", snap, output_dir=tmp_path)
        assert record["data_quality"] == "no_data"

    def test_no_data_when_empty_snapshot(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        assert record["data_quality"] == "no_data"

    def test_data_quality_matches_summary(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        assert record["data_quality"] == record["signal_summary"]["data_quality"]


# ---------------------------------------------------------------------------
# TestIngestSignalFileWritten
# ---------------------------------------------------------------------------

class TestIngestSignalFileWritten:
    def test_file_created(self, tmp_path):
        ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        assert (tmp_path / "signal_record_ep_000.json").exists()

    def test_file_parseable(self, tmp_path):
        ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        content = json.loads((tmp_path / "signal_record_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"

    def test_file_named_by_episode_id(self, tmp_path):
        ingest_signal("ep_042", _snapshot(), output_dir=tmp_path)
        assert (tmp_path / "signal_record_ep_042.json").exists()

    def test_file_content_matches_returned_dict(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        content = json.loads((tmp_path / "signal_record_ep_000.json").read_text())
        assert content["signal_id"] == record["signal_id"]
        assert content["metrics"] == record["metrics"]

    def test_file_written_when_no_snapshot(self, tmp_path):
        ingest_signal("ep_000", output_dir=tmp_path)
        assert (tmp_path / "signal_record_ep_000.json").exists()


# ---------------------------------------------------------------------------
# TestIngestSignalIdempotency
# ---------------------------------------------------------------------------

class TestIngestSignalIdempotency:
    def test_second_ingestion_overwrites(self, tmp_path):
        ingest_signal("ep_000", _snapshot(views=100), output_dir=tmp_path)
        ingest_signal("ep_000", _snapshot(views=999), output_dir=tmp_path)
        content = json.loads((tmp_path / "signal_record_ep_000.json").read_text())
        assert content["metrics"]["views"] == 999

    def test_same_run_id_produces_same_signal_id(self, tmp_path):
        r1 = ingest_signal("ep_000", _snapshot(run_id="run-fixed"), output_dir=tmp_path)
        r2 = ingest_signal("ep_000", _snapshot(run_id="run-fixed"), output_dir=tmp_path)
        assert r1["signal_id"] == r2["signal_id"]


# ---------------------------------------------------------------------------
# TestIngestSignalEmpty
# ---------------------------------------------------------------------------

class TestIngestSignalEmpty:
    def test_no_snapshot_produces_valid_record(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        assert record["episode_id"] == "ep_000"
        assert record["metrics"] == {}
        assert record["data_quality"] == "no_data"

    def test_no_snapshot_run_id_empty(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        assert record["run_id"] == ""

    def test_no_snapshot_has_views_false(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        assert record["signal_summary"]["has_views"] is False

    def test_no_snapshot_normalized_metrics_empty(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        assert record["normalized_metrics"] == {}


# ---------------------------------------------------------------------------
# TestListSignalRecords
# ---------------------------------------------------------------------------

class TestListSignalRecords:
    def test_returns_empty_when_dir_absent(self, tmp_path):
        result = list_signal_records(signals_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_all_records(self, tmp_path):
        ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        ingest_signal("ep_001", _snapshot(), output_dir=tmp_path)
        records = list_signal_records(signals_dir=tmp_path)
        assert len(records) == 2

    def test_records_have_episode_id(self, tmp_path):
        ingest_signal("ep_007", _snapshot(), output_dir=tmp_path)
        records = list_signal_records(signals_dir=tmp_path)
        assert records[0]["episode_id"] == "ep_007"

    def test_records_sorted_by_filename(self, tmp_path):
        ingest_signal("ep_002", _snapshot(), output_dir=tmp_path)
        ingest_signal("ep_001", _snapshot(), output_dir=tmp_path)
        records = list_signal_records(signals_dir=tmp_path)
        assert records[0]["episode_id"] == "ep_001"
        assert records[1]["episode_id"] == "ep_002"


# ---------------------------------------------------------------------------
# TestSignalId
# ---------------------------------------------------------------------------

class TestSignalId:
    def test_deterministic(self):
        id1 = generate_signal_id("ep_000", "run-abc")
        id2 = generate_signal_id("ep_000", "run-abc")
        assert id1 == id2

    def test_different_run_different_id(self):
        id1 = generate_signal_id("ep_000", "run-abc")
        id2 = generate_signal_id("ep_000", "run-xyz")
        assert id1 != id2

    def test_different_episode_different_id(self):
        id1 = generate_signal_id("ep_000", "run-abc")
        id2 = generate_signal_id("ep_001", "run-abc")
        assert id1 != id2

    def test_id_is_14_chars(self):
        sid = generate_signal_id("ep_000", "run-abc")
        assert len(sid) == 14

    def test_id_is_hex(self):
        sid = generate_signal_id("ep_000", "run-abc")
        int(sid, 16)

    def test_signal_id_differs_from_kg_seed_id(self):
        from hypogeum.identifiers import generate_kg_seed_id
        sid = generate_signal_id("ep_000", "run-abc")
        kid = generate_kg_seed_id("ep_000", "run-abc")
        assert sid != kid


# ---------------------------------------------------------------------------
# TestSignalSchema
# ---------------------------------------------------------------------------

class TestSignalSchema:
    def test_captured_record_passes_schema(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        validate("signal_record", record)  # must not raise

    def test_error_record_passes_schema(self, tmp_path):
        record = ingest_signal("ep_000", _error_snapshot(), output_dir=tmp_path)
        validate("signal_record", record)

    def test_empty_record_passes_schema(self, tmp_path):
        record = ingest_signal("ep_000", output_dir=tmp_path)
        validate("signal_record", record)

    def test_schema_rejects_missing_signal_id(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        del record["signal_id"]
        with pytest.raises(SchemaError):
            validate("signal_record", record)

    def test_schema_rejects_invalid_data_quality(self, tmp_path):
        record = ingest_signal("ep_000", _snapshot(), output_dir=tmp_path)
        record["data_quality"] = "unknown_value"
        with pytest.raises(SchemaError):
            validate("signal_record", record)
