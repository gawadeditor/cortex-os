#!/usr/bin/env python3
"""
tools/configure_channel_operating_grant.py

Build + (optionally) sign ONE ADR-016 Channel Operating Grant for a channel:
a single offline signature that authorizes many autonomous episodes over a
bounded period, gated by the channel's weekly $ ceiling + a provider/model
binding list. After this, the daily scheduler path produces autonomously with
NO per-episode signing ceremony.

Cadence stays 100% dashboard config: this script signs with an EMPTY
`cadence_snapshot`, so `runtime_admission._verify_operating_grant` skips the
cadence check -- you may retune `production_cadence` (7 shorts, 2 long + 3
shorts, ...) any time without re-signing. Only a change to the channel's
`weekly_video_budget_usd` invalidates the grant (a real money decision).

Reads, from the target DB:
  - `channel_operations_profiles.weekly_video_budget_usd`  -> weekly ceiling
  - `compute_channel_package_identity(channel_id)`         -> package version/checksum
  - the current workflow binding                           -> workflow checksum
Model bindings are derived from the loaded Config (every openrouter model
slug in a *_MODEL* setting, plus the MUAPI video/image/thumbnail models,
Perplexity, and ElevenLabs voice + music).

SAFETY: dry-run by default. Prints the grant spec and the exact
`scripts/approve_run_grant.py` command. `--apply` runs that command via
subprocess (needs `--private-key`). Refuses `.hypogeum-assets/hypogeum.db`
without `--i-understand-this-is-production`.

    python3 tools/configure_channel_operating_grant.py --channel hypogeum
    python3 tools/configure_channel_operating_grant.py --channel hypogeum \
        --apply --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production \
        --private-key approval_ed25519_private_key.pem
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()

# Canonical (provider, operation) -> domain, from hypogeum.provider_domains.
_OPS = {
    "openrouter": ("text.completion", "openrouter_text"),
    "perplexity": ("research.query", "perplexity_research"),
    "elevenlabs_voice": ("voice.narration_segment", "elevenlabs_voice"),
    "elevenlabs_music": ("music.track", "elevenlabs_music"),
    "muapi_video": ("video.text_to_video", "muapi_video"),
    "muapi_image": ("image.reference", "muapi_image"),
    "muapi_thumbnail": ("image.thumbnail", "muapi_thumbnail"),
}


def _openrouter_model_slugs(cfg) -> list[str]:
    """Every `<provider>/<model>` slug in a *_MODEL* Config setting, EXCEPT
    image models (they are not text.completion calls -- OPENROUTER_IMAGE_MODEL
    routes through a different operation/domain when used at all)."""
    slugs = set()
    for name in dir(cfg):
        if "MODEL" not in name or "IMAGE" in name:
            continue
        v = getattr(cfg, name, None)
        if isinstance(v, str) and "/" in v and " " not in v:
            slugs.add(v)
    return sorted(slugs)


def _build_bindings(cfg) -> list[dict]:
    """One binding per distinct (provider, model, operation) the pipeline uses.
    Generous per-binding ceilings -- the weekly ledger + the plan-bound
    reservation are the real spend enforcement; these just have to not be
    the tighter limit for a legitimate call."""
    b: list[dict] = []

    def add(provider, model, op, max_cost, max_calls, max_total):
        b.append({
            "provider": provider, "model": model, "provider_operation": op,
            "max_cost_per_call": str(max_cost), "max_calls": int(max_calls),
            "max_total_spend": str(max_total),
            "retry_authorized": True, "fallback_authorized": True,
        })

    for slug in _openrouter_model_slugs(cfg):
        add("openrouter", slug, _OPS["openrouter"][0], "5.00", 4000, "400.00")

    add("perplexity", getattr(cfg, "PERPLEXITY_MODEL", "sonar-pro"),
        _OPS["perplexity"][0], "1.00", 2000, "40.00")
    add("elevenlabs", getattr(cfg, "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
        _OPS["elevenlabs_voice"][0], "2.00", 4000, "120.00")
    add("elevenlabs", getattr(cfg, "ELEVENLABS_MUSIC_MODEL_ID", "music_v1"),
        _OPS["elevenlabs_music"][0], "3.00", 500, "80.00")

    video_models = {
        getattr(cfg, "MUAPI_T1_MODEL", "veo3-text-to-video"),
        getattr(cfg, "MUAPI_T2_MODEL", "veo3.1-lite-text-to-video"),
        getattr(cfg, "MUAPI_T3_MODEL", "veo3.1-lite-text-to-video"),
        getattr(cfg, "MUAPI_I2V_MODEL", "veo3-image-to-video"),
    }
    for m in sorted(video_models):
        add("muapi", m, _OPS["muapi_video"][0], "2.50", 2000, "400.00")
    add("muapi", getattr(cfg, "MUAPI_IMAGE_MODEL", "flux-2-dev"),
        _OPS["muapi_image"][0], "1.00", 500, "60.00")
    add("muapi", getattr(cfg, "THUMBNAIL_IMAGE_MODEL", "gpt-image-2-text-to-image"),
        _OPS["muapi_thumbnail"][0], "1.00", 500, "60.00")
    return b


def build_spec(channel_id: str, db: Path, *, days_valid: int) -> dict:
    from hypogeum.config import Config
    from hypogeum import channel_config, workflow_executor
    from hypogeum.channel_budget_policy import resolve_configured_weekly_limit
    from hypogeum.db import get_connection

    cfg = Config()

    conn = get_connection(db)
    try:
        _tz, weekly_limit, _ref = resolve_configured_weekly_limit(channel_id, conn=conn)
    finally:
        conn.close()

    pkg = channel_config.compute_channel_package_identity(channel_id)
    if not pkg.get("available"):
        raise SystemExit(f"channel package identity unavailable for {channel_id!r}: {pkg.get('reason')}")

    wf = workflow_executor.resolve_workflow_binding(
        channel_id, "hypogeum_main_production", "1.0.0",
    )

    now = datetime.now(timezone.utc)
    bindings = _build_bindings(cfg)
    domains = sorted({d for (_op, d) in _OPS.values()})

    return {
        # timestamped to the second so re-signing (e.g. after a weekly-budget
        # change) is a fresh grant + approval artifact, never a UNIQUE clash
        # on {grant_id}:approval. resolve_active_operating_grant picks the
        # most recently registered active one for the channel.
        "grant_id": f"op_grant_{channel_id}_{now:%Y%m%dT%H%M%S}",
        "grant_version": 1,
        "channel_id": channel_id,
        "workflow_id": wf["workflow_id"],
        "workflow_version": wf["workflow_version"],
        "workflow_checksum": wf["workflow_checksum"],
        "channel_package_version": pkg["version"],
        "channel_package_checksum": pkg["checksum"],
        "allowed_provider_domains": domains,
        "cadence_snapshot": "",   # empty -> cadence stays dashboard-editable
        "weekly_exposure_ceiling_usd": str(Decimal(str(weekly_limit))),
        "currency": "USD",
        "valid_from": now.isoformat(),
        "expires_at": (now + timedelta(days=days_valid)).isoformat(),
        "operating_period_ref": f"{channel_id}:{now:%Y-%m-%d}..+{days_valid}d",
        "bindings": bindings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--channel", default="hypogeum")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--days-valid", type=int, default=90)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--private-key", type=Path, default=_ROOT / "approval_ed25519_private_key.pem")
    ap.add_argument("--approver-identity", default="channel operator")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and not args.i_understand_this_is_production and args.apply:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    spec = build_spec(args.channel, db, days_valid=args.days_valid)
    spec_path = _ROOT / f"operating_grant_spec_{args.channel}.json"
    spec_path.write_text(json.dumps(spec, indent=2))

    print(f"Channel Operating Grant spec for {args.channel!r}:")
    print(f"  weekly ceiling : USD {spec['weekly_exposure_ceiling_usd']}  "
          f"(= channel_operations_profiles.weekly_video_budget_usd)")
    print(f"  cadence_snapshot: '' (empty -> cadence stays dashboard-editable)")
    print(f"  valid          : {spec['valid_from']}  ->  {spec['expires_at']}")
    print(f"  domains        : {', '.join(spec['allowed_provider_domains'])}")
    print(f"  bindings       : {len(spec['bindings'])} (provider/model/operation)")
    print(f"  spec written   : {spec_path}")
    print()

    cmd = [
        sys.executable, str(_ROOT / "scripts" / "approve_run_grant.py"),
        "--kind", "channel-operating",
        "--spec", str(spec_path),
        "--private-key", str(args.private_key),
        "--approver-identity", args.approver_identity,
        "--nonce", "<uuid>",
        "--issued-at", "<iso>",
        "--db-path", str(db),
        "--yes",
    ]
    if not args.apply:
        print("DRY RUN. To sign + register, either re-run with --apply, or run:")
        print("  " + " ".join(cmd))
        return 0

    import uuid
    cmd[cmd.index("<uuid>")] = uuid.uuid4().hex
    cmd[cmd.index("<iso>")] = datetime.now(timezone.utc).isoformat()
    print("Running:", " ".join(cmd), "\n")
    return subprocess.call(cmd, cwd=str(_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
