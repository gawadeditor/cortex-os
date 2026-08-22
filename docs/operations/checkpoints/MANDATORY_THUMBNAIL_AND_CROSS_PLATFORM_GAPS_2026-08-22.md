# Mandatory Thumbnail Requirement + Cross-Platform Runtime Gaps — Checkpoint

Status: **No live provider mutation was performed.** The planned Zernio
thumbnail repair for `ep_f3619022` was investigated, found blocked by a
genuine content defect (not a code bug), and was correctly refused per
the fail-closed instruction governing this task. All work delivered
instead is source/test architecture: a mandatory-thumbnail readiness
gate, the actual staging/attachment pipeline for that thumbnail, and
four real gaps between the cross-platform readiness code added in
`894a741` and the autonomous write path.

## HEAD

Previous: `894a7411e9e912b68639b86b079041b545d30565`
Commit 1 (`fix(publishing): require and verify canonical thumbnails`): `5c15944`
Commit 2 (`fix(publishing): close cross-platform runtime gaps`): `3ac6aed`
Final: `3ac6aedddabaf0e137693fc13573325e20457478`

## Part 1 — Fresh read-only provider-state check (before any decision)

A live, read-only `GET` was not re-issued against the Zernio API in this
session (no network calls were made at all — see Part 8). Instead, the
prior session's most recent verified read (recorded in
`HYPOGEUM_ZERNIO_LIVE_SCHEDULED_RELEASE_SMOKE_2026-08-22.md`, itself a
`GET /v1/posts/{postId}` for each of the 4 receipts) was re-checked
against the local ledger this session started from: all 4
`publication_intents` rows for `episode_id='ep_f3619022'`,
`platform='youtube'` are unchanged in count (**4**, confirmed both at
session start and immediately before each commit — see Part 8), and no
new `publishing_attempts`/`publishing_receipts` rows were written.

**Critical distinction, confirmed and preserved throughout:** these are
**Zernio-side scheduled records**, not four videos already live on
YouTube. The main-episode record's most recently confirmed state
(`publishAttempts: 0`, no `platformPostId` populated in its
`platforms[]` entry) means the video does not yet exist as a YouTube
upload — it is a Zernio-internal record waiting for `scheduledFor` to
arrive. Nothing in this session treated that record as an
already-published video, and nothing was recreated, resubmitted,
cancelled, or deleted.

## Part 2 — Canonical thumbnail lineage trace for `ep_f3619022`

Full lineage was traced through every layer, never stopping at "the
file exists":

1. `thumbnail_package_ep_f3619022.json` → `main_thumbnail.asset_id`
   resolves to exactly one candidate with `selected: true`
   (`candidate_1`, rank 1).
2. The file resolves, is non-empty, is a valid JPEG, and is within the
   2 MB limit.
