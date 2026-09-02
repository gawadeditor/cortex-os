# Pipeline Roadmap

**Last updated:** 2026-05-31

This file tracks the complete pipeline architecture — frozen content generation agents, approved production agents, and post-V1 future agents.

---

## Complete Production Pipeline (9 agents)

```
Executive Producer
      ↓  Topic Brief
Research Director
      ↓  Research Package
Narrative Architect
      ↓  Narrative Package
Script Engine
      ↓  Script Package
Visual Package Director
      ↓  Visual Package + Thumbnail Handoff Brief
Thumbnail Director
      ↓  Thumbnail Package
Audio Agent
      ↓  Narration Package
Assembly Agent
      ↓  Final Render Package
Publishing Agent
      ↓  Published Episode + 3 Shorts
```

---

## Content Generation Pipeline (6 agents — all frozen)

The original V1 pipeline. Takes a topic and produces all creative and visual specifications. All six agent specifications are frozen.

| # | Agent | Model | Status |
|---|---|---|---|
| 1 | Executive Producer | Claude Sonnet 4.6 | Frozen |
| 2 | Research Director | Claude Opus 4.6 | Frozen |
| 3 | Narrative Architect | Claude Opus 4.6 | Frozen |
| 4 | Script Engine | Claude Sonnet 4.6 | Frozen |
| 5 | Visual Package Director | Claude Sonnet 4.6 | Frozen |
| 6 | Thumbnail Director | Claude Sonnet 4.6 | Frozen |

---

## Production Pipeline (3 agents — all frozen)

Takes the output of the content generation pipeline and produces a complete published episode package. All three specifications are frozen.

### Audio Agent

**Status:** Frozen — v1
**Rationale:** Voice generation is a production function, not a writing function. Script Engine is responsible for writing only. Audio Agent owns all ElevenLabs orchestration and narration production.

**Responsibilities:**
- ElevenLabs orchestration
- Voice selection and channel voice management
- Narration rendering from Script Package narration_beats
- Segment stitching into continuous narration tracks
- Audio QA (timing, clarity, pacing accuracy)
- Retry handling for failed or degraded segments
- Narration Package generation

**Input:** Script Package
**Output:** Narration Package

---

### Assembly Agent

**Status:** Frozen — v1
**Rationale:** Assembly is not a simple execution step. It owns the full production integration of all media components into a synchronized final episode.

**Responsibilities:**
- Timeline construction
- Clip sequencing (Video Package scene clips in order)
- Narration synchronization (Audio Package narration tracks to timeline)
- Music placement (background score, theme)
- Natural sound placement (ambient, period-appropriate environmental audio)
- SFX placement (sound effects)
- Audio mixing (narration, music, SFX levels)
- Caption generation
- Final render package production (video file, metadata, chapter markers)

**Input:** Visual Package + Narration Package + Thumbnail Package
**Output:** Final Render Package

---

### Publishing Agent

**Status:** Frozen — v1
**Rationale:** Distribution is its own domain with distinct failure modes, platform-specific logic, and retry requirements.

**Responsibilities:**
- Receive Final Render Package from Assembly Agent
- Distribute main episode to YouTube (16:9)
- Distribute 3 Shorts to YouTube Shorts, TikTok (if available), Instagram Reels, Facebook Video
- Platform-specific metadata adaptation (titles, descriptions, captions, hashtags differ per platform)
- Publishing failure handling and retry
- Publishing confirmation and record keeping

**Input:** Final Render Package
**Output:** Publishing Record (confirmation of published URLs, platform responses, failures)

---

## Future Agents (post-production pipeline)

These agents are explicitly out of scope until the 9-agent production pipeline is complete and stable.

### Performance Analyst

**Purpose:** Analyze own content performance across all publishing platforms and feed insights back into the production pipeline.

**Platforms:** YouTube (long-form), YouTube Shorts, TikTok, Instagram Reels, Facebook Video

**Responsibilities:**
- Collect performance metrics from each platform
- Analyze CTR by title pattern and thumbnail style
- Analyze audience retention curves per video
- Analyze view counts, watch time, and completion rates
- Identify winning and weak topics, hooks, and thumbnail patterns
- Feed structured performance insights back into: Executive Producer, Narrative Architect, Script Engine, Thumbnail Director

