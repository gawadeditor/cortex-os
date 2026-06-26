# Episode Zero Lite Run Procedure

## Purpose

Authoritative step-by-step operator checklist before invoking `run_pipeline.py` for Episode Zero. Prevents starting a production run with wrong flags, unverified credentials, unreviewed artifacts, or without completing pre-flight gates.

This is the single source of truth for how Episode Zero is resumed or completed. If this skill is not followed, do not proceed.

## When to Use

- Any session that intends to resume or complete Episode Zero production
- After the MUAPI account is topped up
- After any credential change that may affect Episode Zero
- Before any invocation of `run_pipeline.py` for episode `ep_000`

## Required Files to Read

- `docs/episode-zero-status.md` — current completion state of each artifact
- `docs/first-production-run.md` — original operator checklist
- `docs/production-onboarding-plan.md` — setup and credential requirements
- `memory/project_state.md` — current operational priority and known blocking factors
- `.hypogeum-assets/episodes/ep_000/` — all files present (inventory before assuming what exists)
- `.env.example` — all required environment variables

## Files Not to Edit

- `.hypogeum-assets/episodes/ep_000/*.json` — all generated artifacts are read-only during this procedure
- `docs/episode-zero-status.md` — update only after a run completes, not during pre-flight
- `hypogeum/pipeline.py` — do not modify the pipeline
- Any file in `hypogeum/agents/`
- Any file in `hypogeum/schemas.py`

## Procedure

Run these gates in order. Do not skip a gate because it seems obvious. Do not proceed past a NO-GO.

### Gate 1 — Run /diagnostics

Run the `/diagnostics` skill first. Confirm:
- All required credentials are PRESENT
- Benchmark results exist and are not stale

**If NO-GO from `/diagnostics`: stop here.**

### Gate 2 — Run /collect ep_000

Run the `/collect` skill for episode `ep_000`. Confirm:
- Which artifacts already exist and are non-empty
- Which pipeline stages are complete vs incomplete vs not started
- Identify exactly which stages will re-run vs which will be skipped via checkpoint

The pipeline checkpoints completed planning stages to disk. On resume, stages with existing output files are skipped automatically:

| Stage | Checkpoint file(s) | Skip condition |
|---|---|---|
| Executive Producer | `validation_scorecard_{ep}.json` + `topic_brief_{ep}.json` | Both files present and valid JSON |
| Research Director | `research_package_{ep}.json` | File present and valid JSON |
| Narrative Architect | `narrative_package_{ep}.json` | File present and valid JSON |
| Script Engine | `script_package_{ep}.json` | File present and valid JSON |
| Visual Package Director Phase 1 | `visual_package_{ep}.json` | File present → Phase 1 skipped, Phase 2 (MUAPI) re-runs from first scene |

**What is NOT checkpointed:** VPD Phase 2 (MUAPI scene generation), Audio Agent, Assembly Agent, and Publishing Agent. These always re-run in full. For ep_000 — which failed during MUAPI scene generation — this means: planning stages (EP, RD, NA, SE) will be skipped, VPD Phase 1 planning will be skipped (plan exists), but all MUAPI `submit_scene()` calls will re-run from scene 1.

**If artifact state is unclear or unexpected: stop. Do not assume.**

### Gate 3 — Run /visual-review ep_000

Run the `/visual-review` skill for `ep_000`. Confirm:
- `visual_package_ep_000.json` has been reviewed against documentary grammar
- Shot distribution, tier assignments, and coverage gaps are acceptable
- No BLOCKED verdict from the visual review

**If visual-review returns BLOCKED: stop. Resolve flagged scenes first.**

### Gate 4 — Run /budget-run ep_000

Run the `/budget-run` skill. Confirm:
- Scene cost estimate is calculated
- `MUAPI_EPISODE_BUDGET_CREDITS` is set to a non-zero ceiling
- Dry-run has been completed and passed
- `MUAPI_ALLOW_PAID_FALLBACKS` is confirmed (default: false)

**If NOT AUTHORIZED from `/budget-run`: stop here.**

### Gate 5 — YouTube OAuth

Confirm YouTube OAuth token is valid. This is an out-of-band browser step that cannot be automated:
- `YOUTUBE_OAUTH_TOKEN` must be present and non-expired
- If expired: the operator must re-authorize via browser before proceeding
- Publishing to YouTube will fail silently if this step is skipped

### Gate 6 — Meta Token

Confirm Meta access token is valid:
- `META_ACCESS_TOKEN` has a 60-day expiry
- Confirm it has not expired since last session
- If expired: operator must generate a new token via Meta Developer Console

### Gate 7 — Final Invocation

All gates passed. Invoke:

```bash
python3 run_pipeline.py \
  --topic "The Antikythera Mechanism" \
  --episode-id ep_000 \
  --youtube-privacy private
```

Use `--youtube-privacy private` for Episode Zero. Do not publish publicly until the episode has been reviewed.

After the run completes: run `/collect ep_000` again to confirm all artifacts were produced.

## Expected Output

```
EPISODE ZERO RUN PROCEDURE
============================

Gate 1 — Diagnostics:       PASS / NO-GO
Gate 2 — Artifact collect:  PASS / UNCLEAR
Gate 3 — Visual review:     PASS / FLAGGED / BLOCKED
Gate 4 — Budget run:        AUTHORIZED / NOT AUTHORIZED
Gate 5 — YouTube OAuth:     CONFIRMED / NOT CONFIRMED
Gate 6 — Meta token:        CONFIRMED / EXPIRED

Overall: PROCEED / HOLD
[If HOLD: specific reason and required action]

Invocation command when all gates pass:
python3 run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_000 --youtube-privacy private
```

## Stop Conditions

- Any single gate returning NO-GO, NOT AUTHORIZED, BLOCKED, or UNCLEAR is sufficient to stop. Do not proceed past a failed gate on the assumption that the issue is minor.
- If `docs/episode-zero-status.md` has not been read: stop. Do not assume the current state of the episode from memory.
- If the MUAPI account balance cannot be confirmed (no recent diagnostic): stop at Gate 1.
- Do not set `--youtube-privacy public` for Episode Zero. First production run is always private for review.
- If this is a re-run after a partial failure: run `/collect ep_000` first to determine what already exists before starting the full pipeline again. Some artifacts may be complete and could be lost in a full re-run.
