# Zernio Operational-Enablement Readiness Audit — 2026-08-21

Status: read-only audit artifact. No production code, database, Channel
Package, queue, scheduler, worker control, or credential was modified,
mutated, or exposed as part of producing this document. No live Zernio
call of any kind was made (no GET was needed — see §4). No episode,
publication, intent, provider profile/account/binding, or media was
created, staged, uploaded, published, or scheduled.

Audited HEAD: `6d14d2a986fbcae4789ccfac625c26db6c022968` (branch `main`,
clean except the two permanently-protected untracked files). PID 845
(`run_worker.py`) confirmed running and unmodified throughout.

---

## 1. Evidence sources

Repository artifacts (read in full or targeted sections): `DECISION_LOG.md`
ADR-013; `docs/operations/audits/ZERNIO_CONTRACT_AUDIT_2026-08-21.md`;
`docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md`;
`docs/operations/checkpoints/ADR013_ITEM4_ZERNIO_ADAPTER_CHECKPOINT_2026-08-21.md`;
`docs/operations/checkpoints/ADR013_ITEM4_ZERNIO_ADAPTER_COMPLETION_2026-08-21.md`;
`hypogeum/publishing/{provider,provider_registry,provider_repository,
submission_ledger,zernio,staging,metadata}.py`; `hypogeum/channel_config.py`;
`hypogeum/agents/publishing_agent.py`; `hypogeum/agents/production_output_gate.py`;
`hypogeum/config.py`; `hypogeum/humanizer.py`. Live, read-only `sqlite3`
queries against `.hypogeum-assets/hypogeum.db` and local filesystem
inspection under `.hypogeum-assets/episodes/ep_f3619022/`. No web/API
research was performed in this task (unlike the prior contract-audit
task) — see §4 for why.

## 2. Architecture / source / database / fake / live state

**Architecture (ADR-013)**: provider ≠ platform; Zernio behind the
generic `PublishingProvider` contract; Credential Manager owns
credentials; Channel Package owns provider preference; operational
database owns concrete account/binding/mapping identity; durable
write-ahead submission boundary; no automatic real-row creation by any
adapter.

**Source state**: `hypogeum/publishing/zernio.py` is
`IMPLEMENTATION COMPLETE — NOT OPERATIONALLY ENABLED` per the prior
checkpoint — response parsing, media (`MediaReference`, HTTPS-only,
no upload), scheduling (`scheduled_for`/`timezone_name`/`publish_now`/
`is_draft`, fail-closed validation), and error classification (`200`/`201`/
`409`/`429`/`403`×2/`400`/`5xx`/undocumented) are all implemented against
the verified OpenAPI contract and covered by 73 fake-transport tests (846
passed across the full regression suite as of the last commit). Not
re-verified by re-running tests in this audit — out of scope for a
read-only task; the checkpoint's own evidence is treated as current since
no code changed since `6d14d2a`.

**Database schema state**: no numeric schema-version pragma is used in
this project (`PRAGMA user_version` = 0, unused); schema truth is
`hypogeum/schema.sql` at the audited HEAD plus the idempotent additive
migrations in `hypogeum/db.py::_V2_COLUMN_MIGRATIONS`/`_V3_.../_V4_...`.
83 tables exist in the live database. All ADR-013 items 1–3 tables
(`publication_intents`, `publishing_attempts`, `publishing_receipts`,
`provider_profiles`, `provider_accounts`, `channel_provider_bindings`,
`provider_content_mappings`) are present and match the schema this HEAD
defines.

**Production database row counts** (read-only `SELECT`, verified at audit
time):

| Table | Count |
|---|---|
| `provider_profiles` | 0 |
| `provider_accounts` | 0 |
| `channel_provider_bindings` | 0 |
| `publication_intents` | 0 |
| `publishing_attempts` | 0 |
| `publishing_receipts` | 0 |
| `provider_content_mappings` | 0 |

**Zero real Zernio operational rows exist anywhere.** The durable
submission ledger (items 1–2) has never recorded a single attempt for
*any* provider — `ep_f3619022`'s existing YouTube publish (see §5) predates
that boundary's production wiring and was never retroactively recorded
into it; this is expected, not an anomaly, and is not itself a blocker
(the ledger only needs to govern *future* attempts).

