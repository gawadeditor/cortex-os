#!/usr/bin/env python3
"""
scripts/run_director_check.py — Factory Director CLI

Runs the Factory Director governance assessment and prints the report.

Usage:
    python3 scripts/run_director_check.py
    python3 scripts/run_director_check.py --factory-id hypogeum
    python3 scripts/run_director_check.py --json
    python3 scripts/run_director_check.py --domain ois
    python3 scripts/run_director_check.py --domain credentials --json

Options:
    --factory-id ID   Factory to assess (default: hypogeum)
    --json            Output report as JSON instead of formatted text
    --domain DOMAIN   Show only one domain's checks (still exits on overall level)

Exit codes:
    0 — READY or DEGRADED (factory can run)
    1 — BLOCKED or UNKNOWN (factory cannot run)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.factory_director import assess_factory, format_report

_EXIT_OK    = 0   # READY or DEGRADED
_EXIT_BLOCK = 1   # BLOCKED or UNKNOWN


def _report_to_dict(report) -> dict:
    """Serialise FactoryDirectorReport to a JSON-safe dict."""
    def _check(c) -> dict:
        return {
            "check_id":    c.check_id,
            "name":        c.name,
            "passed":      c.passed,
            "blocking":    c.blocking,
            "detail":      c.detail,
            "remediation": c.remediation,
        }

    def _domain(d) -> dict:
        return {
            "domain":  d.domain,
            "status":  d.status,
            "summary": d.summary,
            "checks":  [_check(c) for c in d.checks],
        }

    clearance = report.run_clearance
    return {
        "factory_id":       report.factory_id,
        "factory_name":     report.factory_name,
        "readiness_level":  report.readiness_level,
        "readiness_score":  report.readiness_score,
        "can_run":          report.can_run,
        "assessed_at":      report.assessed_at,
        "blocking_reasons": report.blocking_reasons,
        "warnings":         report.warnings,
        "domains":          [_domain(d) for d in report.domains],
        "run_clearance": {
            "cleared":           clearance.cleared,
            "reason":            clearance.reason,
            "blocking_signals":  clearance.blocking_signals,
            "pending_approvals": clearance.pending_approvals,
            "checked_at":        clearance.checked_at,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Factory Director — governance readiness check.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit 0 = READY or DEGRADED (can run).\n"
            "Exit 1 = BLOCKED or UNKNOWN (cannot run)."
        ),
    )
    parser.add_argument(
        "--factory-id", default="hypogeum",
        help="Factory ID to assess (default: hypogeum)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of formatted text",
    )
    parser.add_argument(
        "--domain",
        help="Show only one domain's checks (e.g. ois, credentials, run_lifecycle)",
    )
    args = parser.parse_args(argv)

    report = assess_factory(factory_id=args.factory_id)

    if args.domain:
        domain_names = {d.domain for d in report.domains}
        if args.domain not in domain_names:
            print(
                f"ERROR: unknown domain {args.domain!r}. "
                f"Valid: {', '.join(sorted(domain_names))}",
                file=sys.stderr,
            )
            return _EXIT_BLOCK

        if args.json:
            domain = next(d for d in report.domains if d.domain == args.domain)
            print(json.dumps(
                {
                    "factory_id":      report.factory_id,
                    "readiness_level": report.readiness_level,
                    "readiness_score": report.readiness_score,
                    "domain": {
                        "domain":  domain.domain,
                        "status":  domain.status,
                        "summary": domain.summary,
                        "checks": [
                            {
                                "check_id":    c.check_id,
                                "name":        c.name,
                                "passed":      c.passed,
                                "blocking":    c.blocking,
                                "detail":      c.detail,
                                "remediation": c.remediation,
                            }
                            for c in domain.checks
                        ],
                    },
                },
                indent=2,
            ))
        else:
            domain = next(d for d in report.domains if d.domain == args.domain)
            status_icon = {"healthy": "✓", "warning": "⚠", "blocked": "✗", "unknown": "?"}
            icon = status_icon.get(domain.status, "?")
            print(f"{icon} Domain: {domain.domain}  [{domain.status.upper()}]")
            print(f"  {domain.summary}")
            for c in domain.checks:
                tick = "✓" if c.passed else ("✗" if c.blocking else "⚠")
                print(f"  {tick} {c.name}")
                if not c.passed:
                    print(f"      {c.detail}")
                    if c.remediation:
                        print(f"      → {c.remediation}")
    elif args.json:
        print(json.dumps(_report_to_dict(report), indent=2))
    else:
        print(format_report(report))

    return _EXIT_OK if report.can_run else _EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
