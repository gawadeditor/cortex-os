# Factory OS V1 Execution Plan

**Date:** 2026-06-15
**Objective:** Factory OS V1 Operational Complete
**Source:** Factory OS V1 Implementation Gap Audit (2026-06-15)
**Architecture:** Frozen. No new design. Implementation only.

---

## Execution Order

```
Phase 1 — Foundation     (8 working days)
  P1-01  Bootstrap channel                   1 day
  P1-02  Connect pipeline to channel         2 days
  P1-03  User / role system                  3 days
  P1-04  Credential manager                  2 days

Phase 2 — Operations     (15 working days)
  P2-01  OIS signal routing                  5 days
  P2-02  Notification delivery               2 days
  P2-03  Schedule entity + scheduler         5 days
  P2-04  CALOG — channel state machine       3 days

Phase 3 — Governance     (16 working days)
  P3-01  Audit persistence                   2 days
  P3-02  Factory entity system               2 days
  P3-03  RLG — run state machine             5 days
  P3-04  Approval system                     5 days
  P3-05  Policy cascade                      2 days
```

**Total estimated effort: 39 working days (~8 calendar weeks)**

**First task to start immediately: P1-01**

---

## Phase 1 — Foundation

### P1-01 — Channel Bootstrap

**Component:** Channel Profile System / Multi-channel Management
**Effort:** 1 day

**Objective:**
Create the Hypogeum channel record in the live DB. `bootstrap_channel()` exists in `channel_config.py` and the tables already exist — they are just empty. This is the foundation all subsequent channel-aware operations depend on. Without a channel record, the pipeline cannot be channel-aware, CALOG has nothing to govern, and the scheduler has nothing to trigger.

**Files to modify:**
- New: `scripts/bootstrap_channel.py` — idempotent bootstrap script; safe to re-run
- Modify: `hypogeum/channel_config.py` — add `get_or_create_channel()` helper that checks before inserting (idempotency guard)

**Database changes:**
- INSERT into `brand_profiles`: persona_name, tone, target_audience, visual_style, language, content_pillars drawn from current Hypogeum configuration
- INSERT into `model_profiles`: episode_producer=EP_MODEL, research_director=RD_MODEL, thumbnail_planner=TD_PLANNING_MODEL, brand_guardian_p1=BGA_PASS1_MODEL, brand_guardian_p2=BGA_PASS2_MODEL, narration_voice_id=ELEVENLABS_VOICE_ID
- INSERT into `publishing_profiles`: enabled_platforms=["youtube"], schedule={"cadence":"weekly","day_of_week":"thursday","time_of_day":"18:00"}, ai_disclosure_required=true
- INSERT into `channel_profiles`: channel_id="hypogeum", name="The Hypogeum Chronicles", status="configured"

**Tests required:**
- `tests/test_channel_config.py` — add `TestBootstrap` class:
  - `test_bootstrap_creates_channel` — `get_channel_profile("hypogeum")` returns record
  - `test_bootstrap_is_idempotent` — running twice does not raise, count stays at 1
  - `test_bootstrap_model_profile_fields` — all five model fields match .env values
  - `test_bootstrap_publishing_schedule` — schedule dict has cadence, day_of_week, time_of_day

**Dependencies:** None

**Acceptance criteria:**
- `python3 scripts/bootstrap_channel.py` runs without error
- `from hypogeum.channel_config import get_channel_profile; get_channel_profile("hypogeum")` returns a `ChannelProfile` with `status="configured"`
- `from hypogeum.channel_config import load_channel_config; load_channel_config("hypogeum")` returns a `ChannelConfig` with correct model slug values matching current `.env`
- `list_channel_profiles()` returns exactly 1 profile
- Script is safe to run again (idempotent)

---

### P1-02 — Connect Pipeline to Channel Profile

**Component:** Channel Profile System / Run Management
**Effort:** 2 days

**Objective:**
Make `run_pipeline.py` channel-aware. When `--channel-id hypogeum` is passed, the pipeline resolves model assignments, publishing rules, and budget settings from the channel record in DB rather than reading `.env` directly. The pipeline and channel profile become connected. This is what makes channels data rather than code.

**Files to modify:**
- `run_pipeline.py` — add `--channel-id` optional argument; when provided, load ChannelConfig and pass to pipeline
- `hypogeum/config.py` — add `Config.merge_channel_config(channel_config: ChannelConfig) -> Config` classmethod that returns a new Config with .env as base but channel model assignments, voice ID, and budget ceiling overriding the env values
- `hypogeum/pipeline.py` — add `channel_id: Optional[str] = None` parameter to `run_pipeline()`; pass it to `register_run()`; log the channel_id at pipeline_run_started
- `hypogeum/run_registry.py` — add `channel_id: Optional[str] = None` parameter to `register_run()`; write to DB
- `hypogeum/db.py` — add `channel_id` to `_V2_COLUMN_MIGRATIONS["runs"]` list

**Database changes:**
- `_V2_COLUMN_MIGRATIONS["runs"]` += `("channel_id", "TEXT")` — this adds the column via `_apply_v2_column_migrations()` on next startup; no schema.sql edit required

**Tests required:**
- `tests/test_pipeline.py` — add `TestChannelIntegration` class:
  - `test_channel_id_passed_to_register_run` — mock register_run, verify channel_id is forwarded
  - `test_channel_config_overrides_ep_model` — if channel model_profile has a different ep model slug, pipeline uses it
  - `test_channel_config_overrides_voice_id` — narration voice_id from channel profile overrides env
  - `test_no_channel_id_falls_back_to_env` — omitting --channel-id still works (backwards-compat)
- `tests/test_run_registry.py` — add test that `register_run(channel_id="hypogeum")` writes channel_id to DB row

**Dependencies:** P1-01 (channel record must exist for integration test to pass)

**Acceptance criteria:**
- `python3 run_pipeline.py --topic "X" --episode-id ep_test --channel-id hypogeum` resolves channel config without error
- `runs` table row for the run has `channel_id = "hypogeum"` populated
- Model assignments in the run's `model_assignments` JSON reflect the channel profile's values, not raw .env values when they differ
- Omitting `--channel-id` continues to work exactly as before (no regression)

---

### P1-03 — User / Role System

**Component:** User / Role System
**Effort:** 3 days