`secret_profiles`: one `provider='zernio', status='active', scope='factory',
label='Hypogeum Zernio unified publishing'` row exists (pre-existing,
predates all Zernio implementation work; value never read in this task).

**Queue / scheduler / worker-control state** (read-only):
- `run_queue`: 71 rows total by status — `completed: 9`, `failed: 40`,
  `cancelled: 22`. No `pending`/`running` rows at audit time.
- `schedules`: 2 rows — one with `channel_id` empty, `status='active'`;
  one `channel_id='hypogeum', status='paused'`.
- `worker_control`: `worker_status='idle'`, `queue_paused=1`,
  `active_queue_id=''` (empty).

None of this state was interpreted further or acted upon — reading it was
solely to confirm no live queue/worker activity is in flight that this
audit's own read-only queries could be mistaken for interfering with.

**Fake-transport / live-read-only / live-write state**: unchanged from the
completion checkpoint — 73 fake-transport tests, zero live read-only
calls performed by the adapter itself, zero live writes ever. This audit
task itself performed zero live calls of any kind (no GET was needed; see
§4).

## 3. `ep_f3619022` deliverable inventory (Phase B)

**Canonical record**: `episodes` table — `episode_id='ep_f3619022'`,
`status='complete'`, `topic='The Erdstall Tunnels: Europe's Most Baffling
Medieval Underground Network'`, `run_id='04c8d61d-9434-4b44-92f4-a3f9ac114bc6'`
(latest of 15 runs against this episode_id; that run's own `status='complete'`,
completed `2026-08-05T17:41:51Z`). The `episodes.title` column itself is
empty (topic column carries the human-readable title instead — a
pre-existing convention of this table, not something this audit
introduces).

**Previous publication records** (`publishing_outcomes`, 4 rows, all
`platform='youtube'`):

| deliverable_id | publishing_status | platform_video_id | note |
|---|---|---|---|
| `main_episode` | `pending` | `JyQo1DE9SgQ` | target 2026-08-20T14:00Z (18:00 Asia/Dubai) |
| `short_short_001` | `pending` | `ItY0SXWUNeA` | target 2026-08-21T14:00Z |
| `short_short_002` | `pending` | `CHPhES0lALA` | target 2026-08-22T14:00Z |
| `short_short_003` | `pending` | `tnb4cyWcDZU` | target 2026-08-23T14:00Z |

All four carry an identical annotation: `"timezone_corrected_2026-08-17:
publishAt corrected from 18:00 UTC to <date>T14:00:00Z (= <date>T18:00:00+04:00,
18:00 Asia/Dubai per Channel Operations Profile timezone=Asia/Dubai). No
re-upload, no media/editorial change."` — i.e. an operator/process corrected
a timezone bug in the already-uploaded YouTube scheduled-publish times on
2026-08-17, explicitly without re-uploading media. `publish_calendar` (4
matching rows) confirms `upload_status='uploaded', publish_status='scheduled',
platform_supports_scheduled_publish=1, publish_method='api_scheduled'`.

**Whether deletion from platforms is recorded**: no deletion is recorded
anywhere in `publishing_outcomes`, `publish_calendar`, or local files —
`publishing_status` is `pending`/`scheduled` throughout, never `failed`
or a deletion-tagged state. **This audit found no local evidence of the
prior-turn scenario referencing an already-deleted external publication**
for this episode; whatever state that referred to is not reflected in the
current database. **Whether these four YouTube video IDs are currently
live, still pending their scheduled time, or something else cannot be
determined without a live read against YouTube — not performed in this
task (out of scope; this task's live-GET allowance was Zernio-only, per
§4, and even that was not exercised).** `main_episode`'s target time
(2026-08-20) has already passed as of the audited date (2026-08-21);
`short_short_001`'s target (2026-08-21) is today; the other two are
still in the future. The database's own `publishing_status` was not
observed to update past `pending`/`scheduled` for any of the four rows,
which is either accurate (native YouTube scheduled-publish has not yet
been reconciled back into Cortex's tables) or stale (reconciliation
never ran) — this audit cannot distinguish the two without a live read.

