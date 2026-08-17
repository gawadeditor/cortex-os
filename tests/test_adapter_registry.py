"""
tests/test_adapter_registry.py — Focused tests for
hypogeum/adapter_registry.py.

All fixtures use the sanctioned test double
(hypogeum.providers.fake_provider_adapter.FakeProviderAdapter) and
isolated temp DBs -- never a real provider adapter and never the live
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hypogeum import adapter_registry as ar
from hypogeum.providers.fake_provider_adapter import FakeProviderAdapter


@pytest.fixture(autouse=True)
def _reset_instance_cache():
    ar.reset_adapter_instance_cache()
    yield
    ar.reset_adapter_instance_cache()


class TestAtomicRegistration:

    def test_register_and_resolve(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        binding = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        assert binding.provider == "fake"
        assert binding.model == "fake-model"
        assert binding.provider_operation == "video.text_to_video"
        assert binding.adapter_qualified_name == "hypogeum.providers.fake_provider_adapter.FakeProviderAdapter"
        assert len(binding.adapter_implementation_checksum) == 64
        assert binding.status == "active"

        resolved = ar.resolve_adapter("fake", "fake-model", "video.text_to_video", db_path=db_path)
        assert resolved.adapter_implementation_checksum == binding.adapter_implementation_checksum

    def test_checksum_is_module_source_sha256(self, tmp_path):
        import hashlib
        import inspect
        db_path = tmp_path / "adapters.db"
        binding = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        expected = hashlib.sha256(inspect.getsource(inspect.getmodule(FakeProviderAdapter)).encode("utf-8")).hexdigest()
        assert binding.adapter_implementation_checksum == expected

    def test_idempotent_replay_with_identical_implementation(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        b1 = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        b2 = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        assert b1.adapter_implementation_checksum == b2.adapter_implementation_checksum
        assert b1.registration_id == b2.registration_id

    def test_empty_key_component_raises_registration_error(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        with pytest.raises(ar.AdapterRegistrationError):
            ar.register_adapter("", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)

    def test_different_operation_same_provider_model_both_coexist(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        b1 = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        b2 = ar.register_adapter("fake", "fake-model", "video.image_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        assert b1.registration_id != b2.registration_id
        assert ar.resolve_adapter("fake", "fake-model", "video.text_to_video", db_path=db_path) is not None
        assert ar.resolve_adapter("fake", "fake-model", "video.image_to_video", db_path=db_path) is not None


class TestUnregisteredResolutionFailsClosed:

    def test_resolve_with_no_registration_raises(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        with pytest.raises(ar.UnregisteredAdapterError):
            ar.resolve_adapter("nobody", "no-model", "video.text_to_video", db_path=db_path)

    def test_resolve_via_conn_with_no_registration_raises(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            with pytest.raises(ar.UnregisteredAdapterError):
                ar.resolve_adapter_via_conn(conn, "nobody", "no-model", "video.text_to_video")
        finally:
            conn.close()

    def test_wrong_operation_for_registered_provider_model_fails_closed(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
        with pytest.raises(ar.UnregisteredAdapterError):
            ar.resolve_adapter("fake", "fake-model", "voice.narration_segment", db_path=db_path)


class _FakeProviderAdapterVariant(FakeProviderAdapter):
    """A distinct class object (different module-relative identity from
    FakeProviderAdapter's own module source) used only to prove that a
    changed implementation is detected -- never registered against a
    real production key."""


class TestImplementationChecksumDrift:

    def test_registering_same_key_with_different_class_raises_integrity_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / "adapters.db"
        ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)

        # Simulate "the registered implementation's source changed" by
        # registering the SAME key with a checksum function that returns
        # a different value for the same class -- mirrors how
        # workflow_registry's same-version-different-content test forces
        # a genuine content mismatch without needing a second real file.
        monkeypatch.setattr(ar, "compute_adapter_implementation_checksum", lambda cls: "deadbeef" * 8)
        with pytest.raises(ar.AdapterIntegrityError):
            ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)

    def test_load_adapter_class_returns_identical_object_across_calls(self):
        # importlib caches already-imported modules -- load_adapter_class
        # must return the SAME class object FakeProviderAdapter already
        # is, since the Adapter Instance Cache is keyed by class identity.
        loaded = ar.load_adapter_class("hypogeum.providers.fake_provider_adapter.FakeProviderAdapter")
        assert loaded is FakeProviderAdapter

    def test_malformed_qualified_name_raises_registration_error(self):
        with pytest.raises(ar.AdapterRegistrationError):
            ar.load_adapter_class("not_a_qualified_name")


class TestInstanceCache:

    def test_get_or_create_returns_same_instance(self):
        a = ar.get_or_create_adapter_instance(FakeProviderAdapter)
        b = ar.get_or_create_adapter_instance(FakeProviderAdapter)
        assert a is b

    def test_reset_produces_a_fresh_instance(self):
        a = ar.get_or_create_adapter_instance(FakeProviderAdapter)
        ar.reset_adapter_instance_cache()
        b = ar.get_or_create_adapter_instance(FakeProviderAdapter)
        assert a is not b


class TestConcurrentRegistrationRace:

    def test_two_connections_racing_to_register_same_new_key(self, tmp_path):
        db_path = tmp_path / "adapters.db"
        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def _attempt(name):
            barrier.wait()
            try:
                results[name] = ar.register_adapter("fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path)
            except Exception as exc:  # noqa: BLE001
                errors[name] = exc

        t1 = threading.Thread(target=_attempt, args=("t1",))
        t2 = threading.Thread(target=_attempt, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors or all(isinstance(e, sqlite3.OperationalError) for e in errors.values())
        succeeded = list(results.values())
        assert len(succeeded) >= 1
        checksums = {b.adapter_implementation_checksum for b in succeeded}
        assert len(checksums) == 1

        final = ar.resolve_adapter("fake", "fake-model", "video.text_to_video", db_path=db_path)
        assert final is not None


class _TempGitAdapterModule:
    """Builds a minimal, real git repo in tmp_path containing one
    importable adapter-like module, so supersession tests exercise the
    real git-backed committed-source verification path end to end --
    never mocked, never a fake commit hash."""

    def __init__(self, tmp_path, sys_path_monkeypatch):
        import subprocess

        self.subprocess = subprocess
        self.repo_root = tmp_path / "fake_repo"
        self.repo_root.mkdir()
        self._run(["git", "init", "-q"])
        self._run(["git", "config", "user.email", "test@example.com"])
        self._run(["git", "config", "user.name", "Test"])

        self.module_path = self.repo_root / "temp_adapter_module.py"
        self._write("class TempAdapter:\n    version = 1\n")
        self._run(["git", "add", "temp_adapter_module.py"])
        self._run(["git", "commit", "-q", "-m", "v1"])
        self.commit_v1 = self._head()

        sys_path_monkeypatch.syspath_prepend(str(self.repo_root))

    def _run(self, args):
        self.subprocess.run(args, cwd=str(self.repo_root), check=True, capture_output=True)

    def _write(self, content):
        self.module_path.write_text(content)

    def _head(self):
        out = self.subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo_root), check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()

    def import_class(self):
        import importlib
        import temp_adapter_module  # noqa: PLC0415
        importlib.reload(temp_adapter_module)
        return temp_adapter_module.TempAdapter

    def commit_new_version(self, version: int) -> str:
        self._write(f"class TempAdapter:\n    version = {version}\n")
        self._run(["git", "add", "temp_adapter_module.py"])
        self._run(["git", "commit", "-q", "-m", f"v{version}"])
        return self._head()

    def make_dirty(self, version: int) -> None:
        self._write(f"class TempAdapter:\n    version = {version}  # uncommitted\n")


@pytest.fixture
def git_adapter_module(tmp_path, monkeypatch):
    mod = _TempGitAdapterModule(tmp_path, monkeypatch)
    yield mod


class TestAdapterSupersession:

    def _register_v1(self, git_adapter_module, db_path):
        cls = git_adapter_module.import_class()
        binding = ar.register_adapter("fake", "temp-model", "video.text_to_video", "1.0", cls, db_path=db_path)
        assert binding.registration_version == 1
        assert binding.status == "active"
        return binding, cls

    def test_successful_supersession_preserves_old_row_and_activates_new_one(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()

        new_binding = ar.supersede_adapter_registration(
            "fake", "temp-model", "video.text_to_video", cls_v2,
            previous_checksum=v1_binding.adapter_implementation_checksum,
            actor="test_actor", reason="legitimate code change", source_commit=commit_v2,
            confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
        )
        assert new_binding.registration_version == 2
        assert new_binding.status == "active"
        assert new_binding.adapter_implementation_checksum != v1_binding.adapter_implementation_checksum

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            old_row = conn.execute(
                "SELECT * FROM adapter_registrations WHERE registration_id = ? AND registration_version = 1",
                (v1_binding.registration_id,),
            ).fetchone()
            assert old_row["status"] == "superseded"
            assert old_row["adapter_implementation_checksum"] == v1_binding.adapter_implementation_checksum
            assert old_row["registered_at"] == v1_binding.registered_at

            history = conn.execute(
                "SELECT * FROM adapter_registration_supersessions WHERE registration_id = ?",
                (v1_binding.registration_id,),
            ).fetchall()
            assert len(history) == 1
            h = history[0]
            assert h["old_version"] == 1 and h["new_version"] == 2
            assert h["actor"] == "test_actor" and h["reason"] == "legitimate code change"
            assert h["source_commit"] == commit_v2
        finally:
            conn.close()

        # resolution now returns only the new active row
        resolved = ar.resolve_adapter("fake", "temp-model", "video.text_to_video", db_path=db_path)
        assert resolved.registration_version == 2
        assert resolved.adapter_implementation_checksum == new_binding.adapter_implementation_checksum

    def test_unchanged_checksum_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        v1_binding, cls = self._register_v1(git_adapter_module, db_path)
        with pytest.raises(ar.AdapterSupersessionError, match="unchanged checksum"):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls,
                previous_checksum=v1_binding.adapter_implementation_checksum,
                actor="a", reason="r", source_commit=git_adapter_module.commit_v1,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_wrong_previous_checksum_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()
        with pytest.raises(ar.AdapterSupersessionError, match="wrong previous checksum"):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_v2,
                previous_checksum="0" * 64,
                actor="a", reason="r", source_commit=commit_v2,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_uncommitted_adapter_code_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        git_adapter_module.make_dirty(2)
        cls_dirty = git_adapter_module.import_class()
        with pytest.raises(ar.AdapterSupersessionError):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_dirty,
                previous_checksum=v1_binding.adapter_implementation_checksum,
                actor="a", reason="r", source_commit=git_adapter_module.commit_v1,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_provider_model_operation_mismatch_rejected(self, git_adapter_module, tmp_path):
        # Defensive, data-integrity-only check: registration_id is
        # itself derived from (provider, model, provider_operation), so
        # a genuine mismatch can only arise if a row's own stored
        # columns disagree with what its registration_id encodes (e.g.
        # a hand-edited or legacy-migrated row) -- simulate exactly
        # that rather than a caller simply naming an operation with no
        # active registration at all (a different, already-covered
        # "nothing to supersede" case).
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE adapter_registrations SET provider_operation = 'voice.narration_segment' "
            "WHERE registration_id = ? AND registration_version = 1",
            (v1_binding.registration_id,),
        )
        conn.commit()
        conn.close()

        with pytest.raises(ar.AdapterSupersessionError, match="mismatch"):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_v2,
                previous_checksum=v1_binding.adapter_implementation_checksum,
                actor="a", reason="r", source_commit=commit_v2,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_unauthorized_supersede_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()
        with pytest.raises(ar.AdapterSupersessionError, match="unauthorized"):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_v2,
                previous_checksum=v1_binding.adapter_implementation_checksum,
                actor="a", reason="r", source_commit=commit_v2,
                confirm_supersede=False, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_rollback_to_earlier_checksum_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()
        v2_binding = ar.supersede_adapter_registration(
            "fake", "temp-model", "video.text_to_video", cls_v2,
            previous_checksum=v1_binding.adapter_implementation_checksum,
            actor="a", reason="r", source_commit=commit_v2,
            confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
        )
        # Recommit the ORIGINAL v1 content as a new commit (v3 in git
        # history, but byte-identical to v1's source) and attempt to
        # supersede back onto it -- must be rejected as a rollback even
        # though it is a "new" commit.
        git_adapter_module._write("class TempAdapter:\n    version = 1\n")
        git_adapter_module._run(["git", "add", "temp_adapter_module.py"])
        git_adapter_module._run(["git", "commit", "-q", "-m", "revert to v1 content"])
        commit_v3 = git_adapter_module._head()
        cls_v1_again = git_adapter_module.import_class()
        with pytest.raises(ar.AdapterSupersessionError, match="rollback"):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_v1_again,
                previous_checksum=v2_binding.adapter_implementation_checksum,
                actor="a", reason="r", source_commit=commit_v3,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )

    def test_concurrent_conflicting_supersede_rejected(self, git_adapter_module, tmp_path, monkeypatch):
        # Real two-thread race: both threads read the SAME active v1 row
        # (same previous_checksum), then race to be the one whose
        # conditional UPDATE ... WHERE version = ? AND status = 'active'
        # actually flips it -- exactly one may win; the loser must see
        # AdapterSupersessionConflictError, never a silent double-apply.
        db_path = tmp_path / "adapters.db"
        v1_binding, _ = self._register_v1(git_adapter_module, db_path)
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()

        barrier = threading.Barrier(2)
        real_get_connection = ar.get_connection

        def _synced_get_connection(path):
            conn = real_get_connection(path)
            barrier.wait(timeout=10)
            return conn

        monkeypatch.setattr(ar, "get_connection", _synced_get_connection)

        results, errors = {}, {}

        def _attempt(name):
            try:
                results[name] = ar.supersede_adapter_registration(
                    "fake", "temp-model", "video.text_to_video", cls_v2,
                    previous_checksum=v1_binding.adapter_implementation_checksum,
                    actor="a", reason="r", source_commit=commit_v2,
                    confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
                )
            except Exception as exc:  # noqa: BLE001
                errors[name] = exc

        t1 = threading.Thread(target=_attempt, args=("t1",))
        t2 = threading.Thread(target=_attempt, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # SQLite's BEGIN IMMEDIATE fully serializes the two transactions
        # (the second cannot even start until the first commits), so the
        # loser observes the winner's already-committed new active row --
        # manifesting as either the internal rowcount-based conflict
        # check (a true intra-transaction TOCTOU) or, more commonly under
        # this serialization, "wrong previous checksum" once its own
        # (now-stale) previous_checksum no longer matches. Both are
        # correct, safe outcomes: exactly one winner, zero double-apply,
        # zero corruption -- never both threads succeeding.
        assert len(results) == 1
        assert len(errors) == 1
        loser = next(iter(errors.values()))
        assert isinstance(loser, (ar.AdapterSupersessionConflictError, ar.AdapterSupersessionError, sqlite3.OperationalError))
        winner = next(iter(results.values()))
        assert winner.registration_version == 2

        monkeypatch.undo()
        final = ar.resolve_adapter("fake", "temp-model", "video.text_to_video", db_path=db_path)
        assert final.registration_version == 2

    def test_no_active_registration_to_supersede_rejected(self, git_adapter_module, tmp_path):
        db_path = tmp_path / "adapters.db"
        commit_v2 = git_adapter_module.commit_new_version(2)
        cls_v2 = git_adapter_module.import_class()
        with pytest.raises(ar.AdapterSupersessionError):
            ar.supersede_adapter_registration(
                "fake", "temp-model", "video.text_to_video", cls_v2,
                previous_checksum="0" * 64,
                actor="a", reason="r", source_commit=commit_v2,
                confirm_supersede=True, repo_root=git_adapter_module.repo_root, db_path=db_path,
            )
