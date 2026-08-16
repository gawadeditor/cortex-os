"""
tests/test_provider_receipt_path_resolution.py — Focused tests for
hypogeum/provider_receipt.py's resolve_episode_receipts_root() and
relocate_legacy_receipt_trail(): the fix for the duplicated-segment
receipts-path defect (multiple domain callers across this codebase
already hold an episode-scoped directory, e.g.
`_ASSETS_ROOT / "episodes" / episode_id`, and previously passed it into
WriteAheadReceiptStore, which unconditionally appended
"episodes/{execution_id}" a second time).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from hypogeum.provider_receipt import (
    AmbiguousReceiptsRootError,
    EventType,
    ProviderReceiptEvent,
    WriteAheadReceiptStore,
    relocate_legacy_receipt_trail,
    resolve_episode_receipts_root,
)


class TestResolveEpisodeReceiptsRoot:

    def test_bare_repository_asset_root(self, tmp_path):
        root = resolve_episode_receipts_root(tmp_path, "ep_x")
        assert root == tmp_path / "episodes" / "ep_x" / "receipts"

    def test_episodes_root(self, tmp_path):
        episodes_root = tmp_path / "episodes"
        root = resolve_episode_receipts_root(episodes_root, "ep_x")
        assert root == episodes_root / "ep_x" / "receipts"

    def test_exact_episode_root_produces_one_episode_segment(self, tmp_path):
        """The core regression: a caller already holding
        `{assets_root}/episodes/{episode_id}` must never get
        `.../episodes/{episode_id}/episodes/{episode_id}/receipts`."""
        episode_root = tmp_path / "episodes" / "ep_x"
        root = resolve_episode_receipts_root(episode_root, "ep_x")
        assert root == episode_root / "receipts"
        assert str(root).count("episodes") == 1
        assert str(root).count("ep_x") == 1

    def test_already_canonical_receipts_root_is_not_duplicated(self, tmp_path):
        receipts_root = tmp_path / "episodes" / "ep_x" / "receipts"
        root = resolve_episode_receipts_root(receipts_root, "ep_x")
        assert root == receipts_root

    def test_wrong_episode_identity_fails_closed(self, tmp_path):
        other_episode_root = tmp_path / "episodes" / "ep_other"
        with pytest.raises(AmbiguousReceiptsRootError):
            resolve_episode_receipts_root(other_episode_root, "ep_x")

    def test_traversal_in_assets_root_fails_closed(self, tmp_path):
        with pytest.raises(AmbiguousReceiptsRootError):
            resolve_episode_receipts_root(tmp_path / ".." / "escape", "ep_x")

    def test_traversal_in_execution_id_fails_closed(self, tmp_path):
        with pytest.raises(AmbiguousReceiptsRootError):
            resolve_episode_receipts_root(tmp_path, "../etc/passwd")

    def test_empty_execution_id_fails_closed(self, tmp_path):
        with pytest.raises(AmbiguousReceiptsRootError):
            resolve_episode_receipts_root(tmp_path, "")

    def test_relative_path_input(self):
        root = resolve_episode_receipts_root(Path(".hypogeum-assets"), "ep_x")
        assert root == Path(".hypogeum-assets/episodes/ep_x/receipts")

    def test_absolute_path_input(self, tmp_path):
        root = resolve_episode_receipts_root(tmp_path.resolve(), "ep_x")
        assert root == tmp_path.resolve() / "episodes" / "ep_x" / "receipts"


class TestWriteAheadReceiptStoreUsesTheResolver:
    """The store's own _attempt_dir() must go through the same resolver
    -- proving every real production caller (which already holds an
    episode-scoped root) is fixed automatically, with zero call-site
    changes needed."""

    def test_episode_scoped_root_no_longer_duplicates_the_segment(self, tmp_path):
        episode_root = tmp_path / "episodes" / "ep_x"
        store = WriteAheadReceiptStore(episode_root)
        event = ProviderReceiptEvent(
            envelope_id="env1", envelope_version=1, authorization_id="auth1", authorization_version=1,
            attempt_id="attempt1", idempotency_key="idem1", event_type=EventType.ABOUT_TO_SUBMIT,
            event_seq=0, at="2026-01-01T00:00:00Z", provider="fake", model="fake-model",
            provider_operation="video.text_to_video", provider_bound_content_sha256="a" * 64,
            max_cost_per_call=Decimal("0.39"), currency="USD", evidence={},
        )
        store.prepare_attempt(event, execution_id="ep_x")
        expected = episode_root / "receipts" / "idem1" / "0000_about_to_submit.json"
        assert expected.exists()
        assert not (episode_root / "episodes").exists()

    def test_bare_asset_root_still_works_unchanged(self, tmp_path):
        """A non-episode-shaped root (e.g. a pytest tmp_path, matching
        every existing test's usage) must keep producing exactly the
        pre-fix layout."""
        store = WriteAheadReceiptStore(tmp_path)
        event = ProviderReceiptEvent(
            envelope_id="env1", envelope_version=1, authorization_id="auth1", authorization_version=1,
            attempt_id="attempt1", idempotency_key="idem1", event_type=EventType.ABOUT_TO_SUBMIT,
            event_seq=0, at="2026-01-01T00:00:00Z", provider="fake", model="fake-model",
            provider_operation="video.text_to_video", provider_bound_content_sha256="a" * 64,
            max_cost_per_call=Decimal("0.39"), currency="USD", evidence={},
        )
        store.prepare_attempt(event, execution_id="ep_x")
        expected = tmp_path / "episodes" / "ep_x" / "receipts" / "idem1" / "0000_about_to_submit.json"
        assert expected.exists()


class TestRelocateLegacyReceiptTrail:

    def _write_legacy_files(self, legacy_dir: Path) -> dict:
        legacy_dir.mkdir(parents=True)
        contents = {}
        for i, name in enumerate(("0000_about_to_submit.json", "0001_accepted.json")):
            text = f'{{"event_seq": {i}, "marker": "file_{i}"}}'
            (legacy_dir / name).write_text(text)
            contents[name] = text
        return contents

    def test_relocates_and_verifies_checksums(self, tmp_path):
        legacy = tmp_path / "old_location"
        canonical = tmp_path / "new_location"
        contents = self._write_legacy_files(legacy)

        record = relocate_legacy_receipt_trail(legacy, canonical, at="2026-01-01T00:00:00Z")

        assert len(record["files"]) == 2
        assert all(f["verified"] for f in record["files"])
        for name, text in contents.items():
            assert (canonical / name).read_text() == text
        # original is preserved, never deleted
        for name in contents:
            assert (legacy / name).exists()

    def test_idempotent_replay_does_not_error(self, tmp_path):
        legacy = tmp_path / "old_location"
        canonical = tmp_path / "new_location"
        self._write_legacy_files(legacy)

        first = relocate_legacy_receipt_trail(legacy, canonical, at="2026-01-01T00:00:00Z")
        second = relocate_legacy_receipt_trail(legacy, canonical, at="2026-01-01T00:00:00Z")
        assert len(first["files"]) == len(second["files"]) == 2

    def test_conflicting_destination_content_fails_closed(self, tmp_path):
        legacy = tmp_path / "old_location"
        canonical = tmp_path / "new_location"
        self._write_legacy_files(legacy)
        canonical.mkdir(parents=True)
        (canonical / "0000_about_to_submit.json").write_text('{"different": "content"}')

        with pytest.raises(RuntimeError):
            relocate_legacy_receipt_trail(legacy, canonical, at="2026-01-01T00:00:00Z")

    def test_missing_legacy_root_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError):
            relocate_legacy_receipt_trail(tmp_path / "does-not-exist", tmp_path / "dest", at="2026-01-01T00:00:00Z")

    def test_recovery_finds_exactly_one_trail_after_relocation(self, tmp_path):
        """Duplicate legacy + canonical locations must never cause
        duplicate recovery -- reading via the store (which always
        resolves to the canonical path) finds the relocated evidence
        exactly once."""
        episode_root = tmp_path / "episodes" / "ep_x"
        legacy = episode_root / "episodes" / "ep_x" / "receipts" / "idem1"
        legacy.mkdir(parents=True)
        event = ProviderReceiptEvent(
            envelope_id="env1", envelope_version=1, authorization_id="auth1", authorization_version=1,
            attempt_id="attempt1", idempotency_key="idem1", event_type=EventType.ABOUT_TO_SUBMIT,
            event_seq=0, at="2026-01-01T00:00:00Z", provider="fake", model="fake-model",
            provider_operation="video.text_to_video", provider_bound_content_sha256="a" * 64,
            max_cost_per_call=Decimal("0.39"), currency="USD", evidence={},
        )
        import json
        (legacy / "0000_about_to_submit.json").write_text(json.dumps(event.to_dict()))

        canonical = resolve_episode_receipts_root(episode_root, "ep_x") / "idem1"
        relocate_legacy_receipt_trail(legacy, canonical, at="2026-01-01T00:00:00Z")

        store = WriteAheadReceiptStore(episode_root)
        events = store.read_events("ep_x", "idem1")
        assert len(events) == 1
        assert events[0].event_type == EventType.ABOUT_TO_SUBMIT
