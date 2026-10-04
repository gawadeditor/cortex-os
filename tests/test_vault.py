"""hypogeum.vault — secrets encrypted at rest in secret_profiles."""
from __future__ import annotations

import os
import stat

import pytest

from hypogeum import vault
from hypogeum.db import get_connection, initialize_schema


def test_roundtrip_and_prefix():
    enc = vault.encrypt("sk-or-v1-secret")
    assert enc.startswith(vault.PREFIX) and "sk-or" not in enc
    assert vault.decrypt(enc) == "sk-or-v1-secret"
    assert vault.encrypt(enc) == enc                      # idempotent
    assert vault.decrypt("legacy-plaintext") == "legacy-plaintext"
    assert vault.encrypt(None) is None and vault.decrypt(None) is None


def test_key_file_is_private():
    vault.encrypt("x")
    mode = stat.S_IMODE(os.stat(vault.key_path()).st_mode)
    assert mode == 0o600


def test_wrong_or_missing_key_raises_not_garbage(tmp_path):
    enc = vault.encrypt("secret")
    vault.set_key_path(tmp_path / "other.key")
    vault.encrypt("make a different key")
    with pytest.raises(vault.VaultError):
        vault.decrypt(enc)
    vault.set_key_path(tmp_path / "missing.key")
    with pytest.raises(vault.VaultError):
        vault.decrypt(enc)


def test_secret_profiles_stored_encrypted_and_read_back(tmp_path):
    from hypogeum.credential_manager import (create_secret_profile, get_secret_profile,
                                             resolve_credential, update_secret_profile)
    db = tmp_path / "t.db"
    initialize_schema(db)
    p = create_secret_profile("openrouter", "k", "api_key", "sk-plain-123", db_path=db)
    conn = get_connection(db)
    raw = conn.execute("SELECT credential_data FROM secret_profiles").fetchone()[0]
    conn.close()
    assert raw.startswith(vault.PREFIX) and "sk-plain-123" not in raw
    assert get_secret_profile(p.secret_profile_id, db_path=db).credential_data == "sk-plain-123"
    assert resolve_credential(p.secret_profile_id, db_path=db) == "sk-plain-123"
    update_secret_profile(p.secret_profile_id, credential_data="sk-new-456", db_path=db)
    conn = get_connection(db)
    raw = conn.execute("SELECT credential_data FROM secret_profiles").fetchone()[0]
    conn.close()
    assert "sk-new-456" not in raw
    assert resolve_credential(p.secret_profile_id, db_path=db) == "sk-new-456"
