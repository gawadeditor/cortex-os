"""
tests/test_publishing_submission_ledger.py — Durable write-ahead publishing
submission boundary (ADR-013 Decision 7, item 2).

Covers hypogeum.publishing.submission_ledger and its integration into
hypogeum.agents.publishing_agent.run_publishing_agent() at the STEP 6
(YouTube), STEP 8 (Facebook), STEP 9 (Instagram), and STEP 10 (TikTok)
external-call boundaries.

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
— never the production .hypogeum-assets/hypogeum.db. No network, credential,
queue, worker, or real provider call is made anywhere in this file; fake
providers/patched hypogeum.publishing.{youtube,tiktok,meta} functions only.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.submission_ledger import (
    AttemptNotFoundError,
    BlindRetryRefusedError,
    guard_and_prepare_attempt,
    open_new_publication_intent,
    record_ambiguous,
    record_definitive_failure,
    record_success,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "submission_ledger_test.db"
    initialize_schema(p)
    return p


def _attempt_row(db_path, attempt_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM publishing_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _receipt_row(db_path, attempt_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM publishing_receipts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count(db_path, table):
    conn = get_connection(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ===========================================================================
# 1. Attempt durably visible before any external call
# ===========================================================================

class TestDurableVisibilityBeforeExternalCall:
    def test_attempt_committed_before_fake_external_call(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_1", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert guard.action == "proceed"

        # Read via a completely separate connection -- proves the row is
        # durably committed, not just held in the guard's own transaction.
        row = _attempt_row(db_path, guard.attempt_id)
        assert row is not None
        assert row["state"] == "submitting"
        assert row["platform"] == "youtube"
        assert row["provider_id"] == "youtube_direct"
        # No fake external call has been made anywhere in this test.


# ===========================================================================
# 2/17. Filtered/disabled platform creates no attempt
# ===========================================================================

class TestFilteredPlatformCreatesNoRows:
    def test_empty_platform_filter_creates_zero_publishing_rows(self, tmp_path, monkeypatch):
        from hypogeum.agents.publishing_agent import run_publishing_agent
        from tests.test_publishing_agent import _make_config

        db = tmp_path / "hypogeum.db"
        initialize_schema(db)
        assets_root = tmp_path / ".hypogeum-assets"
        assets_root.mkdir()
        (assets_root / "hypogeum.db").write_bytes(b"")
        initialize_schema(assets_root / "hypogeum.db")

        monkeypatch.setattr(
            "hypogeum.agents.publishing_agent.enforce_production_output_gate",
            lambda **k: None,
        )
        monkeypatch.setattr(
            "hypogeum.agents.publishing_agent.get_ai_disclosure_text",
            lambda metadata_source: "AI-generated content disclosure.",
        )
        monkeypatch.setattr(
            "hypogeum.agents.publishing_agent.record_published_topic",
            lambda **k: True,
        )

        def _fail_if_called(*a, **k):
            raise AssertionError("no provider function may be called when every platform is filtered out")
        monkeypatch.setattr("hypogeum.publishing.youtube.upload_video", _fail_if_called)
        monkeypatch.setattr("hypogeum.publishing.meta.upload_facebook_video", _fail_if_called)
        monkeypatch.setattr("hypogeum.publishing.meta.create_instagram_container", _fail_if_called)
        monkeypatch.setattr("hypogeum.publishing.tiktok.init_tiktok_upload", _fail_if_called)

        run_publishing_agent(
            final_render_package={
                "metadata_source": {}, "episode_render": {}, "shorts_renders": [], "thumbnail_references": [],
            },
            config=_make_config(),
            episode_id="ep_filtered",
            output_dir=assets_root / "episodes" / "ep_filtered",
            enabled_platforms=frozenset(),  # blocks every platform
        )

        assert _count(assets_root / "hypogeum.db", "publication_intents") == 0
        assert _count(assets_root / "hypogeum.db", "publishing_attempts") == 0
        assert _count(assets_root / "hypogeum.db", "publishing_receipts") == 0


# ===========================================================================
# 3/4/5. Terminal outcomes
# ===========================================================================

class TestTerminalOutcomes:
    def test_confirmed_success_writes_receipt_and_terminal_success(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_1", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_success(
            db_path, guard.attempt_id, provider_publication_id="yt_abc123",
            native_platform_content_id="yt_abc123", provider_status="public",
        )
        attempt = _attempt_row(db_path, guard.attempt_id)
        receipt = _receipt_row(db_path, guard.attempt_id)
        assert attempt["state"] == "succeeded"
        assert receipt is not None
        assert receipt["provider_publication_id"] == "yt_abc123"
        assert receipt["native_platform_content_id"] == "yt_abc123"
        assert receipt["provider_id"] == "youtube_direct"
        assert receipt["platform"] == "youtube"

    def test_definitive_rejection_records_failed_definitive(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_2", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_definitive_failure(db_path, guard.attempt_id, error_category="auth", error_message="401 unauthorized")
        attempt = _attempt_row(db_path, guard.attempt_id)
        assert attempt["state"] == "failed_definitive"
        assert attempt["last_error_category"] == "auth"

    def test_timeout_records_ambiguous(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_3", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_ambiguous(db_path, guard.attempt_id, error_category="timeout", error_message="request timed out")
        attempt = _attempt_row(db_path, guard.attempt_id)
        assert attempt["state"] == "ambiguous"


# ===========================================================================
# 6/7/8/9. Retry policy
# ===========================================================================

class TestRetryPolicy:
    def test_ambiguous_attempt_cannot_be_blindly_retried(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_4", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_ambiguous(db_path, guard.attempt_id, error_category="timeout", error_message="timeout")
        retry_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_4", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert retry_guard.action == "blocked"
        assert retry_guard.attempt_id == guard.attempt_id  # same ambiguous attempt, not resubmitted

    def test_existing_succeeded_intent_cannot_publish_externally_again(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_5", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="yt_1")
        retry_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_5", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert retry_guard.action == "use_existing"
        assert retry_guard.existing_receipt["provider_publication_id"] == "yt_1"
        # No second attempt row was created.
        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM publishing_attempts WHERE publication_intent_id = ?",
                (guard.publication_intent_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 1

    def test_deliberate_new_intent_can_republish_same_triple(self, db_path):
        """The ep_f3619022 scenario: deliberate republish after the prior
        external publication was deleted."""
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_f3619022", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="yt_deleted_video")

        new_intent_id = open_new_publication_intent(
            db_path, channel_id=None, episode_id="ep_f3619022",
            deliverable_id="main_episode", platform="youtube",
        )
        assert new_intent_id != guard.publication_intent_id

        republish_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_f3619022", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert republish_guard.action == "proceed"
        assert republish_guard.publication_intent_id == new_intent_id

    def test_retry_under_same_intent_reuses_durable_identity(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_6", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_definitive_failure(db_path, guard.attempt_id, error_category="auth", error_message="401")

        retry_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_6", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert retry_guard.action == "proceed"
        assert retry_guard.publication_intent_id == guard.publication_intent_id  # same intent
        assert retry_guard.attempt_id != guard.attempt_id  # new attempt

        attempt2 = _attempt_row(db_path, retry_guard.attempt_id)
        assert attempt2["attempt_number"] == 2


# ===========================================================================
# 10/11. Provider identity / no credentials
# ===========================================================================

class TestProviderIdentityAndSanitization:
    def test_provider_id_persisted_separately_from_platform(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_7", deliverable_id="main_episode",
            platform="facebook", provider_id="meta_direct",
        )
        attempt = _attempt_row(db_path, guard.attempt_id)
        assert attempt["platform"] == "facebook"
        assert attempt["provider_id"] == "meta_direct"
        assert attempt["platform"] != attempt["provider_id"]

    def test_no_credential_columns_exist_in_schema(self, db_path):
        conn = get_connection(db_path)
        try:
            attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(publishing_attempts)").fetchall()}
            receipt_cols = {r[1] for r in conn.execute("PRAGMA table_info(publishing_receipts)").fetchall()}
        finally:
            conn.close()
        forbidden = {"credential", "credential_data", "access_token", "api_key", "authorization", "secret"}
        assert not (attempt_cols & forbidden)
        assert not (receipt_cols & forbidden)

    def test_error_messages_are_sanitized(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_8", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_ambiguous(
            db_path, guard.attempt_id, error_category="network",
            error_message="request failed, Authorization: Bearer sk_live_supersecrettoken123",
        )
        attempt = _attempt_row(db_path, guard.attempt_id)
        assert "sk_live_supersecrettoken123" not in attempt["last_error_message"]
        assert "REDACTED" in attempt["last_error_message"]


# ===========================================================================
# 12/13. Multi-platform independence / partial success
# ===========================================================================

class TestMultiPlatformIndependence:
    def test_facebook_and_instagram_results_remain_independent(self, db_path):
        fb_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_9", deliverable_id="main_episode",
            platform="facebook", provider_id="meta_direct",
        )
        ig_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_9", deliverable_id="short_1",
            platform="instagram", provider_id="meta_direct",
        )
        assert fb_guard.publication_intent_id != ig_guard.publication_intent_id

        record_success(db_path, fb_guard.attempt_id, provider_publication_id="fb_1")
        record_definitive_failure(db_path, ig_guard.attempt_id, error_category="auth", error_message="401")

        fb_attempt = _attempt_row(db_path, fb_guard.attempt_id)
        ig_attempt = _attempt_row(db_path, ig_guard.attempt_id)
        assert fb_attempt["state"] == "succeeded"
        assert ig_attempt["state"] == "failed_definitive"

    def test_partial_multi_platform_success_is_representable(self, db_path):
        yt = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_10", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        tt = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_10", deliverable_id="short_1",
            platform="tiktok", provider_id="tiktok_direct",
        )
        record_success(db_path, yt.attempt_id, provider_publication_id="yt_1")
        record_ambiguous(db_path, tt.attempt_id, error_category="timeout", error_message="timeout")

        conn = get_connection(db_path)
        try:
            states = {
                r["platform"]: r["state"]
                for r in conn.execute("SELECT platform, state FROM publishing_attempts WHERE episode_id = 'ep_10'").fetchall()
            }
        finally:
            conn.close()
        assert states == {"youtube": "succeeded", "tiktok": "ambiguous"}


# ===========================================================================
# 14. Receipt-persistence failure after external success remains recoverable
# ===========================================================================

class TestReceiptPersistenceFailureRecovery:
    def test_receipt_write_failure_leaves_attempt_recoverable_not_lost(self, db_path, monkeypatch):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_11", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )

        import hypogeum.publishing.submission_ledger as sl
        real_get_connection = sl.get_connection

        class _FailingReceiptConnection:
            """Proxies a real sqlite3.Connection but raises on the
            publishing_receipts INSERT specifically -- sqlite3.Connection
            itself does not allow reassigning .execute on an instance."""
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *args, **kwargs):
                if "INSERT INTO publishing_receipts" in sql:
                    raise sqlite3.OperationalError("simulated disk full")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        def _boom(*a, **k):
            return _FailingReceiptConnection(real_get_connection(*a, **k))

        monkeypatch.setattr(sl, "get_connection", _boom)

        with pytest.raises(sqlite3.OperationalError):
            record_success(db_path, guard.attempt_id, provider_publication_id="yt_should_not_be_lost")

        monkeypatch.undo()

        # The external call is known to have succeeded (production code
        # would have a real provider_publication_id in hand) -- but the
        # local receipt write failed. The attempt must NOT silently vanish
        # or appear safe to blindly resubmit.
        attempt = _attempt_row(db_path, guard.attempt_id)
        assert attempt["state"] in ("acknowledged", "submitting")  # never advanced past this point
        assert _receipt_row(db_path, guard.attempt_id) is None

        retry_guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_11", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        assert retry_guard.action == "blocked"  # never a blind resubmit


# ===========================================================================
# 15. Existing Publishing Agent result structure remains compatible
# ===========================================================================

class TestPublishingAgentResultCompatibility:
    def test_make_result_entry_shape_unaffected(self):
        from hypogeum.publishing.record import make_result_entry
        entry = make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            platform_video_id="yt_1", title="Test",
        )
        assert entry["platform"] == "youtube"
        assert entry["status"] == "published"
        assert "platform_video_id" in entry


# ===========================================================================
# 18. Concurrency
# ===========================================================================

class TestConcurrency:
    def test_concurrent_claims_cannot_both_cross_external_call_boundary(self, db_path):
        results = []
        lock = threading.Lock()

        def _attempt():
            g = guard_and_prepare_attempt(
                db_path, channel_id=None, episode_id="ep_concurrent", deliverable_id="main_episode",
                platform="youtube", provider_id="youtube_direct",
            )
            with lock:
                results.append(g)

        threads = [threading.Thread(target=_attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        proceed = [g for g in results if g.action == "proceed"]
        assert len(proceed) == 1
        assert _count(db_path, "publishing_attempts") == 1


# ===========================================================================
# Invalid transitions fail closed
# ===========================================================================

class TestInvalidTransitions:
    def test_cannot_finalize_unknown_attempt(self, db_path):
        with pytest.raises(AttemptNotFoundError):
            record_success(db_path, "attempt_does_not_exist", provider_publication_id="x")

    def test_cannot_record_success_twice(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id=None, episode_id="ep_12", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="yt_1")
        with pytest.raises(BlindRetryRefusedError):
            record_success(db_path, guard.attempt_id, provider_publication_id="yt_1_again")
