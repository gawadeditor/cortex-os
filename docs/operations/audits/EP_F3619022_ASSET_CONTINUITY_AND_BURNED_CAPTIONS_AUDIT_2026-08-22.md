# ep_f3619022 — Asset Continuity & Burned-Caption Forensic Audit

Status: **read-only diagnosis only.** No file was regenerated, no Short was rerendered, no
provider call (Zernio or YouTube) was made, no database row was written, no Channel
Package or readiness code was modified, and no worker/queue state was touched. This
document is the sole output of this task.

HEAD at start and end (unchanged): `3fcb6a42f348dbdc0e046b0fbe98bdb4225354cc`

## 1. Executive verdict

**Thumbnail outcome: `CONFIRMED_LOST_REGENERATION_REQUIRED`**
**Burned-caption outcome: `GENERATED_BUT_NOT_RENDERED`**

Both are explained fully below, with every claim tied to a specific file, database row,
or direct measurement. Two findings materially correct the task's stated operator
premise — both are surfaced prominently rather than assumed away:

- **The historical direct-YouTube publish (`heCS91MkfdE`, 2026-08-01) never had a
  Thumbnail applied by this system at all.** `publishing_record_ep_f3619022.json`
  explicitly records `"thumbnail_applied": false` for that video, and for every other
  deliverable published that day. If the operator's recollection of "a valid Thumbnail"
  is accurate, it was added manually, directly in YouTube Studio, entirely outside this
  system — an action Cortex has no mechanism to observe, stage, or archive, and which
  cannot be recovered now that the video itself has been deleted.
- **Burned-in captions were never merely "skipped by policy" — they were generated
  correctly and then never reached the render step at all**, due to an identity/ID
  mismatch inside Assembly Agent that also independently explains the placeholder
  `sh_00N_placeholder` IDs (Part 9) and the false "production_ready" signal (Part 10).

## 2. Historical Thumbnail lineage (chronological, evidence-sourced)

| Timestamp (UTC) | Event | Source | Evidence |
|---|---|---|---|
| 2026-07-31T13:00 | `thumbnail_concept_brief_ep_f3619022.json` written | file mtime | Concept: human silhouette at schlupf choke-point, 8% frame share, lower-left text-safe zone |
| 2026-07-31T22:00:22 | First thumbnail generation pass: `thumb_5ac66da2f7bf` (main, 16:9) + 3 short thumbnails, `run_id=0d7a1051-3843-4c51-8925-3749cebededd` | `.hypogeum-assets/catalogs/thumbnail_catalog.jsonl` | `asset_id="episodes/ep_f3619022/thumbnails/candidate_1"`, `file_path`/`archive_location` both point at the same live, mutable path `episodes/ep_f3619022/thumbnails/candidate_1.jpg`. Prompt explicitly describes a human figure ("rear-facing figure in coarse wool... 8% silhouette") |
| 2026-08-01T07:01–10:11 | `run_worker.py` "backward planner" run `ec93faa5-...` executes; every stage from `executive_producer` through `compliance_agent` completes in single-digit **milliseconds** (i.e. all upstream content, including the 07-31 thumbnails, was already on disk — this run only re-validated and then published) | `worker_attempts/ec93faa5-2049-4558-90b9-aeb6bb08c50e.json` | `thumbnail_director` stage: 10:11:44.336632 → 10:11:44.342427 (6 ms — not a real generation call) |
| 2026-08-01T10:13:17 | Main episode published to YouTube: `heCS91MkfdE` | `publishing_record_ep_f3619022.json` | `"thumbnail_applied": false`, `"captions_submitted": false` |
| 2026-08-01T10:14:10–51 | 3 Shorts published: `ItY0SXWUNeA`, `CHPhES0lALA`, `tnb4cyWcDZU` | same record | `"thumbnail_applied": false` for all 3 |
| 2026-08-05T17:40:36 | `asset_archive_record_ep_f3619022.json` cataloging pass runs | file | Reports "All 4 thumbnail(s) were already in the catalog (idempotent re-run)" — confirms the 07-31 thumbnails were still the ones on disk/in the catalog at this point, 4 days after publish |
| 2026-08-05T17:41:51 | `publishing_record_ep_f3619022.json` sealed, `overall_status="published"`, but a MAJOR-severity failure (`di_threshold`: 4/8 deliverables failed on Facebook/Instagram) routed to `executive_producer` | same record | Only file of its kind in the episode directory; no backups exist (`.hypogeum-assets/` is gitignored and untracked, so no git history either) |
| **2026-08-08T00:12:58** | **`main.jpg`/`candidate_1.jpg`/`candidate_2.jpg`/`candidate_3.jpg` all overwritten in place** by a second generation pass (`thumbnail_director_version: "2.0"`, `concept_driven: true`) | file mtimes (verified UTC) + `thumbnail_identity_ep_f3619022.json.checked_at: "2026-08-08T00:18:06"` | The 07-31 image's bytes at this exact path no longer exist anywhere; only `thumbnail_catalog.jsonl`'s metadata row for `thumb_5ac66da2f7bf` survives, and it is now a stale pointer to different content |
| 2026-08-08T00:18:06 | Identity Guardian evaluates the **new** (08-08) candidate | `thumbnail_identity_ep_f3619022.json` | `verdict: "BLOCKED"`, TIG-01 "a human figure is visible as the primary subject... standing in the tunnel passage holding a light source" |
| 2026-08-08 (same pass) | QA evaluates the same new candidate | `thumbnail_package_ep_f3619022.json.thumbnail_qa_report` | `overall_status: "fail"` — `c_thumbnail_feed_scale_legibility`: "86% of downscaled pixels below luma 20" |
| 2026-08-08T02:20:59 | `production_output_gate_ep_f3619022.json` evaluated (once, never re-run since) | file | `C_THUMBNAIL: fail` (same legibility reason), `D_SHORTS_CAPTIONS: fail` ("ffmpeg environment does not support the required subtitle filter") |
| 2026-08-22T10:07:50–10:09:10 | Current Zernio submission: main + 3 Shorts staged and scheduled to YouTube (4 `publication_intents`, all `status="succeeded"` at the ledger/submission level) | live DB read this session | **No `main_episode_thumbnail` staging attempt exists** — confirms no thumbnail was ever staged for this Zernio post either |

