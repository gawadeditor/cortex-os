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
