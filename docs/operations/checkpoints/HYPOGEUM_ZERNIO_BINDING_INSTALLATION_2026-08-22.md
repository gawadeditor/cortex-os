# Hypogeum Zernio Binding Installation — Checkpoint

Status: production-database installation of the verified Hypogeum Zernio
provider identity and the single currently-authorized YouTube publishing
binding. **No publishing or autonomous activation was performed or
enabled.** This checkpoint documents the installation only.

## HEAD

`b2e86fc168f4027c0400174271a99d0b72487d49` (branch `main`, unchanged by
this task — no source files were modified). `git status --short` before
and after this task: clean except the two pre-existing, permanently-
protected untracked exceptions (`-`, `docs/operations/BURN_IN_LOG.md`).

## 1. Safety preflight (recorded before any mutation)

- Production DB: `.hypogeum-assets/hypogeum.db`
- PID 845 (`run_worker.py`): running, started `2026-08-12 13:28:23` (same process throughout)
- PID 60882 (`run_dashboard.py`): running, started `2026-08-14 14:35:05` (same process throughout)
- `worker_control.queue_paused`: `'1'`
- Schedule (`schedules` table, `channel_id='hypogeum'`): `status='paused'` (a separate `factory`-scope schedule, unrelated to publishing, was `active` — this is the discovery/production cadence schedule, not a publishing schedule, and was left untouched)
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False` (confirmed both via `.env` absence and a live `Config()` read — not overridden by shell environment)
- Before counts: `provider_profiles=0`, `provider_accounts=0`, `channel_provider_bindings=0`, `media_staging_attempts=0`, `publishing_attempts=0`, `publication_intents=0`

All preflight confirmations satisfied: PID 845 was the old, unrestarted process; queue and the hypogeum channel schedule were paused; autonomous Zernio publishing was disabled; every ADR-013 operational table was empty, so no process could have published merely from bindings existing. Proceeded per instruction (autonomous publishing was NOT already enabled).

## 2. Database backup

- Backup path: `.hypogeum-assets/backups/hypogeum.db.pre_zernio_binding_install_20260822T085850Z.bak`
- Method: `sqlite3.Connection.backup()` (WAL-consistent, not a raw file copy) — the live DB's WAL file was empty (0 bytes) at backup time, so no in-flight uncommitted data existed to miss.
- SHA256 of production DB immediately before backup: `7e1ea09d0f42654da7df6d1d4e67616eb054b9416e8cf8d76a9c921e6d14a647`
- SHA256 of production DB immediately after backup: `7e1ea09d0f42654da7df6d1d4e67616eb054b9416e8cf8d76a9c921e6d14a647` (unchanged — backup is read-only against the source)
- SHA256 of backup file: `67d778e172739fac9890d94d15a7ef4d11376b3bf3d73138a9252185a165afbf`
- Verification: `PRAGMA integrity_check` on the backup file returned `ok`; spot-read of `channel_profiles`/`provider_profiles` row counts confirmed the backup is a genuine, readable snapshot of the pre-mutation state (1 channel profile, 0 provider profiles).
- No existing backup was overwritten — the target path did not exist before this run (checked and enforced by the backup script itself).

Note: the DB's live SHA256 at the very start of the preflight step (`d8c22f1c...`) differs slightly from the hash taken immediately before the backup (`7e1ea09d...`) a few minutes later — expected, minor background write activity from the live `run_dashboard.py`/`run_worker.py` processes' normal idle-loop bookkeeping between the two reads. Neither figure reflects any mutation performed by this task.

## 3. Zernio identity resolution (live, read-only)

Credential resolved via `hypogeum.credential_manager.resolve_credential()` against the active `provider='zernio'` secret profile (label "Hypogeum Zernio unified publishing"). Used only in-process to set an `Authorization: Bearer` header; never printed, logged, or written to any file.

Fresh calls made this session (operative run, immediately preceding installation):

| Endpoint | UTC timestamp | Method | URL | HTTP status |
|---|---|---|---|---|
| profiles | 2026-08-22T09:01:14.556700+00:00 | GET | `https://zernio.com/api/v1/profiles` | 200 |
| accounts | 2026-08-22T09:01:15.667461+00:00 | GET | `https://zernio.com/api/v1/accounts` | 200 |

(An earlier, identical read-only verification pass at 09:00:03–09:00:13 UTC the same session confirmed the same identity before the operative run above; both were `GET` only, no body, no write parameters.)