**Objective:**
Implement the users table, API key authentication, and `check_authorization()`. This is the foundation for every governance action that requires knowing who is acting. Without it, CALOG activation gates (Gate 9: authorization check), approvals (who approved), and any future multi-user operation cannot function. The permission model already exists as pure data in `dashboard_api.py` — this task wires enforcement to it.

**Files to modify:**
- New: `hypogeum/auth.py` — user CRUD, API key management, `check_authorization()`
- `hypogeum/db.py` — add `users` and `api_keys` tables to `schema.sql`
- `hypogeum/schema.sql` — add `users` and `api_keys` CREATE TABLE statements
- `hypogeum/dashboard_api.py` — replace stub `get_permission()` with call to `auth.check_authorization()`
- New: `scripts/create_owner.py` — bootstrap Owner user and first API key; prints key to stdout once

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT NOT NULL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('owner','admin','operator','editor','viewer','client')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id      TEXT NOT NULL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    key_hash    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    last_used_at TEXT,
    expires_at  TEXT,
    created_at  TEXT NOT NULL
);
```

**`hypogeum/auth.py` public API:**
```python
create_user(email, name, role, user_id=None, db_path=None) -> UserRecord
get_user(user_id, db_path=None) -> Optional[UserRecord]
list_users(db_path=None) -> list[UserRecord]
disable_user(user_id, db_path=None) -> bool

create_api_key(user_id, label, db_path=None) -> tuple[str, str]
  # returns (key_id, raw_key); raw_key shown once, never stored
authenticate_api_key(raw_key, db_path=None) -> Optional[UserRecord]
  # hashes raw_key (SHA-256), looks up key_hash, returns user if active

check_authorization(user_id, action, db_path=None) -> bool
  # looks up user role; validates against PERMISSION_TABLE[action].required_role
  # role hierarchy: owner > admin > operator > editor > viewer > client
```

**Tests required:**
- New: `tests/test_auth.py`:
  - `TestUserCRUD` — create, get, list, disable
  - `TestApiKeys` — create returns raw key once; re-authentication works; revoked key returns None
  - `TestCheckAuthorization` — owner can approve_run; viewer cannot; operator can pause_channel
  - `test_disabled_user_cannot_authenticate`
  - `test_api_key_hash_not_stored_as_plaintext` — verify DB stores hash, not raw key

**Dependencies:** None

**Acceptance criteria:**
- `python3 scripts/create_owner.py` creates Owner user and prints one-time API key
- `authenticate_api_key(raw_key)` returns the Owner `UserRecord`
- `check_authorization(owner_id, "approve_run")` → True
- `check_authorization(viewer_id, "approve_run")` → False
- `check_authorization(operator_id, "pause_channel")` → True
- Raw API key never appears in DB; only SHA-256 hash stored
- `disable_user(user_id)` prevents that user's API keys from authenticating

---

### P1-04 — Credential Manager

**Component:** Identity & Secret Management
**Effort:** 2 days

**Objective:**
Replace raw `.env` credential strings with a managed `secret_profiles` table. Channels will reference credentials by `secret_profile_id` rather than reading from env directly. Adds credential testing (live connectivity probe) and expiry tracking. Required for CALOG's Gate 4 (Credential Gate) and multi-channel secret isolation.

V1 note: Credentials stored as plaintext in DB. Acceptable for local self-hosted single-machine deployment. Encryption is a V2 SaaS concern.

**Files to modify:**
- New: `hypogeum/credential_manager.py`
- `hypogeum/schema.sql` — add `secret_profiles` table
- New: `scripts/import_credentials.py` — reads current `.env` values and inserts them as `secret_profiles` records; idempotent

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS secret_profiles (
    secret_profile_id  TEXT NOT NULL PRIMARY KEY,
    provider           TEXT NOT NULL,
    label              TEXT NOT NULL,
    credential_type    TEXT NOT NULL CHECK(credential_type IN
                           ('api_key','oauth2_token','webhook_url','username_password')),
    credential_data    TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK(status IN ('active','expired','revoked','testing')),
    scope              TEXT NOT NULL DEFAULT 'factory'
                           CHECK(scope IN ('factory','channel-specific')),
    channel_id         TEXT,
    expires_at         TEXT,
    last_tested_at     TEXT,
    last_used_at       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
```

**`hypogeum/credential_manager.py` public API:**
```python
create_secret_profile(provider, label, credential_type, credential_data,
                       scope="factory", channel_id=None, expires_at=None,
                       db_path=None) -> SecretProfile

get_secret_profile(secret_profile_id, db_path=None) -> Optional[SecretProfile]
list_secret_profiles(provider=None, scope=None, db_path=None) -> list[SecretProfile]
update_secret_profile(secret_profile_id, credential_data=None, expires_at=None,
                       label=None, db_path=None) -> bool
revoke_secret_profile(secret_profile_id, db_path=None) -> bool

resolve_credential(secret_profile_id, db_path=None) -> str
  # Returns credential_data value. Updates last_used_at.
  # Raises CredentialRevokedError if status != 'active'.

test_secret_profile(secret_profile_id, db_path=None) -> TestResult
  # dataclass: success: bool, latency_ms: int, error: Optional[str]
  # Fires a provider-specific minimal API call.
  # Updates last_tested_at on success.
  # Supported providers: openrouter, perplexity, muapi, elevenlabs, youtube
```

**Tests required:**
- New: `tests/test_credential_manager.py`:
  - `TestCRUD` — create, get, list, revoke
  - `test_resolve_updates_last_used_at`
  - `test_revoked_credential_raises`
  - `test_test_openrouter_credential` (mocked HTTP)
  - `test_credential_data_is_stored_as_given`
  - `test_import_script_is_idempotent` (run twice, count stays same)

**Dependencies:** None

**Acceptance criteria:**
- `python3 scripts/import_credentials.py` creates one `secret_profile` per provider in `.env` (openrouter, perplexity, muapi, elevenlabs, youtube)
- `list_secret_profiles()` returns 5 profiles after import
- `resolve_credential(profile_id)` returns the API key string
- `test_secret_profile(openrouter_profile_id)` makes a real API call and returns `success=True` within 5 seconds
- Revoking a profile prevents `resolve_credential()` from returning the value

---

## Phase 2 — Operations

### P2-01 — OIS Signal Routing

