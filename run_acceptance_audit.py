#!/usr/bin/env python3
"""
run_acceptance_audit.py — Factory Acceptance Audit CLI

Determines whether the factory infrastructure is complete enough to allow
any smoke run or episode run.

Usage:
    python3 run_acceptance_audit.py
    python3 run_acceptance_audit.py --json
    python3 run_acceptance_audit.py --db-path /custom/path/hypogeum.db

Checks (BLOCKER — all must pass for ACCEPTED):
    1.  db_schema_v2              DB schema v2 can be initialized; all v2 tables present
    2.  asset_registry            Asset Registry importable with full public API
    3.  archive_recall            Archive Recall importable with full public API
    4.  reuse_engine              Reuse Engine importable with full public API
    5.  agent_auditor             Agent Performance Auditor importable with full public API
    6.  evolution_engine          Factory Evolution Engine importable with full public API
    7.  channel_manager           Multi-Channel Architecture importable with full public API
    8.  platform_connectors       All 5 platform connectors registered
    9.  dashboard                 Dashboard snapshot + format_text callable

Checks (WARNING — logged, do not block acceptance):
    10. identity_governance_docs  Key brand/identity documents on disk
    11. production_readiness_audit_callable  run_production_readiness_audit.py importable
    12. platform_credentials      Required env credentials present (no external calls)

Exit codes:
    0  — all blocker checks passed  (ACCEPTED)
    1  — one or more blockers failed (REJECTED)

No episodes are started. No content is generated. No external APIs are called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Factory acceptance audit gate. "
            "No content generated. No external APIs called."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the full report as JSON",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the SQLite database path used for schema checks",
    )
    args = parser.parse_args()

    from hypogeum.acceptance_audit import format_report, run_acceptance_audit

    db_path = Path(args.db_path) if args.db_path else None
    report  = run_acceptance_audit(db_path=db_path)

    if args.json:
        # Serialise to JSON (dataclasses → dicts)
        def _serialise(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {
                    k: _serialise(v)
                    for k, v in obj.__dict__.items()
                }
            if isinstance(obj, list):
                return [_serialise(i) for i in obj]
            return obj

        print(json.dumps(_serialise(report), indent=2))
    else:
        print(format_report(report))

    sys.exit(0 if report.ready else 1)


if __name__ == "__main__":
    main()
