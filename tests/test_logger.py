"""
tests/test_logger.py — Unit verification for hypogeum/logger.py

Exercises:
  - Logger creation (default and custom paths)
  - info / warn / error methods write correctly
  - Every entry contains all mandatory fields
  - Timestamps are UTC ISO8601 ending in 'Z'
  - Arbitrary metadata fields serialise correctly
  - Non-JSON-serialisable values fall back to str()
  - Each line is valid JSON (JSONL format)
  - Writes are appended — prior entries are not overwritten
  - episode_id and agent are null when not supplied
  - Core field names are protected against caller kwargs collision
  - Concurrent writes from multiple threads produce valid, non-interleaved JSONL

Run with:  python3 -m pytest tests/test_logger.py -v
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.logger import Logger, get_logger, _utc_now, _to_json_line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tmp_log() -> Path:
    """Return a path to a fresh temporary log file (not yet created)."""
    fd, path = tempfile.mkstemp(suffix=".log", prefix="hypogeum_test_")
    os.close(fd)
    os.unlink(path)  # Remove so logger creates it fresh
    return Path(path)


def read_lines(path: Path) -> list[dict]:
    """Read all JSONL lines from path and parse each as a dict."""
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Logger creation
# ---------------------------------------------------------------------------

class TestLoggerCreation:
    def test_get_logger_returns_logger_instance(self):
        log = get_logger(log_file=tmp_log())
        assert isinstance(log, Logger)

    def test_default_log_file_in_hypogeum_assets(self):
        log = get_logger()
        assert ".hypogeum-assets" in str(log._log_file)
        assert log._log_file.name == "pipeline.log"

    def test_episode_id_none_by_default(self):
        log = get_logger(log_file=tmp_log())
        assert log._episode_id is None

    def test_agent_none_by_default(self):
        log = get_logger(log_file=tmp_log())
        assert log._agent is None

    def test_episode_and_agent_stored(self):
        log = get_logger(
            episode_id="ep_001",
            agent="executive_producer",
            log_file=tmp_log(),
        )
        assert log._episode_id == "ep_001"
        assert log._agent == "executive_producer"

    def test_parent_directory_created_if_missing(self):
        base = Path(tempfile.mkdtemp())
        log_path = base / "nested" / "dir" / "run.log"
        log = get_logger(log_file=log_path)
        log.info("init")
        assert log_path.exists()


# ---------------------------------------------------------------------------
# Mandatory fields on every entry
# ---------------------------------------------------------------------------

class TestMandatoryFields:
    MANDATORY = {"timestamp", "level", "episode_id", "agent", "event"}

    def _make_log(self, **kwargs):
        path = tmp_log()
        log = get_logger(episode_id="ep_test", agent="test_agent", log_file=path)
        return log, path

    def test_info_entry_has_all_mandatory_fields(self):
        log, path = self._make_log()
        log.info("test_event")
        entry = read_lines(path)[0]
        for field in self.MANDATORY:
            assert field in entry, f"Missing mandatory field: {field}"

    def test_warn_entry_has_all_mandatory_fields(self):
        log, path = self._make_log()
        log.warn("test_warn")
        entry = read_lines(path)[0]
        for field in self.MANDATORY:
            assert field in entry

    def test_error_entry_has_all_mandatory_fields(self):
        log, path = self._make_log()
        log.error("test_error")
        entry = read_lines(path)[0]
        for field in self.MANDATORY:
            assert field in entry

    def test_level_values_are_correct(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("a")
        log.warn("b")
        log.error("c")
        lines = read_lines(path)
        assert lines[0]["level"] == "info"
        assert lines[1]["level"] == "warn"
        assert lines[2]["level"] == "error"

    def test_episode_id_in_entry(self):
        path = tmp_log()
        log = get_logger(episode_id="ep_antikythera", log_file=path)
        log.info("event")
        assert read_lines(path)[0]["episode_id"] == "ep_antikythera"

    def test_agent_in_entry(self):
        path = tmp_log()
        log = get_logger(agent="narrative_architect", log_file=path)
        log.info("event")
        assert read_lines(path)[0]["agent"] == "narrative_architect"

    def test_null_episode_id_when_not_set(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("startup")
        assert read_lines(path)[0]["episode_id"] is None

    def test_null_agent_when_not_set(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("startup")
        assert read_lines(path)[0]["agent"] is None

    def test_event_name_stored(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("openrouter_request_sent")
        assert read_lines(path)[0]["event"] == "openrouter_request_sent"


# ---------------------------------------------------------------------------
# Timestamp format
# ---------------------------------------------------------------------------

class TestTimestamp:
    def test_timestamp_ends_with_z(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("ts_test")
        ts = read_lines(path)[0]["timestamp"]
        assert ts.endswith("Z"), f"Timestamp does not end with 'Z': {ts!r}"

    def test_timestamp_is_iso8601_parseable(self):
        from datetime import datetime
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("ts_test")
        ts = read_lines(path)[0]["timestamp"]
        # Remove trailing Z, parse as UTC
        parsed = datetime.fromisoformat(ts.rstrip("Z"))
        assert parsed.year >= 2026

    def test_utc_now_helper_format(self):
        ts = _utc_now()
        assert ts.endswith("Z")
        assert "T" in ts
        assert len(ts) == 27  # "YYYY-MM-DDTHH:MM:SS.ffffffZ"


# ---------------------------------------------------------------------------
# Metadata / arbitrary fields
# ---------------------------------------------------------------------------

class TestMetadataFields:
    def test_string_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", model_slug="anthropic/claude-sonnet-4.6")
        entry = read_lines(path)[0]
        assert entry["model_slug"] == "anthropic/claude-sonnet-4.6"

    def test_int_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", total_tokens=1234)
        assert read_lines(path)[0]["total_tokens"] == 1234

    def test_float_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", score=8.5)
        assert read_lines(path)[0]["score"] == pytest.approx(8.5)

    def test_bool_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", human_review_recommended=True)
        assert read_lines(path)[0]["human_review_recommended"] is True

    def test_none_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", track_id=None)
        assert read_lines(path)[0]["track_id"] is None

    def test_list_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.warn("event", missing_fields=["narration_beats", "section_id"])
        entry = read_lines(path)[0]
        assert entry["missing_fields"] == ["narration_beats", "section_id"]

    def test_dict_field(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", usage={"prompt_tokens": 500, "completion_tokens": 200})
        entry = read_lines(path)[0]
        assert entry["usage"]["prompt_tokens"] == 500

    def test_multiple_metadata_fields(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info(
            "openrouter_response_received",
            model_slug="anthropic/claude-opus-4.6",
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            finish_reason="end_turn",
            response_time_ms=2341,
        )
        entry = read_lines(path)[0]
        assert entry["model_slug"] == "anthropic/claude-opus-4.6"
        assert entry["total_tokens"] == 1500
        assert entry["finish_reason"] == "end_turn"

    def test_non_serialisable_value_falls_back_to_str(self):
        path = tmp_log()
        log = get_logger(log_file=path)

        class Unserializable:
            def __str__(self):
                return "custom_str_value"

        log.info("event", obj=Unserializable())
        entry = read_lines(path)[0]
        assert entry["obj"] == "custom_str_value"

    def test_core_field_collision_is_prefixed(self):
        # If caller passes a kwarg named 'level', it must not overwrite the core field
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event", level="caller_level")
        entry = read_lines(path)[0]
        assert entry["level"] == "info"          # core field preserved
        assert entry["field_level"] == "caller_level"  # caller field namespaced


# ---------------------------------------------------------------------------
# JSON validity (JSONL format)
# ---------------------------------------------------------------------------

class TestJsonValidity:
    def test_each_line_is_valid_json(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("a", x=1)
        log.warn("b", y="hello")
        log.error("c", z=[1, 2, 3])
        with path.open() as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)  # raises if invalid
                assert isinstance(parsed, dict), f"Line {i} is not a JSON object"

    def test_no_trailing_commas_or_extra_whitespace(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("event")
        with path.open() as fh:
            lines = fh.readlines()
        assert len(lines) == 1
        line = lines[0]
        assert line.endswith("\n")
        assert line.strip().startswith("{")
        assert line.strip().endswith("}")

    def test_to_json_line_helper_produces_valid_json(self):
        result = _to_json_line({"key": "value", "num": 42})
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["num"] == 42


# ---------------------------------------------------------------------------
# Append behaviour
# ---------------------------------------------------------------------------

class TestAppendBehaviour:
    def test_second_write_appends_not_overwrites(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        log.info("first_event")
        log.info("second_event")
        lines = read_lines(path)
        assert len(lines) == 2
        assert lines[0]["event"] == "first_event"
        assert lines[1]["event"] == "second_event"

    def test_new_logger_instance_appends_to_existing_file(self):
        path = tmp_log()
        log1 = get_logger(episode_id="ep_1", log_file=path)
        log1.info("from_first_instance")

        log2 = get_logger(episode_id="ep_2", log_file=path)
        log2.info("from_second_instance")

        lines = read_lines(path)
        assert len(lines) == 2
        assert lines[0]["episode_id"] == "ep_1"
        assert lines[1]["episode_id"] == "ep_2"

    def test_fifty_writes_produce_fifty_lines(self):
        path = tmp_log()
        log = get_logger(log_file=path)
        for i in range(50):
            log.info("batch_event", index=i)
        lines = read_lines(path)
        assert len(lines) == 50
        for i, entry in enumerate(lines):
            assert entry["index"] == i


# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_concurrent_writes_produce_valid_jsonl(self):
        """
        50 threads each write 20 entries to the same log file.
        Expected: 1,000 valid, parseable JSON lines with no interleaving.
        """
        path = tmp_log()
        n_threads = 50
        n_writes_per_thread = 20
        errors = []

        def writer(thread_id: int):
            log = get_logger(
                episode_id=f"ep_{thread_id:03d}",
                agent=f"agent_{thread_id}",
                log_file=path,
            )
            for i in range(n_writes_per_thread):
                try:
                    log.info(
                        "concurrent_write",
                        thread_id=thread_id,
                        write_index=i,
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Writer threads raised exceptions: {errors}"

        lines = read_lines(path)
        expected = n_threads * n_writes_per_thread
        assert len(lines) == expected, (
            f"Expected {expected} lines, got {len(lines)} — "
            "some writes may have been lost or interleaved"
        )

        # Every line must have the mandatory fields
        for i, entry in enumerate(lines):
            for field in ("timestamp", "level", "episode_id", "agent", "event"):
                assert field in entry, f"Line {i} missing field {field!r}"

    def test_same_lock_reused_for_same_path(self):
        """Two loggers to the same path share the same lock object."""
        path = tmp_log()
        log1 = get_logger(log_file=path)
        log2 = get_logger(log_file=path)
        assert log1._lock is log2._lock

    def test_different_paths_use_different_locks(self):
        path_a = tmp_log()
        path_b = tmp_log()
        log_a = get_logger(log_file=path_a)
        log_b = get_logger(log_file=path_b)
        assert log_a._lock is not log_b._lock
