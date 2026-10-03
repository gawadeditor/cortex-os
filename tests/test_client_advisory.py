"""Tests for hypogeum.client_advisory + the learning_engine client guard (P4)."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum import client_manager as cm
from hypogeum.analytics_store import store_content_metrics
from hypogeum.client_advisory import generate_client_advisory


def _seed(db, cid, rows):
    for i, (er, hook, hr, good) in enumerate(rows):
        pid = f"V{i}"
        ccid = cm.upsert_content_item(cid, "youtube", platform_post_id=pid,
                                      title=f"Post {i}",
                                      published_at=f"2026-08-{i+1:02d}T{hr:02d}:00:00Z",
                                      source="manual_url", db_path=db)
        store_content_metrics(ccid, cid, "youtube",
                              {"views": 1000 + i, "likes": 50, "engagement_rate": er},
                              source="api", db_path=db)
        cm.store_content_profile(ccid, {
            "hook": {"promise": "x", "strength_1_10": hook, "type": "how_to"},
            "caption": {"length": "short" if good else "long", "has_cta": True,
                        "hashtag_strategy": "few_niche"},
            "thumbnail": {"present": True, "score_1_10": 8 if good else 3,
                          "text_legibility": "strong", "contrast": "high",
                          "subject": "x", "clutter": "clean"},
            "format_tags": ["talking_head"] if good else ["static"],
            "topic_tags": ["espresso"] if good else ["generic"], "notes": "n",
        }, model_slug="m", cost_usd=0.003, db_path=db)


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("Acme Studios", db_path=db)
    cm.link_managed_account(c["client_id"], "youtube", provider_account_id="z",
                            provider_profile_id="zp", db_path=db)
    return db, c["client_id"]


def test_advisory_finds_patterns(client):
    db, cid = client
    _seed(db, cid, [(8.0, 9, 14, True), (7.5, 8, 15, True), (6.9, 8, 13, True),
                    (2.1, 3, 3, False), (1.5, 2, 4, False), (1.2, 3, 2, False)])
    adv = generate_client_advisory(cid, db_path=db)
    assert adv["ok"]
    axes = {w["axis"] for w in adv["what_works"]}
    assert {"hook", "thumbnail", "publishing time"} <= axes
    assert adv["recommendations"]
    assert adv["catalogue"]["scored"] == 6


def test_advisory_needs_minimum_catalogue(client):
    db, cid = client
    _seed(db, cid, [(8.0, 9, 14, True), (2.0, 3, 3, False)])
    adv = generate_client_advisory(cid, db_path=db)
    assert adv["ok"] is False
    assert "need 4" in adv["reason"]
    assert adv["summary"]                       # still gives a plain-language line


def test_advisory_honest_when_nothing_separates(client):
    db, cid = client
    # 4 posts, identical profiles, engagement spread but no axis differs
    _seed(db, cid, [(8.0, 6, 10, True), (7.0, 6, 10, True),
                    (3.0, 6, 10, True), (2.0, 6, 10, True)])
    adv = generate_client_advisory(cid, db_path=db)
    assert adv["ok"] is True
    assert adv["what_works"] == []
    assert "nothing yet separates them" in adv["summary"]


def test_before_after_split_on_managed_since(tmp_path):
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("Fund", stage="active", managed_since="2026-08-01", db_path=db)
    cid = c["client_id"]
    for i, (dt, er) in enumerate([
            ("2026-06-05", 1.8), ("2026-06-20", 2.1), ("2026-07-05", 1.5),
            ("2026-08-05", 4.2), ("2026-08-12", 5.1), ("2026-08-25", 4.8)]):
        ccid = cm.upsert_content_item(cid, "youtube", platform_post_id=f"V{i}",
                                      published_at=dt + "T12:00:00Z",
                                      source="manual_url", db_path=db)
        store_content_metrics(ccid, cid, "youtube",
                              {"views": 1000, "engagement_rate": er},
                              source="manual", db_path=db)
    adv = generate_client_advisory(cid, db_path=db)
    ba = adv["before_after"]
    assert ba["available"] is True
    assert ba["before"]["posts"] == 3 and ba["after"]["posts"] == 3
    assert ba["after"]["engagement"] > ba["before"]["engagement"]
    assert "took over" in ba["verdict"]
    assert any(w["axis"].startswith("before vs after") for w in adv["what_works"])


def test_before_after_absent_without_managed_since(tmp_path):
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("NoDate", managed_since=None, db_path=db)
    adv = generate_client_advisory(c["client_id"], db_path=db)
    assert adv["before_after"]["available"] is False
    assert "managed_since" in adv["before_after"]["reason"]


def test_new_client_start_date_defaults_to_creation_day(tmp_path):
    # 2026-10-03 general setting: every new client starts "managed" the day
    # it is created, so analysis spend + before/after are anchored there.
    from datetime import datetime, timezone
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("Fresh", db_path=db)
    assert c["managed_since"] == datetime.now(timezone.utc).date().isoformat()
    assert cm.create_client("Backdated", managed_since="2025-01-01", db_path=db)["managed_since"] == "2025-01-01"


def test_learning_content_volume_action_is_advisory_for_clients(tmp_path):
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("Acme", db_path=db)
    cid = c["client_id"]
    from hypogeum.analytics_store import store_action, list_actions
    from hypogeum.learning_engine import apply_action
    store_action({
        "source_episode": None, "dimension": "content_volume", "action_type": "increase",
        "rationale": "test", "evidence_json": f'{{"channel_id": "{cid}"}}',
        "status": "pending", "created_at": "2026-09-06T00:00:00+00:00",
    }, db_path=db)
    aid = list_actions(status="pending", db_path=db)[0]["action_id"]
    res = apply_action(aid, db_path=db)
    assert res["ok"] is True
    assert res["status"] == "dismissed"
    assert "no production cadence" in res["note"]
