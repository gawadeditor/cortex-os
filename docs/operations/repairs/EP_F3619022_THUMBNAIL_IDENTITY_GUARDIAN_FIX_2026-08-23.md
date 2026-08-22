# Thumbnail Identity Guardian: deliverable-class-aware fix — 2026-08-23 (Phase 3)

Status: architecture fix committed, real re-verification complete. **No
image was regenerated or re-touched** — `main_episode.recomposed_v1.jpg`
and `short_00{1,2,3}.recomposed_v1.jpg` are byte-identical to the prior
phase's committed files (SHA-256 unchanged). No live Zernio mutation,
no MUAPI call, no worker restart, no queue change.

## 1. The false blocker

`hypogeum.agents.thumbnail_identity_guardian.run_thumbnail_identity_guardian()`
had no `deliverable_class`/aspect parameter. Its system prompt was one
hardcoded string (`hypogeum/prompts/thumbnail_identity_guardian.txt`)
mandating, for every image regardless of class: `1280×720` (16:9),
`"THE HYPOGEUM CHRONICLES"` wordmark present, and a three-line title
hierarchy. Applied to a legitimate `720×1280` (9:16) Short cover, this
produced three BLOCKING violations (TIG-05/06/07) that were never a real
brand violation — they were the tool asking a vertical Short to look
like the horizontal main-episode thumbnail.

## 2. Architecture change

**Factory Core stays channel-agnostic.** `hypogeum/agents/
thumbnail_identity_guardian.py` now builds its system prompt from two
parts:

1. A generic, channel-agnostic template
   (`hypogeum/prompts/thumbnail_identity_guardian.txt`, rewritten —
   contains no wordmark text, palette, typography, or dimensions of any
   kind, only the response-format contract and the instruction to
   evaluate solely against whatever rules are supplied for the image's
   own deliverable class).
2. This channel's own rules for the image's `deliverable_class`,
   resolved via a new, injectable `identity_rules_resolver` callable
   (default: `hypogeum.brand.thumbnail_identity_rules.
   get_thumbnail_identity_rules`) — **Channel Package content**, in the
   same place and by the same import convention every other Factory
   Core agent already reads channel-specific creative rules from
   (`hypogeum.brand.visual_identity`, etc.). A channel cloned from this
   template supplies its own `hypogeum/brand/thumbnail_identity_rules.py`;
   Factory Core never hardcodes which deliverable classes exist, their
   dimensions, or what any of them require.

`hypogeum/brand/thumbnail_identity_rules.py` (new) defines a
`ThumbnailIdentityRules` dataclass and two configured instances for this
channel:

| Field | `main_episode` | `short` |
|---|---|---|
| `aspect_ratio` | 16:9 | 9:16 |
| `expected_dimensions` | 1280x720 | 720x1280 |
| `wordmark_required` | `True` | `False` |
| `title_hierarchy_required` | `True` | `False` |
| `human_figure_policy` | silhouette/back-profile only, no frontal face | same |
| palette / forbidden style language | same dark/charcoal + gold/bronze identity | same |

`run_thumbnail_identity_guardian(..., deliverable_class="main_episode")`
is the default (existing callers — `thumbnail_director.py`,
`run_thumbnail_only.py` — are unchanged and continue evaluating against
the long-form contract exactly as before).

**Deterministic backstop, not just a prompt instruction:**
`_filter_inapplicable_violations()` strips any `TIG-05`/`TIG-07`/
`TIG-W03` violation the vision model might still emit for a class whose
own rules do not require a wordmark or title hierarchy — this makes
"must not require a wordmark layout / title hierarchy" a hard code-level
guarantee, not a hope that the model reads the prompt correctly.