**TIG-01 (the human-figure rule) has existed since the repository's first commit
(2026-06-12, `fbdac27`)** — confirmed via `git log -S"TIG-01"`. It did not change between
the two generation passes, so "QA-policy drift" is **not** a contributing factor here;
both the 07-31 and 08-08 generations independently produced a human-figure-centric
image from the same concept brief, and only the 08-08 one was ever actually checked.

## 3. Exact historical published Thumbnail — location or confirmed loss

Answering the task's eight explicit questions:

1. **Does the historical published Thumbnail still exist byte-for-byte?** No proof one
   was ever *published* (attached to `heCS91MkfdE`) by this system exists at all —
   `thumbnail_applied: false` is an explicit, unambiguous boolean in the only
   publishing record that exists for this episode.
2. **Is it the current `thumbnails/main.jpg`?** No — impossible by timestamp: `main.jpg`
   was overwritten on 2026-08-08, a full week after the video went live on 2026-08-01
   with no thumbnail applied.
3. **Is it `candidate_1/2/3.jpg`?** No, for the same reason — the current files are the
   08-08 generation, not the 07-31 one.
4. **Stored elsewhere (archive/NAS/cache/release package)?** No. `thumbnail_catalog.jsonl`'s
   `archive_location` field is not a true immutable archive — it is an alias to the same
   mutable file path that was later overwritten. No NAS path is referenced anywhere in
   this episode's artifacts or the publishing code. macOS Time Machine is not configured
   on this machine (`tmutil listbackups` → "No machine directory found"). No provider
   (MUAPI) download cache directory exists under `.hypogeum-assets/`.
5. **Was `main.jpg` overwritten after the old publication?** Yes, confirmed — twice, in
   effect: the 07-31 original (pre-publish) was still on disk as of the 08-05 archive
   pass, then overwritten by the 08-08 regeneration.
6. **Did the current canonical pointer change after publication?** No — the pointer
   string (`episodes/ep_f3619022/thumbnails/candidate_1`) is identical across both
   generations. What changed is the *content at that path*, which is precisely the
   "canonical file overwritten" failure mode, not a "stale pointer" one.
