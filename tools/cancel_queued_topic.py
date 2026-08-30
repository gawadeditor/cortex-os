#!/usr/bin/env python3
"""
tools/cancel_queued_topic.py

Operator action: take a topic OUT of a channel's production backlog so the
autonomous worker will never claim it. Use when an episode is not salvageable
and repeated auto-retries would just burn budget on the same failure.

Transitions every matching `episode_topics` row from 'queued' -> 'cancelled'
(with a reason). Does NOT delete artifacts, runs, or the episode record --
fully reversible by re-enqueuing the topic.

SAFETY: dry-run by default. Refuses `.hypogeum-assets/hypogeum.db` without
`--i-understand-this-is-production`.

    # inspect (no writes):
    python3 tools/cancel_queued_topic.py --episode-id ep_af078f34 \
        --db .hypogeum-assets/hypogeum.db

    # apply to production:
    python3 tools/cancel_queued_topic.py --episode-id ep_af078f34 \
        --reason "operator drop: 390 plan-conformance failures, not salvageable" \
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
    g.add_argument("--episode-id", help="cancel every queued topic bound to this episode_id")
    g.add_argument("--topic-id", help="cancel exactly this episode_topics.topic_id")
    ap.add_argument("--reason", default="operator drop (tools/cancel_queued_topic.py)")
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

    if args.episode_id:
        where, params = "episode_id = ? AND status = 'queued'", (args.episode_id,)
    else:
        where, params = "topic_id = ? AND status = 'queued'", (args.topic_id,)

    conn = get_connection(db)
    try:
        rows = conn.execute(
            f"SELECT topic_id, channel_id, topic_text, source, created_at "
            f"FROM episode_topics WHERE {where}",
            params,
        ).fetchall()

        if not rows:
            print("No queued topic matches — nothing to do.")
            return 0

        print(f"Queued topics to cancel ({len(rows)}):")
        for r in rows:
            print(f"  - {r['topic_id']}  [{r['channel_id']}]  ({r['source']}, {r['created_at']})")
            print(f"      {r['topic_text']}")
        print()

        if not args.apply:
            print("DRY RUN — nothing written. Re-run with --apply to cancel.")
            return 0

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            f"UPDATE episode_topics SET status = 'cancelled', updated_at = ?, "
            f"reason = ? WHERE {where}",
            (now, args.reason, *params),
        )
        conn.commit()
        print(f"Cancelled {cur.rowcount} topic row(s). reason={args.reason!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
