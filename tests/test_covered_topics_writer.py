"""
tests/test_covered_topics_writer.py — Covered-Topics Writer test suite

Coverage:
  TestReadTopicMemory      — read_topic_memory() behavior
  TestRecordPublishedTopic — record_published_topic() behavior
  TestTopicAlreadyCovered  — topic_already_covered() behavior
  TestFileFormat           — output file structure and encoding
  TestCorruptionRecovery   — corruption detection, backup, and recovery
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.agents.covered_topics_writer import (
    record_published_topic,
    read_topic_memory,
    topic_already_covered,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project_root(tmp_path: Path) -> Path:
    """Create a tmp project root with memory/ directory."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _topics_path(project_root: Path) -> Path:
    return project_root / "memory" / "covered-topics.md"


def _write_topics(project_root: Path, content: str) -> None:
    _topics_path(project_root).write_text(content, encoding="utf-8")


def _read_topics(project_root: Path) -> str:
    return _topics_path(project_root).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestReadTopicMemory
# ---------------------------------------------------------------------------

class TestReadTopicMemory:
    def test_returns_empty_list_when_file_absent(self, tmp_path):
        root = _make_project_root(tmp_path)
        result = read_topic_memory(root)
        assert result == []

    def test_returns_empty_list_for_empty_file(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "")
        result = read_topic_memory(root)
        assert result == []

    def test_returns_empty_list_for_header_only(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "# Covered Topics\n")
        result = read_topic_memory(root)
        assert result == []

    def test_reads_single_entry(self, tmp_path):
        root = _make_project_root(tmp_path)
        content = (
            "# Covered Topics\n"
            "\n"
            "## ep_001\n"
            "- youtube_video_id: abc123\n"
            "- youtube_url: https://www.youtube.com/watch?v=abc123\n"
            "- published_at: 2026-06-08T10:00:00+00:00\n"
            "- status: completed\n"
        )
        _write_topics(root, content)
        result = read_topic_memory(root)
        assert len(result) == 1
        assert result[0]["episode_id"] == "ep_001"

    def test_reads_multiple_entries(self, tmp_path):
        root = _make_project_root(tmp_path)
        content = (
            "# Covered Topics\n"
            "\n## ep_001\n"
            "- youtube_video_id: vid1\n"
            "- status: completed\n"
            "\n## ep_002\n"
            "- youtube_video_id: vid2\n"
            "- status: completed\n"
        )
        _write_topics(root, content)
        result = read_topic_memory(root)
        assert len(result) == 2
        episode_ids = {e["episode_id"] for e in result}
        assert episode_ids == {"ep_001", "ep_002"}

    def test_entry_fields_parsed_correctly(self, tmp_path):
        root = _make_project_root(tmp_path)
        content = (
            "# Covered Topics\n"
            "\n## ep_xyz\n"
            "- youtube_video_id: VID_XYZ\n"
            "- youtube_url: https://www.youtube.com/watch?v=VID_XYZ\n"
            "- published_at: 2026-06-08T12:00:00+00:00\n"
            "- status: completed\n"
        )
        _write_topics(root, content)
        result = read_topic_memory(root)
        entry = result[0]
        assert entry["youtube_video_id"] == "VID_XYZ"
        assert entry["youtube_url"] == "https://www.youtube.com/watch?v=VID_XYZ"
        assert entry["published_at"] == "2026-06-08T12:00:00+00:00"
        assert entry["status"] == "completed"

    def test_partial_entry_returned_without_error(self, tmp_path):
        root = _make_project_root(tmp_path)
        # Entry with only episode_id — no fields
        content = "# Covered Topics\n\n## ep_partial\n"
        _write_topics(root, content)
        result = read_topic_memory(root)
        assert len(result) == 1
        assert result[0]["episode_id"] == "ep_partial"

    def test_corrupted_file_returns_empty_list(self, tmp_path):
        root = _make_project_root(tmp_path)
        # Write content with wrong header (corruption signal)
        _write_topics(root, "CORRUPTED DATA — not a covered topics file\n")
        result = read_topic_memory(root)
        assert result == []

    def test_never_raises(self, tmp_path):
        root = _make_project_root(tmp_path)
        # Simulate unreadable file by writing binary garbage
        _topics_path(root).write_bytes(b"\xff\xfe\x00\x01")
        result = read_topic_memory(root)  # must not raise
        assert result == []