**Component:** Operations Layer
**Effort:** 5 days

**Objective:**
Extend the thin `signals.py` signal recorder into a functional Operational Intelligence System. The core requirement for V1: signals are classified by severity, blocking signals prevent new runs from starting, and signals are routed to the notification system. Without this, budget exhaustion and credential failures are invisible and cannot halt production.

**Files to modify:**
- New: `hypogeum/ois.py` — full OIS module (signal routing, classification, blocking enforcement)
- `hypogeum/signals.py` — keep as thin writer; re-export key functions from `ois.py` for backwards compatibility
- `hypogeum/schema.sql` — extend `signal_records` schema via migration (not new table)
- `hypogeum/db.py` — add to `_V2_COLUMN_MIGRATIONS["signal_records"]`: severity, signal_type, blocking, resolved_at, channel_id, source_system
- `hypogeum/pipeline.py` — call `ois.is_factory_blocked()` before run registration; raise `OISBlockedError` if blocked
- `hypogeum/agents/finance_agent.py` — call `ois.emit_signal("budget_exhausted", ...)` when budget ceiling hit
- `hypogeum/startup.py` — call `ois.emit_signal("credential_failure", ...)` on connectivity check failure

**`hypogeum/ois.py` public API:**
```python
emit_signal(
    signal_type: str,           # see Signal Types below
    severity: str,              # 'INFO' | 'WARNING' | 'CRITICAL'
    message: str,
    channel_id: Optional[str],
    source_system: str,         # 'pipeline', 'finance_agent', 'startup', 'scheduler', etc.
    blocking: bool = False,     # True = prevents new runs
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> str  # signal_id

resolve_signal(signal_id: str, resolved_by: str, db_path=None) -> bool

is_factory_blocked(db_path=None) -> bool
  # Returns True if any active blocking=True signal exists at factory scope

is_channel_blocked(channel_id: str, db_path=None) -> bool
  # Returns True if any active blocking=True signal exists for this channel or factory

get_active_signals(channel_id=None, severity=None, db_path=None) -> list[SignalRecord]
get_blocking_signals(channel_id=None, db_path=None) -> list[SignalRecord]
```

**Signal types (V1 set):**
- `budget_exhausted` — CRITICAL, blocking=True — finance agent emits when ceiling hit
- `credential_failure` — CRITICAL, blocking=True — startup emits on connectivity failure
- `run_failed` — WARNING, blocking=False — pipeline emits on failure
- `run_completed` — INFO, blocking=False — pipeline emits on success
- `channel_blocked` — CRITICAL, blocking=True — CALOG emits (P2-04 wires this)
- `emergency_halt` — CRITICAL, blocking=True — operator-emitted via CLI
- `low_budget_warning` — WARNING, blocking=False — finance agent emits at 80% ceiling

**Database changes:**
Add to `_V2_COLUMN_MIGRATIONS["signal_records"]`:
```python
("severity",      "TEXT DEFAULT 'INFO'"),
("signal_type",   "TEXT"),
("blocking",      "INTEGER DEFAULT 0"),
("resolved_at",   "TEXT"),
("channel_id",    "TEXT"),
("source_system", "TEXT"),
```

**Tests required:**
- New: `tests/test_ois.py`:
  - `TestEmitSignal` — creates record with correct severity, type, blocking flag
  - `TestIsBlocked` — factory blocked when active blocking signal exists; unblocked when resolved
  - `TestIsChannelBlocked` — factory-scope blocking signal also blocks channel
  - `TestResolveSignal` — resolved signal no longer causes is_blocked=True
  - `TestPipelineOISIntegration` — mock is_factory_blocked=True → pipeline raises OISBlockedError before any agent runs
  - `TestBudgetExhaustionSignal` — finance agent emits budget_exhausted signal at ceiling

**Dependencies:** None (OIS is a foundation; notifications P2-02 will subscribe to it)

**Acceptance criteria:**
- `emit_signal("budget_exhausted", "CRITICAL", ..., blocking=True)` creates DB record
- `is_factory_blocked()` returns True after that signal
- `resolve_signal(signal_id, ...)` clears the block
- Running `run_pipeline.py` when factory is blocked raises `OISBlockedError` before EP is called
- `emit_signal("credential_failure", ...)` in `startup.py` triggers on provider connectivity failure
- `get_blocking_signals()` returns only active (unresolved) blocking signals

---

### P2-02 — Notification Delivery

**Component:** Monitoring / Notifications
**Effort:** 2 days

**Objective:**
Deliver OIS CRITICAL and WARNING signals to the operator via Telegram and/or email. Without this, the operator is blind to budget hits, run failures, and credential expiry. This is what makes the factory observable without requiring a human to poll the dashboard.

**Files to modify:**
- New: `hypogeum/notifications.py`
- `hypogeum/config.py` — add optional notification config fields:
  - `TELEGRAM_BOT_TOKEN: Optional[str]`
  - `TELEGRAM_CHAT_ID: Optional[str]`
  - `NOTIFY_EMAIL_FROM: Optional[str]`
  - `NOTIFY_EMAIL_TO: Optional[str]`
  - `NOTIFY_SMTP_HOST: Optional[str]`
  - `NOTIFY_SMTP_PORT: Optional[int]` (default 587)
  - `NOTIFY_SMTP_PASSWORD: Optional[str]`
  - `NOTIFY_SEVERITY_THRESHOLD: str` (default `"WARNING"` — send WARNING+CRITICAL; set "CRITICAL" for critical-only)
- `hypogeum/schema.sql` — add `notification_deliveries` table
- `hypogeum/ois.py` — after writing signal to DB, call `notifications.route_signal(signal)` if severity >= threshold

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id   TEXT NOT NULL PRIMARY KEY,
    signal_id     TEXT,
    channel_name  TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    subject       TEXT,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','sent','failed')),
    sent_at       TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL
);
```

**`hypogeum/notifications.py` public API:**
```python
route_signal(signal: SignalRecord, config: Config, db_path=None) -> list[str]
  # Delivers to all configured channels (telegram, email).
  # Returns list of delivery_ids.

send_telegram(message: str, bot_token: str, chat_id: str) -> bool
send_email(subject: str, body: str, smtp_config: dict) -> bool

format_signal_message(signal: SignalRecord) -> str
  # Produces human-readable message from signal fields.