**Verified identity (redacted — last 4 characters of each internal ID only, per instruction not to print full identifiers):**

- Exactly **1** Zernio profile: `name="Default"`, `isDefault=true`, id ending `...41c9`.
- Exactly **4** accounts, one per platform (`facebook`, `instagram`, `tiktok`, `youtube`), every one:
  - `isActive=true`, `needsReconnection=false` (connected and active, not merely present);
  - owned by the single profile above (`profileId` matches `...41c9` on all four — unambiguous ownership);
  - ids ending `...8a0b` (facebook), `...a275` (instagram), `...46ca` (tiktok), `...a569` (youtube).
- The YouTube account's `username`/`displayName` both end in a Hypogeum-branded suffix (`...geum`), confirmed live — this is The Hypogeum account, not a different channel's.

Installation gates enforced programmatically before any DB write (script aborts with no mutation if any fails): exactly 1 profile; every required platform present with exactly one active, non-reconnection-required account; all four accounts owned by the same profile; the YouTube account's username/displayName contains the Hypogeum brand marker. All gates passed.

**Disclosure**: during an earlier verification step in this session (before installation), one ad-hoc `sqlite3` SELECT query (run to confirm the just-installed `provider_id`/status, before the redaction discipline below was applied to every subsequent query) printed the full, unredacted `external_profile_id` value to this session's terminal output. This was a mistake on the operator-assistant's part — every query from that point forward, and every value in this checkpoint, uses `substr(..., -4)` redaction. The value was never written to any committed file, this checkpoint, or any other durable artifact — it appeared transiently in the live session transcript only. Flagged here for full transparency rather than omitted.

## 4. Rows installed (transactional, idempotent provider-registry functions)

Used `hypogeum.publishing.provider_repository.upsert_provider_profile()`, `.upsert_provider_account()`, `.bind_channel_provider()` exclusively — no raw SQL. Each function is independently transactional (`BEGIN IMMEDIATE` ... `COMMIT`/`ROLLBACK`) and idempotent by its own unique-key contract; no improvised multi-statement transaction was needed or used, since the supported API already guarantees each step is safe to retry.

**Provider profile** (`provider_profiles`, 1 row):
- `provider_id="zernio"`, `external_profile_id` = the verified real profile id (redacted `...41c9` above), `status="active"`.

**Provider accounts** (`provider_accounts`, 4 rows — one per platform):
- `facebook` → real external account id (redacted `...8a0b`), `status="active"`, `needs_reconnection=0`.
- `instagram` → real external account id (redacted `...a275`), `status="active"`, `needs_reconnection=0`.
- `tiktok` → real external account id (redacted `...46ca`), `status="active"`, `needs_reconnection=0`.
- `youtube` → real external account id (redacted `...a569`), `status="active"`, `needs_reconnection=0`.
- Every row: `provider_profile_id` FK set to the single profile above; `metadata_json` empty (no credential/secret value in any row — confirmed by direct query; the `provider_accounts`/`provider_profiles` schema has no credential-shaped column at all, structurally enforced).
- Recording these 4 accounts does **not** enable those platforms for Hypogeum publishing — no binding was created for tiktok/instagram/facebook (see below).

**Channel binding** (`channel_provider_bindings`, 1 row — the only one created):
- `channel_id="hypogeum"`, `capability="publishing"`, `platform="youtube"`, `provider_id="zernio"`, `provider_profile_id`/`provider_account_id` pointing at the rows above, `enabled=1`.
- No binding was created for `tiktok`/`instagram`/`facebook` — `channel_operations_profiles.enabled_platforms` for `hypogeum` confirmed still `{"youtube": true, "facebook": false, "tiktok": false, "instagram": false}` (unchanged by this task).
- No other channel was touched.

## 5. Idempotency and isolation proof

Re-ran the exact same three operations a second time, using the real values read back from the just-installed rows (no second live Zernio call was needed for this proof).