# ---------------------------------------------------------------------------
# TestRecordPublishedTopic
# ---------------------------------------------------------------------------

class TestRecordPublishedTopic:
    def test_creates_file_when_absent(self, tmp_path):
        root = _make_project_root(tmp_path)
        result = record_published_topic("ep_new", "vid_new", "https://yt.be/vid_new", root)
        assert result is True
        assert _topics_path(root).exists()

    def test_returns_true_on_success(self, tmp_path):
        root = _make_project_root(tmp_path)
        result = record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        assert result is True

    def test_new_entry_readable_after_record(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        entries = read_topic_memory(root)
        assert len(entries) == 1
        assert entries[0]["episode_id"] == "ep_001"

    def test_appends_second_episode(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        record_published_topic("ep_002", "vid2", "https://yt.be/2", root)
        entries = read_topic_memory(root)
        assert len(entries) == 2
        episode_ids = {e["episode_id"] for e in entries}
        assert episode_ids == {"ep_001", "ep_002"}

    def test_updates_existing_episode_status(self, tmp_path):
        root = _make_project_root(tmp_path)
        content = (
            "# Covered Topics\n"
            "\n## ep_001\n"
            "- youtube_video_id: old_vid\n"
            "- youtube_url: https://yt.be/old\n"
            "- published_at: 2026-01-01T00:00:00+00:00\n"
            "- status: planned\n"
        )
        _write_topics(root, content)
        record_published_topic("ep_001", "new_vid", "https://yt.be/new", root)
        entries = read_topic_memory(root)
        assert len(entries) == 1
        assert entries[0]["status"] == "completed"
        assert entries[0]["youtube_video_id"] == "new_vid"
        assert entries[0]["youtube_url"] == "https://yt.be/new"

    def test_preserves_published_at_on_update(self, tmp_path):
        root = _make_project_root(tmp_path)
        original_ts = "2026-01-15T08:00:00+00:00"
        content = (
            "# Covered Topics\n"
            "\n## ep_001\n"
            f"- published_at: {original_ts}\n"
            "- status: planned\n"
        )
        _write_topics(root, content)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        entries = read_topic_memory(root)
        assert entries[0]["published_at"] == original_ts

    def test_deduplication_no_duplicate_entries(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        record_published_topic("ep_001", "vid1b", "https://yt.be/1b", root)
        record_published_topic("ep_001", "vid1c", "https://yt.be/1c", root)
        entries = read_topic_memory(root)
        ep1_entries = [e for e in entries if e["episode_id"] == "ep_001"]
        assert len(ep1_entries) == 1

    def test_sets_status_completed(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        entries = read_topic_memory(root)
        assert entries[0]["status"] == "completed"

    def test_returns_false_for_empty_episode_id(self, tmp_path):
        root = _make_project_root(tmp_path)
        result = record_published_topic("", "vid1", "https://yt.be/1", root)
        assert result is False

    def test_recovers_from_corrupted_file(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "THIS IS NOT A VALID COVERED TOPICS FILE")
        result = record_published_topic("ep_after_corrupt", "vid", "https://yt.be/v", root)
        assert result is True
        entries = read_topic_memory(root)
        assert any(e["episode_id"] == "ep_after_corrupt" for e in entries)

    def test_existing_episodes_preserved_when_adding_new(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        record_published_topic("ep_002", "vid2", "https://yt.be/2", root)
        entries = read_topic_memory(root)
        ep_ids = {e["episode_id"] for e in entries}
        assert "ep_001" in ep_ids
        assert "ep_002" in ep_ids


# ---------------------------------------------------------------------------
# TestTopicAlreadyCovered
# ---------------------------------------------------------------------------

class TestTopicAlreadyCovered:
    def test_returns_false_when_file_absent(self, tmp_path):
        root = _make_project_root(tmp_path)
        assert topic_already_covered("ep_001", root) is False

    def test_returns_false_for_unknown_episode(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        assert topic_already_covered("ep_999", root) is False

    def test_returns_true_for_known_episode(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        assert topic_already_covered("ep_001", root) is True

    def test_returns_true_regardless_of_status(self, tmp_path):
        root = _make_project_root(tmp_path)
        content = (
            "# Covered Topics\n"
            "\n## ep_planned\n"
            "- status: planned\n"
        )
        _write_topics(root, content)
        assert topic_already_covered("ep_planned", root) is True

    def test_returns_false_for_empty_episode_id(self, tmp_path):
        root = _make_project_root(tmp_path)
        assert topic_already_covered("", root) is False

    def test_returns_false_for_corrupted_file(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "GARBAGE")
        assert topic_already_covered("ep_001", root) is False

    def test_exact_match_only(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        # "ep_00" is a prefix of "ep_001" but should not match
        assert topic_already_covered("ep_00", root) is False
        assert topic_already_covered("ep_0010", root) is False


# ---------------------------------------------------------------------------
# TestFileFormat
# ---------------------------------------------------------------------------

class TestFileFormat:
    def test_file_starts_with_header(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        content = _read_topics(root)
        assert content.startswith("# Covered Topics\n")

    def test_episode_section_uses_h2(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        content = _read_topics(root)
        assert "## ep_001" in content

    def test_all_required_fields_present(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        content = _read_topics(root)
        assert "- youtube_video_id:" in content
        assert "- youtube_url:" in content
        assert "- published_at:" in content
        assert "- status:" in content

    def test_file_is_utf8(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_unicode_テスト", "vid1", "https://yt.be/1", root)
        raw = _topics_path(root).read_bytes()
        # Must be decodable as UTF-8
        decoded = raw.decode("utf-8")
        assert "ep_unicode_テスト" in decoded

    def test_status_is_completed_in_file(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        content = _read_topics(root)
        assert "- status: completed" in content

    def test_multiple_entries_each_have_h2(self, tmp_path):
        root = _make_project_root(tmp_path)
        record_published_topic("ep_001", "vid1", "https://yt.be/1", root)
        record_published_topic("ep_002", "vid2", "https://yt.be/2", root)
        content = _read_topics(root)
        assert "## ep_001" in content
        assert "## ep_002" in content


# ---------------------------------------------------------------------------
# TestCorruptionRecovery
# ---------------------------------------------------------------------------

class TestCorruptionRecovery:
    def test_invalid_header_triggers_backup(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "NOT A COVERED TOPICS FILE\n## ep_001\n")
        read_topic_memory(root)
        # Original file should be gone, backup should exist
        assert not _topics_path(root).exists()
        backups = list((root / "memory").glob("covered-topics.md.corrupt.*"))
        assert len(backups) == 1

    def test_backup_preserves_original_content(self, tmp_path):
        root = _make_project_root(tmp_path)
        original = "INVALID HEADER\nsome content\n"
        _write_topics(root, original)
        read_topic_memory(root)
        backups = list((root / "memory").glob("covered-topics.md.corrupt.*"))
        assert backups[0].read_text(encoding="utf-8") == original

    def test_record_creates_fresh_file_after_corruption(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "NOT VALID")
        record_published_topic("ep_fresh", "vid", "https://yt.be/v", root)
        content = _read_topics(root)
        assert content.startswith("# Covered Topics\n")
        assert "## ep_fresh" in content

    def test_read_returns_empty_after_corruption(self, tmp_path):
        root = _make_project_root(tmp_path)
        _write_topics(root, "CORRUPT")
        result = read_topic_memory(root)
        assert result == []

    def test_binary_garbage_handled_gracefully(self, tmp_path):
        root = _make_project_root(tmp_path)
        _topics_path(root).write_bytes(b"\xff\xfe\x80\x81\x00\x01")
        # read_topic_memory must not raise
        result = read_topic_memory(root)
        assert result == []
        # A backup should be created
        backups = list((root / "memory").glob("covered-topics.md.corrupt.*"))
        assert len(backups) == 1

    def test_after_binary_corruption_record_succeeds(self, tmp_path):
        root = _make_project_root(tmp_path)
        _topics_path(root).write_bytes(b"\xff\xfe\x80\x81")
        result = record_published_topic("ep_after", "vid", "https://yt.be/v", root)
        assert result is True
        entries = read_topic_memory(root)
        assert any(e["episode_id"] == "ep_after" for e in entries)
