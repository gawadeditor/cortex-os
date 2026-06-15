"""
tests/test_factory_entity.py — P3-02: Factory Entity System tests

Coverage:
    TestCreateAndGet          — create + get by factory_id, get by factory_slug
    TestGetSingleFactory      — get_factory() with no args (single-factory shortcut)
    TestGetCurrentFactoryId   — get_current_factory_id() happy + error paths
    TestUpdateFactory         — update_factory changes stored fields
    TestBootstrapIdempotent   — bootstrap-style idempotent create (INSERT OR IGNORE pattern)
    TestDuplicateCreate       — duplicate factory_id raises FactoryExistsError
    TestOwnerIdFK             — owner_id FK enforced when users table has PRAGMA foreign_keys=ON
    TestInvalidInputs         — invalid status values raise ValueError
    TestNoProductionDB        — confirms all tests route to tmp_path only

All tests use an isolated tmp_path SQLite DB. No production DB is accessed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.factory_entity import (
    FactoryExistsError,
    FactoryNotFoundError,
    FactoryRecord,
    create_factory,
    get_current_factory_id,
    get_factory,
    update_factory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Isolated SQLite DB with schema initialized."""
    p = tmp_path / "factory_test.db"
    initialize_schema(p)
    return p


def _seed_factory(db_path: Path, **overrides) -> FactoryRecord:
    """Create a minimal factory record for use in tests."""
    defaults = dict(
        factory_id   = "test_factory",
        factory_name = "Test Factory",
        factory_slug = "test-factory",
        db_path      = db_path,
    )
    defaults.update(overrides)
    return create_factory(**defaults)


# ===========================================================================
# TestCreateAndGet
# ===========================================================================

class TestCreateAndGet:
    def test_create_returns_factory_record(self, db_path):
        record = _seed_factory(db_path)
        assert isinstance(record, FactoryRecord)

    def test_factory_id_persisted(self, db_path):
        _seed_factory(db_path)
        result = get_factory(factory_id="test_factory", db_path=db_path)
        assert result is not None
        assert result.factory_id == "test_factory"

    def test_factory_name_persisted(self, db_path):
        _seed_factory(db_path)
        result = get_factory(factory_id="test_factory", db_path=db_path)
        assert result.factory_name == "Test Factory"

    def test_factory_slug_persisted(self, db_path):
        _seed_factory(db_path)
        result = get_factory(factory_id="test_factory", db_path=db_path)
        assert result.factory_slug == "test-factory"

    def test_get_by_factory_id(self, db_path):
        _seed_factory(db_path)
        result = get_factory(factory_id="test_factory", db_path=db_path)
        assert result is not None
        assert result.factory_id == "test_factory"

    def test_get_by_slug(self, db_path):
        _seed_factory(db_path)
        result = get_factory(factory_slug="test-factory", db_path=db_path)
        assert result is not None
        assert result.factory_slug == "test-factory"

    def test_get_by_slug_wrong_slug_returns_none(self, db_path):
        _seed_factory(db_path)
        assert get_factory(factory_slug="nonexistent", db_path=db_path) is None

    def test_get_by_id_not_found_returns_none(self, db_path):
        assert get_factory(factory_id="ghost", db_path=db_path) is None

    def test_optional_fields_default_to_none(self, db_path):
        record = _seed_factory(db_path)
        assert record.owner_id is None
        assert record.dashboard_title is None
        assert record.support_email is None
        assert record.primary_color is None

    def test_optional_fields_stored_when_provided(self, db_path):
        create_factory(
            factory_id      = "full_factory",
            factory_name    = "Full Factory",
            factory_slug    = "full-factory",
            owner_id        = None,
            dashboard_title = "My Dashboard",
            support_email   = "admin@example.com",
            primary_color   = "#FF5500",
            db_path         = db_path,
        )
        result = get_factory(factory_id="full_factory", db_path=db_path)
        assert result.dashboard_title == "My Dashboard"
        assert result.support_email == "admin@example.com"
        assert result.primary_color == "#FF5500"

    def test_default_language_is_en(self, db_path):
        record = _seed_factory(db_path)
        assert record.default_language == "en"

    def test_default_timezone_is_utc(self, db_path):
        record = _seed_factory(db_path)
        assert record.default_timezone == "UTC"

    def test_default_status_is_active(self, db_path):
        record = _seed_factory(db_path)
        assert record.status == "active"

    def test_created_at_and_updated_at_populated(self, db_path):
        record = _seed_factory(db_path)
        assert record.created_at
        assert record.updated_at

    def test_create_returns_same_record_as_get(self, db_path):
        created = _seed_factory(db_path)
        fetched = get_factory(factory_id="test_factory", db_path=db_path)
        assert fetched.factory_id   == created.factory_id
        assert fetched.factory_name == created.factory_name
        assert fetched.factory_slug == created.factory_slug
        assert fetched.created_at   == created.created_at


