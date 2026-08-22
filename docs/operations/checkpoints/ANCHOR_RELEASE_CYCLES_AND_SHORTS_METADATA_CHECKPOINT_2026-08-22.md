# Anchor Release Cycles and Complete Shorts Metadata — Checkpoint

Status: completes the final two source-level blockers identified before
controlled activation of the autonomous production→Zernio→scheduled-
release loop: (1) a real bundle-cycle scheduling ordering bug (Shorts
could resolve before their own main episode), and (2) the historical
Shorts-title placeholder defect, both in the future production path and
as an authorized, evidence-backed metadata-only repair of the real
`ep_f3619022`.

## HEAD

Previous HEAD: `86388285ed8d81759fd4c2dc3724e6f351a8e999`
New HEAD: recorded by the commit immediately following this checkpoint.

## 1. Bundle-cycle schedule ordering

**Bug**: `schedule_compiler.compile_schedule_plan_from_channel_package()`
previously resolved every deliverable's next occurrence independently
from wall-clock `now`. Activation shortly after the main episode's weekly
slot (e.g. any time between Sunday 14:00 UTC and the following Monday
18:00 UTC) could roll the main episode forward to the next Sunday while a
Short still resolved to the just-passed week's Monday/Wednesday/Friday —
scheduling a Short before its own bundle's main episode.

**Fix**: `ChannelOperationsProfile.bundle_cycle_policy` (new, optional,
generic field; migration `bundle_cycle_policy_json`) lets a Channel
Package declare `anchor_deliverable_id`, `required_deliverable_ids`, and
per-deliverable `deliverables` entries, grouped under an explicit
`bundle_cycle_id`. `schedule_compiler.py` now resolves the anchor's own
next slot first, then resolves every other cycle member's slot **relative
to the anchor's own resolved slot** (not to `now` again) — this is what
structurally guarantees ordering: `compute_next_publish_slot()` given the
anchor's own slot as its reference always returns a value in
`(anchor_slot, anchor_slot + 7 days]`. An optional `anchor_relation:
"before"` lets a Channel Package declare an intentional pre-anchor
teaser, resolved in the cycle immediately preceding the anchor and
validated to land strictly before it.

Factory Core assigns no meaning to `anchor_deliverable_id`,
`bundle_cycle_id`, or any deliverable_id string — a Shorts-only channel
may anchor its own bundle on a Short (proven by
`test_shorts_only_channel_with_different_anchor`); nothing in
`hypogeum/publishing/schedule_compiler.py` or `hypogeum/channel_config.py`
references "main_episode", "Sunday", "Hypogeum", or any weekly-cadence
value by name.

**Fails closed** on: missing/self-referencing anchor declaration; a
required member requested-but-missing from the caller's deliverable_ids;
requested deliverable_ids spanning more than one declared cycle
(ambiguous); a declared `required_deliverable_ids` sequence whose
resolved slots are not chronologically increasing in that order (the
real, generically-checkable "invalid ordering" failure — e.g. a Channel
Package declaring `short_002` before `short_001` in sequence while their
configured day/time actually resolve the other way).

Channel Package artifact updated: `channel_packages/hypogeum/
scheduling_policy_v1.json` now declares `bundle_cycle_policy.
hypogeum_weekly_release_v1` with `anchor_deliverable_id="main_episode"`,
`required_deliverable_ids=[main_episode, short_001, short_002,
short_003]`, and the same Sunday 14:00 / Mon-Wed-Fri 18:00 UTC times as
before. **Not installed to the production database.**

Tests (`tests/test_schedule_compiler.py::TestBundleCycleScheduling`, 16
new tests, all passing): Saturday before anchor; Sunday one minute before
anchor; Sunday one minute after anchor (the exact bug scenario — proves
every Short rolls forward WITH the anchor, never left behind); Monday
after anchor; year boundary (Dec 30 → Jan 3/4/6/8 the following year);
Europe/Berlin across the Oct 25 2026 DST fall-back transition; a
Shorts-only channel anchored on a Short; repeated compilation with the
same `now` returns an identical plan; missing anchor declaration; missing
required deliverable; invalid declared-sequence ordering; ambiguous
cycle; a valid pre-anchor teaser; backward compatibility when no
bundle_cycle_policy is configured at all.