**Required infrastructure:** YouTube Analytics API, TikTok Analytics API (availability uncertain), Meta Graph API Insights, cross-platform normalization layer, performance memory files per agent.

---

### Competitor Intelligence Analyst

**Purpose:** Track competitor channels in the history/mystery niche and surface strategic opportunities for differentiation and topic selection.

**Responsibilities:**
- Track competitor channel upload frequency and video length trends
- Analyze high-performing competitor videos by visible engagement signals
- Analyze competitor title patterns, keyword strategies, thumbnail styles
- Identify over-covered topics and under-served angles
- Feed competitive intelligence back into: Executive Producer and Thumbnail Director

**V1 overlap note:** In V1, Research Director performs ad-hoc competitive landscape research per topic brief. When Competitor Intelligence Analyst is introduced, Research Director's competitive landscape responsibility should be revisited to avoid duplication.

**Required infrastructure:** YouTube Data API (public metadata), competitor channel tracking registry, topic coverage map, thumbnail archival workflow.

---

## V2 Infrastructure Upgrades

### Asset Storage — Cloudflare R2 Migration

V1 uses local filesystem storage (`.hypogeum-assets/`). V2 migrates to Cloudflare R2 object storage.

**Migration scope:**
- Episode video clips (MUAPI outputs)
- Narration audio segments (ElevenLabs outputs)
- Final rendered episodes and Shorts
- Thumbnail files
- Caption SRT files

**Channel Music Library** (`.hypogeum-assets/channel/`) may remain local or migrate to R2 depending on library size at migration time.

**Do not design R2 integration until V1 local storage is implemented and stable.**

---

## Analytics Infrastructure Requirements

Full analytics integration requires platform access before future agents can be designed:

| Platform | Access Method | Status |
|---|---|---|
| YouTube | YouTube Analytics API + YouTube Data API | Not configured |
| YouTube Shorts | Same as YouTube | Not configured |
| TikTok | TikTok Analytics API | Availability uncertain |
| Instagram Reels | Meta Graph API | Not configured |
| Facebook Video | Meta Graph API | Not configured |
| Competitor tracking | YouTube Data API (public metadata only) | Not configured |

---

## Deferred Roadmap Items

Items identified during M5.3 Pipeline Integration Verification as non-blocking future improvements. Do not schedule until the 9-agent V1 pipeline has completed at least 5 production runs.

---

### Hypogeum Skills Audit

**Added:** 2026-06-04 (M5.3 verification)
**Status:** Deferred — post-V1-stability

**Purpose:** Systematic review of every manual or semi-manual task currently performed in the Hypogeum production workflow, to identify which tasks could be automated by building or integrating a specialized skill.

**Audit columns per task:**

| Task | Skill | Repository / Source | Expected Gain |
|---|---|---|---|
| Music library cold-start (first episode) | Auto-seed channel music library from ElevenLabs on first run | Internal — elevenlabs.py + music-library.json | Eliminates manual pre-seeding step |
| `ELEVENLABS_VOICE_ID` pinning | Interactive voice selector (stream samples, confirm, write to config) | Internal — config.py + elevenlabs.py | Removes irreversible manual decision risk |
| YouTube OAuth initial authorization | Browser OAuth launcher skill (opens auth URL, polls for token, writes refresh_token to env) | `google-auth-oauthlib` | Eliminates out-of-band browser step |
| Meta access token refresh reminder | Calendar/cron alert at day 55 of token lifecycle | Internal — publishing record metadata | Prevents silent token expiry mid-run |
| `covered-topics.md` gap analysis | Topic deduplication skill comparing EP candidate topics against covered-topics.md | Internal — pipeline.py pre-flight | Prevents re-running topics already published |
| Instagram staging bucket provisioning | Cloud storage setup skill (Cloudflare R2 or S3 bucket creation + CORS config) | `boto3` / R2 API | Removes manual infrastructure step blocking Instagram |
| Assembly Agent chapter timestamp accuracy review | Chapter preview skill: render description draft to stdout before publishing run | Internal — metadata.py | Human QA on chapters without a full publish attempt |
| Performance feedback loop | Performance Analyst agent (see Future Agents above) | YouTube Analytics API | Closes the data loop for EP topic selection |

**How to apply:** When scheduling V2 work, prioritize tasks in this table that unblock the highest-value platform or reduce the most operator friction per build-hour.
