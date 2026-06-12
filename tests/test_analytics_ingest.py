"""
tests/test_analytics_ingest.py — Phase 3C: Analytics Ingestion v1 test suite

Coverage:
    TestListPending              — delegates to analytics_pending, returns correct records
    TestFetchAndStoreNoClient    — no client → error snapshot, pending marked error
    TestFetchAndStoreWithClient  — mock client → snapshot written, pending marked captured
    TestSnapshotFields           — required fields in analytics_snapshot
    TestIdempotency              — second fetch overwrites snapshot cleanly
    TestPendingStatusTransitions — pending→captured, pending→error transitions
    TestMissingPendingFile       — works without a pending file present
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hypogeum.analytics_ingest import fetch_and_store_analytics, list_pending
from hypogeum.identifiers import METADATA_VERSION
from hypogeum.production_metadata import write_analytics_pending


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_yt_client(views=1500, watch_minutes=4200, avg_view_duration=168,
                    likes=42, comments=7, shares=3, subs_gained=15):
    """Return a mock YouTube Analytics client that returns predictable data."""
    client = MagicMock()
    client.reports.return_value.query.return_value.execute.return_value = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "averageViewDuration"},
            {"name": "likes"},
            {"name": "comments"},
            {"name": "shares"},
            {"name": "subscribersGained"},
        ],
        "rows": [
            ["dQw4w9WgXcQ", views, watch_minutes, avg_view_duration,
             likes, comments, shares, subs_gained],
        ],
    }
    return client


# ---------------------------------------------------------------------------
# TestListPending
# ---------------------------------------------------------------------------

class TestListPending:
    def test_returns_empty_when_dir_absent(self, tmp_path):
        result = list_pending(analytics_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_pending_records(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        write_analytics_pending("ep_001", "vid_002", "2026-06-13T10:00:00Z",
                                 output_dir=tmp_path)
        results = list_pending(analytics_dir=tmp_path)
        assert len(results) == 2

    def test_excludes_captured_records(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        # Mark as captured manually
        path = tmp_path / "analytics_pending_ep_000.json"
        record = json.loads(path.read_text())
        record["status"] = "captured"
        path.write_text(json.dumps(record))
        results = list_pending(analytics_dir=tmp_path)
        assert len(results) == 0

    def test_records_have_episode_id(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        results = list_pending(analytics_dir=tmp_path)
        assert results[0]["episode_id"] == "ep_000"


# ---------------------------------------------------------------------------
# TestFetchAndStoreNoClient
# ---------------------------------------------------------------------------

class TestFetchAndStoreNoClient:
    def test_returns_dict(self, tmp_path):
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=None,
            analytics_dir=tmp_path,
        )
        assert isinstance(result, dict)

    def test_status_is_error(self, tmp_path):
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=None,
            analytics_dir=tmp_path,
        )
        assert result["status"] == "error"

    def test_error_field_present(self, tmp_path):
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=None,
            analytics_dir=tmp_path,
        )
        assert "error" in result

    def test_snapshot_file_written_on_error(self, tmp_path):
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=None,
            analytics_dir=tmp_path,
        )
        assert (tmp_path / "analytics_snapshot_ep_000.json").exists()

    def test_pending_record_marked_error(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=None,
            analytics_dir=tmp_path,
        )
        pending = json.loads((tmp_path / "analytics_pending_ep_000.json").read_text())
        assert pending["status"] == "error"


# ---------------------------------------------------------------------------
# TestFetchAndStoreWithClient
# ---------------------------------------------------------------------------

class TestFetchAndStoreWithClient:
    def test_returns_dict(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert isinstance(result, dict)

    def test_status_is_captured(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["status"] == "captured"

    def test_metrics_populated(self, tmp_path):
        client = _make_yt_client(views=1500, likes=42)
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["metrics"].get("views") == 1500
        assert result["metrics"].get("likes") == 42

    def test_snapshot_file_written(self, tmp_path):
        client = _make_yt_client()
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert (tmp_path / "analytics_snapshot_ep_000.json").exists()

    def test_snapshot_file_parseable(self, tmp_path):
        client = _make_yt_client()
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        content = json.loads((tmp_path / "analytics_snapshot_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"
        assert content["status"] == "captured"

    def test_pending_record_marked_captured(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        client = _make_yt_client()
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        pending = json.loads((tmp_path / "analytics_pending_ep_000.json").read_text())
        assert pending["status"] == "captured"

    def test_yt_client_called_with_correct_video_id(self, tmp_path):
        client = _make_yt_client()
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        call_kwargs = client.reports.return_value.query.call_args[1]
        assert "dQw4w9WgXcQ" in call_kwargs.get("filters", "")

    def test_run_id_from_pending_in_snapshot(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 run_id="run-001", output_dir=tmp_path)
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["run_id"] == "run-001"


# ---------------------------------------------------------------------------
# TestSnapshotFields
# ---------------------------------------------------------------------------

class TestSnapshotFields:
    def test_required_fields_present(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        for field in (
            "episode_id", "youtube_video_id", "run_id",
            "window_days", "fetched_at", "status", "metrics",
            "metadata_version",
        ):
            assert field in result, f"Missing field: {field}"

    def test_metadata_version_is_v1(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["metadata_version"] == METADATA_VERSION

    def test_episode_id_in_snapshot(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["episode_id"] == "ep_000"

    def test_youtube_video_id_in_snapshot(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["youtube_video_id"] == "dQw4w9WgXcQ"

    def test_window_days_in_snapshot(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            window_days=14,
            analytics_dir=tmp_path,
        )
        assert result["window_days"] == 14


# ---------------------------------------------------------------------------
# TestIdempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_fetch_overwrites_snapshot(self, tmp_path):
        client1 = _make_yt_client(views=100)
        fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client1,
            analytics_dir=tmp_path,
        )
        client2 = _make_yt_client(views=999)
        result2 = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client2,
            analytics_dir=tmp_path,
        )
        # Latest data should be in the snapshot
        assert result2["metrics"].get("views") == 999

    def test_second_fetch_snapshot_is_valid(self, tmp_path):
        client = _make_yt_client()
        fetch_and_store_analytics("ep_000", "dQw4w9WgXcQ", yt_client=client,
                                   analytics_dir=tmp_path)
        fetch_and_store_analytics("ep_000", "dQw4w9WgXcQ", yt_client=client,
                                   analytics_dir=tmp_path)
        content = json.loads((tmp_path / "analytics_snapshot_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"
        assert content["status"] == "captured"

    def test_fetch_without_pending_file_still_works(self, tmp_path):
        """fetch_and_store_analytics works even if no analytics_pending file exists."""
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_000", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["status"] == "captured"


# ---------------------------------------------------------------------------
# TestPendingStatusTransitions
# ---------------------------------------------------------------------------

class TestPendingStatusTransitions:
    def test_pending_to_captured(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        client = _make_yt_client()
        fetch_and_store_analytics("ep_000", "dQw4w9WgXcQ", yt_client=client,
                                   analytics_dir=tmp_path)
        pending = json.loads((tmp_path / "analytics_pending_ep_000.json").read_text())
        assert pending["status"] == "captured"

    def test_pending_to_error_on_api_failure(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        client = MagicMock()
        client.reports.return_value.query.return_value.execute.side_effect = RuntimeError("quota exceeded")
        fetch_and_store_analytics("ep_000", "dQw4w9WgXcQ", yt_client=client,
                                   analytics_dir=tmp_path)
        pending = json.loads((tmp_path / "analytics_pending_ep_000.json").read_text())
        assert pending["status"] == "error"

    def test_list_pending_excludes_captured(self, tmp_path):
        write_analytics_pending("ep_000", "dQw4w9WgXcQ", "2026-06-12T10:00:00Z",
                                 output_dir=tmp_path)
        client = _make_yt_client()
        fetch_and_store_analytics("ep_000", "dQw4w9WgXcQ", yt_client=client,
                                   analytics_dir=tmp_path)
        # After capture, list_pending should return empty
        results = list_pending(analytics_dir=tmp_path)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# TestMissingPendingFile
# ---------------------------------------------------------------------------

class TestMissingPendingFile:
    def test_works_without_pending_file(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_999", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["episode_id"] == "ep_999"
        assert result["status"] == "captured"

    def test_run_id_empty_when_no_pending_file(self, tmp_path):
        client = _make_yt_client()
        result = fetch_and_store_analytics(
            "ep_999", "dQw4w9WgXcQ",
            yt_client=client,
            analytics_dir=tmp_path,
        )
        assert result["run_id"] == ""
