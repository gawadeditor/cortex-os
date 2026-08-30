#!/usr/bin/env python3
"""
tools/reset_topic_binding.py

Operator action: detach a backlog topic from a failed episode so the NEXT
run gets a genuinely fresh episode_id.

An episode_id is the idempotency key for the admission row, the pre-plan
weekly reservation, and the plan-bound reservation. Once any of those has
reached a terminal state (a released pre-plan reservation, a failed
admission that was completed/released), that episode_id can no longer
authorize spend -- so a retry that reuses it dies at admission
("released reservation cannot be replayed", "episode already completed").
The topic is the durable unit of work; each attempt should be its own
episode. This clears episode_topics.episode_id (-> NULL) and sets status
back to 'queued', leaving the topic text/priority/provenance intact.

Does NOT touch runs, artifacts, or ledger rows for the old episode --
those stay as history.

SAFETY: dry-run by default; refuses `.hypogeum-assets/hypogeum.db` without
--i-understand-this-is-production.

    python3 tools/reset_topic_binding.py --topic-id <uuid> \
        --apply --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PROD_DB = (Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "hypogeum.db").resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--topic-id")
    g.add_argument("--episode-id", help="reset every backlog topic currently bound to this episode_id")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.db import get_connection

    if args.topic_id:
        where, params = "topic_id = ?", (args.topic_id,)
    else:
        where, params = "episode_id = ?", (args.episode_id,)

    conn = get_connection(db)
    try:
        rows = conn.execute(
            f"SELECT topic_id, channel_id, topic_text, status, episode_id "
            f"FROM episode_topics WHERE {where}", params,
        ).fetchall()
        if not rows:
            print("No matching topic — nothing to do.")
            return 0
        print(f"Topics to reset ({len(rows)}):")
        for r in rows:
            print(f"  - {r['topic_id']}  [{r['channel_id']}]  status={r['status']}  "
                  f"episode_id={r['episode_id']}")
            print(f"      {r['topic_text']}")
        print()
        if not args.apply:
            print("DRY RUN — nothing written. Re-run with --apply.")
            return 0
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            f"UPDATE episode_topics SET episode_id = NULL, status = 'queued', "
            f"claimed_at = NULL, consumed_at = NULL, updated_at = ? WHERE {where}",
            (now, *params),
        )
        conn.commit()
        print(f"Reset {cur.rowcount} topic(s): episode_id cleared, status='queued'.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
