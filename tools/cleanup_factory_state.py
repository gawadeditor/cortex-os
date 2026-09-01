#!/usr/bin/env python3
"""
tools/cleanup_factory_state.py

One-time factory hygiene: remove ~4 months of test / eval / failed-run
residue from the database and the local asset area, keeping ONLY the
episodes that were actually published.

Keep-rule
---------
An episode is KEPT iff it has a ``publication_intents`` row with
``status = 'succeeded'``. Everything the kept episodes reference is kept.
Pass ``--keep ep_x`` to protect an extra episode.

An episode with ``status = 'complete'`` that was never published is HELD
(printed, not touched) unless ``--purge-unpublished-complete`` is given —
these need a separate regenerate-or-discard decision.

Categories (each skippable with --skip-<name>)
----------------------------------------------
  stale-episodes  episodes not kept/held + every DB row they own
  dead-runs       runs status in (failed,cancelled,orphaned,paused) whose
                  episode is not kept + every DB row they own
  topics          episode_topics status in (cancelled,skipped)
  dup-grants      run_authorization_grants grant_kind='channel_operating':
                  keep the newest per channel, delete the rest + consumption
  dead-schedules  schedules status='disabled' + their scheduled_runs
  local-files     .hypogeum-assets/episodes/<id>/ for every removed episode
  logs            rotate .hypogeum-assets/pipeline.log + dashboard.out (gz)

SAFETY: dry-run by default. --apply performs the writes. Refuses the
production DB without --i-understand-this-is-production. Always run and
review the dry-run first.

    python3 tools/cleanup_factory_state.py
    python3 tools/cleanup_factory_state.py --apply --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
_EPISODES_DIR = _ROOT / ".hypogeum-assets" / "episodes"
_LOGS = [
    _ROOT / ".hypogeum-assets" / "pipeline.log",
    _ROOT / ".hypogeum-assets" / "dashboard.out",
]
_LOG_TAIL_BYTES = 20 * 1024 * 1024  # keep the last 20 MB when rotating

# Tables we NEVER cascade-delete from, even if they carry episode_id/run_id.
_PROTECTED_TABLES = {
    "channels", "factories", "users", "channel_operations_profiles",
    "channel_profiles", "channel_profile_settings", "brand_profiles",
    "model_profiles", "publishing_profiles", "provider_profiles",
    "secret_profiles", "api_keys", "provider_accounts", "workflow_definitions",
    "channel_templates", "channel_provider_bindings", "discovery_state",
    "factory_settings", "worker_control", "schedules",
}

# The asset catalog is episode-scoped: only remove rows when the EPISODE is
# being removed, never merely because a failed run that produced (a version
# of) the asset is being cleaned. A kept episode keeps its whole catalog.
_EPISODE_ONLY_TABLES = {"archive_index", "assets", "asset_versions"}

_CATEGORIES = ["stale-episodes", "dead-runs", "superseded-runs", "dead-queue",
               "topics", "dup-grants", "dead-schedules", "stale-sync-log",
               "local-files", "logs"]

_DEAD_RUN_STATES = ("failed", "cancelled", "orphaned", "paused")
_DEAD_TOPIC_STATES = ("cancelled", "skipped")


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _all_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _in_clause(values: list[str]) -> tuple[str, list]:
    if not values:
        return "(NULL)", []
    return "(" + ",".join("?" * len(values)) + ")", list(values)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    ap.add_argument("--keep", action="append", default=[], metavar="EPISODE_ID",
                    help="protect an extra episode (repeatable)")
    ap.add_argument("--purge-unpublished-complete", action="store_true",
                    help="also delete status='complete' episodes that were never published")
    for cat in _CATEGORIES:
        ap.add_argument(f"--skip-{cat}", action="store_true", dest=f"skip_{cat.replace('-', '_')}")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.db import get_connection
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")

    mode = "APPLY" if args.apply else "dry-run"
    print(f"db   : {db}")
    print(f"mode : {mode}\n")

    # ---- resolve the keep / held / remove episode sets -------------------
    published = {r[0] for r in conn.execute(
        "SELECT DISTINCT episode_id FROM publication_intents WHERE status='succeeded'")}
    # every episode_id that has ANY footprint — including catalog rows whose
    # episodes-table row was already deleted in an earlier pass (pure orphans).
    all_eps: set[str] = set()
    for _t, _col in (("episodes", "episode_id"), ("runs", "episode_id"),
                     ("assets", "episode_id"), ("archive_index", "episode_id"),
                     ("episode_topics", "episode_id"), ("stage_executions", "episode_id")):
        try:
            all_eps |= {r[0] for r in conn.execute(
                f"SELECT DISTINCT {_col} FROM {_t} WHERE {_col} IS NOT NULL AND {_col} != ''")}
        except Exception:  # noqa: BLE001
            pass
    keep = published | set(args.keep)
    complete_unpub = {r[0] for r in conn.execute(
        "SELECT episode_id FROM episodes WHERE status='complete'")} - keep
    held = set() if args.purge_unpublished_complete else complete_unpub
    remove_eps = sorted(all_eps - keep - held)

    print(f"KEEP  ({len(keep)}): {sorted(keep)}")
    if held:
        print(f"HELD  ({len(held)}) — complete but unpublished, NOT touched "
              f"(use --purge-unpublished-complete): {sorted(held)}")
    print(f"REMOVE({len(remove_eps)}): {remove_eps}\n")

    # run_ids owned by removed episodes + dead runs whose episode isn't kept
    rm_ep_clause, rm_ep_params = _in_clause(remove_eps)
    run_ids: set[str] = set()
    if not args.skip_stale_episodes:
        run_ids |= {r[0] for r in conn.execute(
            f"SELECT run_id FROM runs WHERE episode_id IN {rm_ep_clause} AND run_id IS NOT NULL",
            rm_ep_params)}
    if not args.skip_dead_runs:
        keep_clause, keep_params = _in_clause(sorted(keep))
        dead = {r[0] for r in conn.execute(
            f"SELECT run_id FROM runs WHERE status IN {_in_clause(list(_DEAD_RUN_STATES))[0]} "
            f"AND (episode_id IS NULL OR episode_id NOT IN {keep_clause})",
            list(_DEAD_RUN_STATES) + keep_params)}
        run_ids |= dead
    superseded_ids: set[str] = set()
    if not args.skip_superseded_runs:
        # failed/cancelled runs whose episode was later completed / published:
        # pure history, the episode succeeded — safe to drop with its children.
        superseded_ids = {r[0] for r in conn.execute(
            """
            SELECT r.run_id FROM runs r
            WHERE r.status IN ('failed','cancelled') AND r.run_id IS NOT NULL
              AND (
                r.episode_id IN (SELECT episode_id FROM episodes
                                 WHERE status IN ('complete','completed','published'))
                OR r.episode_id IN (SELECT DISTINCT episode_id FROM publication_intents
                                    WHERE status = 'succeeded')
              )
            """
        )}
        run_ids |= superseded_ids
    run_ids = {r for r in run_ids if r}

    # ---- plan the generic cascade --------------------------------------
    ep_targets = set(remove_eps)
    tables = [t for t in _all_tables(conn) if t not in _PROTECTED_TABLES]
    plan: dict[str, int] = {}
    for t in tables:
        c = _cols(conn, t)
        conds, params = [], []
        if "episode_id" in c and ep_targets:
            cl, p = _in_clause(sorted(ep_targets))
            conds.append(f"episode_id IN {cl}"); params += p
        if "run_id" in c and run_ids and t not in _EPISODE_ONLY_TABLES:
            cl, p = _in_clause(sorted(run_ids))
            conds.append(f"run_id IN {cl}"); params += p
        if not conds:
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE {' OR '.join(conds)}", params).fetchone()[0]
        if n:
            plan[t] = n

    # publishing_receipts: joined via publishing_attempts.attempt_id
    recpt_n = 0
    if run_ids or ep_targets:
        acl, ap_ = _in_clause(sorted(ep_targets))
        rcl, rp_ = _in_clause(sorted(run_ids))
        recpt_n = conn.execute(
            f"SELECT COUNT(*) FROM publishing_receipts WHERE attempt_id IN "
            f"(SELECT attempt_id FROM publishing_attempts WHERE episode_id IN {acl})",
            ap_).fetchone()[0]
    if recpt_n:
        plan["publishing_receipts"] = recpt_n

    # budget_reservations: run/episode scoped only, never channel_weekly
    br_n = 0
    scope_refs = sorted(run_ids | ep_targets)
    if scope_refs:
        cl, p = _in_clause(scope_refs)
        br_n = conn.execute(
            f"SELECT COUNT(*) FROM budget_reservations "
            f"WHERE scope_kind != 'channel_weekly' AND scope_ref IN {cl}", p).fetchone()[0]

    print("=== stale/dead/superseded runs cascade ===")
    print(f"  run_ids removed: {len(run_ids)}  "
          f"(of which superseded — failed run of a published episode: {len(superseded_ids)})")
    for t, n in sorted(plan.items()):
        print(f"  {t:<40} {n}")
    if br_n:
        print(f"  {'budget_reservations (run/ep scoped)':<40} {br_n}")
    print()

    # ---- dead run_queue rows ----------------------------------------
    dq_n = 0
    if not args.skip_dead_queue:
        try:
            dq_n = conn.execute(
                "SELECT COUNT(*) FROM run_queue WHERE status IN ('failed','cancelled')"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            dq_n = 0
        print(f"=== dead-queue ===\n  run_queue rows status in (failed, cancelled): {dq_n}\n")

    # ---- topics -------------------------------------------------------
    topic_n = 0
    if not args.skip_topics:
        tcl = _in_clause(list(_DEAD_TOPIC_STATES))[0]
        topic_n = conn.execute(
            f"SELECT COUNT(*) FROM episode_topics WHERE status IN {tcl}",
            list(_DEAD_TOPIC_STATES)).fetchone()[0]
        print(f"=== topics ===\n  episode_topics status in {_DEAD_TOPIC_STATES}: {topic_n}\n")

    # ---- dup-grants --------------------------------------------------
    grant_del: list[str] = []
    _have_grants = any(
        r[0] == "run_authorization_grants" for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='run_authorization_grants'"))
    if not args.skip_dup_grants and _have_grants:
        rows = list(conn.execute(
            "SELECT grant_id, channel_id FROM run_authorization_grants "
            "WHERE grant_kind='channel_operating' ORDER BY channel_id, registered_at DESC"))
        seen: set[str] = set()
        for r in rows:
            if r["channel_id"] in seen:
                grant_del.append(r["grant_id"])
            else:
                seen.add(r["channel_id"])
        cons_n = 0
        if grant_del:
            cl, p = _in_clause(grant_del)
            try:
                cons_n = conn.execute(
                    f"SELECT COUNT(*) FROM run_authorization_grant_consumption WHERE grant_id IN {cl}",
                    p).fetchone()[0]
            except Exception:  # noqa: BLE001
                cons_n = 0
        print(f"=== dup-grants ===\n  channel_operating grants to delete: {len(grant_del)} "
              f"(keeping newest per channel)\n  consumption rows: {cons_n}\n")

    # ---- dead-schedules --------------------------------------------
    sched_del: list[str] = []
    if not args.skip_dead_schedules:
        sched_del = [r[0] for r in conn.execute(
            "SELECT schedule_id FROM schedules WHERE status='disabled'")]
        sr_n = 0
        if sched_del:
            cl, p = _in_clause(sched_del)
            sr_n = conn.execute(
                f"SELECT COUNT(*) FROM scheduled_runs WHERE schedule_id IN {cl}", p).fetchone()[0]
        print(f"=== dead-schedules ===\n  disabled schedules: {len(sched_del)}\n"
              f"  their scheduled_runs: {sr_n}\n")

    # ---- stale analytics sync log --------------------------------
    # platform_sync_log rows with no channel_id are pre-multi-channel test
    # runs (triggered_by 'test' / 'p12-*'); nothing real writes NULL now.
    synclog_n = 0
    if not args.skip_stale_sync_log:
        try:
            synclog_n = conn.execute(
                "SELECT COUNT(*) FROM platform_sync_log WHERE channel_id IS NULL"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            synclog_n = 0
        print(f"=== stale-sync-log ===\n  platform_sync_log rows with NULL channel_id: {synclog_n}\n")

    # ---- local files ---------------------------------------------
    # Every directory directly under .hypogeum-assets/episodes/ whose name is
    # not a kept/held episode is cruft: DB-tracked stale episodes AND pure
    # filesystem orphans (ep_000, bare-UUID run dirs, val_*/probe scaffolding)
    # that never had a DB row.
    dirs_del: list[Path] = []
    if not args.skip_local_files and _EPISODES_DIR.exists():
        protect = keep | held
        for d in sorted(_EPISODES_DIR.iterdir()):
            if d.is_dir() and d.name not in protect:
                dirs_del.append(d)
        total = sum(
            sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            for d in dirs_del)
        print(f"=== local-files ===\n  episode/run dirs to delete: {len(dirs_del)} "
              f"({total / 1e9:.2f} GB)  [keeping {sorted(protect)}]")
        for d in dirs_del[:40]:
            print(f"    {d.name}")
        if len(dirs_del) > 40:
            print(f"    … and {len(dirs_del) - 40} more")
        print()

    # ---- logs ---------------------------------------------------
    if not args.skip_logs:
        print("=== logs ===")
        for lg in _LOGS:
            if lg.exists():
                print(f"  rotate {lg} ({lg.stat().st_size / 1e9:.2f} GB) -> {lg.name}.gz + fresh tail")
        print()

    if not args.apply:
        conn.close()
        print("Dry-run only. Re-run with --apply --i-understand-this-is-production to execute.")
        return 0

    # =================================================================
    # APPLY
    # =================================================================
    print("--- APPLYING ---")
    try:
        # cascade
        for t in plan:
            if t == "publishing_receipts":
                continue
            c = _cols(conn, t)
            conds, params = [], []
            if "episode_id" in c and ep_targets:
                cl, p = _in_clause(sorted(ep_targets)); conds.append(f"episode_id IN {cl}"); params += p
            if "run_id" in c and run_ids and t not in _EPISODE_ONLY_TABLES:
                cl, p = _in_clause(sorted(run_ids)); conds.append(f"run_id IN {cl}"); params += p
            if not conds:
                continue
            conn.execute(f"DELETE FROM {t} WHERE {' OR '.join(conds)}", params)
        if recpt_n:
            acl, ap_ = _in_clause(sorted(ep_targets))
            conn.execute(
                f"DELETE FROM publishing_receipts WHERE attempt_id IN "
                f"(SELECT attempt_id FROM publishing_attempts WHERE episode_id IN {acl})", ap_)
        if br_n:
            cl, p = _in_clause(scope_refs)
            conn.execute(
                f"DELETE FROM budget_reservations "
                f"WHERE scope_kind != 'channel_weekly' AND scope_ref IN {cl}", p)
        if not args.skip_topics and topic_n:
            conn.execute(
                f"DELETE FROM episode_topics WHERE status IN {_in_clause(list(_DEAD_TOPIC_STATES))[0]}",
                list(_DEAD_TOPIC_STATES))
        if not args.skip_dup_grants and grant_del:
            cl, p = _in_clause(grant_del)
            try:
                conn.execute(
                    f"DELETE FROM run_authorization_grant_consumption WHERE grant_id IN {cl}", p)
            except Exception:  # noqa: BLE001
                pass
            conn.execute(f"DELETE FROM run_authorization_grants WHERE grant_id IN {cl}", p)
        if not args.skip_dead_schedules and sched_del:
            cl, p = _in_clause(sched_del)
            conn.execute(f"DELETE FROM scheduled_runs WHERE schedule_id IN {cl}", p)
            conn.execute(f"DELETE FROM schedules WHERE schedule_id IN {cl}", p)
        if not args.skip_stale_sync_log and synclog_n:
            conn.execute("DELETE FROM platform_sync_log WHERE channel_id IS NULL")
        if not args.skip_dead_queue and dq_n:
            conn.execute("DELETE FROM run_queue WHERE status IN ('failed','cancelled')")
        conn.commit()
        print("  DB writes committed.")
    finally:
        conn.close()

    if not args.skip_local_files:
        for d in dirs_del:
            shutil.rmtree(d, ignore_errors=True)
            print(f"  removed {d}")

    if not args.skip_logs:
        for lg in _LOGS:
            if not lg.exists():
                continue
            gz = lg.with_suffix(lg.suffix + ".gz")
            try:
                with lg.open("rb") as fin, gzip.open(gz, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                with lg.open("rb") as fin:
                    size = lg.stat().st_size
                    if size > _LOG_TAIL_BYTES:
                        fin.seek(size - _LOG_TAIL_BYTES)
                    tail = fin.read()
                lg.write_bytes(tail)
                print(f"  rotated {lg} -> {gz.name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN: could not rotate {lg}: {exc}")

    print("\nDone. Run `graphify update .` to refresh the code graph is not needed "
          "(data-only change), but restart the dashboard so it re-reads the DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