**Long-form deliverable**: `main_ep_f3619022.mp4`, 330,361,153 bytes
(~315 MB), exists on disk, mtime 2026-08-17 18:33 (matches the
timezone-correction event date exactly). `episode_render` metadata:
1920×1080, 30fps, 744.06s duration (~12m24s), 29 scenes, 2 sections.
Captions: `captions/main.srt` exists.

**Short deliverables** — three physical files exist, using a
`short_<N>_ep_f3619022.mp4` (0-indexed) naming convention that does
**not** match the `final_render_package_ep_f3619022.json`'s own
`shorts_renders[]` array, which instead lists three placeholder entries
(`sh_000_placeholder`, `sh_001_placeholder`, `sh_002_placeholder`, each
with empty `video_asset_id` and `duration_seconds: 0.0`) — **this is a
confirmed metadata/reality mismatch inside the render package JSON**, not
a missing-file problem (the physical files and their captions/thumbnails
under a `short_00N` naming convention are present and real):

| Physical file | Size | Caption | Thumbnail |
|---|---|---|---|
| `short_0_ep_f3619022.mp4` (↔ `short_001`) | 54,890,407 B | `captions/sh_short_001.srt` | `thumbnails/short_001.jpg` |
| `short_1_ep_f3619022.mp4` (↔ `short_002`) | 46,905,952 B | `captions/sh_short_002.srt` | `thumbnails/short_002.jpg` |
| `short_2_ep_f3619022.mp4` (↔ `short_003`) | 48,091,032 B | `captions/sh_short_003.srt` | `thumbnails/short_003.jpg` |

All mtimes 2026-08-17. Resolution/duration for the individual shorts was
not independently re-derived in this pass (would require a media-probe
tool call, not a pure file-existence read); the render package declares
the shorts' target resolution as `1080x1920` generically but the
per-short `shorts_renders[]` entries carrying real duration values were
not found (see mismatch above) — **duration/aspect-ratio for the three
real short files is therefore not confirmed from existing records without
either regenerating metadata or probing the files directly, neither of
which this audit performed.**