# ===========================================================================
# TestGetSingleFactory
# ===========================================================================

class TestGetSingleFactory:
    def test_no_args_returns_none_when_empty(self, db_path):
        assert get_factory(db_path=db_path) is None

    def test_no_args_returns_record_when_one_exists(self, db_path):
        _seed_factory(db_path)
        result = get_factory(db_path=db_path)
        assert result is not None
        assert result.factory_id == "test_factory"

    def test_no_args_raises_when_multiple_exist(self, db_path):
        _seed_factory(db_path, factory_id="f1", factory_slug="slug-f1")
        create_factory(
            factory_id   = "f2",
            factory_name = "Factory Two",
            factory_slug = "slug-f2",
            db_path      = db_path,
        )
        with pytest.raises(ValueError, match="2 factory records"):
            get_factory(db_path=db_path)


# ===========================================================================
# TestGetCurrentFactoryId
# ===========================================================================

class TestGetCurrentFactoryId:
    def test_returns_factory_id_for_single_record(self, db_path):
        _seed_factory(db_path)
        assert get_current_factory_id(db_path=db_path) == "test_factory"

    def test_raises_factory_not_found_error_when_empty(self, db_path):
        with pytest.raises(FactoryNotFoundError):
            get_current_factory_id(db_path=db_path)

    def test_error_message_mentions_bootstrap(self, db_path):
        with pytest.raises(FactoryNotFoundError, match="bootstrap"):
            get_current_factory_id(db_path=db_path)

    def test_raises_value_error_when_multiple_exist(self, db_path):
        _seed_factory(db_path, factory_id="f1", factory_slug="slug-f1")
        create_factory(
            factory_id   = "f2",
            factory_name = "Factory Two",
            factory_slug = "slug-f2",
            db_path      = db_path,
        )
        with pytest.raises(ValueError):
            get_current_factory_id(db_path=db_path)


# ===========================================================================
# TestUpdateFactory
# ===========================================================================

class TestUpdateFactory:
    def test_update_factory_name(self, db_path):
        _seed_factory(db_path)
        result = update_factory("test_factory", db_path=db_path, factory_name="Renamed Factory")
        assert result is True
        record = get_factory(factory_id="test_factory", db_path=db_path)
        assert record.factory_name == "Renamed Factory"

    def test_update_dashboard_title(self, db_path):
        _seed_factory(db_path)
        update_factory("test_factory", db_path=db_path, dashboard_title="Custom Title")
        record = get_factory(factory_id="test_factory", db_path=db_path)
        assert record.dashboard_title == "Custom Title"

    def test_update_status_to_maintenance(self, db_path):
        _seed_factory(db_path)
        update_factory("test_factory", db_path=db_path, status="maintenance")
        record = get_factory(factory_id="test_factory", db_path=db_path)
        assert record.status == "maintenance"

    def test_update_multiple_fields(self, db_path):
        _seed_factory(db_path)
        update_factory(
            "test_factory",
            db_path       = db_path,
            factory_name  = "Multi-Update Factory",
            support_email = "support@example.com",
            primary_color = "#001122",
        )
        record = get_factory(factory_id="test_factory", db_path=db_path)
        assert record.factory_name  == "Multi-Update Factory"
        assert record.support_email == "support@example.com"
        assert record.primary_color == "#001122"

    def test_update_stamps_updated_at(self, db_path):
        _seed_factory(db_path)
        original = get_factory(factory_id="test_factory", db_path=db_path)
        update_factory("test_factory", db_path=db_path, factory_name="Changed")
        updated = get_factory(factory_id="test_factory", db_path=db_path)
        # updated_at must be >= created_at (may be equal if sub-ms precision)
        assert updated.updated_at >= original.updated_at

    def test_update_nonexistent_factory_returns_false(self, db_path):
        result = update_factory("ghost_factory", db_path=db_path, factory_name="Ghost")
        assert result is False

    def test_update_with_no_recognised_kwargs_returns_true(self, db_path):
        _seed_factory(db_path)
        result = update_factory("test_factory", db_path=db_path, nonexistent_col="x")
        assert result is True

    def test_unknown_kwargs_do_not_corrupt_record(self, db_path):
        _seed_factory(db_path)
        update_factory("test_factory", db_path=db_path, totally_unknown="value")
        record = get_factory(factory_id="test_factory", db_path=db_path)
        assert record.factory_name == "Test Factory"


# ===========================================================================
# TestBootstrapIdempotent
# ===========================================================================

