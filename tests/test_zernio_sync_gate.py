"""Zernio-only syncs stop once no channel publishes through Zernio."""
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider_repository import zernio_publishing_in_use


def _bind(db, provider, enabled=1, capability="publishing", platform="tiktok"):
    conn = get_connection(db)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO channel_provider_bindings (binding_id, channel_id, capability, platform, provider_id, "
            "provider_profile_id, provider_account_id, logical_alias, enabled, created_at, updated_at) "
            "VALUES (?, 'c', ?, ?, ?, 'p', 'a', 'default', ?, 't', 't')",
            (f"b_{provider}_{platform}_{capability}_{enabled}", capability, platform, provider, enabled),
        )
        conn.commit()
    finally:
        conn.close()


def test_in_use_while_any_publishing_binding_is_zernio(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    _bind(db, "zernio")
    assert zernio_publishing_in_use(db) is True


def test_not_in_use_when_everything_is_postiz(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    _bind(db, "postiz")
    _bind(db, "zernio", enabled=0, platform="youtube")
    _bind(db, "zernio", capability="analytics", platform="facebook")  # analytics-only binding doesn't count
    assert zernio_publishing_in_use(db) is False
