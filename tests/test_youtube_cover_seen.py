"""tests/test_youtube_cover_seen.py — a public Short nobody has looked at in Studio raises an alert; a Done press alone does not clear it."""
from __future__ import annotations

from datetime import datetime, timezone

import hypogeum.db as db
from hypogeum import operator_alerts as oa
from hypogeum.dashboard_content_grid import mark_youtube_cover_done, record_studio_cover_check

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _hold(p, post_id, released_at, status="released"):
    c = db.get_connection(p)
    c.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, held_at, released_at) "
              "VALUES (?, 'hypogeum', 'ep_x', ?, ?, ?, ?, ?)", (post_id, released_at, released_at, status, released_at, released_at))
    c.commit()
    c.close()


def test_public_short_nobody_looked_at_is_alerted_and_done_does_not_clear_it(tmp_path):
    p = tmp_path / "c.db"
    db.initialize_schema(p)
    _hold(p, "A", "2026-10-19T10:00:00")
    assert "coverseen:A" in oa.check_youtube_cover_seen(p, NOW)
    mark_youtube_cover_done("A", "hypogeum", db_path=p)                      # a button press is not proof
    assert "coverseen:A" in oa.check_youtube_cover_seen(p, NOW)
    record_studio_cover_check("A", "hypogeum", "looked in Studio", db_path=p)
    assert oa.check_youtube_cover_seen(p, NOW) == {}


def test_too_new_old_or_still_held_shorts_are_left_alone(tmp_path):
    p = tmp_path / "c.db"
    db.initialize_schema(p)
    _hold(p, "new", "2026-10-20T06:00:00")                                    # only 6h public
    _hold(p, "old", "2026-10-10T10:00:00")                                    # before the Studio audit existed
    _hold(p, "held", "2026-10-19T10:00:00", status="held")                    # not public
    assert oa.check_youtube_cover_seen(p, NOW) == {}
