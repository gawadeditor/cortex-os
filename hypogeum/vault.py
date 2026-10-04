"""
hypogeum/vault.py — encryption at rest for secret_profiles.credential_data.

Until 2026-10-04 every provider key / OAuth token in secret_profiles was
stored as plaintext (credential_manager.py's own "V1 note"). The DB is
copied to .hypogeum-assets/backups/ and the NAS, so a leaked backup was a
leaked key set. Now every value is written as

    enc:v1:<Fernet token>      (AES-128-CBC + HMAC-SHA256, cryptography lib)

with the master key in a file OUTSIDE the repo and the asset tree:
~/.cortex/vault.key (0600). Losing that file loses every stored secret — the
operator keeps a copy in their password manager (scripts/encrypt_secrets.py
says so when it creates it).

Reads accept a legacy plaintext value unchanged (so encrypting the existing
rows is a separate, backed-up migration — scripts/encrypt_secrets.py) but
never accept a ciphertext they can't verify: VaultError, not garbage.

    encrypt(value) -> str          # idempotent on an already-encrypted value
    decrypt(value) -> str          # plaintext passes through
    is_encrypted(value) -> bool
    key_path() -> Path / set_key_path(path)   # tests point this at tmp_path
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="vault")

PREFIX = "enc:v1:"
_DEFAULT_KEY_PATH = Path.home() / ".cortex" / "vault.key"
_key_path_override: Optional[Path] = None
_fernet_cache: dict = {}


class VaultError(RuntimeError):
    pass


def key_path() -> Path:
    return _key_path_override or _DEFAULT_KEY_PATH


def set_key_path(path: Optional[Path]) -> None:
    global _key_path_override
    _key_path_override = Path(path) if path else None
    _fernet_cache.clear()


def _fernet(create: bool):
    from cryptography.fernet import Fernet
    p = key_path()
    if str(p) in _fernet_cache:
        return _fernet_cache[str(p)]
    if not p.exists():
        if not create:
            raise VaultError(f"vault key not found at {p} — encrypted secrets can't be read "
                             "(restore it from the operator's password manager)")
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(Fernet.generate_key())
        log.warn("vault_key_created", path=str(p))
    f = Fernet(p.read_bytes().strip())
    _fernet_cache[str(p)] = f
    return f


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "" or is_encrypted(value):
        return value
    return PREFIX + _fernet(create=True).encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    if not is_encrypted(value):
        return value
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(create=False).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise VaultError(f"a stored secret doesn't match the vault key at {key_path()}") from exc