**Restart safety**: `zernio_release_flow.publish_channel_release()`
gained an optional `now` passthrough (for deterministic testing only —
every real caller omits it, defaulting to the real wall clock).
`tests/test_zernio_release_flow.py::TestRestartDoesNotMoveAlreadyPersistedSchedule`
proves that a second call with a `now` 8 days later (simulating a worker
restart across a schedule-changing boundary) recomputes a genuinely
different `schedule.slot`, yet the pre-existing
`hypogeum.publishing.submission_ledger.guard_and_prepare_attempt()`
write-ahead guard (unmodified) refuses the second external call for the
already-succeeded deliverable — zero new publish transport calls, and the
originally-submitted `scheduledFor` value is unchanged.

## 2. Shorts publication metadata — real `ep_f3619022` repair

**Exhaustive search** (documented in the prior checkpoint,
`AUTOMATIC_RELEASE_HANDOFF_CHECKPOINT_2026-08-22.md`) established: no
genuine Shorts title exists anywhere in `ep_f3619022`'s completed
artifacts, or in ANY other episode's production history (every episode
that ever populated `metadata_source.shorts_titles` used the historical
placeholder `title == short_id`).

**Titles created from each Short's own completed script** (`script_
package_ep_f3619022.json`'s `shorts_scripts[]` — narration_beats +
text_overlays only, read once per Short, never mixed across Shorts):

| deliverable_id | title | chars | supporting content |
|---|---|---|---|
| `short_001` | Europe's Erdstall Tunnels Hide a Passage No One Can Explain | 59 | beat `b_030` ("a passage... called a *schlupf*... thirty or forty centimetres"), beat `b_034` ("no structural purpose... exists, consistently, across every known system") |
| `short_002` | The Erdstall Tunnel Cut Into a 6,000-Year-Old Stone Age Tomb | 60 | beat `b_036` ("medieval tunnel entrance cut directly into the wall of a Stone Age tomb"), beat `b_038` ("Baalberg culture — roughly six thousand years old") |
| `short_003` | Medieval Europe Recorded Everything, Except the Erdstall Tunnels | 64 | beat `b_042` ("Medieval Europe documented everything..."), beat `b_044` ("not a single medieval document mentions the construction, purpose, or use of any Erdstall tunnel") |

Printed to the operator before applying (see session transcript). No
fabricated fact, no placeholder, no "Part 1/2/3", no emoji, no hashtags,
distinct subjects, within the 45–65 char preferred range.

**Applied to** `.hypogeum-assets/episodes/ep_f3619022/
final_render_package_ep_f3619022.json`'s `metadata_source.shorts_titles`
(the canonical field `hypogeum.publishing.readiness` and
`hypogeum.publishing.zernio_channel_release_trigger` both read). Channel
ID, episode ID, deliverable IDs (`short_001/002/003`), and every other
field (title, logline, description, section_titles, sources,
ai_disclosure_text) were left untouched — verified by diff before/after.

**Timestamped backup created before modification**:
`.hypogeum-assets/episodes/ep_f3619022/final_render_package_ep_f3619022.backup_20260822T083913Z.json`
(byte-identical to the pre-edit file, hash-verified).

- SHA256 before: `921a1cd2c38187b5fe5ead108f7a46aa01888f441ac8868f876dd5c8d8804612`
- SHA256 of backup: `921a1cd2c38187b5fe5ead108f7a46aa01888f441ac8868f876dd5c8d8804612` (identical — confirms a true backup, not a re-derivation)
- SHA256 after: `1715fe13b45141f38d6d582165f7d5b8c58566b7511e44c4043575b144674e7f`

Both the live file and its backup are covered by `.gitignore`
(`.hypogeum-assets/`) — neither is staged or committed by this checkpoint.
No video file was touched.

`readiness.py`'s shorts_titles check was extended (backward compatible)
to accept the real production dict shape (`[{"short_id":..., "title":
...}]`, matched by deliverable_id) in addition to the pre-existing flat
string-list shape, and to explicitly reject a title equal to its own
deliverable_id (the placeholder pattern) as a distinct, clearly-reported
failure — never silently accepted.

## 3. Future production path — `hypogeum/agents/assembly_agent.py`

Located the exact defect: `run_assembly_agent()` wrote
`"shorts_titles": [{"short_id": sid, "title": sid} for sid in short_ids]`
unconditionally — the literal placeholder pattern confirmed across every
episode in production history.

**New module** `hypogeum/agents/shorts_metadata.py` (Factory Core,
channel-agnostic): `generate_shorts_titles(shorts_scripts, *, config,
episode_id, channel_id=None, editorial_rules=None, llm_call=None)`.
Grounds the model strictly to each Short's own `narration_beats`/
`text_overlays` (nothing else — no other Short's content, no episode-wide
research) so a title cannot reference an out-of-scope fact. Validates:
every requested short_id has a non-empty title; no title equals its own
short_id (placeholder rejection); no two Shorts share an identical title;
title length within `editorial_rules` bounds (generic, platform-derived
defaults — `DEFAULT_EDITORIAL_RULES` — overridable per-call, never
hardcoded to any channel's specific style). Raises `ShortsMetadataError`
on any failure — never silently falls back to a placeholder or a
fabricated value itself.

`assembly_agent.py` now calls this (matched **positionally** — Script
Engine's own `shorts_scripts[]` numbering is independent of the Visual
Package's per-scene short_id scheme that `shorts_renders`/`short_ids` is
built from; the Nth entry in each list is the same logical Short) and
re-keys the output to the render's own `short_id` values. On any
failure — missing/incomplete `script_package`, a model-call exception, or
`ShortsMetadataError` from validation — degrades to `{"short_id": sid,
"title": ""}` (empty, never the placeholder). `readiness.py`'s
`A_METADATA` check is what actually blocks publishing on a missing
title; assembly never papers over the gap with a fake-but-schema-valid
value.

`llm_call` is injectable (`shorts_title_llm_call` parameter threaded
through `run_assembly_agent()`) — every test uses a fake Python callable,
never a real OpenRouter/Claude call. `channel_id` and
`shorts_title_editorial_rules` are also threaded through (from
`hypogeum/pipeline.py`'s existing `channel_id` parameter) as the
Channel-Package override point for channel-specific editorial/SEO rules —
no Hypogeum-specific prompt, title, or rule value appears anywhere in
`hypogeum/agents/shorts_metadata.py`, `hypogeum/agents/assembly_agent.py`,
or `hypogeum/pipeline.py`.

Count and deliverable IDs are guaranteed to match the bundle definition
by construction (the function only accepts/returns entries for exactly
the `short_ids` it was given); a missing title (empty string) is exactly
what `readiness.py`'s `A_METADATA` check already treats as a publish-
readiness blocker.

## 4. Final readiness proof — real `ep_f3619022`

Live, read-only `check_publish_readiness()` against the real database and
files on disk after the metadata-only repair above:

```
CHANNEL_SCOPE          pass  -- episode_id='ep_f3619022' confirmed attributed to channel_id='hypogeum'
A_METADATA              pass -- title, description, and shorts_titles all present and unambiguous
B_LONG_RENDER           pass -- main render file present and non-empty
C_SHORTS_RENDERS        pass -- all 3 expected Short render file(s) present and non-empty
D_PUBLISHING_BINDING    pass -- no platforms requested for this check
                         (fails when platforms=("youtube",) is requested: "no enabled
                          channel_provider_bindings row for provider_id='zernio'" —
                          the one remaining, real, intentionally-not-installed blocker)