get_delivery_history(limit=50, db_path=None) -> list[DeliveryRecord]
```

**Tests required:**
- New: `tests/test_notifications.py`:
  - `test_telegram_called_on_critical_signal` — mock httpx; verify correct API call formed
  - `test_email_called_on_warning_signal` — mock smtplib; verify send called
  - `test_no_delivery_if_no_config` — if TELEGRAM_BOT_TOKEN is None, no error; just skips
  - `test_delivery_recorded_in_db` — after send, `notification_deliveries` has 1 row
  - `test_failed_delivery_recorded_with_error`
  - `test_format_signal_message_includes_severity_type_message`

**Dependencies:** P2-01 (OIS provides SignalRecord type and calls route_signal)

**Acceptance criteria:**
- Setting `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` enables Telegram notifications
- CRITICAL signal → Telegram message delivered within 5 seconds of `emit_signal()`
- INFO signal → no delivery (below default threshold)
- `get_delivery_history()` returns records of all sent notifications
- Delivery failure does not raise; signal is still recorded; error is written to `notification_deliveries`
- `format_signal_message` output includes severity, signal_type, message, and timestamp

---

### P2-03 — Schedule Entity + Scheduler Process

**Component:** Scheduling
**Effort:** 5 days

**Objective:**
Implement the Schedule record and a polling scheduler process. A channel's `publishing_profile.schedule` dict already stores cadence/day_of_week/time_of_day. This task creates the Schedule entity that reads those preferences, computes `next_run_at`, and fires `run_pipeline.py` at the right time. This is what makes the factory autonomous.

**Files to modify:**
- New: `hypogeum/scheduler.py` — Schedule CRUD, `compute_next_run_at()`, `check_and_fire_schedules()`, conflict detection
- New: `run_scheduler.py` — entry point; polling loop; configurable interval
- `hypogeum/schema.sql` — add `schedules` table
- `hypogeum/run_registry.py` — add `schedule_id: Optional[str]` param to `register_run()`
- `hypogeum/db.py` — add to `_V2_COLUMN_MIGRATIONS["runs"]`: `("schedule_id", "TEXT")`
- `hypogeum/dashboard_api.py` — add `get_schedules()` to `FactoryState`

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id   TEXT NOT NULL PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    workflow      TEXT NOT NULL DEFAULT 'episode_production',
    cadence       TEXT NOT NULL CHECK(cadence IN ('daily','weekly','biweekly','monthly','manual')),
    day_of_week   TEXT CHECK(day_of_week IN
                       ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')),
    time_of_day   TEXT NOT NULL DEFAULT '18:00',
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','archived')),
    next_run_at   TEXT,
    last_run_id   TEXT,
    last_run_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

Add to `_V2_COLUMN_MIGRATIONS["runs"]`:
```python
("schedule_id", "TEXT"),
```

**`hypogeum/scheduler.py` public API:**
```python
create_schedule(channel_id, cadence, day_of_week=None, time_of_day="18:00",
                timezone="UTC", schedule_id=None, db_path=None) -> ScheduleRecord

get_schedule(schedule_id, db_path=None) -> Optional[ScheduleRecord]
list_schedules(channel_id=None, status=None, db_path=None) -> list[ScheduleRecord]
pause_schedule(schedule_id, db_path=None) -> bool
resume_schedule(schedule_id, db_path=None) -> bool
archive_schedule(schedule_id, db_path=None) -> bool

compute_next_run_at(cadence, day_of_week, time_of_day, timezone, from_dt=None) -> datetime
  # Returns next UTC datetime when this schedule should fire.
  # from_dt defaults to now(UTC).

check_and_fire_schedules(db_path=None, dry_run=False) -> list[str]
  # Iterates all active schedules where next_run_at <= now().
  # For each: check channel state (skip if not active), check for active run
  #   (skip with 'deferred' if channel has a running or queued run),
  #   call run_pipeline() with channel's topic from episode queue (or skip if no topic queued).
  # Updates next_run_at to the following slot after firing.
  # Returns list of run_ids fired.
  # dry_run=True logs what would fire without running.
```

**`run_scheduler.py` behavior:**
- Loads Config and db_path
- Polls `check_and_fire_schedules()` every 60 seconds
- Emits OIS INFO signal on each check (only if something fired)
- Handles `KeyboardInterrupt` cleanly
- Configurable: `SCHEDULER_POLL_INTERVAL_SECONDS` env var (default 60)

**Conflict detection in `check_and_fire_schedules()`:**
- Check `runs` table for rows with `channel_id = X` and `status = 'running'`
- If found: log "deferred — channel has active run"; update `next_run_at` to next cadence slot
- Do not attempt to start a second concurrent run for the same channel

**Tests required:**
- New: `tests/test_scheduler.py`:
  - `TestComputeNextRunAt` — weekly thursday 18:00; correct next datetime from various `from_dt` inputs
  - `test_next_run_at_skips_past_slot` — if thursday already passed this week, returns next thursday
  - `test_daily_schedule_fires_tomorrow_at_time`
  - `TestScheduleCRUD` — create, get, list, pause, resume, archive
  - `TestCheckAndFire` — mock run_pipeline; verify called when next_run_at <= now
  - `test_conflict_defers_not_fires` — existing running run → schedule deferred, next_run_at bumped
  - `test_paused_schedule_not_fired`
  - `test_next_run_at_updated_after_fire`
  - `test_dry_run_does_not_call_pipeline`

**Dependencies:** P1-01 (channel exists), P1-02 (pipeline accepts channel_id)

**Acceptance criteria:**
- `create_schedule("hypogeum", "weekly", "thursday", "18:00")` creates a schedule with correct `next_run_at`
- `python3 run_scheduler.py` starts, logs poll cycle, exits cleanly on Ctrl+C
- `check_and_fire_schedules()` fires when `next_run_at <= now()`
- After firing, `schedules.next_run_at` is updated to the following week's slot
- Conflict detection: channel with a running run → no second run started
- `schedules.last_run_id` updated after a successful fire
- `runs.schedule_id` populated on scheduler-triggered runs

---

### P2-04 — CALOG — Channel State Machine

**Component:** CALOG
**Effort:** 3 days

**Objective:**
Implement governed channel lifecycle with 7 states and 5 activation gates. Currently any code can write channel status directly. After this task, all state changes go through `calog.transition_channel_state()`, which enforces authority, runs gates, and creates an immutable audit record. The scheduler (P2-03) and pipeline (P1-02) will check channel state before acting.

**Files to modify:**
- New: `hypogeum/calog.py`
- `hypogeum/schema.sql` — add `channel_state_transitions` table
- `hypogeum/channel_config.py` — remove direct `UPDATE ... SET status = ?` writes; replace with `calog.transition_channel_state()` calls
- `hypogeum/pipeline.py` — add channel state check at run start: `if calog.get_channel_state(channel_id) != "active": raise ChannelNotActiveError`
- `hypogeum/scheduler.py` — replace raw status check with `calog.get_channel_state(channel_id) == "active"` in `check_and_fire_schedules()`

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS channel_state_transitions (
    transition_id        TEXT NOT NULL PRIMARY KEY,
    channel_id           TEXT NOT NULL,
    from_state           TEXT,
    to_state             TEXT NOT NULL,
    triggered_by         TEXT NOT NULL,
    trigger_source       TEXT NOT NULL CHECK(trigger_source IN
                             ('operator','system_ois','system_calog','system_scheduler')),
    gate_check_results   TEXT,
    reason               TEXT,
    transitioned_at      TEXT NOT NULL
);
```