- `upsert_provider_profile()` second call: returned the **same** `provider_profile_id` (asserted programmatically) — no duplicate row.
- `upsert_provider_account()` second call, all 4 platforms: each returned the **same** `provider_account_id` (asserted programmatically) — no duplicate rows.
- `bind_channel_provider()` second call: idempotent no-op path (identical provider/profile/account for the same key touches only `updated_at`) — no duplicate binding.
- Row counts after the second run: `provider_profiles=1`, `provider_accounts=4`, `channel_provider_bindings=1` — **unchanged** from after the first run.
- Bindings for `channel_id != 'hypogeum'`: `0`.
- Bindings for `channel_id='hypogeum' AND platform != 'youtube'`: `0`.
- `channel_operations_profiles.enabled_platforms` for `hypogeum`: unchanged (`tiktok`/`instagram`/`facebook` still `false`).
- `metadata_json` on every `provider_profiles`/`provider_accounts` row: empty — no credential was ever copied into these tables.

## 6. Readiness verification (read-only)

Live `check_publish_readiness("ep_f3619022", channel_id="hypogeum", platform="youtube", assets_root=..., db_path=<production>)` — never mutates (per the module's own contract, unchanged this task):

```
CHANNEL_SCOPE          pass -- episode_id='ep_f3619022' confirmed attributed to channel_id='hypogeum'
A_METADATA              pass -- title, description, and shorts_titles all present and unambiguous
B_LONG_RENDER           pass -- main render file present and non-empty
C_SHORTS_RENDERS        pass -- all 3 expected Short render file(s) present and non-empty
D_PUBLISHING_BINDING    pass -- enabled binding confirmed for provider_id='zernio', platform(s): ['youtube']
E_MEDIA_STAGING_READY   pass -- active credential present; 4 deliverable(s) staging-eligible
passed: True
```

All six checks pass for the first time in this task chain. Neither `publish_channel_release()` nor the worker publishing trigger (`zernio_channel_release_trigger.process_channel_ready_bundles()`) was invoked at any point.

## 7. Zero activation side effects — confirmed

- No Zernio `POST`/write/upload/presign call occurred — every HTTP call made this session was `GET /v1/profiles` or `GET /v1/accounts`; `requests.post` was never invoked anywhere in this task.
- No media file was opened for upload — no `.mp4`/video/thumbnail file was read by any script this task ran.
- `media_staging_attempts`: `0` before and after (unchanged).
- `publishing_attempts`: `0` before and after (unchanged).
- `publication_intents`: `0` before and after (unchanged).
- `worker_control.queue_paused`: `'1'` before and after (unchanged).
- `schedules` (`channel_id='hypogeum'`): `status='paused'` before and after (unchanged).
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False` before and after (unchanged; not touched).
- PID 845: same process, same start time (`2026-08-12 13:28:23`), never signaled/stopped/restarted.
- PID 60882: same process, same start time (`2026-08-14 14:35:05`), never signaled/stopped/restarted.
- No paid generation occurred — no LLM/provider call other than the two documented `GET` calls above was made this task.

## Before / after operational-table counts

| Table | Before | After |
|---|---|---|
| `provider_profiles` | 0 | 1 |
| `provider_accounts` | 0 | 4 |
| `channel_provider_bindings` | 0 | 1 |
| `media_staging_attempts` | 0 | 0 |
| `publishing_attempts` | 0 | 0 |
| `publication_intents` | 0 | 0 |

## Remaining activation sequence (not performed; informational only, in order)

1. Install `bundle_cycle_policy` (`update_channel_operations_profile("hypogeum", bundle_cycle_policy=<channel_packages/hypogeum/scheduling_policy_v1.json's "bundle_cycle_policy" object>)`).
2. Dry-run, then apply, stale publishing-record reconciliation (`reconcile_stale_publishing_records_for_episode`) for `ep_f3619022`'s 8 stale historical rows.
3. Dry-run, then apply, the 8 stale-topic reconciliation (`reconcile_stale_topics_for_episode`).
4. Persist `AUTO_ZERNIO_PUBLISHING_ENABLED=True` in the real `.env`.
5. Restart PID 845 onto HEAD `b2e86fc` (or later) so the running worker process actually loads the automatic-publishing code path installed in the prior two checkpoints.
6. Perform a publishing-only controlled smoke run while the production queue remains paused.
7. Verify the Zernio-scheduled bundle (staged/scheduled correctly, matching the anchored bundle-cycle plan).
8. Only then unpause bounded production for exactly one replacement bundle.

None of steps 1–8 were performed. This checkpoint stops after reporting the binding installation, per instruction — no publishing, no autonomous activation, no schedule-policy install, no reconciliation, no flag change, no worker restart, no unpause.