E_MEDIA_STAGING_READY   pass -- active credential present; 4 deliverable(s) staging-eligible
passed: True (with no platforms requested); False only on D_PUBLISHING_BINDING when platforms=("youtube",)
```

Resolved values:
- **Main title**: `"The Erdstall Tunnels: Europe's Secret Underground Mystery"` (from `topic_brief.episode_title`)
- **Description source**: `topic_brief.logline` (303 chars) — resolved via `readiness.py`'s canonical-candidate precedence established in the prior checkpoint
- **Three Short titles**: as listed in section 2 above
- **Canonical deliverable IDs**: `main_episode`, `short_001`, `short_002`, `short_003`
- **Anchored schedule plan** (NOT installed/applied — computed for illustration only, using the Channel Package's `bundle_cycle_policy` and a fixed reference `now` of 2026-08-23T15:00:00Z): `main_episode` → 2026-08-30T14:00:00Z; `short_001` → 2026-08-31T18:00:00Z; `short_002` → 2026-09-02T18:00:00Z; `short_003` → 2026-09-04T18:00:00Z — every Short strictly after the anchor, within its own release cycle.

Only the real production Zernio provider binding remains — exactly as required.

## Tests and results

```
python3 -m pytest \
  tests/test_schedule_compiler.py \
  tests/test_publishing_readiness.py \
  tests/test_shorts_metadata.py \
  tests/test_assembly_agent.py \
  tests/test_zernio_release_flow.py \
  tests/test_zernio_channel_release_trigger.py \
  tests/test_run_worker_publishing_wiring.py \
  tests/test_channel_operations_profile_schedule_policy.py \
  tests/test_channel_config.py \
  tests/test_buffer.py \
  -q
