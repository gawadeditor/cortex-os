#!/usr/bin/env python3
"""
scripts/connect_instagram_client.py — connect a Professional Instagram account
to a Cortex client through the Instagram API with Instagram Login.

    python3 scripts/connect_instagram_client.py --client-name "Muhamed Abdelgawad"

Reads the access token from the macOS clipboard (copy it from Meta first) and
clears the clipboard right after; on an interactive terminal you can pass
--prompt to type it hidden instead. Never pass it on the command line or
paste it into a chat. Verifies it against graph.instagram.com/me,
stores it in Credential Manager, creates the client if needed, links the
account, and runs a first sync. Re-running with a new token just replaces it.

Where the token comes from: Meta app "Cortex Publisher" -> Instagram ->
"API setup with Instagram business login" -> Generate access tokens -> add
your account -> log in. See docs/plans/PERSONAL_BRANDING_PLAN.md.
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum import client_manager as cm  # noqa: E402
from hypogeum.credential_manager import delete_secret_profile, get_secret_profile  # noqa: E402
from hypogeum.publishing import instagram_graph as ig  # noqa: E402
from hypogeum.publishing.client_content_sync import sync_client_content  # noqa: E402


def _find_client(name: str):
    for c in cm.list_clients():
        if (c.get("name") or "").strip().lower() == name.strip().lower():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-name", required=True)
    ap.add_argument("--prompt", action="store_true", help="type the token hidden instead of using the clipboard")
    args = ap.parse_args()

    if args.prompt:
        token = getpass.getpass("Instagram access token (hidden): ").strip()
    else:
        import subprocess
        token = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["pbcopy"], input="", text=True)  # clear the clipboard right away
        if token:
            print(f"✓ token read from clipboard ({len(token)} chars) and clipboard cleared")
        if token and (" " in token or len(token) < 50):
            print("clipboard doesn't look like an Instagram token — copy it from Meta and re-run")
            return 1
    if not token:
        print("no token entered — nothing changed")
        return 1

    # Verify before storing anything: a temporary credential under a probe id.
    probe = "probe"
    ig.store_account_token(probe, token)
    try:
        me = ig.InstagramGraphClient(probe).profile()
    except ig.InstagramGraphError as exc:
        print(f"token rejected by Instagram: {exc}")
        return 1
    finally:
        if get_secret_profile(ig.secret_profile_id_for(probe)):
            delete_secret_profile(ig.secret_profile_id_for(probe))

    ig_user_id = str(me.get("user_id") or me.get("id"))
    username = me.get("username") or ""
    if (me.get("account_type") or "").upper() not in ("BUSINESS", "CREATOR", "MEDIA_CREATOR"):
        print(f"@{username} is account_type={me.get('account_type')!r} — insights need a Professional account")
        return 1
    ig.store_account_token(ig_user_id, token, username=username)
    print(f"✓ token stored for @{username} ({me.get('account_type')})")

    client = _find_client(args.client_name)
    if client is None:
        cid = re.sub(r"[^a-z0-9]+", "_", args.client_name.lower()).strip("_")
        client = cm.create_client(args.client_name, client_id=cid, engagement_mode="observe")
        print(f"✓ client created: {client['client_id']}")
    else:
        print(f"✓ client exists: {client['client_id']}")
    cm.link_managed_account(client["client_id"], "instagram", handle=username,
                            provider_id="instagram_graph", provider_account_id=ig_user_id,
                            native_account_id=ig_user_id)
    print(f"✓ @{username} linked")

    out = sync_client_content(client["client_id"])
    print(f"✓ first sync: {out['content_items']} posts, {out['metric_rows']} with metrics"
          + (f", errors: {list(out['errors'])[:5]}" if out["errors"] else ""))
    print(f"   dashboard: /client/{client['client_id']}")
    return 0 if out["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
