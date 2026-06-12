#!/usr/bin/env python3
"""
run_production_readiness_audit.py — Pre-ep_000 production readiness checker

Verifies all preconditions for a production episode run without generating
any content, making any API calls that cost money, or touching episode state.

Usage:
    python run_production_readiness_audit.py --episode-id ep_000

Checks performed:
    1.  Model slugs valid       — EP_MODEL, RD_MODEL, TD_PLANNING_MODEL,
                                  BGA_PASS1_MODEL, BGA_PASS2_MODEL are
                                  non-empty and not known-invalid slugs
    2.  BGA state               — BGA_ENABLED flag and BGA_PASS1_MODEL set
    3.  OpenRouter reachable    — HEAD/GET openrouter.ai (no auth required)
    4.  MUAPI key present       — MUAPI_API_KEY non-empty
    5.  ElevenLabs key present  — ELEVENLABS_API_KEY non-empty + ELEVENLABS_VOICE_ID set
    6.  YouTube creds present   — YOUTUBE_CLIENT_ID + YOUTUBE_REFRESH_TOKEN non-empty
    7.  Analytics creds present — YOUTUBE_ANALYTICS_CLIENT_ID or shared OAuth present
    8.  DB write path works     — initialize_schema() in tmp location (no-op if exists)
    9.  Archive write path      — .hypogeum-assets/ writable
    10. Finance write path      — FINANCE_LEDGER_PATH writable, or default path writable
    11. Phase 5 hooks callable  — import run_post_run_intelligence, call with no-op args

Exit codes:
    0  — all checks passed (READY)
    1  — one or more checks failed (NOT READY)

No content is generated. No ep_000 run is started. No publishing occurs.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

# Slugs confirmed absent from the live OpenRouter /models list (checked 2026-06-12).
# anthropic/claude-fable-5 and openai/gpt-5.5-pro are both live — not in this set.
_KNOWN_INVALID_SLUGS: set[str] = set()


def check_model_slugs(config) -> dict:
    """Check that all per-agent model slugs are non-empty and not known-invalid."""
    fields = {
        "EP_MODEL": getattr(config, "EP_MODEL", ""),
        "RD_MODEL": getattr(config, "RD_MODEL", ""),
        "TD_PLANNING_MODEL": getattr(config, "TD_PLANNING_MODEL", ""),
        "BGA_PASS1_MODEL": getattr(config, "BGA_PASS1_MODEL", ""),
        "BGA_PASS2_MODEL": getattr(config, "BGA_PASS2_MODEL", ""),
    }
    issues = []
    for name, slug in fields.items():
        if not slug:
            issues.append(f"{name} is empty")
        elif slug in _KNOWN_INVALID_SLUGS:
            issues.append(f"{name}={slug!r} is a known-invalid model slug")

    return {
        "check": "model_slugs",
        "ok": len(issues) == 0,
        "detail": fields,
        "issues": issues,
    }


def check_bga_state(config) -> dict:
    """Check BGA flag and model assignment."""
    enabled = bool(getattr(config, "BGA_ENABLED", False))
    pass1 = getattr(config, "BGA_PASS1_MODEL", "")
    issues = []
    if not pass1:
        issues.append("BGA_PASS1_MODEL is empty")
    return {
        "check": "bga_state",
        "ok": len(issues) == 0,
        "detail": {"BGA_ENABLED": enabled, "BGA_PASS1_MODEL": pass1},
        "issues": issues,
    }


def check_openrouter_reachable() -> dict:
    """Attempt a lightweight GET to openrouter.ai to verify network reachability."""
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "hypogeum-readiness-audit/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status < 500
        return {"check": "openrouter_reachable", "ok": ok, "detail": {"status": resp.status}, "issues": []}
    except Exception as exc:
        return {
            "check": "openrouter_reachable",
            "ok": False,
            "detail": {},
            "issues": [f"OpenRouter unreachable: {exc}"],
        }


def check_muapi_key(config) -> dict:
    key = getattr(config, "MUAPI_API_KEY", "") or ""
    ok = bool(key.strip())
    return {
        "check": "muapi_key_present",
        "ok": ok,
        "detail": {"MUAPI_API_KEY": "***" if ok else "(empty)"},
        "issues": [] if ok else ["MUAPI_API_KEY is not set"],
    }


def check_elevenlabs_creds(config) -> dict:
    key = getattr(config, "ELEVENLABS_API_KEY", "") or ""
    voice = getattr(config, "ELEVENLABS_VOICE_ID", "") or ""
    issues = []
    if not key.strip():
        issues.append("ELEVENLABS_API_KEY is not set")
    if not voice.strip():
        issues.append("ELEVENLABS_VOICE_ID is not set")
    return {
        "check": "elevenlabs_creds",
        "ok": len(issues) == 0,
        "detail": {
            "ELEVENLABS_API_KEY": "***" if key else "(empty)",
            "ELEVENLABS_VOICE_ID": voice if voice else "(empty)",
        },
        "issues": issues,
    }


def check_youtube_creds(config) -> dict:
    client_id = getattr(config, "YOUTUBE_CLIENT_ID", "") or ""
    refresh = getattr(config, "YOUTUBE_REFRESH_TOKEN", "") or ""
    issues = []
    if not client_id.strip():
        issues.append("YOUTUBE_CLIENT_ID is not set")
    if not refresh.strip():
        issues.append("YOUTUBE_REFRESH_TOKEN is not set")
    return {
        "check": "youtube_creds",
        "ok": len(issues) == 0,
        "detail": {
            "YOUTUBE_CLIENT_ID": "***" if client_id else "(empty)",
            "YOUTUBE_REFRESH_TOKEN": "***" if refresh else "(empty)",
        },
        "issues": issues,
    }


def check_analytics_creds(config) -> dict:
    # Analytics reuses YouTube OAuth; check for key presence
    client_id = getattr(config, "YOUTUBE_CLIENT_ID", "") or ""
    refresh = getattr(config, "YOUTUBE_REFRESH_TOKEN", "") or ""
    ok = bool(client_id.strip() and refresh.strip())
    return {
        "check": "analytics_creds",
        "ok": ok,
        "detail": {"shared_youtube_oauth": ok},
        "issues": [] if ok else ["Analytics requires YOUTUBE_CLIENT_ID + YOUTUBE_REFRESH_TOKEN"],
    }


def check_db_write_path() -> dict:
    """Initialize schema in a temp DB to verify SQLite write path."""
    try:
        from hypogeum.db import initialize_schema

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "readiness_test.db"
            initialize_schema(db_path=db_path)
            ok = db_path.exists()
        return {"check": "db_write_path", "ok": ok, "detail": {}, "issues": []}
    except Exception as exc:
        return {
            "check": "db_write_path",
            "ok": False,
            "detail": {},
            "issues": [f"DB write failed: {exc}"],
        }


def check_archive_write_path() -> dict:
    """Verify .hypogeum-assets/ is writable."""
    try:
        assets_dir = Path(__file__).resolve().parent / ".hypogeum-assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        probe = assets_dir / ".readiness_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"check": "archive_write_path", "ok": True, "detail": {}, "issues": []}
    except Exception as exc:
        return {
            "check": "archive_write_path",
            "ok": False,
            "detail": {},
            "issues": [f"Archive path not writable: {exc}"],
        }


def check_finance_write_path(config) -> dict:
    """Verify the finance ledger path (or default) is writable."""
    try:
        ledger_path_str = getattr(config, "FINANCE_LEDGER_PATH", "") or ""
        if ledger_path_str:
            ledger_dir = Path(ledger_path_str).parent
        else:
            ledger_dir = Path(__file__).resolve().parent / ".hypogeum-assets" / "finance"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        probe = ledger_dir / ".readiness_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {
            "check": "finance_write_path",
            "ok": True,
            "detail": {"ledger_dir": str(ledger_dir)},
            "issues": [],
        }
    except Exception as exc:
        return {
            "check": "finance_write_path",
            "ok": False,
            "detail": {},
            "issues": [f"Finance path not writable: {exc}"],
        }


def check_phase5_hooks_callable() -> dict:
    """Import and call run_post_run_intelligence with no-op args to verify hooks callable."""
    try:
        from hypogeum.post_run_intelligence import run_post_run_intelligence

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = run_post_run_intelligence(
                episode_id="_readiness_probe",
                production_metadata=None,
                source_registry=None,
                run_id="",
                analytics_snapshot=None,
                output_dir=tmp_path / "intelligence",
                knowledge_dir=tmp_path / "knowledge",
                signals_dir=tmp_path / "signals",
                db_path=tmp_path / "probe.db",
            )
        ok = isinstance(result, dict) and "all_flows_ok" in result
        return {
            "check": "phase5_hooks_callable",
            "ok": ok,
            "detail": {"all_flows_ok": result.get("all_flows_ok")},
            "issues": [] if ok else ["run_post_run_intelligence did not return expected dict"],
        }
    except Exception as exc:
        return {
            "check": "phase5_hooks_callable",
            "ok": False,
            "detail": {},
            "issues": [f"Phase 5 import/call failed: {exc}"],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_audit(episode_id: str) -> dict:
    from hypogeum.config import Config

    config = Config()

    checks = [
        check_model_slugs(config),
        check_bga_state(config),
        check_openrouter_reachable(),
        check_muapi_key(config),
        check_elevenlabs_creds(config),
        check_youtube_creds(config),
        check_analytics_creds(config),
        check_db_write_path(),
        check_archive_write_path(),
        check_finance_write_path(config),
        check_phase5_hooks_callable(),
    ]

    passed = [c for c in checks if c["ok"]]
    failed = [c for c in checks if not c["ok"]]
    ready = len(failed) == 0

    report = {
        "schema_version": "v1",
        "episode_id": episode_id,
        "audited_at": _now_iso(),
        "ready": ready,
        "passed_count": len(passed),
        "failed_count": len(failed),
        "checks": checks,
    }
    return report


def _print_report(report: dict) -> None:
    ready = report["ready"]
    status = "READY" if ready else "NOT READY"
    print(f"\n{'='*60}")
    print(f"  Production Readiness Audit — {report['episode_id']}")
    print(f"  {status}  ({report['passed_count']} passed, {report['failed_count']} failed)")
    print(f"{'='*60}")
    for c in report["checks"]:
        icon = "OK" if c["ok"] else "FAIL"
        print(f"  [{icon}] {c['check']}")
        for issue in c.get("issues", []):
            print(f"         ! {issue}")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-ep_000 production readiness audit. No content is generated."
    )
    parser.add_argument(
        "--episode-id",
        required=True,
        help="Episode identifier to audit for (e.g. ep_000)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output full report as JSON instead of human-readable format",
    )
    args = parser.parse_args()

    report = run_audit(episode_id=args.episode_id)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)

    sys.exit(0 if report["ready"] else 1)


if __name__ == "__main__":
    main()
