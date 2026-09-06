"""Tests for hypogeum.client_manager — the Cortex Advisory tenant layer."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum import client_manager as cm


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "clients.db"
    initialize_schema(p)
    return p


def test_create_and_get_client(db):
    c = cm.create_client("Acme Widgets", brief={"goal": "grow IG"}, db_path=db)
    assert c["client_id"].startswith("acme_widgets_")
    assert c["status"] == "active" and c["engagement_mode"] == "observe"
    got = cm.get_client(c["client_id"], db_path=db)
    assert got["brief"] == {"goal": "grow IG"}
    assert cm.list_clients(status="active", db_path=db)


def test_bad_enum_rejected(db):
    with pytest.raises(ValueError):
        cm.create_client("X", engagement_mode="publish_everything", db_path=db)


def test_update_and_archive(db):
    c = cm.create_client("X", db_path=db)
    assert cm.update_client(c["client_id"], engagement_mode="manage",
                            name="X Corp", db_path=db) is True
    assert cm.get_client(c["client_id"], db_path=db)["engagement_mode"] == "manage"
    assert cm.archive_client(c["client_id"], db_path=db) is True
    assert cm.list_clients(status="active", db_path=db) == []


def test_managed_account_link_is_idempotent(db):
    c = cm.create_client("X", db_path=db)
    a1 = cm.link_managed_account(c["client_id"], "youtube",
                                 provider_account_id="z1", handle="@x", db_path=db)
    a2 = cm.link_managed_account(c["client_id"], "youtube",
                                 provider_account_id="z1-new", handle="@x", db_path=db)
    assert a1["managed_account_id"] == a2["managed_account_id"]
    accts = cm.list_managed_accounts(c["client_id"], db_path=db)
    assert len(accts) == 1 and accts[0]["provider_account_id"] == "z1-new"
    cm.unlink_managed_account(a1["managed_account_id"], db_path=db)
    assert cm.list_managed_accounts(c["client_id"], db_path=db) == []


def test_content_item_upsert_stable_id_and_no_null_clobber(db):
    c = cm.create_client("X", db_path=db)
    cid = cm.upsert_content_item(c["client_id"], "youtube", platform_post_id="V1",
                                 title="First", caption="hi", db_path=db)
    assert cid == cm.new_content_id("youtube", "V1")
    # second, thinner upsert must not null the title
    cm.upsert_content_item(c["client_id"], "youtube", platform_post_id="V1",
                           thumbnail_url="http://t/1.jpg", db_path=db)
    it = cm.get_content_item(cid, db_path=db)
    assert it["title"] == "First" and it["thumbnail_url"] == "http://t/1.jpg"
    assert len(cm.list_content_items(c["client_id"], db_path=db)) == 1