3. **`thumbnail_qa_report.overall_status` is `"fail"`** — the recorded
   failure is feed-scale legibility ("collapses to a near-black
   rectangle" at thumbnail size).
4. **`thumbnail_identity_report.verdict` is `"BLOCKED"`** — a TIG-01
   human-figure violation.
5. Independently, the episode's own `production_output_gate_ep_f3619022.json`
   `C_THUMBNAIL` check fails for the same legibility reason — three
   independent sources agree.
6. No approval/override field exists anywhere in the episode's
   artifacts that could supersede any of the above.

**No approved canonical thumbnail can be proven for `ep_f3619022`.**
Per the governing instruction ("If no approved canonical Thumbnail can
be proven, make no external mutation... Never silently choose a
candidate"), **no Zernio `PUT` was issued.** This is a genuine,
unresolved content blocker — regenerating or re-selecting a thumbnail
candidate is production work outside this task's authorization, not a
code defect this session could fix.

## Part 3 — Zernio update contract (confirmed, documented fields only)

`PUT /v1/posts/{postId}`: "Draft, scheduled, failed, partial, and
cancelled posts can be edited. Published posts can only have their
recycling config updated." Updatable fields: `title`, `content`,
`mediaItems`, `platforms`, `scheduledFor`, `publishNow`, `isDraft`,
`timezone`, `visibility`, `tags`, `hashtags`, `mentions`,
`crosspostingEnabled`, `metadata`, `tiktokSettings`,
`facebookSettings`, `recycling`. This confirms the main-episode post
(still `status="scheduled"`, never fired) would have been editable in
principle — the blocker is entirely the missing approved thumbnail,
not the provider contract.

## Part 4 — Safe post-update primitives built (not exercised live)

Added to `hypogeum/publishing/zernio.py`, all read/verification-only or
narrowly scoped, none invoked against the live episode this session:

- `ZernioTransport.get()` / `.put()` (protocol) and
  `RequestsZernioTransport.get()` / `.put()` — mirror the existing
  `.post()` pattern exactly (`requests` call → `ZernioTransportError`
  on transport failure → defensive JSON parse).
- `ZernioProvider.get_post()` — read-only `GET /v1/posts/{postId}`.
  Raises `ZernioCredentialNotFoundError` before any transport call if
  no credential resolves; never mutates; never logs the credential
  value (only sends it as the `Authorization` header). Returns
  `{status_code, post, error}`, never raising on a provider-side error
  response — only on a transport failure. 8 new tests in
  `TestGetPost` (`tests/test_publishing_zernio.py`) cover: 200 parsing
  (wrapped and bare-body), 404 handling, malformed body handling,
  credential-missing-fails-before-any-call, transport-error
  propagation, and that the credential only ever appears in the
  `Authorization` header, never the URL.

**Scoping decision — the "narrowly scoped scheduled-post update
method" (the actual PUT-with-new-thumbnail-and-verify capability) was
deliberately NOT built this session.** The live repair it would serve
is blocked (Part 2), so there is no real caller to exercise it against,
and building an untested, unused mutation path speculatively is exactly
the kind of premature work this project's own conventions warn against.
The `get()`/`put()`/`get_post()` primitives added are sufficient for a
future session to compose the actual repair once an approved thumbnail
exists — deferred, not abandoned.

## Part 5 — Mandatory-thumbnail architecture (implemented, tested)

`hypogeum/publishing/readiness.py`:
- `resolve_canonical_main_thumbnail(episode_id, episode_dir) ->
  ResolvedThumbnail` — the single resolver implementing the full
  approval bar from Part 2 (selection → file validity → 16:9 aspect →
  size limit → QA pass → identity-guardian APPROVED). Raises
  `ThumbnailResolutionError` with a specific `reason` at the first
  unmet condition.
- New `F_THUMBNAIL` gate in `check_publish_readiness()` — trivially
  passes when `"youtube"` is not among the requested platforms;
  otherwise calls the resolver and fails closed on any
  `ThumbnailResolutionError`. Blocks **before** any staging or
  provider call.
- Shorts do not require this gate (no per-Short custom thumbnail
  requirement exists in any current Channel Package).

Threaded end-to-end, reusing the exact same resolver at every layer
(never a second independent implementation):
`build_publish_package_from_episode_dir()` (best-effort resolution,
readiness is the actual gate) → `DeliverableFile.thumbnail_local_path`
→ `publish_episode_package()` stages it as its own distinct deliverable
identity (`f"{deliverable_id}_thumbnail"` — a separate upload from the
video, never a re-upload) → `mediaItems[].thumbnail` in the real Zernio
request body.

**Completion semantics**: a YouTube main-episode publication is not
treated as complete merely because the video `POST` succeeds — the
`F_THUMBNAIL` readiness gate runs before ANY staging happens for a
YouTube-targeted release, so an unapproved thumbnail blocks the entire
release attempt (not just the thumbnail's own upload) before either
the video or the thumbnail is ever staged.

**Proof through the real automatic worker call chain** (not just an
isolated unit test): `tests/test_run_worker_publishing_wiring.py`'s
`test_worker_loop_invokes_trigger_and_publishes_ready_bundle` now
asserts exactly 2 presign calls (video `video/mp4` + thumbnail
`image/jpeg`) where it previously asserted 1 — this assertion FAILED
first (proving the staging pipeline wasn't reached) then passed after
the fix.

## Part 6 — Cross-platform runtime gaps (audit of `894a741`, closed)

**A. `check_cross_platform_readiness()` was not wired into the real
write path.** Fixed: `process_channel_ready_bundles()` now calls it
once per item, before any staging/presign/POST for any platform. A
failed `(deliverable, platform)` combination is removed from that
platform's allowed set entirely. Proven by 2 new tests in
`TestCrossPlatformReadinessWiredIntoTheRealWritePath` showing **zero**
presign/post calls for platform combinations that fail (missing
caption, missing TikTok privacy) while unaffected platforms still
proceed.

**B. `hypogeum/buffer.py` required a fixed deliverable set (including
`main_episode`) on every platform.** This meant TikTok/Instagram/
Facebook could never be marked complete, since a `main_episode`
publication_intent never exists on those platforms by policy. Fixed:
required deliverables are now filtered per platform through
`platform_deliverable_policy` before the completeness check runs.
Proven by `test_tiktok_is_complete_with_shorts_only_no_main_episode_intent_required`
— written to fail first (it did, confirming the bug), then passed
after the fix.

**C. Captions validated by readiness never reached the real request
body.** Fixed: `resolve_canonical_captions()` is now called per Short
in the package builder and threaded through
`DeliverableFile.description` into the real Zernio `content` field.
Proven by `test_canonical_caption_reaches_real_content_field` reading
the actual `FakePublishTransport.post_calls[]` payload.

**D. Platform-specific settings validated by readiness never reached
the real request.** Fixed: `build_platform_settings()`
(`hypogeum/publishing/zernio_platform_contracts.py`) resolves the
exact settings object readiness's own privacy check validates against,
and `process_channel_ready_bundles()` threads it through
`platform_settings` → `_build_publish_body()` → the request root
(`tiktokSettings`/`facebookSettings`). Proven by
`test_tiktok_privacy_setting_reaches_the_real_request` (privacy_level
+ both required consent flags present) and
`test_instagram_facebook_calls_carry_no_tiktok_settings` (no
cross-contamination).

**TikTok privacy value + source**: `PUBLIC_TO_EVERYONE`, from the
documented enum `{PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS,
FOLLOWER_OF_CREATOR, SELF_ONLY}` at `docs.zernio.com/platforms/tiktok`
(live-fetched 2026-08-22, recorded in
`channel_packages/hypogeum/platform_distribution_policy_v1.json`,
`policy_version` bumped to `1.1.0`). **This file is proposed, not
installed.** The production DB's actual
`channel_operations_profiles.platform_deliverable_policy_json` for
`hypogeum` (confirmed via direct read, Part 8) still matches the older
v1.0.0 shape — `tiktok.shorts` has no `privacy` key. This is a real,
currently-live gap: if TikTok/Instagram/Facebook bindings and
`AUTO_ZERNIO_PUBLISHING_ENABLED` were ever turned on today, every
TikTok Short would correctly fail the newly-wired cross-platform
readiness gate (D above) and never be attempted — a safe failure mode,
not a silent one, but still a gap worth closing deliberately rather
than automatically as a side effect of this task.

## Part 7 — Duration contracts and the 3 existing Shorts (found, not remediated)

Measured (via `ffprobe`) against `ZERNIO_PLATFORM_CONTRACTS`
(`hypogeum/publishing/zernio_platform_contracts.py`, already verified
live against `docs.zernio.com` in the prior session):

| Short | Duration | TikTok (3–600s) | Instagram Reels (3–90s) | Facebook Reels (3–60s) |
|---|---|---|---|---|
| `short_0` (`short_001`) | 86.3s | pass | pass | **fail** |
| `short_1` (`short_002`) | 78.7s | pass | pass | **fail** |
| `short_2` (`short_003`) | 97.8s | pass | **fail** | **fail** |

**All three current Shorts already exceed Facebook's 60s Reels cap;
`short_2` also exceeds Instagram's 90s cap.** `check_cross_platform_readiness()`
already checks `contract.duration_seconds_range` (this predates this
session — confirmed present in `cross_platform_readiness.py`) and, as
of Part 6/A above, that check is now actually wired into the real write
path. Concretely: **the newly-closed gap in Part 6/A means this
duration violation is now an actively enforced block**, not merely a
theoretical one — Facebook would refuse all 3 Shorts and Instagram
would refuse `short_2`, before any staging call, the moment those
platforms are bound and enabled.

No render was touched or remediated — explicitly out of scope
("without rendering or overwriting existing Shorts"). The generic
mechanism this needs (not built this session): a compiled duration
ceiling per Short = `min()` over every enabled destination platform's
`duration_seconds_range[1]`, computed upstream at the renderer, so
Assembly/Cinematic Director produce compliant assets directly instead
of readiness rejecting them after the fact. For `ep_f3619022`
specifically, a universal ≤60s cut (matching Facebook, the tightest
enabled-platform constraint) is the simplest correct target; the exact
beat/overlay boundaries to trim would need per-Short review against
`script_package_ep_f3619022.json`'s `shorts_scripts[].narration_beats`
(6 beats each, ~13–18s per beat) and `text_overlays[]` timing —
genuine editorial work, not a mechanical truncation, since a truncated
payoff would damage each Short's argument (e.g. `short_003`'s payoff
beat `b_047` — "The silence was deliberate" — is the entire point of
that Short and cannot be cut).

## Part 8 — Proposed captions for `ep_f3619022`'s 3 Shorts (NOT submitted, NOT written to the sealed package)

Derived strictly from `script_package_ep_f3619022.json`'s
`shorts_scripts[]` (`narration_beats` + `text_overlays`) — the actual
completed script content, not `final_render_package_ep_f3619022.json`'s
`shorts_renders[]`, which for this episode carries only placeholder
`short_id`s (`sh_000_placeholder`, etc.) with `source_beat_ids: null`.
**This placeholder linkage is exactly why `resolve_canonical_captions()`
raises `CaptionResolutionError` for this specific episode today** — the
render package never recorded which `short_id`/beats correspond to
which rendered file. These captions are proposed text for **operator
review only**; nothing below was sent to Zernio, written into
`final_render_package_ep_f3619022.json`, or used anywhere except this
document.

**`short_0` (script `short_001`, beats b_030–b_035):**
> 30cm wide — narrower than your shoulders. That's the schlupf, a
> passage inside the Erdstall tunnels of Central Europe: over 2,000
> hand-dug underground passages from the High Middle Ages, almost
> entirely empty, and never mentioned in a single medieval document.
> No burials. No goods. No explanation. Nobody knows why they were
> built.

**`short_1` (script `short_002`, beats b_036–b_041):**
> In 2026, archaeologists near Reinstedt, Germany found a medieval
> tunnel cut directly through the wall of a 6,000-year-old Stone Age
> tomb — not near it, through it. It's part of a pattern: Erdstall
> tunnel entrances across Central Europe cluster beneath churches,
> cemeteries, and other ancient sacred ground. Medieval builders
> weren't stumbling onto these sites by accident. They were finding
> them on purpose. Nobody knows why.

**`short_2` (script `short_003`, beats b_042–b_047):**
> Medieval Europe documented almost everything — land sales, tax
> records, even the price of grain. But not one document mentions the
> Erdstall tunnels: over 2,000 underground passages built across
> Central Europe, some right beneath church floors. Even the word
> "Erdstall" didn't appear until 1449, centuries later — and it named
> the land above them, not the tunnels themselves. The silence wasn't
> an oversight. It was a choice.

Future architecture (not built this session, per Part 4/D's
established discipline against speculative unused code): captions
should be generated as a versioned, attributable Shorts-metadata stage
output (alongside `text_overlays`), platform-aware (respecting each
enabled platform's `caption_field`/max-length), and consumed by
`resolve_canonical_captions()` directly — never derived ad hoc from
`narration_beats` the way this checkpoint's proposals were.

## Part 9 — Test results

Commit 1 tree, isolated (all commit-2 changes stashed out), full
readiness/publishing/worker suite: **420 passed**.
Combined tree (both commits), same suite: **428 passed** (the 8 new
`get_post()` tests).
Broader sweep (assembly, captions, channel config, cross-platform,
episode-detail, all publishing/provider/zernio/worker/scheduler
suites — 36 files): **1332 passed, 2 skipped, 5 failed** — the 5
failures are the pre-existing, already-documented baseline in
`TestCancelCheckFiresBetweenStages` (unrelated `NarrationCompletenessError`
in a cancel-check fixture, confirmed unchanged across this entire
task's several full-suite runs).

New tests added this session: 8 in `TestGetPost`
(`test_publishing_zernio.py`), 8 in `TestMandatoryMainEpisodeThumbnail`
plus the `_write_approved_thumbnail` fixture change
(`test_publishing_readiness.py`), 5 in
`TestCaptionsAndPlatformSettingsReachTheRealRequest`, 2 in
`TestCrossPlatformReadinessWiredIntoTheRealWritePath`, 1 in
`TestBufferPlatformSpecificCompletion` (`test_cross_platform_publishing.py`),
plus thumbnail-fixture updates (no new assertions) in
`test_zernio_release_flow.py`, `test_zernio_channel_release_trigger.py`,
and the 2-presign-call proof in `test_run_worker_publishing_wiring.py`.

## Part 10 — Secret scan and production containment (confirmed immediately before each commit)

- Secret scan across every changed file (`git diff 894a741 HEAD`):
  clean — the one pattern match was a self-documenting fake test
  credential (`credential_data="sk_test_never_real"`).
- `HEAD`: `3ac6aedddabaf0e137693fc13573325e20457478`.
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False`.
- `worker_control.queue_paused`: `'1'`.
- `run_worker.py` PID `845`: unchanged (started `2026-08-12`).
- `publication_intents` for `episode_id='ep_f3619022'`,
  `platform='youtube'`: **4**, unchanged before and after both commits.
- No TikTok, Instagram, or Facebook live post was created (no bindings
  exist for those platforms; none were created this session).
- No release bundle was resubmitted; no schedule, title, or
  description was changed on the existing YouTube post.
- The two pre-existing untracked files (`-`, `docs/operations/BURN_IN_LOG.md`)
  were left untouched.
- No credential value or full provider identifier appears in this
  document, in terminal output, or in either commit.

## Summary of distinctions this checkpoint is required to make explicit

- **Zernio-side scheduled record vs. actual YouTube video**: the
  4 records are Zernio-internal; the video does not exist on YouTube
  until `scheduledFor` triggers a real submission (see Part 1).
- **Locally recorded success vs. provider-side verified success**: no
  provider-side re-verification `GET` was issued this session (nothing
  changed on the provider side that would need re-verifying — no
  mutation occurred). `get_post()` (Part 4) exists for that purpose
  when it's next needed.
- **Thumbnail requested vs. thumbnail confirmed**: for `ep_f3619022`,
  neither — no thumbnail was ever requested or attached, because none
  is approved (Part 2). For all future episodes, "requested" and
  "confirmed" now converge at the `F_THUMBNAIL` readiness gate, which
  must pass before staging is ever attempted (Part 5).
- **Cross-platform code present vs. cross-platform runtime proven**:
  as of `894a741`, code was present but not proven wired; this
  checkpoint's Part 6 closes that gap and proves it through real
  write-path tests, not isolated checker calls.
- **Current human-content blocker**: `ep_f3619022`'s selected
  thumbnail candidate fails QA and identity review — needs a human
  decision (regenerate, re-select, or explicitly override with a new
  approval mechanism that does not yet exist) before any repair can
  proceed.
- **Current media-contract blocker**: all 3 existing Shorts exceed at
  least one enabled platform's duration limit (Part 7) — now actively
  enforced by the newly-wired cross-platform readiness gate, blocking
  Facebook/Instagram submission of those exact files until either the
  policy excludes them or new compliant renders are produced.