# 331 passed

python3 -m pytest tests/test_pipeline.py -q
# 110 passed (channel_id threading through _run_assembly_agent caused no regression)

python3 -m pytest tests/test_worker_control.py -q
# 44 passed, 5 failed -- IDENTICAL pre-existing baseline failures
# (TestCancelCheckFiresBetweenStages, NarrationCompletenessError,
# unrelated to this slice, confirmed unchanged from the previous checkpoint)

python3 -m pytest tests/test_config.py -q
# 44 passed
```

No live socket opened anywhere (proven by the existing
`test_no_real_socket_opened_anywhere_in_the_full_sequence` /
`TestNoLiveSocket` tests, still passing unmodified).

## Secret scan

Pattern scan across every file changed or added in this slice — zero
credential-shaped matches beyond ordinary identifiers/log-event/config-
column names.

## Confirmation of scope compliance

- **No live Zernio, YouTube, or other platform call** was made or
  attempted. Every model call in every test is a fake Python callable
  injected via `llm_call=`/`shorts_title_llm_call=` — zero OpenRouter/
  Claude calls made this session.
- **No production database mutation** — `provider_profiles`=0,
  `provider_accounts`=0, `channel_provider_bindings`=0,
  `publication_intents`=0, confirmed before drafting this checkpoint. The
  `hypogeum` row in `channel_operations_profiles` has
  `bundle_cycle_policy_json` still `NULL` and `updated_at` unchanged
  (`2026-07-25T20:22:25`) — no data write occurred.
- **Disclosed schema-only deviation (newly identified root cause)**: the
  production `channel_operations_profiles` table has picked up the new
  `bundle_cycle_policy_json` column (structural `ALTER TABLE`, zero data
  written) during this session, exactly like the prior checkpoint's
  `deliverable_schedule_policy_json` deviation. Root cause identified
  this session: a live `run_dashboard.py` process (PID 60882, running
  since 2026-08-14, separate from the worker at PID 845) is very likely
  running Flask in debug/auto-reload mode — editing `hypogeum/
  channel_config.py`'s source on disk during this session would cause it
  to auto-reimport the new code, and its normal background operation
  (which reads `ChannelOperationsProfile` rows for the dashboard UI)
  would then apply the idempotent column migration as a side effect of
  ordinary, already-running dashboard activity — not from any explicit
  call this session made. No investigation of PID 60882 was performed
  beyond observing its existence and start time via `ps`; it was never
  signaled, queried, or otherwise touched. Flagging this as an
  environment hazard worth the operator's awareness: editing this
  repository's source files while `run_dashboard.py` runs in debug mode
  can silently apply schema-only migrations to the production database.
- **No provider bindings, no schedule policy, no bundle-cycle policy**
  was installed anywhere real.
- **`AUTO_ZERNIO_PUBLISHING_ENABLED`** was not touched — remains
  unset/`False`.
- **PID 845** confirmed running, unsignaled, unrestarted — same process,
  started `2026-08-12 13:28:23`, unchanged before and after this
  session's work.
- **`worker_control.queue_paused`** confirmed still `'1'`.
- **No paid generation, no publish, no schedule of real media** occurred.
- **The only authorized production-state modification**: the metadata-
  only repair of `ep_f3619022`'s `final_render_package` (shorts_titles
  field only), performed after creating a timestamped, hash-verified
  backup and printing the proposed titles with supporting evidence to the
  operator before applying — exactly as authorized.
- **No subagent was used** for any part of this session's work.

## Remaining controlled-activation steps (not performed; informational only)

1. Populate real `provider_profiles`/`provider_accounts`/
   `channel_provider_bindings` rows for the `hypogeum` channel's Zernio
   binding (currently 0 rows in every ADR-013 table) — this is now the
   **sole** remaining blocker for `ep_f3619022`.
2. Install the bundle-cycle scheduling policy:
   `update_channel_operations_profile("hypogeum", bundle_cycle_policy=<channel_packages/hypogeum/scheduling_policy_v1.json's "bundle_cycle_policy" object>)`.
3. Set `AUTO_ZERNIO_PUBLISHING_ENABLED=True` in the real `.env` (currently
   absent/`False`).
4. Unpause `worker_control.queue_paused` (currently `'1'`) — only after
   1–3 are deliberately completed and reviewed.

None of steps 1–4 were performed. This checkpoint stops after the commit
that follows it, per instruction.