**States implemented:** `draft` | `configured` | `active` | `paused` | `blocked` | `inactive` | `archived`

**V1 Activation Gates (5 of 10 — sufficient for V1 Operational):**
- Gate 1 — Profile completeness: `get_channel_profile(channel_id)` has non-null brand_profile_id, model_profile_id, publishing_profile_id
- Gate 4 — Credential health: at least one `secret_profile` with `status="active"` and `provider` in ["openrouter","muapi","elevenlabs"] exists in DB
- Gate 5 — Budget defined: `FINANCE_BUDGET_CEILING_USD > 0` in config OR channel has a non-zero budget ceiling configured
- Gate 9 — Authorization: requesting user holds `channel.lifecycle.manage` (via `auth.check_authorization()`)
- Gate 10 — No blocking OIS signals: `ois.is_channel_blocked(channel_id)` returns False

**`hypogeum/calog.py` public API:**
```python
get_channel_state(channel_id, db_path=None) -> str
  # Returns current state string. Returns "draft" if no record exists.

activate_channel(channel_id, requested_by_user_id, db_path=None) -> ActivationResult
  # dataclass: success: bool, new_state: str, gate_results: list[GateResult],
  #            transition_id: Optional[str]
  # Runs 5 gates; if all pass (ready or warning): sets status="active" and
  #   records ChannelStateTransition.
  # If any gate returns incomplete/blocked: returns success=False, no state change.

pause_channel(channel_id, requested_by_user_id, reason: str, db_path=None) -> str
  # Returns transition_id. Sets status="paused". No gate check.

resume_channel(channel_id, requested_by_user_id, db_path=None) -> ActivationResult
  # Runs Gates 4, 9, 10 only (light check). Sets status="active" if pass.

deactivate_channel(channel_id, requested_by_user_id, reason: str, db_path=None) -> str
  # Sets status="inactive".

block_channel(channel_id, signal_id: str, db_path=None) -> str
  # Called by OIS when a blocking signal is emitted for this channel.
  # Sets status="blocked". trigger_source="system_ois".

unblock_channel(channel_id, signal_id: str, db_path=None) -> bool
  # Called by OIS when blocking signal is resolved.
  # Sets pending_reactivation flag; does NOT auto-set to active.
  # Operator must call activate_channel() or resume_channel() after.

get_transition_history(channel_id, db_path=None) -> list[ChannelStateTransition]
run_activation_check(channel_id, db_path=None) -> ActivationResult
  # Runs gates without changing state. Safe to call from dashboard.
```

**OIS integration:** In P2-01's `emit_signal()`, add: if `signal.blocking=True` and `signal.channel_id is not None`, call `calog.block_channel(signal.channel_id, signal.signal_id)`.

**Tests required:**
- New: `tests/test_calog.py`:
  - `TestActivateChannel` — all gates pass → status becomes "active", transition recorded
  - `test_activation_fails_if_no_credentials` — Gate 4 blocks if no active secret profiles
  - `test_activation_fails_if_no_profile` — Gate 1 blocks if channel has no brand_profile_id
  - `test_blocking_signal_blocks_channel` — emit blocking OIS signal → channel transitions to "blocked"
  - `test_blocked_channel_cannot_start_run` — pipeline raises ChannelNotActiveError
  - `test_pause_records_transition`
  - `test_resume_runs_light_gates`
  - `test_transition_history_is_immutable` — cannot update a transition record after creation
  - `test_get_channel_state_returns_draft_if_no_record`

**Dependencies:** P1-01 (channel data), P1-03 (check_authorization), P1-04 (credential check in Gate 4), P2-01 (OIS blocking signals)

**Acceptance criteria:**
- `calog.activate_channel("hypogeum", owner_user_id)` — runs 5 gates, returns success=True, channel status becomes "active"
- `calog.get_channel_state("hypogeum")` returns "active"
- Emitting a budget_exhausted blocking signal → channel transitions to "blocked" automatically
- `run_pipeline.py --channel-id hypogeum` raises `ChannelNotActiveError` when channel is "configured" (not yet activated)
- `channel_state_transitions` has one row per state change with non-null `transitioned_at`
- `run_activation_check()` returns gate results without modifying channel state

---

## Phase 3 — Governance

### P3-01 — Audit Persistence

**Component:** Run Management / Governance
**Effort:** 2 days

**Objective:**
Wire `stage_executions` and `gate_verdicts` DB writes into the production pipeline. The tables and write functions already exist in `db.py` — they are just never called. After this task, every run produces a complete per-stage audit trail and every gate verdict is recorded. This makes run analytics, agent health scoring, and the agent director's data complete.

