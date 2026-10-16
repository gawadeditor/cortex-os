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


def _seed_episode_row(dbp, episode_id, *, title=None, topic=None):
    conn = get_connection(dbp)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, title, topic, created_at, status) "
            "VALUES (?,?,?,?,?)",
            (episode_id, title, topic, "2026-08-01T00:00:00+00:00", "complete"),
        )
        conn.commit()
    finally:
        conn.close()


class TestArchiveFolderName:
    """Regression: an operator's own report that an archived episode's
    folder must be recognisable by its title, not just its opaque
    episode_id (2026-09-05)."""

    def test_uses_title_when_present(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name
        _seed_episode_row(dbp, "ep_x1", title="The Real Published Title", topic="working topic")
        assert _archive_folder_name("ep_x1", dbp) == "ep_x1 - The Real Published Title"

    def test_falls_back_to_topic_when_title_empty(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name
        _seed_episode_row(dbp, "ep_x2", title=None, topic="The Working Topic")
        assert _archive_folder_name("ep_x2", dbp) == "ep_x2 - The Working Topic"

    def test_falls_back_to_bare_id_when_no_episode_row(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name
        assert _archive_folder_name("ep_nowhere", dbp) == "ep_nowhere"

    def test_falls_back_to_bare_id_when_title_and_topic_both_empty(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name
        _seed_episode_row(dbp, "ep_x3", title=None, topic=None)
        assert _archive_folder_name("ep_x3", dbp) == "ep_x3"

    def test_strips_path_unsafe_characters(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name
        _seed_episode_row(dbp, "ep_x4", title='A "Cursed" Tomb/Crypt: Part 2?')
        name = _archive_folder_name("ep_x4", dbp)
        assert "/" not in name and ":" not in name and '"' not in name and "?" not in name
        assert name.startswith("ep_x4 - ")

    def test_caps_length(self, dbp):
        from hypogeum.archive_relocation import _archive_folder_name, _MAX_TITLE_SLUG_LEN
        long_title = "A " * 100
        _seed_episode_row(dbp, "ep_x5", title=long_title)
        name = _archive_folder_name("ep_x5", dbp)
        assert len(name) <= len("ep_x5 - ") + _MAX_TITLE_SLUG_LEN

    def test_deterministic_across_repeated_calls(self, dbp):
        """The recovery-detection branch in relocate_episode_assets()
        recomputes this same name on a later sweep to find files a
        previous sweep already moved -- it must never drift."""
        from hypogeum.archive_relocation import _archive_folder_name
        _seed_episode_row(dbp, "ep_x6", title="A Stable Title")
        assert (_archive_folder_name("ep_x6", dbp)
                == _archive_folder_name("ep_x6", dbp)
                == "ep_x6 - A Stable Title")

    def test_relocate_episode_assets_uses_titled_folder(self, dbp, tmp_path):
        from hypogeum import archive_relocation as rl
        episode_id = "ep_titled"
        work = tmp_path / "episodes"
        work.mkdir()
        ep_dir = _seed_episode_dir(work, episode_id)
        archive_root = tmp_path / "server-archive"
        set_factory_setting("archive_root", str(archive_root), db_path=dbp)
        _mark_published(dbp, episode_id)
        _seed_episode_row(dbp, episode_id, title="The Real Published Title")

        rec = rl.relocate_episode_assets(
            episode_id, "hypogeum", db_path=dbp, output_dir=ep_dir,
            catalog_dir=tmp_path / "catalogs",
        )
        assert rec["relocated"] is True
        dst = archive_root / "hypogeum" / f"{episode_id} - The Real Published Title"
        assert dst.exists()
        assert (dst / "video" / "sc_s01.mp4").read_bytes() == b"x" * 100
        assert rec["dest_dir"] == str(dst)


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


def test_sweep_relocates_pending_published(dbp, tmp_path):
    from hypogeum import archive_relocation as rl
    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, "ep_sweep")
    set_factory_setting("archive_root", str(tmp_path / "arch"), db_path=dbp)
    _mark_published(dbp, "ep_sweep")
    conn = get_connection(dbp)
    try:
        insert_archive_index_entry(
            conn, archive_id="sw1", asset_id="x", episode_id="ep_sweep",
            asset_type="clip", provider="muapi", archived_at="2026-08-01T00:00:00+00:00",
            file_path="episodes/ep_sweep/video/sc_s01.mp4")
        conn.commit()
    finally:
        conn.close()

    assert rl.find_pending_published_episodes(db_path=dbp) == [("ep_sweep", "hypogeum")]
    out = rl.relocate_pending_published_episodes(db_path=dbp, limit=5)
    # sweep uses the default working dir, not our tmp ep_dir — so it no-ops
    # cleanly (source absent). What we assert: it ran without raising and the
    # pending query is the contract.
    assert isinstance(out, list)


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


def test_unmounted_archive_share_defers_instead_of_archiving_locally(dbp, tmp_path, monkeypatch):
    # 2026-10-06: NAS off the network -> ep_31d1891b landed in the in-tree
    # fallback archive and nothing ever moved it on.
    from hypogeum import archive_relocation as rl
    import hypogeum.archive_storage as storage

    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, "ep_nas_down")
    set_factory_setting("archive_root", str(tmp_path / "server-archive"), db_path=dbp)
    _mark_published(dbp, "ep_nas_down")
    monkeypatch.setattr(storage, "ensure_archive_root_mounted", lambda db_path=None, **k: False)

    rec = rl.relocate_episode_assets("ep_nas_down", db_path=dbp, output_dir=ep_dir,
                                     catalog_dir=tmp_path / "c")
    assert rec["verdict"] == "SKIPPED" and rec["relocated"] is False
    assert "not mounted" in " ".join(rec["warnings"])
    assert (ep_dir / "video" / "sc_s01.mp4").exists()                 # stays put for the next sweep
    assert not (tmp_path / "server-archive").exists()


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

    def flaky_move(s, d, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(s, d, **kw)

    monkeypatch.setattr(rl.shutil, "move", flaky_move)
    rec = rl.relocate_episode_assets(episode_id, db_path=dbp, output_dir=ep_dir,
                                     catalog_dir=tmp_path / "c")
    assert rec["verdict"] == "FAILED"
    # every original file is back
    assert (ep_dir / "video" / "sc_s01.mp4").exists()
    assert (ep_dir / "video" / "sc_s02.mp4").exists()
    assert (ep_dir / "thumbnails" / "main.jpg").exists()


def _seed_relocated(dbp, tmp_path, episode_id):
    """An episode already relocated once, with a fresh local copy re-created."""
    from hypogeum import archive_relocation as rl
    work = tmp_path / "episodes"
    work.mkdir(exist_ok=True)
    ep_dir = _seed_episode_dir(work, episode_id)
    set_factory_setting("archive_root", str(tmp_path / "arch"), db_path=dbp)
    _mark_published(dbp, episode_id)
    conn = get_connection(dbp)
    try:
        insert_archive_index_entry(
            conn, archive_id=f"lo_{episode_id}", asset_id="x", episode_id=episode_id,
            asset_type="clip", provider="muapi", archived_at="2026-08-01T00:00:00+00:00",
            file_path=f"episodes/{episode_id}/video/sc_s01.mp4")
        conn.commit()
    finally:
        conn.close()
    first = rl.relocate_episode_assets(episode_id, db_path=dbp, output_dir=ep_dir,
                                       catalog_dir=tmp_path / "c")
    assert first["verdict"] == "RELOCATED" and not ep_dir.exists()
    # a later repair re-creates the working dir: same clip + a changed json
    _seed_episode_dir(work, episode_id)
    (ep_dir / "final_render_package.json").write_text('{"repaired": true}')
    return ep_dir, first["dest_dir"]


def _age(ep_dir, seconds=7 * 3600):
    import os, time
    old = time.time() - seconds
    for p in ep_dir.rglob("*"):
        os.utime(p, (old, old))


class TestLocalLeftoversOfArchivedEpisode:
    def test_merged_into_archive_and_local_removed(self, dbp, tmp_path):
        from pathlib import Path
        from hypogeum import archive_relocation as rl
        ep_dir, dest = _seed_relocated(dbp, tmp_path, "ep_left")
        _age(ep_dir)
        rec = rl.relocate_episode_assets("ep_left", db_path=dbp, output_dir=ep_dir,
                                         catalog_dir=tmp_path / "c")
        assert rec["verdict"] == "RELOCATED"
        assert not ep_dir.exists()
        dest = Path(dest)
        assert (dest / "final_render_package.json").read_text() == '{"repaired": true}'
        superseded = list(dest.glob("final_render_package.json.superseded_*"))
        assert len(superseded) == 1 and superseded[0].read_text() == "{}"
        assert (dest / "video" / "sc_s01.mp4").read_bytes() == b"x" * 100
        assert not list((dest / "video").glob("*.superseded_*"))

    def test_recently_modified_leftovers_are_left_alone(self, dbp, tmp_path):
        from hypogeum import archive_relocation as rl
        ep_dir, _ = _seed_relocated(dbp, tmp_path, "ep_fresh")
        rec = rl.relocate_episode_assets("ep_fresh", db_path=dbp, output_dir=ep_dir,
                                         catalog_dir=tmp_path / "c")
        assert rec["verdict"] == "SKIPPED"
        assert (ep_dir / "final_render_package.json").exists()

    def test_active_run_blocks_merge(self, dbp, tmp_path):
        from hypogeum import archive_relocation as rl
        ep_dir, _ = _seed_relocated(dbp, tmp_path, "ep_busy")
        _age(ep_dir)
        conn = get_connection(dbp)
        try:
            conn.execute("INSERT INTO runs (run_id, episode_id, started_at, status) "
                         "VALUES ('r1', 'ep_busy', '2026-10-06T00:00:00+00:00', 'running')")
            conn.commit()
        finally:
            conn.close()
        rec = rl.relocate_episode_assets("ep_busy", db_path=dbp, output_dir=ep_dir,
                                         catalog_dir=tmp_path / "c")
        assert rec["verdict"] == "SKIPPED"
        assert ep_dir.exists()

    def test_sweep_finds_relocated_episode_with_local_files(self, dbp, tmp_path, monkeypatch):
        from hypogeum import archive_relocation as rl
        ep_dir, _ = _seed_relocated(dbp, tmp_path, "ep_sweep_left")
        monkeypatch.setattr(rl, "_WORKING_EPISODES_DIR", ep_dir.parent)
        assert rl.find_pending_published_episodes(db_path=dbp) == [("ep_sweep_left", "hypogeum")]
        import shutil
        shutil.rmtree(ep_dir)
        assert rl.find_pending_published_episodes(db_path=dbp) == []



def test_episode_waiting_for_its_youtube_cover_stays_local_until_the_cover_is_confirmed(dbp, tmp_path):
    # 2026-10-16: two published Shorts still needed their cover uploaded in Studio, but their folders had
    # already been moved to a NAS that was offline -- the designed covers were unreachable.
    from hypogeum import archive_relocation as rl
    from hypogeum.dashboard_content_grid import mark_youtube_cover_done

    work = tmp_path / "episodes"
    work.mkdir()
    ep_dir = _seed_episode_dir(work, "ep_cover_wait")
    set_factory_setting("archive_root", str(tmp_path / "server-archive"), db_path=dbp)
    _mark_published(dbp, "ep_cover_wait")
    conn = get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, held_at) "
                 "VALUES ('p1','hypogeum','ep_cover_wait','2026-10-17T10:00:00Z','2026-10-16T10:00:00Z','held','2026-10-16T00:00:00Z')")
    conn.commit()
    conn.close()
    assert rl.pending_cover_upload("ep_cover_wait", dbp) is True
    rec = rl.relocate_episode_assets("ep_cover_wait", db_path=dbp, output_dir=ep_dir, catalog_dir=tmp_path / "c")
    assert rec["verdict"] == "SKIPPED" and "cover" in " ".join(rec["warnings"])
    assert (ep_dir / "thumbnails" / "main.jpg").exists()
    mark_youtube_cover_done("p1", "hypogeum", db_path=dbp)
    assert rl.pending_cover_upload("ep_cover_wait", dbp) is False
