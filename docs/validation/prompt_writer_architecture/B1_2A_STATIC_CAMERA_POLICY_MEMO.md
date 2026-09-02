# PHASE B1.2A — Static Camera Policy Decision Memo

**Status:** Decided and implemented (interim owner: `hypogeum/video_dna.py`). This is
not the future Camera DNA module (not yet built, per the roadmap) — it is the
existing Video DNA module absorbing sole ownership of a concept it already
partially held (`static_with_atmosphere`).

## Owners inspected

| Owner | Position found | Evidence |
|---|---|---|
| `hypogeum/video_dna.py` | Permitted a nuanced form (`static_with_atmosphere` in `allowed_movements`) | Direct read |
| `hypogeum/prompts/vpd_scene_prompts.txt` | Banned the literal word "static" as a camera verb, but already offered "locked-off"/"held frame" as legitimate substitute phrasing for the same physical camera behavior — closer to nuanced than absolute in practice, but stated as an absolute "NEVER use" bullet | Direct read |
| `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md` (Layer 6) | Banned outright, no exception: "Do not use `static` as a camera verb," always requiring `controlled slow push in` or `slow drift` | Direct read |
| `hypogeum/prompts/shot_diversity_planner.txt` | Permitted a near-static option (`locked_off_hold`) with "used sparingly... never as a default" — already compatible in spirit with a nuanced policy | Direct read |
| `hypogeum/skills/compiled/veo_translation_skill.compact.md` | Lists "static shot" as normal, valid, official Veo vocabulary with no caveat | Direct read |
| `hypogeum/skills/compiled/seedance_translation_skill.compact.md` | Lists "Static camera | Establishing hold" as a normal, valid vocabulary term with no caveat | Direct read |
| Pinned upstream Veo Prompt Director (`third_party/skills/veo_prompt_director/...`) | Lists "Static" as valid ("the camera remains fixed"), unconditionally, in a worked example that also uses fluorescent lighting | Confirmed via `vpd_scene_prompts.txt`'s own quoted override text (pinned file itself not re-read this pass — file is never edited, per repo convention) |
| Pinned upstream Seedance2 | Not independently re-confirmed this pass | [PRIOR AUDIT] — flagged for a direct read if a future slice touches it |
| `hypogeum/agents/visual_package_director.py` Stage A4 prompt + Visual Language Engine skill | Forbids `static_camera` as a lazy **presentation-method default** — a different, narrower mechanism (upstream planning choice, not a written camera-verb ban) | Direct read; compatible with the adopted policy, not contradictory, no edit made |
| `CLAUDE.md` | Summarized Cinematic Prompt Architecture's (then-)absolute ban as a "Key rule" | Direct read; this is derivative documentation of the file above, not an independently-approved production decision with its own separate rationale — updated in this slice to stay accurate to the file it summarizes |

## Decision

No repository evidence was found of an approved production decision that depends on
an absolute static-camera ban surviving unchanged. `CLAUDE.md`'s "Key rules" line
was the closest candidate, but it is a summary of `cinematic_prompt_architecture.md`'s
content, not an independent decision — updating it to stay accurate to that file's
new content is required, not a violation of an approved decision. `video_dna.py`
(the closest thing to an existing canonical module) and the Shot Diversity Planner
were already compatible with a nuanced policy before this slice.

**Adopted verbatim, per the requested canonical rule:**

> Static framing is permitted only when the frame contains meaningful subject,
> environmental, atmospheric, or lighting motion. A completely frozen image is
> forbidden. Static framing must be intentional, rare, and narratively justified.

## Canonical owner

`hypogeum/video_dna.py` — `camera_language.static_framing_policy` (positive form,
rendered by `describe_video_dna_constraints()`) and `forbidden_visual_styles`
(negative form: "completely frozen frame with no subject, environmental,
atmospheric, or lighting motion", surfaced via `get_video_dna_forbidden_items()`).

Split into positive/negative form to satisfy the module's own existing,
test-enforced discipline (`tests/test_video_dna.py::TestVideoDnaPositiveNegativeSeparation`
asserts the positive directive contains no negative language, e.g. "forbidden") —
not a new invention, the same pattern every other Video DNA rule already follows.

## Files updated to reference (not redefine) the canonical policy

- `hypogeum/prompts/vpd_scene_prompts.txt` — the flat "NEVER use... static" bullet
  replaced with a stated policy block citing `video_dna.py` as owner; the Veo Prompt
  Director override paragraph updated from an absolute-ban claim to the nuanced one;
  new DIVERSITY ASSIGNMENT COMPLIANCE section (Objective 1) cross-references this
  policy for `locked_off_hold`.
- `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md` — Layer 6
  and the validation checklist bullet, both changed from absolute ban to a pointer at
  the canonical policy.
- `hypogeum/prompts/shot_diversity_planner.txt` — one sentence added cross-referencing
  the canonical policy near `locked_off_hold`, wording itself unchanged (already
  compatible).
- `hypogeum/skills/compiled/veo_translation_skill.compact.md` and
  `seedance_translation_skill.compact.md` — annotated with a "Hypogeum Policy Note"
  pointing to the canonical policy, without deleting either file's factual provider-
  vocabulary listing (deleting a real provider capability would be inaccurate, not a
  policy fix).
- `CLAUDE.md` — "Key rules" line updated to match, so it stays accurate to the file
  it summarizes.

## Not touched

- Pinned third-party skills (Seedance2, Veo Prompt Director) — never edited, per
  existing repo convention (`visual_package_director.py` comments: "loaded verbatim,
  never edited"). The existing override paragraph in `vpd_scene_prompts.txt` remains
  the correct mechanism for asserting precedence over them; only its wording changed.
- `hypogeum/agents/visual_package_director.py` Stage A4 prompt / VLE skill's
  `static_camera` forbidden-default rule — already compatible, no contradiction found.
- Camera DNA / Motion DNA modules — not created, per this slice's explicit constraints.
