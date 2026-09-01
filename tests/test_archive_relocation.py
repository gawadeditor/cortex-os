"""hypogeum/archive_relocation.py — whole-tree move + catalog repoint."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema, get_connection, insert_archive_index_entry, set_factory_setting


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "rl.db"
    initialize_schema(p)
    return p


def _mark_published(dbp, episode_id, channel_id="hypogeum"):
    conn = get_connection(dbp)
    try:
        conn.execute(
            "INSERT INTO publication_intents "
            "(publication_intent_id, channel_id, episode_id, deliverable_id, platform, "
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("pi_1", channel_id, episode_id, "main_episode", "youtube",
             "succeeded", "2026-08-01T00:00:00+00:00", "2026-08-02T12:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_episode_dir(root, episode_id):
    ep = root / episode_id
    (ep / "video").mkdir(parents=True)
    (ep / "thumbnails").mkdir(parents=True)
    (ep / "video" / "sc_s01.mp4").write_bytes(b"x" * 100)
    (ep / "video" / "sc_s02.mp4").write_bytes(b"y" * 200)
    (ep / "thumbnails" / "main.jpg").write_bytes(b"z" * 50)
    (ep / "final_render_package.json").write_text("{}")
    return ep


def test_relocates_whole_tree_and_repoints_catalog(dbp, tmp_path):
    from hypogeum import archive_relocation as rl

    episode_id = "ep_pub1"
    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, episode_id)
    archive_root = tmp_path / "server-archive"
    set_factory_setting("archive_root", str(archive_root), db_path=dbp)
    _mark_published(dbp, episode_id)

    conn = get_connection(dbp)
    try:
        insert_archive_index_entry(
            conn, archive_id="a1", asset_id="episodes/ep_pub1/video/sc_s01",
            episode_id=episode_id, asset_type="clip", provider="muapi",
            archived_at="2026-08-01T00:00:00+00:00",
            file_path="episodes/ep_pub1/video/sc_s01.mp4",
        )
        conn.commit()
    finally:
        conn.close()

    rec = rl.relocate_episode_assets(
        episode_id, "hypogeum", db_path=dbp, output_dir=ep_dir,
        catalog_dir=tmp_path / "catalogs",
    )
    assert rec["verdict"] in ("RELOCATED", "WARNING")
    assert rec["relocated"] is True
    assert rec["files_moved"] == 4
    assert not ep_dir.exists()
    dst = archive_root / "hypogeum" / episode_id
    assert (dst / "video" / "sc_s01.mp4").read_bytes() == b"x" * 100
    assert (dst / "final_render_package.json").exists()

    conn = get_connection(dbp)
    try:
        row = conn.execute(
            "SELECT archive_location, moved_at, storage_status, published_at "
            "FROM archive_index WHERE archive_id='a1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["archive_location"] == str(dst / "video" / "sc_s01.mp4")
    assert row["moved_at"] is not None
    assert row["storage_status"] == "cold_archived"
    assert row["published_at"] == "2026-08-02T12:00:00+00:00"


def test_second_call_is_noop(dbp, tmp_path):
    from hypogeum import archive_relocation as rl
    episode_id = "ep_pub2"
    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, episode_id)
    set_factory_setting("archive_root", str(tmp_path / "arch"), db_path=dbp)
    _mark_published(dbp, episode_id)
    conn = get_connection(dbp)
    try:
        insert_archive_index_entry(
            conn, archive_id="b1", asset_id="x", episode_id=episode_id,
            asset_type="clip", provider="muapi", archived_at="2026-08-01T00:00:00+00:00",
            file_path="episodes/ep_pub2/video/sc_s01.mp4",
        )
        conn.commit()
    finally:
        conn.close()

    rl.relocate_episode_assets(episode_id, db_path=dbp, output_dir=ep_dir,
                               catalog_dir=tmp_path / "c")
    rec2 = rl.relocate_episode_assets(episode_id, db_path=dbp, output_dir=ep_dir,
                                      catalog_dir=tmp_path / "c")
    assert rec2["verdict"] == "SKIPPED"
    assert rec2["relocated"] is True


def test_unpublished_episode_is_not_relocated(dbp, tmp_path):
    from hypogeum import archive_relocation as rl
    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, "ep_unpub")
    rec = rl.relocate_episode_assets("ep_unpub", db_path=dbp, output_dir=ep_dir,
                                     catalog_dir=tmp_path / "c")
    assert rec["verdict"] == "SKIPPED"
    assert rec["relocated"] is False
    assert ep_dir.exists()


def test_rollback_on_move_failure(dbp, tmp_path, monkeypatch):
    from hypogeum import archive_relocation as rl
    episode_id = "ep_fail"
    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, episode_id)
    set_factory_setting("archive_root", str(tmp_path / "arch"), db_path=dbp)
    _mark_published(dbp, episode_id)

    calls = {"n": 0}
    real_move = rl.shutil.move

    def flaky_move(s, d):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(s, d)

    monkeypatch.setattr(rl.shutil, "move", flaky_move)
    rec = rl.relocate_episode_assets(episode_id, db_path=dbp, output_dir=ep_dir,
                                     catalog_dir=tmp_path / "c")
    assert rec["verdict"] == "FAILED"
    # every original file is back
    assert (ep_dir / "video" / "sc_s01.mp4").exists()
    assert (ep_dir / "video" / "sc_s02.mp4").exists()
    assert (ep_dir / "thumbnails" / "main.jpg").exists()
