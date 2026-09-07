"""
hypogeum/weekly_reservation_reaper.py — reconcile dead episodes' weekly
budget holds (Factory Core, 2026-09-05).

THE LEAK THIS CLOSES
--------------------
hypogeum.muapi commits the FULL ``plan.maximum_bounded_cost_usd`` to the
channel's ``channel_weekly`` scope the instant the first scene of a
verified plan is accepted by the provider (see muapi.py's
``bs.commit_reservation(... charged_amount=plan_max ...)``). That estimate
then stays counted against the weekly ceiling for the rest of the ISO week
— ``db.sum_reserved_and_committed`` sums ``reserved_amount`` for every
non-released row — and **nothing ever trues it down** when the episode
later fails QA, is superseded, or is otherwise abandoned. A channel that
burned three plans that died in assembly can find its whole weekly
ceiling consumed by episodes that shipped nothing.

``hypogeum.plan_reservation.release_superseded_plan_reservation`` only
helps the resume/recompile path (it releases a still-``reserved`` row so
the *replacement* plan can re-reserve) — it cannot touch a ``committed``
row and it does not run for a plan with no replacement.

WHAT THIS DOES
--------------
Once per worker iteration (cheap: a handful of indexed SELECTs over the
current + previous ISO week), for every ``committed`` ``channel_weekly``
``plan-weekly:*`` row whose episode is **terminally dead** — episode
``status='superseded'`` OR its latest ``runs`` row is ``failed``/``error``
— AND is **not live** (no running/queued run or queue item) AND **never
published** (no successful ``publishing_outcomes``):

    trued-down amount = (Σ that episode's dedup'd call-scope committed
                         charges)  −  (its preplan-incurred weekly row)

i.e. the episode's real POST-PLAN spend. The row is shrunk to that via
``db.shrink_terminal_weekly_reservation`` (released outright when the
episode generated no post-plan paid media at all). The row stays
``committed`` — real money was spent and still consumes that much weekly
headroom; only the padding between the plan-maximum estimate and the
actual charge is returned to the ceiling.

Pass 2 (2026-09-06): a dead episode also strands its ``reserved``
(never-committed) holds — the ~$20 ``preplan:*`` pre-plan hold, and a
``plan-weekly:*`` row that stayed ``reserved`` because its first scene
never reached provider acceptance. Those carry no real spend (real
charges land on call/episode scopes; ``preplan-incurred:*`` keeps the
real pre-plan LLM cost counted), so the reaper releases them outright via
``db.release_budget_reservation`` (which only ever flips ``reserved``).

One-directional and idempotent: the reaper only ever shrinks, recomputes
the target every pass, and no-ops once a row is already at/below it. A
published episode's hold is never touched (it shipped — it keeps its
plan-scoped commitment). No-raise contract: every failure is captured in
the returned dict, never propagated into the worker loop.

This module never talks to the network and opens only the one SQLite
connection it needs for the ledger.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

__all__ = ["reconcile_terminal_episode_weekly_reservations"]


def _week_keys(now_utc: datetime) -> list[str]:
    """ISO week labels for this week and last week (``2026-W36`` form)."""
    keys = []
    for delta in (0, 7):
        d = (now_utc - timedelta(days=delta)).isocalendar()
        keys.append(f"{d[0]}-W{d[1]:02d}")
    return keys


def reconcile_terminal_episode_weekly_reservations(
    *,
    db_path: Optional[object] = None,
    channel_id: Optional[str] = None,
    log=None,
    now_utc: Optional[datetime] = None,
) -> dict:
    """
    Returns:
        {
          "ok": bool,
          "examined": int,          # committed plan-weekly rows looked at
          "reaped": [ {episode_id, channel_id, from_usd, to_usd, result} ],
          "freed_usd": float,       # total returned to weekly ceilings
          "error": str | None,
        }
    """
    out = {"ok": False, "examined": 0, "reaped": [], "freed_usd": 0.0, "error": None}
    now = now_utc or datetime.now(timezone.utc)
    at = now.isoformat()

    try:
        from hypogeum.db import get_connection, shrink_terminal_weekly_reservation

        conn = get_connection(db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 20000")

            wk = _week_keys(now)
            placeholders = ",".join("?" for _ in wk)
            rows = conn.execute(
                f"""
                SELECT reservation_id, scope_ref, idempotency_key, reserved_amount, channel_id
                FROM budget_reservations
                WHERE scope_kind = 'channel_weekly'
                  AND status = 'committed'
                  AND idempotency_key LIKE 'plan-weekly:%'
                  AND substr(scope_ref, instr(scope_ref, ':') + 1) IN ({placeholders})
                """,
                tuple(wk),
            ).fetchall()

            # episode_ids that are still live anywhere — never reap these.
            live = {
                r[0] for r in conn.execute(
                    """
                    SELECT episode_id FROM runs WHERE status = 'running' AND episode_id IS NOT NULL
                    UNION
                    SELECT episode_id FROM run_queue
                      WHERE status IN ('queued','claimed','running') AND episode_id IS NOT NULL
                    UNION
                    SELECT episode_id FROM episode_topics
                      WHERE status IN ('queued','claimed') AND episode_id IS NOT NULL
                    """
                ).fetchall() if r[0]
            }
            superseded = {
                r[0] for r in conn.execute(
                    "SELECT episode_id FROM episodes WHERE COALESCE(status,'') = 'superseded'"
                ).fetchall() if r[0]
            }
            # 'cancelled' included 2026-09-07: a run the operator stopped
            # mid-pipeline (worker bootout) leaves the episode just as dead
            # as a 'failed' one, but was previously never reaped — its
            # plan-weekly hold (~$20) leaked against the ceiling forever.
            # The `ep not in completed_run` guard below still protects an
            # episode that was cancelled on one attempt and later completed.
            failed = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT episode_id FROM runs "
                    "WHERE status IN ('failed','error','cancelled') AND episode_id IS NOT NULL"
                ).fetchall() if r[0]
            }
            completed_run = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT episode_id FROM runs "
                    "WHERE status IN ('complete','completed') AND episode_id IS NOT NULL"
                ).fetchall() if r[0]
            }
            published = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT episode_id FROM publishing_outcomes "
                    "WHERE publishing_status = 'success' AND episode_id IS NOT NULL"
                ).fetchall() if r[0]
            }

            for row in rows:
                out["examined"] += 1
                parts = row["idempotency_key"].split(":")
                if len(parts) < 4:
                    continue
                ep = parts[2]
                ch = row["channel_id"] or parts[1]
                if channel_id is not None and ch != channel_id:
                    continue
                if ep in live or ep in published:
                    continue
                # terminally dead = superseded, or failed with no later success
                dead = ep in superseded or (ep in failed and ep not in completed_run)
                if not dead:
                    continue

                preplan = conn.execute(
                    "SELECT COALESCE(SUM(CAST(committed_amount AS REAL)),0) s "
                    "FROM budget_reservations WHERE scope_kind='channel_weekly' "
                    "AND status='committed' AND idempotency_key=?",
                    (f"preplan-incurred:{ch}:{ep}",),
                ).fetchone()["s"]
                calls = conn.execute(
                    "SELECT idempotency_key, MAX(CAST(committed_amount AS REAL)) m "
                    "FROM budget_reservations WHERE scope_kind='call' AND status='committed' "
                    "AND scope_ref LIKE ? GROUP BY idempotency_key",
                    (f"%{ep}%",),
                ).fetchall()
                call_total = sum((c["m"] or 0.0) for c in calls)
                target = max(0.0, round(call_total - float(preplan), 6))

                before = float(row["reserved_amount"])
                res = shrink_terminal_weekly_reservation(
                    conn, row["reservation_id"], f"{target:.6f}", at=at,
                )
                if res in ("shrunk", "released"):
                    after = 0.0 if res == "released" else target
                    out["reaped"].append({
                        "episode_id": ep, "channel_id": ch,
                        "from_usd": round(before, 4), "to_usd": round(after, 4),
                        "result": res,
                    })
                    out["freed_usd"] = round(out["freed_usd"] + (before - after), 4)

            # Pass 2 — RESERVED (never-committed) holds for a dead episode.
            # Pass 1 only trues down 'committed' plan-weekly rows; a dead
            # episode also strands (a) its $20-ish `preplan:*` pre-plan hold
            # and (b) a `plan-weekly:*` row that stayed 'reserved' because its
            # first scene never reached provider ACCEPTANCE (scenes failed
            # earlier). Both consume the weekly ceiling forever. A `reserved`
            # row is safe to release outright — no real spend is attached to
            # it (real charges land on 'call'/'episode' scopes, and the
            # `preplan-incurred:*` committed row keeps the real pre-plan LLM
            # cost counted). db.release_budget_reservation only flips
            # 'reserved' rows, so a committed/released row is never touched.
            from hypogeum.db import release_budget_reservation  # noqa: PLC0415

            reserved_rows = conn.execute(
                f"""
                SELECT reservation_id, idempotency_key, reserved_amount, channel_id
                FROM budget_reservations
                WHERE scope_kind = 'channel_weekly'
                  AND status = 'reserved'
                  AND substr(scope_ref, instr(scope_ref, ':') + 1) IN ({placeholders})
                """,
                tuple(wk),
            ).fetchall()
            for row in reserved_rows:
                out["examined"] += 1
                key = row["idempotency_key"] or ""
                m = re.match(r"(?:preplan|plan-weekly|preplan-incurred):[^:]+:(ep_[0-9a-fA-F]+)", key)
                if not m:
                    continue
                ep = m.group(1)
                ch = row["channel_id"] or (key.split(":")[1] if ":" in key else None)
                if channel_id is not None and ch != channel_id:
                    continue
                if ep in live or ep in published:
                    continue
                if not (ep in superseded or (ep in failed and ep not in completed_run)):
                    continue
                amt = float(row["reserved_amount"] or 0.0)
                if release_budget_reservation(conn, row["reservation_id"], at=at):
                    out["reaped"].append({
                        "episode_id": ep, "channel_id": ch,
                        "from_usd": round(amt, 4), "to_usd": 0.0, "result": "released_reserved",
                    })
                    out["freed_usd"] = round(out["freed_usd"] + amt, 4)
        finally:
            conn.close()

        out["ok"] = True
        if log is not None and out["reaped"]:
            log.info(
                "weekly_reservation_reaper_reconciled",
                examined=out["examined"],
                reaped=len(out["reaped"]),
                freed_usd=out["freed_usd"],
                episodes=[r["episode_id"] for r in out["reaped"]],
            )
    except Exception as exc:  # noqa: BLE001 — no-raise contract
        out["error"] = f"{type(exc).__name__}: {exc}"
        if log is not None:
            try:
                log.warning("weekly_reservation_reaper_failed", error=out["error"])
            except Exception:  # noqa: BLE001
                pass
    return out
