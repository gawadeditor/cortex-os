# Bundle-Cycle Policy Install and Stale-Record Reconciliation — Checkpoint

Status: performs steps 1–3 of the remaining activation sequence from the
prior checkpoint (`HYPOGEUM_ZERNIO_BINDING_INSTALLATION_2026-08-22.md`),
per explicit operator selection ("Steps 1-3: policy install +
reconciliation (dry-run then apply)"). **Autonomous publishing was not
enabled, the worker was not restarted, and nothing was published or
unpaused.**

## HEAD

`faa87a3d9e6ad157119ebae720fb35a9be3579e1` (unchanged by this task — no
source files were modified, only production database rows).

## Preflight (re-confirmed before mutating)

- PID 845 (`run_worker.py`): same process, started `2026-08-12 13:28:23`
- PID 60882 (`run_dashboard.py`): same process, started `2026-08-14 14:35:05`
- `worker_control.queue_paused`: `'1'`
- `schedules` (`channel_id='hypogeum'`): `status='paused'`
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False`

## Database backup

- Backup path: `.hypogeum-assets/backups/hypogeum.db.pre_bundle_policy_and_reconciliation_20260822T093746Z.bak`
- Method: `sqlite3.Connection.backup()` (WAL-consistent)
- SHA256 of production DB before/after backup: `fdff089a2b969c583bf1774b55143baca296f05d9acf4bcff9b555256adce6c1` (unchanged — confirms backup is read-only against the source)
- SHA256 of backup file: `872b323cc2137cd1b908931281cd7f8f82b0de84d6aaad2bce546734ae9cfd8c`
- `PRAGMA integrity_check` on the backup: `ok`
- No existing backup was overwritten.

## Step 1 — Installed `bundle_cycle_policy`

`update_channel_operations_profile("hypogeum", bundle_cycle_policy=<channel_packages/hypogeum/scheduling_policy_v1.json's "bundle_cycle_policy" object>)`.

- Before: `bundle_cycle_policy={}` (empty — confirms the column existed from an earlier schema-only migration but held no data, consistent with the disclosure in the prior checkpoint).
- After: `bundle_cycle_policy` matches the Channel Package file's `bundle_cycle_policy` object exactly (`hypogeum_weekly_release_v1`, anchor `main_episode`, required `[main_episode, short_001, short_002, short_003]`).
- Verified unchanged by this update: `deliverable_schedule_policy`, `enabled_platforms` (`{"youtube": true, "facebook": false, "tiktok": false, "instagram": false}`), `publishing_schedule`, `operator_enabled`, `publishing_enabled`, `unattended_operation_enabled`.
- `update_channel_operations_profile()` returned `True` (read-back verification passed, per its own fail-closed contract).

## Step 2 — Stale publishing-record reconciliation (`ep_f3619022`)

`reconcile_stale_publishing_records_for_episode("ep_f3619022", "hypogeum", dry_run=True)` then `dry_run=False`:

- Dry-run: `eligible=True`, 8 rows found (4 `publishing_outcomes` rows, status `pending`; 4 `publish_calendar` rows, status `scheduled`; all platform `youtube`), `applied_count=0` — matches the count previously documented (the operator-confirmed deleted-upload scenario for `ep_f3619022`).
- Apply: `eligible=True`, `applied_count=8`, all 8 rows moved to `publishing_status`/`publish_status = 'skipped'`. No row was deleted; `platform_video_id` and every other field left intact as historical evidence.
- Verified: all 8 rows now read `skipped` in the database.
- Idempotency: re-running the dry-run immediately afterward returns `eligible=True`, `rows=0`, reason "no unconfirmed publishing_outcomes/publish_calendar rows remain for this episode — nothing to reconcile."

## Step 3 — Stale-topic reconciliation (`ep_f3619022`)

`reconcile_stale_topics_for_episode("ep_f3619022", "hypogeum", dry_run=True)` then `dry_run=False`:

- Dry-run: `eligible=True`, `episode_status='complete'`, 8 queued rows found (1 `manual`, 7 `auto_retry`), `applied_count=0` — matches the previously-documented 8 stale rows.
- Apply: `eligible=True`, `applied_count=8`, all 8 rows' status moved to `cancelled`.
- Verified: all 8 targeted rows now read `cancelled`; the episode's other, non-queued `episode_topics` rows (`claimed`/`consumed`) were left untouched, as required (only `queued` rows are eligible).
- Idempotency: re-running the dry-run immediately afterward returns `eligible=True`, `rows=0`, reason "no queued topics remain for this episode — nothing to reconcile."

## Post-mutation verification

- PID 845 / PID 60882: unchanged (same processes, same start times) — neither signaled, stopped, or restarted.
- `worker_control.queue_paused`: still `'1'`.
- `schedules` (`channel_id='hypogeum'`): still `status='paused'`.
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: still `False`.
- ADR-013 operational tables unaffected by these 3 steps: `provider_profiles=1`, `provider_accounts=4`, `channel_provider_bindings=1` (unchanged from the prior checkpoint), `media_staging_attempts=0`, `publishing_attempts=0`, `publication_intents=0` (unchanged, still zero).
- Live, read-only `check_publish_readiness("ep_f3619022", channel_id="hypogeum", platform="youtube")`: all six checks still pass (`CHANNEL_SCOPE`, `A_METADATA`, `B_LONG_RENDER`, `C_SHORTS_RENDERS`, `D_PUBLISHING_BINDING`, `E_MEDIA_STAGING_READY`) — unaffected by this task, as expected (these 3 steps touch legacy-table/topic-queue bookkeeping, not readiness inputs).
- No Zernio/YouTube/other platform call was made. No media file was opened. No paid generation occurred.

## Remaining activation sequence (not performed; informational only)

4. Persist `AUTO_ZERNIO_PUBLISHING_ENABLED=True` in the real `.env`.
5. Restart PID 845 onto HEAD `b2e86fc` (or later) so the running worker process loads the automatic-publishing code path.
6. Perform a publishing-only controlled smoke run while the production queue remains paused.
7. Verify the Zernio-scheduled bundle.
8. Only then unpause bounded production for exactly one replacement bundle.

None of steps 4–8 were performed this task.
