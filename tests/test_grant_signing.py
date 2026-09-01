"""hypogeum/grant_signing.py + channel_package_scaffold.py."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hypogeum.db import initialize_schema

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "grant.db"
    initialize_schema(p)
    return p


# --- can_sign_from_dashboard --------------------------------------------------

def test_can_sign_false_without_key(monkeypatch, tmp_path):
    from hypogeum import grant_signing as gs

    class _Cfg:
        AUTHORIZATION_APPROVAL_PUBLIC_KEY = "somekey"
        AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH = str(tmp_path / "nope.pem")

    monkeypatch.setattr("hypogeum.config.Config", lambda: _Cfg())
    assert gs.can_sign_from_dashboard() is False


def test_can_sign_false_without_public_key(monkeypatch, tmp_path):
    from hypogeum import grant_signing as gs
    key = tmp_path / "k.pem"
    key.write_text("x")

    class _Cfg:
        AUTHORIZATION_APPROVAL_PUBLIC_KEY = None
        AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH = str(key)

    monkeypatch.setattr("hypogeum.config.Config", lambda: _Cfg())
    assert gs.can_sign_from_dashboard() is False


def test_can_sign_true_when_both_present(monkeypatch, tmp_path):
    from hypogeum import grant_signing as gs
    key = tmp_path / "k.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")

    class _Cfg:
        AUTHORIZATION_APPROVAL_PUBLIC_KEY = "pub"
        AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH = str(key)

    monkeypatch.setattr("hypogeum.config.Config", lambda: _Cfg())
    assert gs.can_sign_from_dashboard() is True


def test_sign_refuses_when_unavailable(monkeypatch):
    from hypogeum import grant_signing as gs
    monkeypatch.setattr(gs, "can_sign_from_dashboard", lambda config=None: False)
    r = gs.sign_channel_operating_grant("x", approver_identity="test")
    assert r["ok"] is False and "unavailable" in r["error"]


# --- channel package scaffold ----------------------------------------------

@pytest.fixture()
def pkg_root(tmp_path):
    root = tmp_path / "channel_packages"
    shutil.copytree(_REPO / "channel_packages", root)
    return root


def test_scaffold_creates_dir_and_registers_workflow(dbp, pkg_root):
    from hypogeum.channel_package_scaffold import (
        scaffold_channel_package, channel_package_exists,
    )
    assert not channel_package_exists("acme", packages_root=pkg_root)
    r = scaffold_channel_package("acme", db_path=dbp, packages_root=pkg_root)
    assert r["ok"] is True and r["created"] is True
    assert r["workflows_registered"] == ["hypogeum_main_production@1.0.0"]
    assert channel_package_exists("acme", packages_root=pkg_root)

    import json
    m = json.loads((pkg_root / "acme" / "channel_package_manifest.json").read_text())
    assert m["channel_id"] == "acme"

    from hypogeum.db import get_connection
    conn = get_connection(dbp)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM workflow_definitions WHERE channel_id='acme'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_scaffold_is_idempotent(dbp, pkg_root):
    from hypogeum.channel_package_scaffold import scaffold_channel_package
    scaffold_channel_package("acme", db_path=dbp, packages_root=pkg_root)
    r2 = scaffold_channel_package("acme", db_path=dbp, packages_root=pkg_root)
    assert r2["ok"] is True and r2["created"] is False


def test_scaffold_rejects_bad_channel_id(dbp, pkg_root):
    from hypogeum.channel_package_scaffold import scaffold_channel_package
    for bad in ("../evil", "a/b", ".hidden", ""):
        r = scaffold_channel_package(bad, db_path=dbp, packages_root=pkg_root)
        assert r["ok"] is False