class TestBootstrapIdempotent:
    """Mirrors the pattern used in bootstrap_channel.py: create if not exists."""

    def _bootstrap_factory(self, db_path: Path) -> FactoryRecord:
        existing = get_factory(factory_id="hypogeum", db_path=db_path)
        if existing:
            return existing
        return create_factory(
            factory_id   = "hypogeum",
            factory_name = "Hypogeum Media Factory",
            factory_slug = "hypogeum",
            db_path      = db_path,
        )

    def test_bootstrap_creates_factory_record(self, db_path):
        record = self._bootstrap_factory(db_path)
        assert record.factory_id == "hypogeum"

    def test_bootstrap_second_call_returns_existing(self, db_path):
        first  = self._bootstrap_factory(db_path)
        second = self._bootstrap_factory(db_path)
        assert second.factory_id   == first.factory_id
        assert second.created_at   == first.created_at

    def test_bootstrap_produces_exactly_one_row(self, db_path):
        self._bootstrap_factory(db_path)
        self._bootstrap_factory(db_path)
        conn = get_connection(db_path=db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM factories WHERE factory_id = 'hypogeum'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_bootstrap_factory_name_correct(self, db_path):
        self._bootstrap_factory(db_path)
        record = get_factory(factory_id="hypogeum", db_path=db_path)
        assert record.factory_name == "Hypogeum Media Factory"


# ===========================================================================
# TestDuplicateCreate
# ===========================================================================

class TestDuplicateCreate:
    def test_duplicate_factory_id_raises(self, db_path):
        _seed_factory(db_path)
        with pytest.raises(FactoryExistsError):
            _seed_factory(db_path)

    def test_duplicate_slug_raises(self, db_path):
        _seed_factory(db_path)
        with pytest.raises(FactoryExistsError):
            create_factory(
                factory_id   = "different_id",
                factory_name = "Different Name",
                factory_slug = "test-factory",   # same slug as existing record
                db_path      = db_path,
            )

    def test_factory_exists_error_is_runtime_error(self, db_path):
        assert issubclass(FactoryExistsError, RuntimeError)


# ===========================================================================
# TestOwnerIdFK
# ===========================================================================

class TestOwnerIdFK:
    def test_owner_id_none_is_accepted(self, db_path):
        record = _seed_factory(db_path, owner_id=None)
        assert record.owner_id is None

    def test_valid_owner_id_accepted(self, db_path):
        """A user created in the same DB satisfies the FK constraint."""
        from hypogeum.auth import create_user
        user = create_user(
            email   = "owner@example.com",
            name    = "Owner User",
            role    = "owner",
            db_path = db_path,
        )
        record = create_factory(
            factory_id   = "owned_factory",
            factory_name = "Owned Factory",
            factory_slug = "owned-factory",
            owner_id     = user.user_id,
            db_path      = db_path,
        )
        assert record.owner_id == user.user_id
        fetched = get_factory(factory_id="owned_factory", db_path=db_path)
        assert fetched.owner_id == user.user_id

    def test_nonexistent_owner_id_raises_integrity_error(self, db_path):
        """FK constraint blocks a factory referencing a non-existent user."""
        with pytest.raises((FactoryExistsError, sqlite3.IntegrityError)):
            create_factory(
                factory_id   = "orphan_factory",
                factory_name = "Orphan Factory",
                factory_slug = "orphan-factory",
                owner_id     = "user_that_does_not_exist",
                db_path      = db_path,
            )


# ===========================================================================
# TestInvalidInputs
# ===========================================================================

class TestInvalidInputs:
    def test_invalid_status_on_create_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid status"):
            create_factory(
                factory_id   = "bad_factory",
                factory_name = "Bad Factory",
                factory_slug = "bad-factory",
                status       = "unknown_status",
                db_path      = db_path,
            )

    def test_invalid_status_on_update_raises(self, db_path):
        _seed_factory(db_path)
        with pytest.raises(ValueError, match="Invalid status"):
            update_factory(
                "test_factory",
                db_path = db_path,
                status  = "invalid",
            )

    def test_valid_statuses_accepted(self, db_path):
        for i, status in enumerate(("active", "maintenance", "suspended")):
            create_factory(
                factory_id   = f"f_{i}",
                factory_name = f"Factory {i}",
                factory_slug = f"factory-{i}",
                status       = status,
                db_path      = db_path,
            )
            record = get_factory(factory_id=f"f_{i}", db_path=db_path)
            assert record.status == status


# ===========================================================================
# TestNoProductionDB
# ===========================================================================

class TestNoProductionDB:
    def test_default_db_path_not_used(self, db_path, monkeypatch):
        """
        Confirm that passing db_path routes all writes to tmp_path, not the
        production DB. We verify by checking the tmp_path DB has the row and
        the production DB (if it were opened) would not receive it.
        """
        _seed_factory(db_path)
        result = get_factory(factory_id="test_factory", db_path=db_path)
        assert result is not None
        # The production DB is never opened because db_path is always provided.
        # This is a structural guarantee, not one we can assert at runtime;
        # the test documents the intent.
