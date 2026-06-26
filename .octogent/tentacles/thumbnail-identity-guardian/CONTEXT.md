---
agent_name: thumbnail_identity_guardian
domain: visual-production
implementation_file: hypogeum/agents/thumbnail_identity_guardian.py
status: implemented
version: v1
---

# Thumbnail Identity Guardian (TIG)

Post-generation vision gate for The Hypogeum Chronicles thumbnail stack. Inspects
the **actual rendered thumbnail image** for compliance with the channel's visual
identity before the thumbnail is committed to the production pipeline.

Unlike the Brand Guardian Agent (BGA), which operates on text descriptions before
image generation, the TIG operates on the generated bitmap using Claude's vision
capability.

## Prompt Files

- `hypogeum/prompts/thumbnail_identity_guardian.txt`

Loaded by: `hypogeum/agents/thumbnail_identity_guardian.py`

## Purpose

Catch visual identity violations that only become detectable in the rendered image:
- Faces that appear despite prompt instructions
- Backgrounds that rendered brighter than intended
- Neon/cyberpunk visual artifacts
- Wordmark that is absent or illegible in the final composition

## Verdicts

| Verdict | Meaning | Production effect |
|---|---|---|
| `APPROVED` | No violations found | No effect |
| `WARNING` | Advisory violations only | No effect; recorded in package |
| `BLOCKED` | One or more hard violations | `production_ready=False` (fail-closed default) |
| `error` | TIG call failed | `production_ready=False` (fail-closed default) |

## Fail-Closed Default

`TIG_FAIL_OPEN` defaults to `False`. A BLOCKED or error verdict sets
`production_ready=False` in the Thumbnail Package unless `TIG_FAIL_OPEN=true`
is explicitly set in `.env`. Do not set `TIG_FAIL_OPEN=true` in production
without a deliberate governance decision.

## BLOCKING Rules

| Rule ID | Violation |
|---|---|
| TIG-01 | Human face as primary subject |
| TIG-02 | Bright or high-key background (>40% brightness) |
| TIG-03 | Neon, cyberpunk, or sci-fi visual language |
| TIG-04 | Talking-head or interview framing |
| TIG-05 | Missing or illegible text overlay (wordmark or title) |
| TIG-06 | Wrong dimensions or aspect ratio (expected 1280×720 / 16:9) |
| TIG-07 | Wordmark absent from composition |

## WARNING Rules (advisory only)

| Rule ID | Violation |
|---|---|
| TIG-W01 | Background brightness elevated (not yet high-key, but trending warm) |
| TIG-W02 | Subtitle text weak or low-contrast |
| TIG-W03 | Wordmark scale smaller than ideal |
| TIG-W04 | Image subject competes with wordmark placement |

## Config Keys

```env
TIG_ENABLED=true          # defaults True in code; gate runs unless explicitly disabled
TIG_MODEL=anthropic/claude-sonnet-4.6   # vision model; default claude-sonnet-4.6
TIG_FAIL_OPEN=false       # fail-closed default; BLOCKED/error → production_ready=False
```

## Public API

```python
run_thumbnail_identity_guardian(
    episode_id: str,
    image_path: Path,
    tig_client: Callable | None = None,  # injectable for tests
    config = None,
    output_dir: Path | None = None,
) -> dict
```

Returns a `thumbnail_identity` dict with: `episode_id`, `image_path`, `verdict`,
`violations`, `blocking_count`, `warning_count`, `checked_at`, `tig_version`,
`metadata_version`.

## Reads

- `image_path` — the rendered thumbnail image (JPG, PNG, or WebP) from:
  `.hypogeum-assets/episodes/{episode_id}/thumbnails/main.jpg`
- `generation_plan` — **not read**; TIG receives only the image path and episode_id

## Writes

- `thumbnail_identity_{episode_id}.json` → `.hypogeum-assets/tig/` (or `output_dir`)

## Integration Point

Called by `thumbnail_director.py:run_thumbnail_director` in Phase 3, after
Thumbnail QA and before Thumbnail Package assembly. Guard: `config.TIG_ENABLED`.

Imported lazily inside the guard block — enables patching at
`hypogeum.agents.thumbnail_identity_guardian.run_thumbnail_identity_guardian`.

## Injectable Client

Tests inject a mock callable:
```python
tig_client(image_path: Path, episode_id: str) -> str
```
Must return a JSON string: `{"violations": [...]}`. When `tig_client=None`, uses the
built-in `_default_tig_call()` which base64-encodes the image and calls Claude vision.

## Prohibited Responsibilities

- Must not generate images (MUAPI calls are prohibited)
- Must not replace BGA Pass 2 — TIG operates post-generation; BGA Pass 2 operates pre-generation on text descriptions
- Must not replace Thumbnail QA — Thumbnail QA checks structural properties (dimensions, asset_class, path); TIG checks visual identity
- Must not write any file other than `thumbnail_identity_{episode_id}.json`
- Must not access pipeline state beyond the image file and episode_id

## Enforcement Gap

TIG does not validate exact font rendering (Cinzel/Raleway) — this is not feasible
via bitmap vision inspection. Font enforcement remains prompt-based (BGA) only.

TIG does not validate exact hex palette values — it checks background brightness
(0–40% bounds) but cannot reliably distinguish #080C0E from #0A0E10 in a JPEG.

## Tests

`tests/test_thumbnail_identity_guardian.py` — full coverage of verdict paths,
injectable client interface, image-not-found BLOCKED, fail-open/fail-closed behavior.

`tests/test_thumbnail_director.py:TestTDTIG` — wiring tests (14 tests) covering
TIG called/skipped, all verdict outcomes, fail-open/closed, package structure.
