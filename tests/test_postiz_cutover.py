"""tests/test_postiz_cutover.py — reversible Zernio<->Postiz binding switches."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum.publishing import postiz_cutover as cut
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    list_bindings,
    resolve_active_binding,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.zernio_publish_runtime import _route_platforms_by_binding

CH = "twinkle_mini_tales"


def _integrations():
    rows = []
    for channel_id, handles in cut.CHANNEL_HANDLES.items():
        for platform, handle in handles.items():
            rows.append({"id": f"pz_{channel_id}_{platform}", "identifier": platform,
                         "profile": "" if platform == "facebook" else handle,
                         "name": handle if platform == "facebook" else "Display Name",
                         "disabled": False})
    return rows


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "cutover.db"
    initialize_schema(p)
    zprof = upsert_provider_profile(p, provider_id="zernio", external_profile_id="zprof")
    for channel_id in cut.CHANNEL_HANDLES:
        for platform in cut.PLATFORMS:
            acc = upsert_provider_account(p, provider_id="zernio", platform=platform,
                                          external_account_id=f"z_{channel_id}_{platform}",
                                          provider_profile_id=zprof["provider_profile_id"])
            bind_channel_provider(p, channel_id=channel_id, capability="publishing", platform=platform,
                                  provider_id="zernio", provider_profile_id=zprof["provider_profile_id"],
                                  provider_account_id=acc["provider_account_id"])
    return p


def test_match_requires_exactly_one_integration_per_handle():
    rows = _integrations()
    assert len(cut.match_integrations(rows)) == 8
    with pytest.raises(cut.CutoverError, match="found 0"):
        cut.match_integrations([r for r in rows if r["id"] != "pz_hypogeum_tiktok"])
    with pytest.raises(cut.CutoverError, match="found 2"):
        cut.match_integrations(rows + [dict(rows[0], id="dup")])


def test_register_routes_nothing(db_path):
    cut.register_postiz_accounts(_integrations(), db_path=db_path)
    assert all(p == "zernio" for chans in cut.status(db_path).values() for p in chans.values())


def test_switch_to_postiz_and_back(db_path):
    cut.register_postiz_accounts(_integrations(), db_path=db_path)
    r = cut.switch_platform(CH, "facebook", "postiz", db_path=db_path)
    assert (r["from"], r["to"]) == ("zernio", "postiz")
    b = resolve_active_binding(db_path, channel_id=CH, capability="publishing", platform="facebook")
    assert b["provider_id"] == "postiz"
    routed = _route_platforms_by_binding(list(cut.PLATFORMS), channel_id=CH, logical_alias="default",
                                         db_path=db_path)
    assert [(type(p).__name__, g) for p, g in routed] == [
        ("ZernioProvider", ["youtube", "tiktok", "instagram"]), ("PostizProvider", ["facebook"])]
    # other channel untouched
    assert cut.status(db_path)["hypogeum"]["facebook"] == "zernio"

    back = cut.switch_platform(CH, "facebook", "zernio", db_path=db_path)
    assert back["to"] == "zernio"
    b = resolve_active_binding(db_path, channel_id=CH, capability="publishing", platform="facebook")
    assert b["provider_id"] == "zernio"
    from hypogeum.publishing.provider_repository import get_provider_account
    assert get_provider_account(db_path, provider_account_id=b["provider_account_id"])[
        "external_account_id"] == f"z_{CH}_facebook"


def test_switch_is_idempotent(db_path):
    cut.register_postiz_accounts(_integrations(), db_path=db_path)
    cut.switch_platform(CH, "instagram", "postiz", db_path=db_path)
    cut.switch_platform(CH, "instagram", "postiz", db_path=db_path)
    enabled = [b for b in list_bindings(db_path, channel_id=CH, platform="instagram") if b["enabled"]]
    assert len(enabled) == 1


def test_switch_without_registration_changes_nothing(db_path):
    with pytest.raises(cut.CutoverError, match="register"):
        cut.switch_platform(CH, "facebook", "postiz", db_path=db_path)
    assert cut.status(db_path)[CH]["facebook"] == "zernio"


def test_account_is_matched_to_its_own_channel(db_path):
    cut.register_postiz_accounts(_integrations(), db_path=db_path)
    cut.switch_platform("hypogeum", "youtube", "postiz", db_path=db_path)
    b = resolve_active_binding(db_path, channel_id="hypogeum", capability="publishing", platform="youtube")
    from hypogeum.publishing.provider_repository import get_provider_account
    assert get_provider_account(db_path, provider_account_id=b["provider_account_id"])[
        "external_account_id"] == "pz_hypogeum_youtube"
