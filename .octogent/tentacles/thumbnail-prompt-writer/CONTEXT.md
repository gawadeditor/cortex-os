---
agent_name: thumbnail_prompt_writer
domain: visual-production
implementation_file: hypogeum/agents/thumbnail_prompt_writer.py
status: implemented
---

# Thumbnail Prompt Writer

Sends the Thumbnail Handoff Brief to the configured GPT model and returns the validated thumbnail_generation_plan.

## Model

**Runtime model: `openai/gpt-5.5-pro`** (config key: `TD_PLANNING_MODEL`, default: `openai/gpt-5.5-pro`).

The thumbnail reasoning stack must remain GPT-based. This is a governance mandate, not a default. Do not change `TD_PLANNING_MODEL` to a Claude model without a deliberate governance decision and corresponding test update.

> Previously this doc incorrectly claimed the runtime model was Claude Sonnet 4.6. The actual runtime model has been `openai/gpt-5.5-pro` since the user mandate was applied. Corrected 2026-06-12.

## Owned Responsibility

Sole authority for the thumbnail planning LLM call (Phase 1 of thumbnail production).
Owns:
- Sending the Thumbnail Handoff Brief to `config.TD_PLANNING_MODEL` via `run_agent()`
- Building the `input_package` with `task = "plan_thumbnail_generation"` and the brief
- Logging `td_planning_started` (with candidates_count and short_candidates_count) before the call
- Logging `td_planning_completed` (with main_candidates_planned and shorts_count) after the call
- Returning the validated `thumbnail_generation_plan` dict to the caller

## Allowed Inputs

| Input | Type | Source |
|-------|------|--------|
| handoff_brief | dict | ThumbnailHandoffBrief — output of thumbnail_candidate_selector |
| config | Config | Runtime config (API credentials) |
| episode_id | str | Episode identifier for attribution and logging |
| output_dir | Path | Resolved output directory for run_agent artifact storage |
| log | Logger | Caller-provided logger |

## Emitted Artifact

`thumbnail_generation_plan` — validated dict matching the `thumbnail_generation_plan` schema.
Contains:
- `main_episode.ranked_candidates` — ordered list of main thumbnail candidates with image_prompt, aspect_ratio, ctr_rationale
- `shorts` — per-short ranked candidate lists with 9:16 aspect ratio prompts
- `primary_subject_preserved` — integrity flag
- `curiosity_gap_alignment` — narrative alignment note

Consumed by `thumbnail_director.py` Phase 2 to drive MUAPI `generate_thumbnail()` calls.

## Prohibited Responsibilities

- No MUAPI calls (thumbnail_director owns Phase 2)
- No candidate selection or scoring (thumbnail_candidate_selector owns this)
- No handoff brief construction (thumbnail_handoff_brief_builder owns this)
- No output file writing or package assembly (thumbnail_director owns this)
- No schema validation (run_agent() handles output_schema validation)

## Reviewer / Approver Chain

Changes to the `output_schema` value or `model_slug` must be reviewed with the Visual Production domain owner — these affect schema validation and model billing. Changes to logging events (`td_planning_started`, `td_planning_completed`) must be cross-checked against `thumbnail_director.py` to avoid duplicate or missing log events.

## Required Tests

`tests/test_thumbnail_prompt_writer.py` — 16 tests covering:
- Returns generation_plan dict from run_agent
- run_agent called once
- handoff_brief passed in input_package
- task = "plan_thumbnail_generation" in input_package
- output_schema = "thumbnail_generation_plan"
- model_slug = "openai/gpt-5.5-pro" (via config.TD_PLANNING_MODEL)
- max_tokens = config.TD_PLANNING_MAX_TOKENS (default: 8192; currently 16384 in production)
- episode_id forwarded to run_agent
- output_dir forwarded to run_agent
- primary_subject_identity passes through via brief
- negative_constraints pass through via brief
- td_planning_started logged before run_agent
- td_planning_completed logged after run_agent
- td_planning_completed includes main_candidates_planned count
- td_planning_completed includes shorts_count
- returns whatever run_agent returns (passthrough)