7. **Was the old Thumbnail never registered in the canonical package?** It *was*
   registered in `thumbnail_catalog.jsonl` (that catalog entry is how it was found at
   all). No `thumbnail_package_ep_f3619022.json` snapshot from before 08-08 survives, so
   whether it was ever marked `selected: true` in a package cannot be confirmed either
   way.
8. **Did later QA-policy changes retroactively invalidate an approved asset?** No
   evidence of this — see the TIG-01 dating above. There is no evidence the 07-31 asset
   was ever run through QA/identity checks at all (no report survives), so "previously
   approved" cannot be established either.

**Why `CONFIRMED_LOST_REGENERATION_REQUIRED` rather than `INSUFFICIENT_EVIDENCE`:** the
evidence is not ambiguous about what Cortex itself did — it conclusively shows no
Cortex-applied thumbnail exists on the historical video, and the one candidate image
that existed on disk before publish was later overwritten with no separate copy
anywhere. Whether or not the operator manually uploaded a different file directly to
YouTube is a real, acknowledged unknown — but that file, if it existed, is unrecoverable
from any source available to this investigation (the video is deleted, and no trace of
a manual upload exists in any Cortex record). Under both possible histories, the
practical conclusion is identical: nothing recoverable exists, and the current
candidate independently fails QA and identity review. Regeneration is required; no
recovery action can substitute for it.

## 4. Current `main.jpg` relationship to the historical asset

`main.jpg` is byte-identical to the current `candidate_1.jpg` (same SHA256:
`62e1a2be4eecd9ef5097901ee1bddd83aca665828dc846f894e6e3a68e27fdcc`) — it is simply the
selected candidate copied to the canonical filename. Both are the **2026-08-08**
generation. Neither has any relationship to the 2026-08-01 publish beyond sharing a
logical asset-id string; the 2026-08-01 publish used no thumbnail at all.

## 5. QA-policy timeline

- **2026-06-12** — TIG-01 (human-figure rule) added at repository inception.
- **2026-07-31** — First thumbnail generation for this episode; no surviving QA/identity
  evidence.
- **2026-08-01** — Video published to YouTube without any thumbnail attached
  (`thumbnail_applied: false`).
- **2026-08-08** — Second thumbnail generation (v2.0, concept-driven); QA fails
  (`c_thumbnail_feed_scale_legibility`), Identity Guardian blocks (TIG-01).
- **2026-08-08** — `production_output_gate.py`'s `D_SHORTS_CAPTIONS` check independently
  confirms captions were not burned in, citing an environment-level cause.
