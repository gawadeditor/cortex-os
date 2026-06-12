---
agent_name: brand_guardian_agent
domain: brand_compliance
implementation_file: hypogeum/agents/brand_guardian_agent.py
status: implemented
version: v1A
---

# Brand Guardian Agent v1A

Pre-generation brand compliance gate for The Hypogeum Chronicles. Enforces the
visual identity defined in `docs/brand_guardian/brand_spec_executable.md` (bible v1.1)
by scanning scene descriptions and thumbnail plans **before** any paid MUAPI
generation spend occurs.

## Scope — v1A Only

**Pass 1** — After VPD Phase 1 (Claude planning), before Phase 2 (MUAPI scene generation)  
**Pass 2** — After TD Phase 1 (Claude thumbnail planning), before Phase 2 (image generation)

Pass 3 (post-render audit) is explicitly deferred to a future version.

## Public API

```python
run_bga_pass1(visual_package, episode_id, config, output_dir=None) -> dict
run_bga_pass2(thumbnail_generation_plan, episode_id, config, output_dir=None) -> dict
```

Both functions return the bga_compliance_report dict on success and raise
`BGARejectedError` when outcome is REJECTED.

## Enforcement Scope (ENFORCEABLE_CORE only)

| Category | Severity | Rules |
|---|---|---|
| Talking heads, lip sync, direct-to-camera, interview framing | BLOCKING | E-VG05, E-VG06 |
| Neon, cyberpunk, modern urban, bright-key, cool blue, green, pink/purple | BLOCKING | E-C02–E-C08 |
| Thumbnail human face composition | BLOCKING | E-TH01 |
| Thumbnail light/bright background | BLOCKING | E-TH07 |
| Wordmark structure violations (when wordmark specified) | BLOCKING | E-L01–E-L09 |
| Non-Cinzel/Raleway font (when explicitly named in prompt) | BLOCKING | E-T01–E-T03 |
| Off-register mood, bright atmosphere, explicitly saturated palette | FLAG | E-C02 (character) |

## NOT Enforced (v1A)

- Exact hex values (all NOT_VERIFIED in bible v1.1)
- Music identity (absent from visual brand spec)
- Pixel-level generated output analysis (deferred to Pass 3)
- AI disclosure wording, YouTube title limits, watermark, end-card policy

## Rejection Thresholds

**Pass 1:** REJECTED if any BLOCKING violation, or flag_count / total_scenes ≥ 25%.

**Pass 2:** REJECTED if rank-1 main thumbnail candidate has BLOCKING, or all main
candidates have BLOCKING violations (no viable candidate exists).

## Output

Writes `bga_compliance_report_p{1|2}_{episode_id}.json` to the episode output directory.
Schema `bga_compliance_report` (#24 in schemas.py) validated before write.

## Integration Points

| Caller | Insertion Point | Guard |
|---|---|---|
| `visual_package_director.py:run_visual_package_director` | Between Phase 1 and Phase 2 | `config.BGA_ENABLED` |
| `thumbnail_director.py:run_thumbnail_director` | Between Phase 1 and Phase 2 | `config.BGA_ENABLED` |

`config.BGA_ENABLED` defaults to `False` in code. **Production `.env` has `BGA_ENABLED=true` — the gate is active in all production runs.**

## Config

```env
BGA_ENABLED=true              # production setting — gate is active
BGA_PASS1_MODEL=anthropic/claude-sonnet-4.6   # Pass 1 model (config key: BGA_PASS1_MODEL)
BGA_PASS2_MODEL=openai/gpt-5.5-pro            # Pass 2 model (config key: BGA_PASS2_MODEL)
```

## Models

**Pass 1:** `config.BGA_PASS1_MODEL` (default: `anthropic/claude-sonnet-4.6`), max_tokens=8192.

**Pass 2:** `config.BGA_PASS2_MODEL` (default: `openai/gpt-5.5-pro`), max_tokens=8192.

Pass 2 is GPT-based. This is a governance mandate (same mandate as TD_PLANNING_MODEL). Do not change BGA_PASS2_MODEL to a Claude model without a deliberate governance decision.

## System Prompt

`hypogeum/prompts/brand_guardian_agent.txt`

Instructs the configured model to evaluate visual descriptions for violations against
the ENFORCEABLE_CORE rules and return a JSON `{"violations": [...]}` object. Python
applies rejection thresholds deterministically after parsing the response.

## Tests

`tests/test_brand_guardian_agent.py` — 17 test classes, all Claude calls mocked.

## Caller Responsibilities

Callers that catch `BGARejectedError` should:
1. Log the rejection at error level with episode_id and violation summary
2. Inspect `exc.report["violations"]` to determine which scenes/candidates to revise
3. Re-plan the failing scenes/candidates (VPD re-planning is a future capability; v1A raises and stops)

This tentacle does **not** log at the caller level. It returns a bool via the report dict
and raises BGARejectedError. Callers own result logging.

## Prohibited Responsibilities

- No publishing decisions (Publishing Agent owns this)
- No pixel-level analysis of generated images or video (Pass 3 — not implemented)
- No schema validation of pipeline artifacts beyond bga_compliance_report
- No provider API calls (MUAPI, ElevenLabs, etc.)
- No reading or writing any file other than bga_compliance_report_p{1|2}_{episode_id}.json
