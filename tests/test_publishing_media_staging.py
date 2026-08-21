"""
tests/test_publishing_media_staging.py — Generic, provider-neutral durable
media-staging contract and write-ahead boundary.

Covers hypogeum.publishing.media_staging: SourceMedia/StagedMediaResult
dataclasses, compute_sha256(), the durable state machine, and
guard_and_prepare_staging_attempt()'s reuse/blocked/proceed semantics.

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
-- never the production .hypogeum-assets/hypogeum.db. No network, no real
file staging, no provider-specific logic (that lives in
tests/test_publishing_zernio_media_staging.py).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider import MediaReference
from hypogeum.publishing.media_staging import (
    BlindStagingRetryRefusedError,
    StagedMediaResult,
    StagingAttemptNotFoundError,
    compute_sha256,
    guard_and_prepare_staging_attempt,
    mark_staging_upload_ready,
    mark_staging_uploading,
    record_staging_ambiguous,
    record_staging_definitive_failure,
    record_staging_uploaded,
    staged_result_to_media_reference,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "media_staging_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def local_file(tmp_path):
    p = tmp_path / "sample.mp4"
    p.write_bytes(b"fake video bytes" * 100)
    return p


def _row(db_path, staging_attempt_id):
    conn = get_connection(db_path)
    try:
        r = conn.execute(
            "SELECT * FROM media_staging_attempts WHERE staging_attempt_id = ?", (staging_attempt_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _upload_lifecycle(db_path, staging_attempt_id, *, public_url="https://media.example.com/x.mp4"):
    mark_staging_upload_ready(db_path, staging_attempt_id)
    mark_staging_uploading(db_path, staging_attempt_id)
    return record_staging_uploaded(db_path, staging_attempt_id, public_url=public_url, remote_object_id="key/x")


# ===========================================================================
# compute_sha256
# ===========================================================================

class TestComputeSha256:
    def test_matches_known_hash(self, tmp_path):
        p = tmp_path / "known.txt"
        p.write_bytes(b"hello world")
        import hashlib
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert compute_sha256(p) == expected

    def test_large_file_streamed_in_chunks(self, tmp_path):
        p = tmp_path / "big.bin"
        p.write_bytes(b"x" * (5 * 1024 * 1024))  # 5 MB, forces multiple chunk reads
        h = compute_sha256(p, chunk_size=1024)
        import hashlib
        assert h == hashlib.sha256(b"x" * (5 * 1024 * 1024)).hexdigest()


# ===========================================================================
# Durable state: attempt creation, transitions, reuse
# ===========================================================================

class TestDurableVisibilityBeforeExternalCall:
    def test_attempt_committed_and_advanced_to_presigning(self, db_path):
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_1",
            deliverable_id="main_episode", source_sha256="a" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard.action == "proceed"
        row = _row(db_path, guard.staging_attempt_id)
        assert row["state"] == "presigning"


class TestReuseAndExpiry:
    def test_confirmed_upload_is_reused_no_new_attempt(self, db_path):
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_1",
            deliverable_id="main_episode", source_sha256="a" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        _upload_lifecycle(db_path, guard1.staging_attempt_id)

        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_1",
            deliverable_id="main_episode", source_sha256="a" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "use_existing"
        assert guard2.staging_attempt_id == guard1.staging_attempt_id
        assert isinstance(guard2.existing_result, StagedMediaResult)
        assert guard2.existing_result.public_url == "https://media.example.com/x.mp4"

    def test_expired_upload_is_never_reused_and_permits_new_attempt(self, db_path):
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_exp",
            deliverable_id="main_episode", source_sha256="b" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        mark_staging_upload_ready(db_path, guard1.staging_attempt_id)
        mark_staging_uploading(db_path, guard1.staging_attempt_id)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        record_staging_uploaded(
            db_path, guard1.staging_attempt_id, public_url="https://media.example.com/expired.mp4",
            expires_at=past,
        )

        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_exp",
            deliverable_id="main_episode", source_sha256="b" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "proceed"
        assert guard2.staging_attempt_id != guard1.staging_attempt_id
        # the old row is durably marked expired, never silently reused
        old_row = _row(db_path, guard1.staging_attempt_id)
        assert old_row["state"] == "expired"

    def test_non_expiring_upload_reused_indefinitely(self, db_path):
        """expires_at=None (as Zernio's presign publicUrl produces, per the
        contract audit) must never be treated as already-expired."""
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_noexp",
            deliverable_id="main_episode", source_sha256="c" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        _upload_lifecycle(db_path, guard1.staging_attempt_id, public_url="https://media.example.com/durable.mp4")

        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_noexp",
            deliverable_id="main_episode", source_sha256="c" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "use_existing"


class TestAmbiguousBlocksRetry:
    def test_ambiguous_attempt_blocks_new_call(self, db_path):
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_ambig",
            deliverable_id="main_episode", source_sha256="d" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        record_staging_ambiguous(db_path, guard1.staging_attempt_id, error_category="transport_error", error_message="timeout")

        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_ambig",
            deliverable_id="main_episode", source_sha256="d" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "blocked"

    def test_definitive_failure_permits_new_attempt(self, db_path):
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_def",
            deliverable_id="main_episode", source_sha256="e" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        record_staging_definitive_failure(db_path, guard1.staging_attempt_id, error_category="validation_error", error_message="bad request")

        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_def",
            deliverable_id="main_episode", source_sha256="e" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "proceed"
        assert guard2.staging_attempt_id != guard1.staging_attempt_id


class TestNoDuplicateUploads:
    def test_no_duplicate_upload_for_same_scope_and_content(self, db_path):
        """A retry mid-flight (prepared/presigning/upload_ready/uploading)
        must never permit a second concurrent-looking attempt."""
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_dup",
            deliverable_id="main_episode", source_sha256="f" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard1.action == "proceed"
        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_dup",
            deliverable_id="main_episode", source_sha256="f" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "blocked"

    def test_different_deliverable_is_independent(self, db_path):
        """Different scope (deliverable_id) with the SAME content hash must
        not be conflated -- each deliverable gets its own attempt."""
        guard1 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_scope",
            deliverable_id="main_episode", source_sha256="g" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_scope",
            deliverable_id="short_001", source_sha256="g" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "proceed"
        assert guard2.staging_attempt_id != guard1.staging_attempt_id

    def test_none_channel_id_scope_works(self, db_path):
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id=None, episode_id="ep_none",
            deliverable_id="main_episode", source_sha256="h" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard.action == "proceed"
        guard2 = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id=None, episode_id="ep_none",
            deliverable_id="main_episode", source_sha256="h" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        assert guard2.action == "blocked"


class TestConcurrency:
    def test_concurrent_claims_only_one_proceeds(self, db_path):
        results = []
        lock = threading.Lock()

        def _attempt():
            g = guard_and_prepare_staging_attempt(
                db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_concurrent",
                deliverable_id="main_episode", source_sha256="i" * 64, source_size_bytes=1000,
                source_mime_type="video/mp4",
            )
            with lock:
                results.append(g.action)

        threads = [threading.Thread(target=_attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count("proceed") == 1
        assert results.count("blocked") == 7


class TestInvalidTransitionsAndMissingAttempts:
    def test_unknown_staging_attempt_id_raises(self, db_path):
        with pytest.raises(StagingAttemptNotFoundError):
            mark_staging_upload_ready(db_path, "not_a_real_id")

    def test_cannot_record_uploaded_twice(self, db_path):
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_twice",
            deliverable_id="main_episode", source_sha256="j" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        _upload_lifecycle(db_path, guard.staging_attempt_id)
        with pytest.raises(BlindStagingRetryRefusedError):
            record_staging_uploaded(db_path, guard.staging_attempt_id, public_url="https://media.example.com/y.mp4")


# ===========================================================================
# Security: presigned upload URL query string never persisted
# ===========================================================================

class TestSecurity:
    def test_credential_shaped_metadata_key_is_redacted(self, db_path):
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_sec",
            deliverable_id="main_episode", source_sha256="k" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        mark_staging_upload_ready(db_path, guard.staging_attempt_id)
        mark_staging_uploading(db_path, guard.staging_attempt_id)
        record_staging_uploaded(
            db_path, guard.staging_attempt_id, public_url="https://media.example.com/z.mp4",
            provider_metadata={"uploadUrl": "https://s3.example.com/x?sig=SECRETVALUE", "note": "fine"},
        )
        row = _row(db_path, guard.staging_attempt_id)
        assert "SECRETVALUE" not in row["metadata_json"]
        assert "[REDACTED]" in row["metadata_json"]

    def test_error_message_authorization_header_redacted(self, db_path):
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id="zernio", channel_id="hypogeum", episode_id="ep_sec2",
            deliverable_id="main_episode", source_sha256="l" * 64, source_size_bytes=1000,
            source_mime_type="video/mp4",
        )
        record_staging_ambiguous(
            db_path, guard.staging_attempt_id, error_category="transport_error",
            error_message="failed: Authorization: Bearer abc123SECRETTOKEN",
        )
        row = _row(db_path, guard.staging_attempt_id)
        assert "abc123SECRETTOKEN" not in row["last_error_message"]


# ===========================================================================
# staged_result_to_media_reference conversion
# ===========================================================================

class TestConversion:
    def test_converts_to_media_reference(self):
        result = StagedMediaResult(
            media_staging_provider_id="zernio", public_url="https://media.example.com/v.mp4",
            source_sha256="m" * 64, size_bytes=1000, mime_type="video/mp4",
            created_at="2026-08-21T00:00:00Z", status="uploaded",
        )
        ref = staged_result_to_media_reference(result, media_type="video", title="Episode 1", alt_text="alt")
        assert isinstance(ref, MediaReference)
        assert ref.url == "https://media.example.com/v.mp4"
        assert ref.media_type == "video"
        assert ref.title == "Episode 1"
        assert ref.alt_text == "alt"
