"""Tests for hypogeum.workspaces — multi-tenant Phase 1 (ownership).

The property everything later relies on: an unassigned resource has NO
owner (never silently the platform owner), and creation paths assign
explicitly."""
from __future__ import annotations

import pytest

from hypogeum import workspaces as ws
from hypogeum.db import get_connection, initialize_schema
from hypogeum.factory_entity import create_factory, get_current_factory_id, get_factory


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    ws._READY.discard(str(p))
    return p


@pytest.fixture()
def platform(db):
    create_factory("hypogeum", "Hypogeum Media Factory", "hypogeum", db_path=db)
    return db


def test_single_factory_install_is_its_own_platform_owner(platform):
    assert ws.platform_workspace_id(platform) == "hypogeum"


def test_backfill_assigns_existing_resources_and_marks_platform_owner(platform):
    from hypogeum.channel_config import create_channel_profile
    from hypogeum.client_manager import create_client
    # pre-existing rows written before workspaces existed (no assignment)
    conn = get_connection(platform)
    from hypogeum.channel_config import _ensure_tables
    _ensure_tables(conn)
    conn.execute("INSERT INTO channel_profiles (channel_id, name, status, created_at) "
                 "VALUES ('old_ch','Old','active','2026-01-01')")
    conn.commit()
    conn.close()
    assert ws.workspace_of("old_ch", platform) is None          # ownerless until backfilled

    dry = ws.backfill_platform_workspace(platform, dry_run=True)
    assert dry["assigned"]["channel"] == 1 and ws.workspace_of("old_ch", platform) is None

    out = ws.backfill_platform_workspace(platform)
    assert out["platform_workspace"] == "hypogeum" and out["channels"] == ["old_ch"]
    assert ws.workspace_of("old_ch", platform) == "hypogeum"
    assert ws.list_workspaces(platform)[0]["kind"] == "platform_owner"
    # idempotent
    assert ws.backfill_platform_workspace(platform)["assigned"]["channel"] == 0


def test_client_workspace_resources_stay_separate(platform):
    from hypogeum.channel_config import create_channel_profile
    from hypogeum.client_manager import create_client
    from hypogeum.credential_manager import create_secret_profile
    ws.backfill_platform_workspace(platform)
    ws.create_workspace("Acme Media", workspace_id="acme", db_path=platform)

    create_channel_profile("mine", "Mine", db_path=platform)                       # operator default
    create_channel_profile("acme_ch", "Acme", db_path=platform, workspace_id="acme")
    c = create_client("Acme Advisory", db_path=platform, workspace_id="acme")
    key = create_secret_profile("openrouter", "acme key", "api_key", "sk-test-acme",
                                scope="channel-specific", channel_id="acme_ch", db_path=platform)

    assert ws.workspace_of("mine", platform) == "hypogeum"
    assert ws.workspace_of("acme_ch", platform) == "acme"
    assert ws.owner_of("client", c["client_id"], platform) == "acme"
    assert ws.owner_of("secret_profile", key.secret_profile_id, platform) == "acme"   # inherits channel's
    assert ws.resources_of("acme", "channel", platform) == ["acme_ch"]
    assert "acme_ch" not in ws.resources_of("hypogeum", "channel", platform)


def test_moving_a_channel_needs_explicit_move(platform):
    ws.backfill_platform_workspace(platform)
    ws.create_workspace("Acme", workspace_id="acme", db_path=platform)
    ws.assign("channel", "ch1", "hypogeum", db_path=platform)
    with pytest.raises(ws.WorkspaceError):
        ws.assign("channel", "ch1", "acme", db_path=platform)
    ws.assign("channel", "ch1", "acme", move=True, db_path=platform)
    assert ws.workspace_of("ch1", platform) == "acme"


def test_assign_to_unknown_workspace_rejected(platform):
    with pytest.raises(ws.WorkspaceError):
        ws.assign("channel", "ch1", "nope", db_path=platform)


def test_only_one_platform_owner(platform):
    ws.backfill_platform_workspace(platform)
    with pytest.raises(ws.WorkspaceError):
        ws.create_workspace("Other", workspace_id="other", kind="platform_owner", db_path=platform)


def test_no_arg_factory_lookup_means_platform_owner_with_clients_present(platform):
    """policy_cascade / run clearance call get_factory() with no id; adding a
    client workspace must not break them (it used to raise ValueError)."""
    ws.backfill_platform_workspace(platform)
    ws.create_workspace("Acme", workspace_id="acme", db_path=platform)
    assert get_factory(db_path=platform).factory_id == "hypogeum"
    assert get_current_factory_id(db_path=platform) == "hypogeum"


def test_unmarked_multi_workspace_is_ambiguous_not_guessed(db):
    create_factory("a", "A", "a", db_path=db)
    create_factory("b", "B", "b", db_path=db)
    assert ws.platform_workspace_id(db) is None
    with pytest.raises(ValueError):
        get_factory(db_path=db)
    # and a new resource is left ownerless rather than given to someone
    assert ws.assign_new("channel", "x", None, db_path=db) is None
    assert ws.workspace_of("x", db) is None
