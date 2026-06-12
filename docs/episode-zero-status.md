# Episode Zero Status

**Last updated:** 2026-06-05  
**Status:** PAUSED — awaiting MUAPI balance top-up

---

## Run Summary

- **Episode ID:** ep_000
- **Topic:** The Antikythera Mechanism: Who Really Built the Impossible Machine?
- **Scorecard:** 7.8/10 PASS (differentiation_opportunity 5.5 flagged, all other dimensions 7.5+)
- **Privacy setting:** private (YouTube)

---

## Failure Point

**Stage:** Visual Package Director — Phase 2 (MUAPI scene submission)  
**Error:** `MUAPIQuotaExhausted: MUAPI quota exhausted (HTTP 402). Account billing issue requires resolution. All scene generation halted.`  
**Cause:** MUAPI account balance depleted. Claude planning (Phase 1) completed successfully and wrote `visual_package_ep_000.json`. The 402 fired on the first `submit_scene()` call in Phase 2.

---

## Generated Packages

All files written to `.hypogeum-assets/episodes/ep_000/`:

| File | Size | Stage | Notes |
|---|---|---|---|
| `topic_brief_ep_000.json` | 3,924 bytes | Executive Producer | Decision: PASS, score 7.8 |
| `validation_scorecard_ep_000.json` | 2,518 bytes | Executive Producer | |
| `research_package_ep_000.json` | 16,158 bytes | Research Director | 12 claims, 4 Perplexity phases |
| `narrative_package_ep_000.json` | 29,221 bytes | Narrative Architect | |
| `script_package_ep_000.json` | 33,951 bytes | Script Engine | 6 sections, 29 main beats, 3 shorts (17 beats) |
| `visual_package_ep_000.json` | 95,432 bytes | Visual Package Director | 46 planned scenes |

**Not generated:**
- Thumbnail images (`.hypogeum-assets/episodes/ep_000/thumbnails/` — empty)
- Narration audio (`.hypogeum-assets/episodes/ep_000/narration/` — empty)
- Captions/SRT (`.hypogeum-assets/episodes/ep_000/captions/` — empty)
- Video clips (`.hypogeum-assets/episodes/ep_000/video/` — empty)
- Episode render (`.hypogeum-assets/episodes/ep_000/renders/` — empty)
- Publishing record (`.hypogeum-assets/episodes/ep_000/publishing/` — empty)

---

## Estimated Cost (this run)

| Provider | Usage | Est. Cost |
|---|---|---|
| OpenRouter claude-sonnet-4.6 | EP (×2), RD synthesis, NA, SE, VPD planning | ~$0.85 |
| Perplexity sonar-pro | 4 discovery queries | ~$0.012 |
| MUAPI | 0 scenes generated (halted at first 402) | $0.00 |
| ElevenLabs | 0 narration calls | $0.00 |
| **Total (successful partial run)** | | **~$0.86** |

*Additional ~$2.50 incurred on failed runs during session while diagnosing token ceiling issues.*

---

## Bugs Fixed During This Run (all accepted)

| Bug | Fix | Location |
|---|---|---|
| `ResponseParseError` on markdown-fenced JSON responses | Added `_strip_markdown_fence()` called before `json.loads()` | `hypogeum/agent.py` |
| All planning agents (RD/NA/SE/VPD) hit token ceiling mid-output | Raised `_*_MAX_TOKENS` to 16384 (RD/NA/SE) and 32768 (VPD) | Each agent module |
| OpenRouter caps claude-opus-4.6 at 12288 output tokens | Switched RD synthesis model to claude-sonnet-4.6 | `research_director.py` |
| VPD receiving full 34KB script_package as input | Added `_compact_script_package()`: truncates beat text to 20 words | `visual_package_director.py` |

**Test count after fixes: 1595 passing.**

---

## Resume Command

Once MUAPI balance is topped up, run:

```bash
python3 run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_000 --youtube-privacy private
```

**Note:** The pipeline has no checkpoint/resume logic. It re-runs all 9 agents from EP on every invocation. The existing ep_000 JSON artifacts will be overwritten. This is expected and correct.

---

## Pre-Resume Checklist

- [ ] Top up MUAPI account balance at https://muapi.ai (or equivalent billing page)
- [ ] Verify MUAPI key is still valid: check `MUAPI_API_KEY` in `.env`
- [ ] Confirm OpenRouter balance sufficient for another full run (~$1.00)
- [ ] Confirm Perplexity balance sufficient for 4 queries
- [ ] Confirm ElevenLabs balance sufficient for ~30 narration segments + music
- [ ] Run `python3 run_pipeline.py ...` as above

---

## Go/No-Go for Episode 1

**BLOCKED** until Episode Zero completes end-to-end (YouTube upload confirmed, sources/chapters/SRT verified in description).
