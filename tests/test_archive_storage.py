"""hypogeum/archive_storage.py — archive-root resolution, validation, reuse log."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema, get_connection, set_factory_setting


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "arch.db"
    initialize_schema(p)
    return p


def test_default_root_when_unset(dbp, tmp_path):
    from hypogeum import archive_storage as st
    info = st.describe_archive_root(db_path=dbp)
    assert info["source"] == "default"
    assert info["is_default"] is True
    root = st.resolve_archive_root(db_path=dbp)
    assert root.is_absolute() and root.exists()


def test_db_setting_wins_and_is_resolved(dbp, tmp_path):
    from hypogeum import archive_storage as st
    target = tmp_path / "server-archive"
    st.set_archive_root(str(target), db_path=dbp)
    info = st.describe_archive_root(db_path=dbp)
    assert info["source"] == "db"
    assert st.resolve_archive_root(db_path=dbp) == target.resolve()
    assert target.exists()


def test_validate_rejects_relative_and_working_tree(dbp):
    from hypogeum import archive_storage as st
    ok, err = st.validate_archive_root("relative/path")
    assert not ok and "absolute" in err
    ok, err = st.validate_archive_root(str(st._WORKING_EPISODES_DIR / "x"))
    assert not ok and "working episodes" in err


def test_bad_db_value_falls_back_to_default(dbp):
    from hypogeum import archive_storage as st
    set_factory_setting("archive_root", "not/absolute", db_path=dbp)
    # resolve must not raise, must land on the default
    root = st.resolve_archive_root(db_path=dbp)
    assert root == st._DEFAULT_ARCHIVE_ROOT


def test_record_asset_reuse_appends_episode(dbp):
    from hypogeum import archive_storage as st
    conn = get_connection(dbp)
    try:
        from hypogeum.db import insert_archive_index_entry
        insert_archive_index_entry(
            conn, archive_id="a1", asset_id="asset_x", episode_id="ep_src",
            asset_type="clip", provider="muapi", archived_at="2026-01-01T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    assert st.record_asset_reuse("asset_x", "ep_new", db_path=dbp) is True
    conn = get_connection(dbp)
    try:
        raw = conn.execute(
            "SELECT used_in_episodes FROM archive_index WHERE archive_id='a1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert set(json.loads(raw)) == {"ep_src", "ep_new"}
    # idempotent
    assert st.record_asset_reuse("asset_x", "ep_new", db_path=dbp) is False
