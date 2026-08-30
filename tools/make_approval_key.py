#!/usr/bin/env python3
"""
Run once:  python3 tools/make_approval_key.py

Generates the Ed25519 approval keypair that the factory's spend-authorization
system (hypogeum.authorization_authority) verifies signatures against. Writes
the PRIVATE key to `approval_ed25519_private_key.pem` at the repo root
(gitignored, mode 600) and prints the PUBLIC key.

After running:
  1. put the printed PUBLIC KEY in `.env` as AUTHORIZATION_APPROVAL_PUBLIC_KEY
  2. back up `approval_ed25519_private_key.pem` offline (USB / password
     manager) -- losing it just means running this again and re-signing the
     channel operating grants.

The private key never runs inside the pipeline/worker; only the offline
signing scripts (scripts/approve_run_grant.py,
tools/configure_channel_operating_grant.py) load it.
"""
import base64
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

_REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = _REPO_ROOT / "approval_ed25519_private_key.pem"

if OUT.exists():
    print(f"A key file already exists at:\n  {OUT}\n"
          "Delete it first if you really want a new one (then every channel\n"
          "operating grant must be re-signed against the new public key).")
    raise SystemExit(1)

priv = Ed25519PrivateKey.generate()

OUT.write_bytes(priv.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
))
os.chmod(OUT, stat.S_IRUSR | stat.S_IWUSR)  # 600

pub_b64 = base64.b64encode(
    priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode()

print()
print("=" * 64)
print("  PRIVATE KEY saved to:")
print(f"    {OUT}")
print("    -> back this up offline (USB / password manager).")
print()
print("  PUBLIC KEY  -- put this in .env as AUTHORIZATION_APPROVAL_PUBLIC_KEY:")
print()
print(f"    {pub_b64}")
print("=" * 64)