New universal rule ids (apply to every class): `TIG-08` (unrelated/
fabricated imagery not grounded in the real episode/Short), `TIG-09`
(placeholder/flat-fill/logo-only imagery), `TIG-10` (composition/focal
hierarchy unreadable at that deliverable's real feed scale). `TIG-01`
(human figure), `TIG-02` (palette), `TIG-03` (forbidden style), `TIG-04`
(talking-head), and `TIG-06` (wrong dimensions/aspect) are unchanged in
meaning but now parametrized per-class instead of hardcoded to 16:9.

## 3. A genuine main_episode rule-text bug found and fixed along the way

While re-testing, `main_episode`'s corrected thumbnail
(`candidate_1.jpg`, unchanged since the prior phase) newly triggered a
BLOCKING `TIG-05` on 3 consecutive real vision calls: "no separate
Raleway Regular subtitle line" beyond the existing two-line headline.
Checking this channel's own authoritative spec
(`hypogeum/brand/thumbnail_style_system.md` §4.2) showed the real,
documented structure is a **two-line headline+emphasis stack** (Line 1
"headline"/setup, Line 2 "emphasis"/payload) plus a separate small
wordmark — never a distinct third "subtitle" line. The OLD hardcoded
prompt's "Cinzel SemiBold for headline, Raleway Regular for subtitle"
wording was already imprecise relative to the channel's own real spec;
my first pass at porting it into `ThumbnailIdentityRules` preserved that
imprecision (and made it slightly more explicit), which is what
triggered the false block. `typography_description`/
`focal_hierarchy_description` for `main_episode` were corrected to match
§4.2 exactly (headline + emphasis stack, no separate subtitle) — a rule-
text correction sourced from the channel's own documentation, not an
image change, not a verdict rewrite, and not a human override. Re-run
against the same unchanged image: **APPROVED**, stable across repeat
calls.

## 4. Final re-verification — all four, real gate results

| Deliverable | Dimensions | QA (failures) | Identity verdict | Identity blocking |
|---|---|---|---|---|
| main_episode | 1280×720 | `warn`* (0 failures) | **APPROVED** | 0 |
| short_001 | 720×1280 | `warn`* (0 failures) | **APPROVED** | 0 |
| short_002 | 720×1280 | `warn`* (0 failures) | **APPROVED** | 0 |
| short_003 | 720×1280 | `warn`* (0 failures) | **APPROVED** | 0 |

\* `overall_status="warn"` comes solely from `c_thumbnail_text_overlay`
being skipped (no `generation_plan` for a post-hoc recomposition) — this
is informational, never a failure. `qa_status` in the manifest is
derived as `"pass"` whenever `failures == []`, matching exactly what
`hypogeum.agents.production_output_gate._check_thumbnail()` itself
treats as passing. **This is the one place that value is computed** —
both manifest files
(`EP_F3619022_SHORTS_REPAIR_MANIFEST_2026-08-23.json` and
`EP_F3619022_THUMBNAIL_MANIFEST_2026-08-23.json`) are now the SAME
consolidated file, rebuilt from these exact real results, resolving the
prior inconsistency between a "pass" manifest and a "warning" narrative.

All four deliverables now reach an approved, non-blocking identity
result — no human override used, no verdict file rewritten by hand.

## 5. Existing Zernio post targets — unchanged, re-confirmed from the manifest (no new fetch)

| Deliverable | Existing post ID | Content mutation would proceed | Media staging required |
|---|---|---|---|
| main_episode | `6a89751ca12350d9b38baf7c` | `true` | `true` |
| short_001 | `6a89752c7a56b107295b2b2c` | `true` | `true` |
| short_002 | `6a897539c2f26fb88c8c21c9` | `true` | `true` |
| short_003 | `6a897546c2f26fb88c8c2a16` | `true` | `true` |

All four proposed updates still target these exact existing post IDs and
their preserved schedule/timezone (echoed verbatim from the original
read-only GET) — no replacement post is ever proposed anywhere in the
manifest.

## 6. Tests

`tests/test_thumbnail_identity_guardian.py`: 51/51 pass (38 pre-existing
+ 1 legitimately updated for the TIG-05→TIG-09 "file not found" rule-id
correction + 12 new, covering: a vertical Short no longer rejected for
lacking the horizontal hierarchy; a valid 9:16 Short passing; wrong-
aspect/placeholder/identity-breaking/unrelated-imagery/prohibited-human-
figure Short covers each failing; `main_episode`'s wordmark and title-
hierarchy requirements unchanged; the real Channel Package rules objects
themselves; and — the strongest proof of channel-agnosticism — an
entirely custom, non-Hypogeum deliverable class/rules object injected
via `identity_rules_resolver`, working identically, with the built
prompt verified to contain zero hardcoded "Hypogeum"/"Cinzel" references
when a different channel's rules are supplied.

## 7. Remaining steps before the final in-place Zernio repair

1. **Media staging** (still not performed): presign + upload the
   repaired local Shorts
   (`.hypogeum-assets/episodes/ep_f3619022/repair/short_00N.repaired.mp4`)
   and the four approved thumbnails, to obtain real public URLs — the
   only remaining item every proposed PUT body is missing.
2. Re-run `build_proposed_update_body()` with those real URLs in place
   of `media_url=None`/`thumbnail_url=None` — the body shape and post
   IDs/schedule already validated above do not otherwise change.
3. Explicit authorization for a live `ZernioProvider.repair_existing_post()`
   PUT against each of the four existing post IDs above — not
   performed, not authorized in this phase.