**Thumbnails**: `thumbnails/main.jpg` (202,920 B), `short_001.jpg`
(184,779 B), `short_002.jpg` (254,558 B), `short_003.jpg` (176,539 B) all
exist, mtimes 2026-08-08 (predate the timezone-correction event — thumbnails
were not touched by that operation, consistent with its own "no
media/editorial change" note).

**Titles, descriptions, hashtags, platform metadata — genuine
discrepancy found**: the currently-saved
`final_render_package_ep_f3619022.json`'s `metadata_source` object
(mtime 2026-08-17 18:33, same as the correction event) has **empty
strings** for `episode_title`, `description`, and an **empty array**
for `shorts_titles`. `hypogeum.publishing.metadata.get_episode_title()`
applied to this exact file today would return `""` (confirmed by reading
`humanize_field()`'s empty-input passthrough — no fallback exists in that
function). A **different** title exists in `topic_brief_ep_f3619022.json`:
`"The Erdstall Tunnels: Europe's Secret Underground Mystery"` — itself
distinct from the `episodes.topic` column's wording ("...Europe's Most
Baffling Medieval Underground Network"). **Two non-identical candidate
titles exist across records, and the field the publishing code actually
reads today is empty.** Whether the *original* 2026-08-01 YouTube upload
used a real title (almost certainly yes — YouTube requires one) that was
later blanked by the 2026-08-17 edit, or whether `metadata_source` was
always populated from a different, no-longer-present source, could not be
determined from local files available to this audit. **This is a human
decision item, not something this audit resolves or guesses.**
Descriptions, hashtags, and per-platform metadata overrides were not
separately located beyond this same empty `metadata_source` object.

**Whether any deliverable is missing or stale**: no deliverable file is
*missing*. Two staleness/consistency issues are confirmed: (1) the render
package's `shorts_renders[]` metadata does not match the real physical
short files (naming/duration/asset-ID mismatch); (2) the render package's
`metadata_source` title/description fields are empty against a
publish-time reality that must have had real values, and two different
candidate titles exist elsewhere with no local record of which was
actually used.

**Whether a deliberate new publication intent is required**: the ADR-013
items 1–3 submission ledger has zero rows for this episode (see §2) —
there is no existing `publication_intent` to reuse or conflict with for a
*Zernio* attempt specifically (Zernio has never attempted this episode).
A first Zernio attempt for `ep_f3619022` would, by the existing, unmodified
`guard_and_prepare_attempt()` logic, open a brand-new intent automatically
(no prior intent exists to find) — **no explicit `open_new_publication_intent()`
call is needed for a first Zernio attempt on this episode**; one would
only become necessary for a *second* deliberate Zernio republish after a
first Zernio attempt already succeeded. This audit did not open, read for
mutation, or otherwise touch any intent — the "zero rows" fact above is
sufficient to answer the question without doing so.

**Whether the episode can be republished without regenerating or paying
for any content**: yes, structurally — all four media files, thumbnails,
and captions already exist locally with no missing assets, so a Zernio
publish could in principle reuse them exactly as generated. Whether it
*should* be attempted before the title/metadata discrepancy above is
resolved is a separate, open question this audit flags rather than
answers.

## 4. Public media URL readiness (Phase C)

**Cortex does not currently produce a publicly reachable HTTPS URL for
this episode's assets.** Traced the exact runtime path:

- `hypogeum/agents/publishing_agent.py` STEP 6 (YouTube), STEP 8
  (Facebook), and STEP 10 (TikTok) all call their direct providers with a
  **local** `video_path: Path` — these platforms' native APIs accept
  direct multipart upload, so no public URL is ever produced or needed
  for them today.
- STEP 9 (Instagram) is the **only** existing path that produces a public
  URL, via `hypogeum.publishing.staging.get_staging_provider()` →
  `UrlStagingProvider.stage_file(local_path, filename) -> str` (HTTP POST
  to `META_STAGING_BASE_URL + "/upload"`, expects a JSON `{"url": ...}` or
  plain-text URL response).
- **`META_STAGING_BASE_URL` is present as a key in `.env` but its value is
  empty** — confirmed via `Config().META_STAGING_BASE_URL` evaluating to
  `None`/falsy at audit time (value itself never printed; only its
  emptiness was checked). `get_staging_provider()` therefore returns
  `NullStagingProvider()`, whose `stage_file()` raises
  `StagingNotConfiguredError` immediately, unconditionally.

**Conclusion: no staging mechanism in this repository is currently
usable, for any provider, including the existing Instagram path.** Zernio
needs this same capability but for *every* platform (not just Instagram),
so this gap is strictly more load-bearing for Zernio adoption than it has
ever been for the existing direct adapters.

Other Phase C questions, answered from the verified contract audit
(`ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md §4`) since no staging
path exists to test empirically:
- **URL expiry**: `POST /v1/media/presign`'s `uploadUrl` (the PUT target)
  expires in exactly 1 hour (`expiresIn: 3600`, documented). The resulting
  `publicUrl` itself carries no documented expiry — reasonable to expect
  it is durable once the upload completes, but this is not independently
  confirmed by a live test (none performed).
- **Signed query parameters**: presigned `uploadUrl` almost certainly
  carries a signature in its query string (standard cloud-storage
  presigned-PUT convention); this adapter's own `_redact_url()` already
  strips query strings from any URL it might echo into an exception
  message, so this is handled defensively regardless of which staging
  path is chosen.
- **Fetch window sufficiency for scheduled publishing**: cannot be
  confirmed without a live test; if `publicUrl` is genuinely durable (not
  expiring), this is a non-issue for any schedule length. If it does
  expire on some undocumented timeline, a schedule far in the future
  could fail at actual-publish-time — an unresolved risk for scheduled
  (not immediate) posts specifically.
- **Is `/v1/media/presign` required?** No — any already-public HTTPS URL
  works (§3 of the contract audit); Zernio's presign endpoint is one
  option among several, not a hard requirement.
- **Would presigned upload require POST/PUT calls?** Yes — `POST
  /v1/media/presign` to obtain `{uploadUrl, publicUrl}`, then a `PUT` of
  the raw file to `uploadUrl`. Neither was called in this audit (prohibited).
- **Must upload happen immediately before post creation?** Not necessarily
  — since `publicUrl` appears durable, upload could happen well ahead of
  the scheduled post-creation call, decoupling media staging from
  scheduling entirely (a favorable property for the fixed-slot goal).
- **Cleanup/retention/retry/idempotency**: undocumented for
  `/v1/media/presign` specifically (no delete-media endpoint was found in
  the OpenAPI spec read during the contract audit); `UrlStagingProvider`
  does define `delete_staged_file()` in its own Protocol, but Zernio's
  presign path has no equivalent documented cleanup call. Retry safety
  for a presign+PUT sequence was not analyzed in this pass.
- **Factory Core vs. provider adapter**: a generic, reusable public-URL
  staging capability belongs in Factory Core (extending the existing
  `hypogeum.publishing.staging.StagingProvider` Protocol pattern, which
  was explicitly designed — per its own docstring — for exactly this:
  "cloud bucket (S3/R2) can be added later as a deployment-time
  configuration change, not a code change"). It should **not** be
  Zernio-specific, since Instagram already needs the identical capability
  and any future URL-requiring provider would too.

**Classification: BLOCKED ON MEDIA DELIVERY.** No safe public-media path
currently exists for any platform through this adapter. The smallest
generic component required is one of:
1. **Configure the existing generic path** — set a real, working
   `META_STAGING_BASE_URL` (a deployment/ops decision, not a code
   change — the code already exists and is provider-neutral).
2. **Implement Zernio's presigned-upload flow** as a small, new,
   Zernio-owned helper (`POST /v1/media/presign` + `PUT`) — self-contained,
   no new infrastructure, but Zernio-specific rather than reusable by
   Instagram's existing path.

Neither was implemented or exercised in this audit. This is the most
load-bearing blocker for goal 1 (republish `ep_f3619022`).

## 5. Proposed operational identity mapping (Phase D)

**No live GET was performed.** The existing sanitized evidence
(`ZERNIO_CONTRACT_AUDIT_2026-08-21.md §2`, gathered 2026-08-21, the same
audited date) already confirms: exactly one Zernio profile, `isDefault:
true`, owning four accounts — one each for `facebook`, `instagram`,
`tiktok`, `youtube` — all `isActive: true, needsReconnection: false`, each
exposing a Zernio-internal `_id`, a native-platform identity via
`platformUserId`+`username`, and all four sharing one `profileId._id`.
This evidence is same-day and was explicitly gathered for this exact
purpose; re-fetching it would add a live call with no new information, so
none was made — consistent with "use existing artifacts first."

**PROPOSED — NOT APPLIED.** Actual external ID values are intentionally
NOT reproduced here (the source audit is itself sanitized and never
recorded them, and this task performed no fresh call to obtain them) —
each row below is a structural plan; the real IDs must be filled in by
whoever performs the actual, separately-authorized setup task, using a
fresh read at that time if the ones referenced here are no longer current:

```
PROPOSED — NOT APPLIED

provider_profiles (1 row):
  provider_id           = "zernio"
  external_profile_id   = <Zernio profile _id, from GET /v1/profiles>
  display_label         = "Hypogeum" (or the profile's own name field)
  status                = "active"

provider_accounts (4 rows, one per platform):
  provider_id                 = "zernio"                (all 4)
  provider_profile_id         = <the provider_profiles row above>  (all 4)
  platform                    = "youtube" | "tiktok" | "instagram" | "facebook"
  external_account_id         = <Zernio SocialAccount._id for that platform>
  native_platform_account_id  = <platformUserId for that platform>
  display_label                = <username, sanitized>
  status                       = "active"
  needs_reconnection            = false

channel_provider_bindings (1 row to start -- publishing capability,
  one platform at a time is recommended for the first controlled test,
  e.g. youtube only; the remaining 3 platforms follow the same shape):
  channel_id             = "hypogeum"
  capability              = "publishing"
  platform                 = "youtube"            (start with one)
  provider_id                = "zernio"
  provider_profile_id          = <the provider_profiles row above>
  provider_account_id            = <the matching provider_accounts row>
  logical_alias                   = "default"
  enabled                           = true
```

Credential identity remains entirely separate: the existing
`secret_profiles` row (`provider='zernio', scope='factory'`) is not
referenced by any of the rows above and requires no change — Credential
Manager ownership stays exactly where ADR-013 Decision 3 places it.

## 6. Fixed publishing-calendar findings (Phase E)

**Authoritative configured schedule found**:
`channel_operations_profiles` (channel_id='hypogeum'): `timezone =
"Asia/Dubai"`, `production_cadence_json = {"videos_per_week": 1}`,
`publishing_schedule_json = {"day_of_week": "thursday", "time_of_day":
"18:00"}`. **This is a real, configured, authoritative rule — not an
example** — for the long-form episode's weekly release slot only.

**Historical observed times** (not themselves authoritative, but real
data): `ep_f3619022`'s actual `publish_calendar`/`publishing_outcomes`
rows show `main_episode` targeted 2026-08-20T18:00 Asia/Dubai (a
Thursday — consistent with the configured rule above), then
`short_001`/`short_002`/`short_003` targeted on the three immediately
following days (Fri/Sat/Sun), each at the same 18:00 Asia/Dubai local
time. **No document, config field, or database column was found stating
this one-short-per-day-after-main pattern as a general rule** — it is
only observed for this one episode's manually-corrected schedule.

Per-item findings against the task's explicit checklist:

| Item | Status |
|---|---|
| Weekday (long-form) | **Defined** — Thursday, `channel_operations_profiles.publishing_schedule_json` |
| Local time (long-form) | **Defined** — 18:00 |
| Timezone | **Defined** — Asia/Dubai (no DST in this zone — moot) |
| Offset from long-form release (Shorts) | **Not defined as a rule** — only observed once (1/2/3 days after) |
| Platform differences (TikTok/IG/FB timing) | **Not found anywhere** — no per-platform schedule policy located |
| Recurrence interval | **Partially defined** — weekly cadence (`videos_per_week: 1`) for long-form; none for Shorts |
| Minimum lead time | **Not found** — no policy field or documented constant |
| DST behavior | **Not applicable** (Asia/Dubai has no DST) but no explicit handling code was located either way |
| Missed-slot behavior | **Not found** |
| Duplicate-slot prevention | **Not found as a scheduling rule** (the existing `publishing_outcomes` UNIQUE constraint prevents a duplicate *local record*, not a duplicate *external post* — a separate concern already addressed generically by the ADR-013 items 1–3 submission ledger, not by any calendar-specific logic) |
| One-episode-ahead assignment rule | **Out of scope for this task** per its own instructions (goal 3/4) — not audited |

**This audit does not assume Shorts follow the observed 1/2/3-day-after
pattern as policy.** Per the task's explicit instruction, no schedule was
invented. **Required human decisions before any fixed-calendar-driven
Zernio scheduling can proceed**: (1) confirm or set an explicit Shorts
release-offset/cadence rule (a policy, not a one-off), (2) decide whether
TikTok/Instagram/Facebook should mirror the YouTube weekday/time or use
platform-specific slots, (3) set a minimum lead time appropriate for
Zernio's scheduling contract, (4) decide missed-slot and duplicate-slot
handling policy (even though local dedup is already structurally safe via
the submission ledger, the *editorial* question of "what happens if a
slot is missed" is still undecided).

## 7. Controlled live-write readiness gate table (Phase F)

| Gate | Status | Evidence |
|---|---|---|
| Adapter complete | **PASS** | `ADR013_ITEM4_ZERNIO_ADAPTER_COMPLETION_2026-08-21.md`; 73 fake-transport tests, 846-test regression, all passing at `6d14d2a` |
| Credential valid | **PASS** (structurally) | `secret_profiles` row exists, `status='active'`; value not re-verified live in this task (not needed — status alone answers "does an active credential row exist") |
| Profile verified | **PASS** (as of 2026-08-21 same-day evidence) | `ZERNIO_CONTRACT_AUDIT_2026-08-21.md` — 1 profile, `isDefault: true` |
| Four accounts verified | **PASS** (as of 2026-08-21 same-day evidence) | Same audit — 4 accounts, all `isActive: true`, `needsReconnection: false` |
| Operational mappings installed | **FAIL** | `provider_profiles`/`provider_accounts`/`channel_provider_bindings` all 0 rows (§2) |
| Media URL available | **FAIL** | `META_STAGING_BASE_URL` unconfigured; no working staging path exists for any platform (§4) |
| Media lifetime sufficient | **UNKNOWN** | No live test performed; `publicUrl` durability not independently confirmed (§4) |
| Episode assets complete | **PASS**, with a caveat | All 4 media files + 4 thumbnails + 4 caption files exist on disk (§3); the render-package's own `shorts_renders[]` metadata is internally inconsistent with the real files, though the real files themselves are present |
| Metadata complete | **FAIL** | `metadata_source.episode_title`/`.description`/`.shorts_titles` are empty in the current render package; two non-identical candidate titles exist elsewhere with no record of which was actually used (§3) |
| Fixed schedule approved | **REQUIRES HUMAN APPROVAL** | Long-form weekday/time/timezone is authoritatively configured; Shorts offset/cadence, platform differences, minimum lead time, and missed/duplicate-slot policy are all undecided (§6) |
| New publication intent authorized | **PASS** (not required yet) | Zero existing ledger rows for this episode under any provider — a first Zernio attempt opens a fresh intent automatically; no explicit action needed unless a *second* Zernio attempt is later required (§3) |
| Publishing gate authorized | **UNKNOWN** | `hypogeum.agents.production_output_gate` exists and presumably passed at original publish time (real YouTube IDs exist); not re-run in this read-only audit |
| Idempotency/reconciliation ready | **PASS** | ADR-013 items 1–2's durable submission ledger + item 4's `x-request-id` reuse are implemented and tested; genuinely new for Zernio specifically, unexercised live |
| Rollback/cancellation path ready | **PASS** (contract-verified, not implemented in Cortex) | `DELETE /v1/posts/{postId}` (draft/scheduled only) and `PATCH /v1/posts/{postId}` are documented and verified (`ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md §5`); no Cortex-side code calls either yet — a future orchestrator would need to |
| Monitoring/logging ready | **UNKNOWN** | No dedicated Zernio-specific dashboard/alerting surface was located in this pass; the existing `hypogeum.logger` structured-JSONL convention is available but this adapter itself performs no logging (deliberately, matching the no-credential-exposure design) — operational visibility beyond the DB tables themselves was not audited in depth |

**Overall: not ready for any controlled live write.** Two `FAIL` gates
(operational mappings, media URL) block goal 1 outright; one further
`FAIL` (metadata) would produce an incorrect/incomplete publish even if
the first two were resolved; the schedule gate requires human decisions
before goal 2 (fixed scheduling) can proceed at all.

## 8. Blockers, in dependency order

1. **Media delivery** (§4) — no working public-URL path exists for any
   platform. Nothing downstream can be tested end-to-end without this.
2. **Metadata correction** (§3) — the render package's title/description
   are empty; must be resolved (which title is correct, repopulate
   `metadata_source`) before any real content is sent, independent of #1.
3. **Operational mapping installation** (§5) — a separate, explicit,
   human-approved task must create the real `provider_profiles`/
   `provider_accounts`/`channel_provider_bindings` rows (plan proposed in
   §5, not applied). Can proceed in parallel with #1/#2 since it depends
   only on already-confirmed identity evidence.
4. **Fixed-schedule decisions** (§6) — Shorts offset/cadence, per-platform
   timing, minimum lead time, and missed/duplicate-slot policy must be
   explicitly decided (not inferred) before goal 2 can be attempted, even
   once #1–#3 are resolved.
5. **A single, separately-authorized, isolated live-write smoke test**
   (ADR-013 Decision 7 item 10) — only after 1–4, and only for one
   platform/one deliverable at a time, per the project's own established
   caution pattern.

## 9. Smallest safe next step

Resolve blocker #1 (media delivery) as its own, separate, explicit task —
specifically: a human decision between the two options identified in §4
(configure `META_STAGING_BASE_URL` against a real staging service, vs.
implement Zernio's `POST /v1/media/presign` + `PUT` flow as a new,
narrowly-scoped adapter capability) — since every other blocker (#2–#4)
can be worked in parallel but none of them make goal 1 achievable without
this one. This audit does not authorize or begin that work.

---

## 10. Status

**BLOCKED ON MEDIA DELIVERY, BLOCKED ON EPISODE ASSETS (metadata),
BLOCKED ON SCHEDULE DECISION**

Not `READY FOR CONTROLLED SETUP — NOT LIVE WRITE`: three independent
blockers exist (media URL delivery, episode metadata correctness, and
fixed-schedule human decisions for Shorts), each of which alone would
prevent a correct controlled write even if the others were resolved.
Operational identity (profile/accounts) is well-evidenced and ready for
the mapping-installation step in parallel, but installing mappings alone
does not clear any of the three blockers above.