**Files to modify:**
- `hypogeum/pipeline.py` — wrap each agent call with `_record_stage_start()` and `_record_stage_complete()` (new private helpers); these call `db.insert_stage_execution()` and `db.update_stage_execution_complete()`. All DB writes wrapped in `try/except` — never crash the pipeline.
- `hypogeum/agents/qa_agent.py` — call `db.insert_gate_verdict()` with QA verdict after writing JSON
- `hypogeum/agents/compliance_agent.py` — call `db.insert_gate_verdict()` with compliance verdict
- `hypogeum/agents/brand_guardian_agent.py` — call `db.insert_gate_verdict()` with BGA pass/fail for Pass 1 and Pass 2
- `hypogeum/agents/finance_agent.py` — already calls `db.insert_budget_authorization()`; verify it is actually firing (it may be called but the table had 0 rows — investigate)
- `hypogeum/db.py` — no schema changes needed; functions already exist

**Database changes:**
- No new tables. `stage_executions` and `gate_verdicts` tables already exist.
- Verify `budget_authorizations` table write is actually executing; if not, fix the call site in `finance_agent.py`.

**Tests required:**
- `tests/test_pipeline.py` — add `TestAuditPersistence` class:
  - `test_stage_executions_written_for_each_agent` — after a mock pipeline run (all agents mocked), `stage_executions` has 9 rows (one per agent stage)
  - `test_gate_verdicts_written_for_qa_bga_compliance` — `gate_verdicts` has at least 3 rows
  - `test_stage_execution_has_duration_ms` — `completed_at` is not null after run
  - `test_db_write_failure_does_not_crash_pipeline` — mock db to raise; pipeline still completes
  - `test_budget_authorization_written` — `budget_authorizations` has 1 row after finance gate

**Dependencies:** P1-02 (channel_id available in pipeline for stage records)

**Acceptance criteria:**
- After a complete pipeline run (real or mocked): `stage_executions` has 9+ rows with `status="success"` for each agent
- `gate_verdicts` has at least 3 rows: one BGA, one QA, one compliance
- `budget_authorizations` has 1 row per run with `verdict` populated
- DB write failure in any stage record write does not raise or halt the pipeline
- `agent_auditor.audit_agents()` returns non-empty results (previously needed stage_executions data)

---

### P3-02 — Factory Entity System

**Component:** Multi-channel Management
**Effort:** 2 days

**Objective:**
Create the `factories` table and bootstrap the Hypogeum factory record. Currently `factory_id = "hypogeum"` is hardcoded in `dashboard_api.py`. After this task, factory name, branding, and settings come from DB. Required for policy cascade (P3-05), for white-label deployment, and for `dashboard_api.get_factory_state()` to return dynamic factory identity.