- **2026-08-22 (this session's earlier work)** — A new `F_THUMBNAIL` mandatory-thumbnail
  readiness gate is added to `hypogeum/publishing/readiness.py`, reusing the same
  QA/identity fields — this closes the gap for *future* episodes but was added **after**
  today's 10:07–10:09 UTC Zernio submission already completed without it.

No evidence anywhere shows the QA/identity *rules themselves* changing between any of
these events in a way that would retroactively invalidate a previously-passing asset.

## 6. Asset persistence failure (Part 4 — lifecycle audit)

Tracing `generation → candidate storage → selection → approval → canonicalization →
release package → publication → archive`:

| Transition | Copied/moved/linked? | Hash recorded? | Immutable? |
|---|---|---|---|
| Generation → `thumbnails/candidate_N.jpg` | Written directly to the canonical path (no staging intermediate) | Not in `thumbnail_package_ep_f3619022.json` (candidates have `generated_at: null`); yes in `thumbnail_catalog.jsonl` per-entry, but the catalog is written once and never re-verified against current file content | **No** — this is the root defect: the "canonical" path and the "generation output" path are the same file, so a second generation run silently destroys the first |
| Selection (`candidate_1` → `main.jpg`) | File copy (confirmed via SHA256 match) | No | The copy step doesn't re-verify the candidate hasn't already been overwritten since generation |
| Approval | **No approval field exists anywhere** — `selected: true` is the only signal, and it is set unconditionally at generation time, never gated on QA/identity passing | n/a | n/a |
| Canonicalization | Same file, same path, across every regeneration | No | **Confirmed destructive**: 08-08 generation overwrote 07-31's bytes with zero preservation |
| Release package (`final_render_package`) | `thumbnail_references[]` lists asset-id strings only, never a hash | No | A hash here would have let readiness detect "the referenced asset changed since this package was built" — it cannot today |
| Publication | `publishing_record_ep_f3619022.json` correctly recorded `thumbnail_applied: false` — the historical record is honest | n/a | n/a |
| Archive | `thumbnail_catalog.jsonl`'s `archive_location` is an **alias**, not a copy — confirmed by both entries pointing at the same live path that was later overwritten | n/a | **This is the single biggest fixable gap**: a real archive step (copy-on-generate to a versioned, immutable path) would have made this entire investigation moot |

**Root cause classification (evidence-based, not inferred):**
- **canonical file overwritten** — confirmed (Part 2/3).
- **archive omission** — confirmed (`thumbnail_catalog.jsonl`'s "archive" is a live alias, not a real archive).
- **incomplete manifest** — confirmed (`thumbnail_references[]` has no hash, no `generated_at`, no way to detect drift).
- **stale canonical pointer** — **not** present here (the pointer string never changed; only the content changed — this is a distinct failure mode from what's usually called "stale pointer").
- **QA-policy drift** — **not** supported by evidence (Part 5 above).
- **cleanup deletion** — no evidence of an explicit deletion; this was an overwrite-in-place by a regeneration pass, not a cleanup job.

## 7. Burned-caption pipeline trace

**Caption assets exist and are real** (Part 5 of the task, table below), but were never
burned into the 3 current Shorts.

| Asset | Path | mtime (UTC) | Content |
|---|---|---|---|
| SRT (Caption Agent output) | `captions/sh_short_001.srt`, `_002.srt`, `_003.srt` | 2026-08-01T01:58:58 | Real, non-empty (2.2–2.5 KB each) |
| ASS (burn-ready, styled) | `captions/sh_short_001.ass`, `_002.ass`, `_003.ass` | **2026-08-17T14:20:18–21** | Real, non-empty (3.3–4.0 KB each) — written only ~4 minutes before the Shorts were rendered, i.e. `_build_caption_ass()` (`hypogeum/agents/video_renderer.py`) genuinely ran and succeeded for this render pass |
| Rendered Shorts | `short_0/1/2_ep_f3619022.mp4` | 2026-08-17T14:24:43–14:25:08 | No subtitle stream (`ffprobe` shows only `h264`+`aac`); **zero burned-in text confirmed by direct frame extraction** (12 frames sampled: t=3/15/30/50s × 3 Shorts — none show any overlay text; see Part 8) |

**Mechanism, traced through the actual code** (`hypogeum/agents/assembly_agent.py`):

1. `short_ids = timeline_plan.short_ids` (line 462) is the single source used for **both**
   (a) building `short_srt_paths` — the dict that gets passed to `render_episode()` to
   drive caption burn-in (lines 714–720), and (b) building the final `shorts_renders[]`
   metadata array written into `final_render_package_ep_f3619022.json` (lines 801–827).
2. For this episode's actual final render (`package_metadata.rendered_at:
   "2026-08-17T14:33:42"`), the loop at line 804 (`for short_index, short_id in
   enumerate(short_ids)`) produced **zero** entries — proven directly by the fact that
   all 3 `shorts_renders[]` entries in the current `final_render_package` are the
   `sh_{idx:03d}_placeholder` fallback (lines 829–842: "Pad shorts_renders to exactly 3
   if needed (schema requires exactly 3)"), each with `duration_seconds: 0.0`,
   `video_asset_id: ""`, `caption_asset_id: ""`.
3. Because `short_srt_paths` (line 716's loop) is built from the **same** `short_ids`
   list, it must also have been empty for this run — meaning `render_episode()` was
   called with no caption paths for any of the 3 Shorts it was about to render.
4. `render_episode()` (`hypogeum/agents/video_renderer.py`) nonetheless rendered all 3
   Shorts correctly (their video/audio content is complete and their durations are
   real, non-zero, non-placeholder), because its own internal Short-rendering logic
   does not depend on `short_ids`/`shorts_timelines` matching — it renders whatever
   Short-scoped timeline data it is given directly. **The captions were simply never
   in that data to begin with**, because assembly_agent.py never attached them.
5. The one place the caption burn-in filter is actually assembled
   (`video_renderer.py` lines 1263–1269) is unconditional whenever `srt_path is not
   None and srt_path.exists()` — there is no environment capability check (no probe of
   `ffmpeg -filters` for `subtitles`/`ass` support) anywhere in this path. This means
   two independent defects exist, not one:
   - **(Confirmed, primary, for this specific render)** the ID-linkage break in
     step 2–3 above meant `srt_path` was `None` for all 3 Shorts, so the burn-in
     branch was never entered at all.
   - **(Confirmed separately, environment-level)** this machine's current `ffmpeg`
     (Homebrew 8.1.2) has **no `subtitles` or `ass` filter and no `libass` in its build
     configuration** — verified directly via `ffmpeg -filters` and `ffmpeg -version`
     in this session. `production_output_gate.py`'s `D_SHORTS_CAPTIONS` check recorded
     the identical finding on 2026-08-08: `"ffmpeg environment does not support the
     required subtitle filter"`. Even if defect #1 above is fixed, caption burn-in
     will still fail in this exact environment until `ffmpeg` is rebuilt/reinstalled
     with `--enable-libass`. This was **not** the proximate cause of the current
     Shorts' missing captions (the ID break prevented the filter from ever being
     attempted), but it is a second, independent blocker that will surface as soon as
     the first one is fixed, and must be resolved before any future burned-caption
     render can be trusted.

I could not fully trace **why** `timeline_plan.short_ids` was empty/mismatched at the
exact moment of this specific 08-17 render — the only log covering that date
(`.hypogeum-assets/pipeline.log`) has since rotated (it now starts at
2026-08-21T11:38:21, well after the event), and reproducing the failure would require
rerunning the pipeline, which this task explicitly forbids. What is fully confirmed by
static evidence is the downstream mechanism (steps 1–5 above) and that it is the same
root cause driving both the missing burned captions and the placeholder Short IDs
(Part 9).

## 8. Actual video-frame evidence

12 frames extracted via `ffmpeg -ss <t> -i <file> -frames:v 1` at t=3s, 15s, 30s, 50s for
each of `short_0`, `short_1`, `short_2` (durations 86.3s / 78.7s / 97.8s respectively —
sample points span each Short's opening hook, mid-body, and later sections, where the
script's own `text_overlays[]` entries place hook/key-information/payoff cards).
**None of the 12 frames show any burned-in text of any kind.** `ffprobe` confirms each
file carries exactly one video stream (`h264`) and one audio stream (`aac`) — no
embedded subtitle stream exists in any of the three files. This is direct pixel-level
confirmation, not an inference from metadata.

## 9. Placeholder-ID root cause

`sh_000_placeholder` / `sh_001_placeholder` / `sh_002_placeholder` in
`final_render_package_ep_f3619022.json.shorts_renders[]` are produced by the exact
fallback described in Part 7 step 2 (`assembly_agent.py` lines 829–842), which has
existed unchanged since the repository's first commit (`fbdac27`, 2026-06-12) as a
schema-satisfying safety net for "fewer than 3 real Shorts were built" — a condition
that was clearly never expected to fire for a healthy production run, but did fire here.

Confirmed downstream consequences of the placeholder IDs specifically (as distinct from
the caption-linkage break, which shares the same root but is a separate symptom):
- **Loss of script-to-Short mapping in the render manifest** — confirmed:
  `shorts_renders[].short_id` cannot be joined against
  `script_package.shorts_scripts[].short_id` (`short_001`/`_002`/`_003`), even though
  that real content exists and is complete.
- **Broken social-caption resolution** (distinct from burned-in captions) — confirmed
  from this session's earlier work: `resolve_canonical_captions()`
  (`hypogeum/publishing/readiness.py`) raises `CaptionResolutionError` for this
  episode's Shorts specifically because of this same placeholder linkage — already
  documented in this session's prior checkpoint
  (`MANDATORY_THUMBNAIL_AND_CROSS_PLATFORM_GAPS_2026-08-22.md`, Part 8).
- **No incorrect social-caption mapping occurred** (i.e. it fails closed, not silently
  wrong) — cross-platform readiness blocks these Shorts from any caption-requiring
  platform rather than sending a mismatched or empty caption.
- **Publishing-readiness false positive did NOT occur for the general case** — the
  *current* mandatory-thumbnail/caption-readiness code (built this session, landed
  after the fact) would today correctly block re-submission of a Short with an
  unresolved caption on a non-YouTube platform. What it does **not** and never has
  checked is burned-in caption presence for YouTube itself (Part 10).
- A completed production artifact retaining a placeholder identity (as this one does)
  is itself a defect independent of any downstream consumer — the manifest is simply
  wrong about what it contains, regardless of whether every consumer happens to fail
  safely today.

Separately: today's earlier reconciliation pass (2026-08-22T08:39:13 UTC, evidenced by
`final_render_package_ep_f3619022.backup_20260822T083913Z.json`) added a correct
`metadata_source.shorts_titles[]` array keyed by the **real** `short_001`/`_002`/`_003`
IDs, proving the real ID↔content mapping is fully recoverable elsewhere in the episode's
own artifacts — but that reconciliation did not touch `shorts_renders[]` itself, so the
placeholders remain.

## 10. False-completeness root cause

`final_render_package_ep_f3619022.json.package_integrity` self-reports, in the same
file, in the same JSON object:
```
"shorts_complete": false,
"production_ready": true,
"shorts_runtime_status": "not_planned"
```
This is an internally inconsistent self-report — `shorts_complete: false` and
`production_ready: true` coexist — but more importantly, **nothing in the current
Zernio publishing path ever reads either field**. Confirmed by direct grep: zero
references to `production_ready` or `shorts_complete` anywhere in
`hypogeum/publishing/readiness.py`, `cross_platform_readiness.py`, `buffer.py`, or
`zernio_channel_release_trigger.py`. This is consistent with this codebase's fail-closed
philosophy of never trusting a self-reported flag — but it has the side effect that an
*honest* negative self-report is equally ignored, not just a dishonest positive one.

The deeper cause: **`hypogeum/agents/production_output_gate.py` — the one module
explicitly built (per its own docstring, dated to a "2026-08-02 Production Capability
Conformance Audit" that used this exact episode as its motivating case) to re-verify
real pixel-level output quality, including `D_SHORTS_CAPTIONS`'s burned-caption check —
is wired **only** into the legacy direct-YouTube path
(`hypogeum/agents/publishing_agent.py`, via `enforce_production_output_gate()`).
Confirmed via exhaustive grep across the repository: no caller of
`run_production_output_gate`/`enforce_production_output_gate` exists anywhere in the
Zernio path (`zernio_channel_release_trigger.py`, `zernio_release_flow.py`,
`zernio_publish_runtime.py`, `run_worker.py`'s `_process_channel_publishing`) — only a
single comment in `run_worker.py` mentions the module by name, as an analogy, not a call.
`readiness.py`'s own docstring states this division of labor explicitly: *"a caller that
wants both technical QA and publish readiness runs both"* — but no caller in the Zernio
path ever does.

**This is the complete, evidence-backed answer to "why did bundle completeness and
publication readiness pass despite missing burned captions":** the one check capable of
detecting it (`D_SHORTS_CAPTIONS`, which had already caught this exact defect once,
on 2026-08-08) was never invoked by the code path that actually submitted this episode
to Zernio today. YouTube also does not require a social caption field, so the separate,
already-wired `resolve_canonical_captions()` check never had a reason to fire for this
platform either. No check in the active path was ever positioned to catch this.

## 11. Affected future episodes / channels

This is architectural, not episode-specific:
- **Every future episode's Zernio submission** is exposed to the same gap until
  `production_output_gate` (or an equivalent real-pixel burned-caption check) is wired
  into the Zernio path the same way `F_THUMBNAIL` was wired into it this session.
- **Every future episode produced on a machine whose `ffmpeg` lacks `libass`** will
  silently produce caption-less Shorts even after the ID-linkage defect (Part 7) is
  fixed, with the render itself still reporting success. This is a fleet/environment
  provisioning concern, not a per-episode one — it affects every Channel Package, not
  just Hypogeum.
- **Any future episode where `timeline_plan.short_ids` diverges from what
  `render_episode()` actually rendered** will reproduce both the placeholder-ID and the
  missing-caption defects together, since they share the exact same root (Part 7/9).
  This is a latent bug in `assembly_agent.py`, not something specific to this episode's
  content or history.

## 12. Exact remediation sequence (diagnosis only — none of this was performed)

1. **Fix the `assembly_agent.py` linkage bug** (Part 7): the code that builds
   `shorts_renders[]` and `short_srt_paths` must consume the *actual* Shorts
   `render_episode()` produced (`render_manifest["shorts_renders"]`, already available
   at line 803 as `_manifest_shorts` but currently only used for a `file_exists` lookup
   inside a loop keyed by the possibly-empty `short_ids`) rather than re-deriving
   identity from a separately-computed list that can drift out of sync. The "pad to
   exactly 3" fallback (lines 829–842) should raise/fail closed instead of silently
   masking a mismatch with placeholder data, since a completed production artifact must
   never retain a placeholder identity.
2. **Verify/fix the `ffmpeg` build used for Assembly rendering** (Part 7): confirm
   whether it includes `libass`; if not, rebuild/reinstall with `--enable-libass`
   before trusting any future burned-caption render. Consider having
   `video_renderer.py` probe filter availability once at startup and fail closed
   (rather than silently producing a caption-less-but-otherwise-successful render) if
   an `.srt`/`.ass` path was supplied but the `subtitles` filter is unavailable.
3. **Wire real-output verification into the Zernio path** (Part 10): either call
   `run_production_output_gate()`/`enforce_production_output_gate()` from
   `zernio_channel_release_trigger.py` (or an equivalent seam) the same way
   `hypogeum/agents/publishing_agent.py` already does, or build a narrower burned-caption
   pixel check inside `readiness.py`/`cross_platform_readiness.py` using the same
   `_verify_burned_captions()` logic `production_output_gate.py` already has. Per this
   session's own established convention (`F_THUMBNAIL`), this must be a single,
   reused resolver — never a second independent implementation.
4. **Fix thumbnail archival** (Part 6): the Asset Librarian's `archive_location` for
   thumbnails must point to an immutable, versioned copy (e.g.
   `thumbnails/candidate_1.v{N}.jpg` or a content-addressed path), not an alias to the
   same mutable canonical filename a later generation can overwrite. Record a SHA256
   hash in both `thumbnail_catalog.jsonl` and `thumbnail_package_*.json` so any future
   drift between "what the catalog says" and "what's actually on disk" is detectable,
   not just archaeologically reconstructible after the fact (as this audit had to do).
5. **Regenerate a compliant thumbnail for `ep_f3619022`** (only after 1–4 above are not
   prerequisites for this specific step, but are the reason a *third* attempt should not
   repeat the same failure) — per the F_THUMBNAIL gate already built this session, this
   requires a candidate that passes both feed-scale legibility and TIG-01 (no human
   figure as primary subject). Not performed in this task.
6. **Re-render the 3 Shorts with captions actually burned in**, once steps 1–2 are
   fixed, or apply a targeted repair that re-runs only the burn-in step against the
   already-rendered clips if the codebase supports that without a full Assembly
   re-run. Not performed in this task.

## 13. Files and functions requiring later changes (diagnosis only, nothing edited)

- `hypogeum/agents/assembly_agent.py` — lines ~461–462 (`short_ids`/`shorts_timelines`
  aliasing), ~699–720 (`short_srt_paths`/`short_narration_paths` construction), ~801–842
  (`shorts_renders[]` construction and the placeholder-padding fallback).
- `hypogeum/agents/video_renderer.py` — `render_episode()`'s `shorts_renders` internal
  manifest (source of truth for what was actually rendered), lines ~1263–1269 (burn-in
  filter assembly — needs a capability check).
- `hypogeum/agents/production_output_gate.py` — `enforce_production_output_gate()`,
  `run_production_output_gate()`, `_verify_burned_captions()` — reusable as-is; the gap
  is entirely in who calls it, not its own logic.
- `hypogeum/publishing/zernio_channel_release_trigger.py` — the seam where a real-output
  gate call would need to be added (`process_channel_ready_bundles()` or
  `build_publish_package_from_episode_dir()`).
- `hypogeum/agents/thumbnail_catalog` writer (wherever `thumbnail_catalog.jsonl` entries
  are appended — not located precisely in this read-only pass; needed for the
  `archive_location`-must-be-immutable fix in Part 12 item 4).
- `hypogeum/agents/thumbnail_package` writer — to add a `generated_at` timestamp and
  SHA256 per candidate (currently `null`/absent).

## 14. Recovery actions that do not require regeneration

- **None identified for the Thumbnail.** No archived, cached, or alternate copy of any
  pre-08-08 thumbnail exists anywhere searched (episode directory, catalogs, backups
  directory, Time Machine, NAS references, git history). Recovery is not possible
  without regeneration.
- **Partial recovery is possible for the Shorts' render manifest** without any
  regeneration: `shorts_renders[].short_id` (and `video_asset_id`,
  `duration_seconds`, `scene_count`) can be corrected in place using data already
  proven to exist and be consistent — the real files on disk (`short_0/1/2_ep_f3619022.mp4`,
  their true `ffprobe` durations), `script_package.shorts_scripts[].short_id`
  (`short_001`/`_002`/`_003`), and today's own reconciled `shorts_titles[]` (already
  keyed correctly). This would fix the manifest's honesty without touching any video
  pixel — it would **not** add burned captions, only stop the manifest from lying about
  what was rendered. Not performed in this task (explicitly out of scope: "Do not...
  modify readiness" / no source changes authorized).
- **Burned captions cannot be recovered without a re-render** — the source `.ass`/`.srt`
  files are intact and correct, but by definition, adding pixels to already-encoded
  video requires re-encoding.

## 15. Regeneration actions — only if recovery proves impossible (not performed)

- Thumbnail: generate a new candidate set through the existing Thumbnail Strategy →
  Thumbnail Director pipeline, explicitly avoiding a human figure as primary subject
  (per TIG-01) and verifying feed-scale legibility before selection — this is the only
  path forward per Part 3/14.
- Shorts: once the `assembly_agent.py` linkage bug (Part 12 item 1) and the `ffmpeg`
  libass availability (Part 12 item 2) are both fixed, re-run Assembly for the 3 Shorts
  (not the main episode, which has no caption or ID defect) to produce new files with
  captions genuinely burned into the frames. Neither action was taken in this task.

---

## Appendix — provider identifiers (redacted per repository policy)

- Historical YouTube main video ID: `heCS91MkfdE` (per task's own established lead —
  reproduced here only because the task explicitly supplied it and asked it be treated
  as a lookup lead; this document does not introduce any new provider identifier not
  already given to it, and does not include the 3 historical Shorts' video IDs beyond
  what appears in the read-only `publishing_record_ep_f3619022.json` referenced above).
- Current Zernio post IDs (from live-read `publishing_receipts` rows, 2026-08-22): all 4
  begin `6a897...` — full values withheld here; available in
  `.hypogeum-assets/hypogeum.db.publishing_receipts.provider_publication_id` for any
  operator with direct database access.

## Appendix — containment confirmed throughout this task

`AUTO_ZERNIO_PUBLISHING_ENABLED=False`, `worker_control.queue_paused='1'`,
`run_worker.py` PID unchanged, exactly 4 `publication_intents` for
`episode_id='ep_f3619022', platform='youtube'` before and after this task, no new
database writes, no file under `.hypogeum-assets/episodes/ep_f3619022/` was created,
modified, or deleted by this task (12 JPEG frame extracts were written to this session's
scratchpad directory only, outside the repository and outside `.hypogeum-assets/`), the
two pre-existing untracked repository files were not touched, and no live Zernio or
YouTube API call was made (the last live Zernio `GET` verification is the one already
recorded in `HYPOGEUM_ZERNIO_LIVE_SCHEDULED_RELEASE_SMOKE_2026-08-22.md`, and nothing in
the local ledger has changed since — queue remained paused throughout).