**Files to modify:**
- New: `hypogeum/factory_entity.py`
- `hypogeum/schema.sql` — add `factories` table
- `scripts/bootstrap_channel.py` — add factory bootstrap step (create factory record if not exists)
- `hypogeum/dashboard_api.py` — replace hardcoded `factory_id="hypogeum"` with `factory_entity.get_factory()` call; read `factory_name`, `dashboard_title` from DB record

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS factories (
    factory_id       TEXT NOT NULL PRIMARY KEY,
    factory_name     TEXT NOT NULL,
    factory_slug     TEXT NOT NULL UNIQUE,
    owner_id         TEXT REFERENCES users(user_id),
    default_language TEXT NOT NULL DEFAULT 'en',
    default_timezone TEXT NOT NULL DEFAULT 'UTC',
    status           TEXT NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active','maintenance','suspended')),
    dashboard_title  TEXT,
    support_email    TEXT,
    primary_color    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
```

**`hypogeum/factory_entity.py` public API:**
```python
create_factory(factory_id, factory_name, factory_slug, owner_id=None,
               dashboard_title=None, support_email=None, db_path=None) -> FactoryRecord

get_factory(factory_id=None, factory_slug=None, db_path=None) -> Optional[FactoryRecord]
  # If factory_id and factory_slug both None: returns the single installed factory.

update_factory(factory_id, db_path=None, **kwargs) -> bool

get_current_factory_id(db_path=None) -> str
  # Returns factory_id of the single installed factory.
  # Raises FactoryNotFoundError if no factory record exists yet.
```

**Tests required:**
- New: `tests/test_factory_entity.py`:
  - `test_create_and_get_factory`
  - `test_get_current_factory_id_returns_single_record`
  - `test_factory_name_in_dashboard_api` — `get_factory_state().factory_name` reads from DB
  - `test_bootstrap_creates_factory_record`
  - `test_update_factory_changes_field`

**Dependencies:** P1-03 (owner_id FK to users table)

**Acceptance criteria:**
- `python3 scripts/bootstrap_channel.py` creates factory record: factory_id="hypogeum", factory_name="Hypogeum Media Factory"
- `factory_entity.get_factory("hypogeum")` returns correct record
- `dashboard_api.get_factory_state().factory_name` returns "Hypogeum Media Factory" (from DB, not hardcoded)
- `update_factory("hypogeum", dashboard_title="Hypogeum OS")` persists the change

---

### P3-03 — RLG — Run State Machine

**Component:** Run Management
**Effort:** 5 days

**Objective:**
Implement the formal run state machine with immutable transition records. Currently `run_registry.py` sets status directly with no authority checks. After this task, all run state changes go through `run_registry.transition_run_state()`, which validates the transition, records it, and emits an OIS signal. The state machine enables pause/resume, the `awaiting_review` gate, and the `awaiting_publish_approval` gate required for the approval system (P3-04).

**Files to modify:**
- `hypogeum/run_registry.py` — add `transition_run_state()`, `get_run_state()`, extended run states; keep existing `register_run()`, `complete_run()`, `fail_run()` as façade wrappers over `transition_run_state()`
- `hypogeum/schema.sql` — add `run_state_transitions` table
- `hypogeum/db.py` — add to `_V2_COLUMN_MIGRATIONS["runs"]`: `("queued_at", "TEXT")`, `("paused_at", "TEXT")`
- `hypogeum/pipeline.py` — replace direct status management with RLG calls where needed; add `awaiting_review` state hook after compliance gate (sets run to `awaiting_publish_approval` when `require_human_review=False`)

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS run_state_transitions (
    transition_id   TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,
    trigger_source  TEXT NOT NULL,
    reason          TEXT,
    metadata        TEXT,
    transitioned_at TEXT NOT NULL
);
```

Add to `_V2_COLUMN_MIGRATIONS["runs"]`:
```python
("queued_at",  "TEXT"),
("paused_at",  "TEXT"),
```

**States and valid transitions:**

| From | To | Authority |
|---|---|---|
| None | queued | system:pipeline |
| queued | running | system:pipeline |
| running | paused | operator |
| paused | running | operator |
| running | awaiting_review | system:pipeline (when require_human_review=True) |
| awaiting_review | awaiting_publish_approval | system:approvals (approval granted) |
| awaiting_publish_approval | publishing | system:pipeline |
| running | awaiting_publish_approval | system:pipeline (when require_human_review=False) |
| awaiting_publish_approval | publishing | system:pipeline |
| publishing | completed | system:pipeline |
| running | failed | system:pipeline |
| * | blocked | system:ois |

**`run_registry.py` additions:**
```python
transition_run_state(run_id, to_state, triggered_by, trigger_source,
                      reason=None, metadata=None, db_path=None) -> str  # transition_id
  # Validates transition is valid from current state.
  # Raises InvalidRunTransitionError if not.
  # Writes RunStateTransition record (immutable).
  # Updates runs.status.
  # Emits OIS signal (run_completed, run_failed, etc.) via ois.emit_signal().

get_run_state(run_id, db_path=None) -> str
get_run_transition_history(run_id, db_path=None) -> list[dict]

pause_run(run_id, operator_user_id, reason, db_path=None) -> str
resume_run(run_id, operator_user_id, db_path=None) -> str
```

**Tests required:**
- `tests/test_run_registry.py` — extend with `TestStateMachine`:
  - `test_valid_transition_recorded` — queued → running → completed produces 3 rows in run_state_transitions
  - `test_invalid_transition_raises` — completed → running raises InvalidRunTransitionError
  - `test_transition_record_is_immutable` — cannot UPDATE a run_state_transitions row
  - `test_pause_sets_paused_at`
  - `test_existing_register_run_still_works` — backwards-compat test on register_run()
  - `test_transition_emits_ois_signal` — mock ois.emit_signal; verify called on completed transition

**Dependencies:** P2-01 (OIS for signal emission), P1-03 (user IDs for triggered_by)

**Acceptance criteria:**
- After a complete pipeline run: `run_state_transitions` has rows for queued → running → awaiting_publish_approval → publishing → completed
- `transition_run_state(run_id, "running", "operator", ...)` from `paused` succeeds; from `completed` raises
- `get_run_transition_history(run_id)` returns ordered list of all transitions
- `pause_run(run_id, user_id, "investigating")` transitions to paused; sets `paused_at`
- Existing `complete_run()` and `fail_run()` functions still work (they call `transition_run_state` internally)

---

### P3-04 — Approval System

**Component:** Approval System
**Effort:** 5 days

**Objective:**
Implement the approval workflow that governs human-review gates and model/budget change requests. After this task, when a channel has `require_human_review=True`, the pipeline enters `awaiting_review` state, creates an approval request, and sends a notification to the operator. The run does not advance to publishing until the operator approves via CLI or dashboard API. This is the Factory OS governance gate for production.

**Files to modify:**
- New: `hypogeum/approvals.py`
- `hypogeum/schema.sql` — add `approvals` table
- `hypogeum/pipeline.py` — after compliance gate, if `compliance_report.get("human_review_required")`: call `approvals.create_approval_request(run_id, "human_review", ...)` and transition run to `awaiting_review` via RLG; block further execution until `approvals.is_approved(run_id)`
- `hypogeum/dashboard_api.py` — add `get_pending_approvals()` to `FactoryState`
- `hypogeum/notifications.py` — add `notify_pending_approval(approval)` call from `approvals.create_approval_request()`
- New: `scripts/approve_run.py` — CLI tool: `python3 scripts/approve_run.py --approval-id XXX --user-id YYY [--notes "..."]`

**Database changes:**

Add to `hypogeum/schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    episode_id      TEXT NOT NULL,
    approval_type   TEXT NOT NULL CHECK(approval_type IN
                        ('human_review','publish_approval','model_swap','budget_increase')),
    requested_by    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','expired')),
    requested_at    TEXT NOT NULL,
    decided_by      TEXT,
    decided_at      TEXT,
    notes           TEXT,
    evidence        TEXT
);
```

**`hypogeum/approvals.py` public API:**
```python
create_approval_request(run_id, episode_id, approval_type, requested_by,
                         evidence=None, db_path=None) -> str  # approval_id

approve(approval_id, decided_by_user_id, notes=None, db_path=None) -> bool
  # Sets status="approved", decided_by, decided_at.
  # Emits OIS signal "approval_granted".
  # Calls run_registry.transition_run_state(run_id, "awaiting_publish_approval", ...)

reject(approval_id, decided_by_user_id, notes=None, db_path=None) -> bool
  # Sets status="rejected".
  # Emits OIS signal "approval_rejected".
  # Calls run_registry.transition_run_state(run_id, "failed", ...)

get_approval(approval_id, db_path=None) -> Optional[ApprovalRecord]
list_pending_approvals(run_id=None, db_path=None) -> list[ApprovalRecord]
is_approved(run_id, approval_type, db_path=None) -> bool

expire_stale_approvals(older_than_hours=72, db_path=None) -> int  # count expired
```

**Pipeline integration in `pipeline.py`:**
```python
# After compliance gate, before publishing:
if compliance_report.get("human_review_required"):
    approval_id = approvals.create_approval_request(run_id, episode_id, "human_review", "system:pipeline")
    # Notification fires automatically inside create_approval_request
    rlg.transition_run_state(run_id, "awaiting_review", "system:pipeline", ...)
    log.info("pipeline_awaiting_human_review", approval_id=approval_id)
    return PipelineResult(status="awaiting_review", ...)  # not "completed"
```

Note: The approval flow is asynchronous. The `run_pipeline.py` invocation returns immediately with status "awaiting_review". The operator runs `approve_run.py`, which advances the run to publishing. A separate `run_pipeline.py --resume-from-publishing --run-id XXX` completes the pipeline.

**Tests required:**
- New: `tests/test_approvals.py`:
  - `TestCreateApprovalRequest` — creates DB record with status="pending"
  - `TestApprove` — sets status, decided_by, decided_at; transitions run to awaiting_publish_approval
  - `TestReject` — sets status="rejected"; transitions run to failed
  - `test_approve_requires_valid_user_role` — viewer cannot approve; admin can
  - `test_is_approved_returns_false_for_pending`
  - `test_notification_sent_on_create` — mock notifications; verify called
  - `test_expire_stale_approvals_marks_old_pending_as_expired`
  - `test_pipeline_returns_awaiting_review_when_human_review_required`

**Dependencies:** P1-03 (user IDs for decided_by), P2-02 (notification on approval request), P3-03 (RLG state transition to awaiting_review)

**Acceptance criteria:**
- Channel with `require_human_review=True` in `approval_rules` → pipeline returns `status="awaiting_review"` after compliance gate
- Telegram notification sent to operator with approval_id and episode title
- `python3 scripts/approve_run.py --approval-id XXX --user-id owner_id` sets approval to "approved"
- Run advances to `awaiting_publish_approval` after approval
- Viewer user attempting to approve gets `PermissionDeniedError`
- `list_pending_approvals()` returns all runs waiting for human decision
- Rejected approval transitions run to "failed" with reason recorded

---

### P3-05 — Policy Cascade

**Component:** Multi-channel Management / Factory Governance
**Effort:** 2 days

**Objective:**
Implement factory → channel policy bounds. Factory-level rules (monthly budget ceiling, model blocklist) take precedence over channel settings. A channel cannot be created or updated with settings that violate factory policy. The finance gate reads the effective policy rather than raw channel config. This completes the multi-channel governance model.

**Files to modify:**
- New: `hypogeum/policy_cascade.py`
- `hypogeum/factory_entity.py` — add `factory_policies` JSON column to factory record; add `get_factory_policies()` function
- `hypogeum/schema.sql` — add `factory_policies TEXT` to `factories` table (via `_V2_COLUMN_MIGRATIONS`)
- `hypogeum/db.py` — add to `_V2_COLUMN_MIGRATIONS["factories"]`: `("factory_policies", "TEXT")`
- `hypogeum/channel_config.py` — call `policy_cascade.validate_channel_config(channel_config, factory_id)` in `create_channel_profile()` and `update_channel_profile()`; raise `PolicyViolationError` on violation
- `hypogeum/agents/finance_agent.py` — call `policy_cascade.get_effective_budget_ceiling(channel_id)` instead of reading `FINANCE_BUDGET_CEILING_USD` directly from Config

**`hypogeum/policy_cascade.py` public API:**
```python
get_effective_policy(channel_id, db_path=None) -> EffectivePolicy
  # dataclass with:
  #   budget_ceiling_usd: float  (min of factory monthly / run count, channel ceiling)
  #   allowed_model_slugs: Optional[list[str]]  (None = all allowed)
  #   blocked_model_slugs: list[str]
  #   require_human_review_for: list[str]  (niche categories)
  #   auto_publish_permitted: bool
  # Cascade: factory overrides channel where factory is more restrictive.

get_effective_budget_ceiling(channel_id, db_path=None) -> float
  # Returns the effective per-episode budget ceiling after cascade.

validate_channel_config(channel_config, factory_id, db_path=None) -> list[str]
  # Returns list of violation strings. Empty list = valid.
  # Violations: model in blocked_model_slugs, budget ceiling > factory ceiling, etc.
```

**Factory policies stored as JSON in `factories.factory_policies`:**
```json
{
  "monthly_ceiling_usd": 300,
  "blocked_model_slugs": [],
  "allowed_model_slugs": null,
  "require_human_review_for": [],
  "budget_increase_requires_approval": true,
  "model_swap_requires_approval": true
}
```

**Tests required:**
- New: `tests/test_policy_cascade.py`:
  - `test_factory_monthly_ceiling_bounds_channel_ceiling` — factory ceiling $300 / 4 episodes = $75 effective max; channel ceiling $100 raises PolicyViolationError
  - `test_blocked_model_slug_rejected_on_channel_create`
  - `test_get_effective_budget_ceiling_uses_factory_when_lower`
  - `test_no_factory_policy_uses_channel_config_directly`
  - `test_validate_channel_config_returns_empty_for_valid_config`

**Dependencies:** P3-02 (factory entity must exist with policy field), P1-01 (channel must exist)

**Acceptance criteria:**
- Setting `factory_policies.monthly_ceiling_usd = 50` and trying to create a channel with `episode_ceiling_usd = 60` raises `PolicyViolationError`
- `get_effective_policy("hypogeum")` returns an `EffectivePolicy` with correct merged values
- Finance gate uses `get_effective_budget_ceiling()` instead of reading raw `.env` value
- Adding a model slug to `factory_policies.blocked_model_slugs` prevents that model from being set on any channel

---

## Implementation Order Summary

```
TASK        COMPONENT                    DAYS  DEPENDS ON
─────────────────────────────────────────────────────────
P1-01       Channel bootstrap             1    —
P1-02       Pipeline → channel            2    P1-01
P1-03       User / role system            3    —
P1-04       Credential manager            2    —
─────────────────────────────────────────────────────────
P2-01       OIS signal routing            5    —
P2-02       Notification delivery         2    P2-01
P2-03       Scheduler                     5    P1-01, P1-02
P2-04       CALOG                         3    P1-01, P1-03, P1-04, P2-01
─────────────────────────────────────────────────────────
P3-01       Audit persistence             2    P1-02
P3-02       Factory entity system         2    P1-03
P3-03       RLG — run state machine       5    P2-01, P1-03
P3-04       Approval system               5    P1-03, P2-02, P3-03
P3-05       Policy cascade                2    P3-02, P1-01
─────────────────────────────────────────────────────────
TOTAL                                    39
```

## Total Estimated Effort

| Phase | Tasks | Days |
|---|---|---|
| Phase 1 — Foundation | 4 tasks | 8 days |
| Phase 2 — Operations | 4 tasks | 15 days |
| Phase 3 — Governance | 5 tasks | 16 days |
| **Total** | **13 tasks** | **39 working days** |

Calendar time: approximately 8 weeks at full focus, or 10–12 weeks at partial focus.

## First Task

**Start immediately on P1-01.**

P1-01 is 1 day, has no dependencies, produces directly observable output (`list_channel_profiles()` returns 1 result), and unblocks the two largest Phase 1 deliverables (P1-02 and P2-03). It is also the only task whose output is visible in the current dashboard: after P1-01, `get_channel_summaries()` returns the Hypogeum channel instead of the "No channels configured" notification.
