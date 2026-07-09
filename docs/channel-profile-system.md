# Channel Profile System — Phase B4.75 Design

**Status:** Design only — no implementation  
**Phase:** B4.75 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B1 (Channel Definition), B2 (Channel Clone Engine), B3 (Template System), B3.5 (Factory Entity), B4 (Configuration Layer), B4.5 (Module System)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Phase B1 defined the Channel as a technical data entity: stable IDs, model assignments, prompt packs, budget rules, publishing rules. What it did not define is the Channel as a **business object** — the full description of who the channel is, who it serves, what it stands for, and what it is trying to achieve.

Currently, this business intelligence is scattered or missing entirely:

| Where | What Lives There |
|---|---|
| `brand_bible_hypogeum.json` | Color palette, typography, emotional register |
| `.env` | Budget ceilings, model names, voice ID |
| `channel_config.py` / `channel_manager.py` | Brand + publishing profiles (fragmented) |
| Agent prompts (hardcoded) | Audience description, tone, content pillars |
| Operator's head | Competitor awareness, monetization goals, learning priorities |

The result: every agent reconstructs its understanding of the channel from fragments. There is no single query that answers "who is this channel, who does it serve, and what are the rules?" There is no dashboard surface to read or update the channel's strategic identity. There is no migration path when the channel grows into a Media Entity in V2.

Phase B4.75 defines the **Channel Profile** as the solution. The Channel Profile is not a new entity — it is the strategic intelligence layer that lives inside the Channel and makes it a fully governed business object.

---

## 2. V1 Hierarchy and Positioning

The V1 hierarchy is unchanged:

```
Factory
└── Templates         (production blueprints — B3)
    └── Channels      (active production identities — B1 + B4.75)
        └── Runs      (pipeline executions — B7)
```

The Channel Profile does not introduce a new hierarchy layer. It deepens what a Channel **is**.

```
Channel (B1 entity)
│
├── Channel Core         channel_id, status, created_at, factory_id
│                        (immutable identity — defined in B1)
│
├── Channel Profile      ← B4.75 (this document)
│   │                    The strategic intelligence of the channel.
│   │                    Single source of truth for who, what, whom, why.
│   │
│   ├── Identity         name, description, niche, industry, language, region
│   ├── Audience         target_audience, demographics, interests, personas
│   ├── Brand            tone_of_voice, brand_identity, forbidden_patterns
│   ├── Visual           visual_identity, typography, color_palette
│   ├── Content          content_pillars, reference_channels, competitors
│   ├── Platform         platform_strategy
│   ├── Goals            publishing_goals, monetization_goals, learning_goals
│   ├── Rules            approval_rules, archive_policy
│   └── Metadata         version, last_updated, updated_by
│
├── Channel Config       prompt_pack, model assignments, provider routing
│                        (resolved at runtime by the Config Layer — B4)
│
└── Channel Budget       episode_ceiling_usd, credit budgets, retry policy
                         (enforced at run execution — B7)
```

**The key distinction:**

| Layer | Changes When | Consumed By |
|---|---|---|
| Channel Core | Never (immutable identity) | All systems |
| Channel Profile | Operator updates channel strategy | All agents (read) |
| Channel Config | Config Layer resolves template + channel + runtime | Agents (at run start) |
| Channel Budget | Finance / policy decisions | Run OS, cost guards |

---

## 3. V1 Hierarchy Rationale

> **Why Channel, not Media Entity?**

V2 will introduce the Media Entity as the parent of Channels:

```
V2:  Factory → Media Entities → Channels → Runs
```

V1 does not need this. In V1, one operator typically manages one primary channel per niche. The Channel itself is the business unit. Introducing Media Entity in V1 would add a governance layer with no operational benefit and would force every V1 operator to manage two entities before their first episode exists.

The Channel Profile is designed with V2 compatibility as a first-class concern (see §8). The migration path is non-destructive: when V2 arrives, the Channel Profile does not change — it gains a parent.

---

## 4. Channel Profile Schema

Every Channel Profile is stored as a versioned, structured document inside the channel record. All fields are required unless marked `optional`.

```
ChannelProfile
│
├── ── IDENTITY ────────────────────────────────────────────────────────
│
├── channel_id              string          required
│                           Stable, immutable slug. e.g., "hypogeum"
│                           Must match the parent Channel entity channel_id.
│
├── channel_name            string          required
│                           Display name. e.g., "The Hypogeum Chronicles"
│
├── channel_description     string          required
│                           One paragraph. Who the channel is, what it produces,
│                           and why it exists. Injected into every agent context
│                           as the opening frame. Max 300 words.
│
├── niche                   string          required
│                           Machine-readable content category.
│                           e.g., "archaeology-history"
│                           Used for multi-channel routing and template matching.
│
├── industry                string          required
│                           Business vertical. e.g., "education", "entertainment",
│                           "news-media", "finance", "wellness"
│                           Distinct from niche: niche is content, industry is market.
│
├── language                string          required
│                           BCP-47 code. e.g., "en", "es", "pt-BR"
│                           Primary production language.
│
├── region                  string[]        required
│                           Target geographic regions. e.g., ["US", "UK", "CA", "AU"]
│                           Used by publishing agent for geo-targeting and by
│                           research director for source selection.
│
├── ── AUDIENCE ────────────────────────────────────────────────────────
│
├── target_audience         string          required
│                           Primary audience description in plain language.
│                           e.g., "History enthusiasts aged 35–60 with an interest
│                           in ancient civilizations and unsolved mysteries."
│                           Injected into EP, RD, NA, SE agent prompts.
│
├── audience_demographics
│   ├── age_range           string          required
│   │                       e.g., "35–60"
│   ├── gender_split        string          optional
│   │                       e.g., "55% male, 45% female" (for seeding; updated by learning)
│   ├── education_level     string          optional
│   │                       e.g., "college-educated or higher"
│   ├── income_bracket      string          optional
│   │                       e.g., "middle to upper-middle income"
│   ├── viewing_device      string[]        optional
│   │                       e.g., ["desktop", "smart-tv", "mobile"]
│   └── viewing_context     string          optional
│                           e.g., "lean-back evening viewing"
│
├── audience_interests      string[]        required
│                           Ordered list of validated interests.
│                           e.g., ["ancient history", "archaeology", "mysteries",
│                                  "documentaries", "science", "travel"]
│                           Used by Research Director for topic scoring and
│                           by Analytics for audience signal interpretation.
│
├── audience_personas       Persona[]       optional
│                           Structured representations of audience archetypes.
│                           Each persona is a named profile with interests,
│                           motivations, and content preferences.
│
│   Persona schema:
│   ├── persona_id          string          e.g., "persona_explorer"
│   ├── persona_name        string          e.g., "The Explorer"
│   ├── description         string          Brief description of this archetype
│   ├── age_range           string
│   ├── primary_motivation  string          e.g., "Discovery and intellectual satisfaction"
│   ├── content_hook        string          What makes this persona click or stay
│   ├── pain_point          string          What they dislike in content (for avoidance)
│   └── weight              float           Relative proportion of audience. 0.0–1.0
│                                           All persona weights must sum to 1.0.
│
├── ── BRAND ───────────────────────────────────────────────────────────
│
├── tone_of_voice           ToneProfile     required
│   ├── primary             string          required
│   │                       e.g., "authoritative"
│   ├── secondary           string[]        required
│   │                       e.g., ["measured", "mysterious", "reverent"]
│   ├── register            string          required
│   │                       "formal" | "semi-formal" | "conversational" | "casual"
│   ├── forbidden_tones     string[]        required
│   │                       e.g., ["sensationalist", "comedic", "clickbait", "alarmist"]
│   └── voice_notes         string          optional
│                           Extended narrator voice guidance. Style directives
│                           injected into Script Engine and Narrative Architect prompts.
│
├── brand_identity          BrandIdentity   required
│   ├── persona_name        string|null     optional
│   │                       On-screen channel identity. e.g., "The Chronicler"
│   │                       null if the channel has no named host persona.
│   ├── brand_promise       string          required
│   │                       One sentence. What the channel commits to its audience.
│   │                       e.g., "Every story is grounded in evidence. Every episode
│   │                           earns its mystery."
│   ├── brand_values        string[]        required
│   │                       e.g., ["accuracy", "depth", "wonder", "credibility"]
│   ├── emotional_register
│   │   ├── primary         string          e.g., "awe"
│   │   ├── secondary       string[]        e.g., ["curiosity", "reverence"]
│   │   └── forbidden       string[]        e.g., ["fear", "outrage", "sentimentality"]
│   └── narrative_stance    string          required
│                           How the channel positions itself in relation to the story.
│                           e.g., "scholarly investigator" | "curious outsider" |
│                               "authoritative guide" | "impartial observer"
│
├── forbidden_patterns      ForbiddenPatterns   required
│   ├── topics              string[]        Topics never covered
│   │                       e.g., ["conspiracy theories", "supernatural claims without
│   │                              attribution", "modern political events"]
│   ├── phrases             string[]        Exact phrases never used
│   │                       e.g., ["mind-blowing", "you won't believe", "shocking truth"]
│   ├── visual_elements     string[]        Visual patterns never used
│   │                       e.g., ["jump cuts", "meme overlays", "stock footage logos"]
│   ├── production_patterns string[]        Production choices never made
│   │                       e.g., ["rapid-fire editing under 1s cuts", "reaction shots",
│   │                              "background music above -20db during speech"]
│   └── brand_violations    string[]        Brand-level hard stops (BGA enforcement)
│                           e.g., ["competitor branding in frame", "off-palette
│                                  color use in title cards"]
│
├── ── VISUAL IDENTITY ─────────────────────────────────────────────────
│
├── visual_identity         VisualIdentity  required
│   ├── style_descriptor    string          required
│   │                       One phrase. e.g., "cinematic dark archaeology"
│   ├── aesthetic_notes     string          required
│   │                       Extended visual style brief. Used by VPD for scene
│   │                       direction and by TD for thumbnail composition.
│   ├── lighting_style      string          optional
│   │                       e.g., "low-key, amber-gold, side-lit"
│   ├── camera_style        string          optional
│   │                       e.g., "slow dramatic push-ins, aerial establishing shots"
│   └── motion_style        string          optional
│                           e.g., "deliberate pacing, no fast cuts, slow dissolves"
│
├── typography              Typography      required
│   ├── primary             string          required
│   │                       Font name + weight + case rule.
│   │                       e.g., "Cinzel SemiBold (ALL CAPS)"
│   ├── secondary           string          required
│   │                       e.g., "Raleway Regular (sentence case)"
│   ├── usage_rules         string          optional
│   │                       When to use which font. e.g., "Cinzel for title cards
│   │                       only. Raleway for lower thirds and captions."
│   └── forbidden_fonts     string[]        optional
│                           Fonts explicitly banned. e.g., ["Comic Sans", "Papyrus"]
│
├── color_palette           ColorPalette    required
│   ├── colors              map<name, hex>  required
│   │                       Named colors. e.g.:
│   │                       "antique_gold": "#C49A42"
│   │                       "deep_obsidian": "#0A0A0A"
│   │                       "stone_grey": "#6E6E6E"
│   │                       "parchment": "#D4B896"
│   ├── primary_color       string          required
│   │                       Key name from colors map. e.g., "antique_gold"
│   ├── background_color    string          required
│   │                       Key name from colors map. e.g., "deep_obsidian"
│   ├── accent_color        string          optional
│   │                       Key name from colors map.
│   ├── usage_rules         string          optional
│   │                       Color application guidance for VPD, TD, BGA.
│   └── forbidden_colors    string[]        optional
│                           Hex values or color names never to appear in produced assets.
│
├── ── CONTENT STRATEGY ────────────────────────────────────────────────
│
├── content_pillars         ContentPillar[] required
│                           Ordered list of topic categories the channel produces.
│                           Each pillar constrains what is in-scope for episode generation.
│
│   ContentPillar schema:
│   ├── pillar_id           string          e.g., "ARCHAEOLOGY"
│   ├── pillar_name         string          e.g., "Archaeology"
│   ├── description         string          What this pillar covers
│   ├── weight              float           Relative production frequency. 0.0–1.0
│   │                                       All pillar weights must sum to 1.0.
│   └── examples            string[]        optional   Example episode topics
│
├── competitors             Competitor[]    optional
│                           Known competing channels. Used by Analytics to benchmark
│                           performance and by Research Director to avoid topic overlap
│                           or to identify coverage gaps.
│
│   Competitor schema:
│   ├── competitor_id       string          e.g., "comp_history_channel"
│   ├── channel_name        string          e.g., "The History Channel"
│   ├── platform            string          e.g., "youtube"
│   ├── platform_url        string|null     optional   External URL
│   ├── niche               string          e.g., "history-documentary"
│   ├── audience_overlap    string          "high" | "medium" | "low" | "unknown"
│   ├── differentiation     string          How this channel differs from the competitor
│   └── monitor             boolean         Whether Analytics should track this channel
│
├── reference_channels      ReferenceChannel[]  optional
│                           Aspirational channels used for tone, style, or production
│                           quality reference. Not competitors — benchmarks.
│
│   ReferenceChannel schema:
│   ├── ref_id              string
│   ├── channel_name        string
│   ├── platform            string
│   ├── platform_url        string|null     optional
│   ├── what_to_reference   string          Specific aspect: "thumbnail style",
│   │                                       "narration pacing", "visual aesthetic"
│   └── what_not_to_copy    string          optional   Guard against overcorrection
│
├── ── PLATFORMS ───────────────────────────────────────────────────────
│
├── platform_strategy       PlatformStrategy    required
│   ├── primary_platform    string          required
│   │                       The primary output platform.
│   │                       e.g., "youtube"
│   ├── enabled_platforms   enum[]          required
│   │                       All active platforms.
│   │                       e.g., ["youtube", "tiktok", "instagram", "email"]
│   ├── platform_configs    map<platform, PlatformConfig>   required per enabled platform
│   │
│   │   PlatformConfig schema (per platform):
│   │   ├── cadence         string          e.g., "weekly"
│   │   ├── day_of_week     string|null     e.g., "thursday"
│   │   ├── time_of_day     string|null     e.g., "18:00 UTC"
│   │   ├── format          string          "long_form" | "short_form" | "reel" | "newsletter"
│   │   ├── duration_target string|null     e.g., "12–18 minutes"
│   │   ├── default_tags    string[]        Applied to every upload on this platform
│   │   ├── ai_disclosure   boolean         Require AI disclosure in description
│   │   ├── auto_thumbnail  boolean         Auto-attach generated thumbnail
│   │   └── auto_captions   boolean         Auto-attach generated captions
│   │
│   └── cross_post_rules    string          optional
│                           How content is adapted when published across platforms.
│                           e.g., "YouTube → full episode; TikTok → hook clip
│                           from first 60s; Instagram → still + caption"
│
├── ── GOALS ───────────────────────────────────────────────────────────
│
├── publishing_goals        PublishingGoals required
│   ├── episodes_per_month  int             required
│   │                       Target episode output. e.g., 4
│   ├── growth_target       string          optional
│   │                       Subscriber/follower growth target.
│   │                       e.g., "10,000 subscribers by Q4 2026"
│   ├── engagement_target   string          optional
│   │                       e.g., "avg 8% engagement rate"
│   └── ctr_target          string          optional
│                           e.g., "avg 6% click-through rate on thumbnails"
│
├── monetization_goals      MonetizationGoals   optional
│   ├── revenue_model       string[]        e.g., ["adsense", "sponsorship",
│   │                                              "membership", "course"]
│   ├── cpm_target          float|null      optional   Target CPM in USD
│   ├── monthly_revenue_target string|null  optional   e.g., "$2,000/month by Q3 2026"
│   ├── sponsorship_policy  string          optional
│   │                       e.g., "Sponsor must be history/education/travel adjacent.
│   │                       No crypto, gambling, supplements."
│   └── membership_tier     string|null     optional
│                           Platform membership/community tier name if applicable
│
├── learning_goals          LearningGoals   required
│   ├── track_quality       boolean         required   Track BGA/QA pass rates
│   ├── track_cost          boolean         required   Track cost per episode
│   ├── track_audience      boolean         required   Track audience retention / engagement
│   ├── track_growth        boolean         required   Track subscriber/follower growth
│   ├── track_providers     boolean         optional   Track provider quality over time
│   ├── improvement_cadence string          required
│   │                       How often learning recommendations are reviewed.
│   │                       "weekly" | "monthly" | "quarterly" | "manual"
│   └── auto_apply_threshold string         required
│                           Minimum confidence level for auto-applying improvements.
│                           "high_confidence_only" | "medium_and_above" | "never"
│
├── ── RULES ───────────────────────────────────────────────────────────
│
├── approval_rules          ApprovalRules   required
│   ├── bga_enabled         boolean         Brand Guardian active
│   ├── bga_pass1_threshold string          "strict" | "standard" | "permissive"
│   ├── bga_pass2_threshold string          "strict" | "standard" | "permissive"
│   ├── qa_threshold        string          "strict" | "standard" | "permissive"
│   ├── compliance_required boolean         Compliance gate mandatory
│   ├── require_human_review boolean        Hold for human sign-off before publish
│   └── auto_publish        boolean         If false, every episode requires operator
│                                           approval before the publishing run triggers.
│
├── archive_policy          ArchivePolicy   required
│   ├── retain_raw_assets   boolean         required
│   │                       Keep source images, audio, video before compositing
│   ├── retain_intermediate boolean         required
│   │                       Keep intermediate pipeline outputs (scripts, scene plans)
│   ├── retention_days      int|null        optional
│   │                       Days to retain run artifacts. null = indefinite.
│   ├── compress_after_days int|null        optional
│   │                       Days after which assets are moved to cold storage.
│   └── delete_on_archive   boolean         required
│                           If channel is archived, delete production assets after
│                           retention_days. false = retain indefinitely.
│
└── ── METADATA ────────────────────────────────────────────────────────
    │
    ├── profile_version     string          Semver. e.g., "1.0.0"
    │                       Incremented on every profile save.
    │                       The run record captures the profile_version at run start.
    ├── created_at          datetime
    ├── last_updated        datetime
    ├── updated_by          string          User ID of last editor
    └── entity_id           string|null     V2 compatibility. Null in V1.
                                            Set when this channel is adopted by a
                                            V2 Media Entity. See §8.
```

---

## 5. Agent Consumption Model

The Channel Profile is not pushed to agents at run time — it is **resolved and injected** by the Config Layer (B4) into each agent's context at the start of a run. Each agent receives only the subset of the profile it needs.

This is the agent consumption map:

### 5.1 Executive Producer

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `channel_description` | Opening frame for all decisions |
| `content_pillars` | Constrains which topics are in-scope for this channel |
| `audience_interests` | Scores topic relevance against known audience interest |
| `tone_of_voice` | Sets the episode brief's emotional register |
| `brand_identity.brand_promise` | Ensures the episode brief honors the channel's commitment |
| `publishing_goals.episodes_per_month` | Sets production cadence expectation |
| `forbidden_patterns.topics` | Hard-stops for topic selection |

**Injection method:** Full profile summary block prepended to EP system prompt at run start.

---

### 5.2 Research Director

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `niche` | Scopes source search to relevant domains |
| `region` | Selects geographically relevant sources |
| `language` | Filters sources to primary production language |
| `audience_interests` | Ranks research threads by audience relevance |
| `audience_demographics` | Calibrates depth and technicality of sources |
| `content_pillars` | Ensures research stays within defined pillars |
| `forbidden_patterns.topics` | Hard-stops for research expansion |
| `competitors` (monitor=true only) | Prevents topic overlap with active competitors |

**Injection method:** Research framing block injected at start of each research task.

---

### 5.3 Narrative Architect

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `channel_description` | Frames the channel's narrative identity |
| `tone_of_voice` | Structures the narrative register |
| `brand_identity.narrative_stance` | Positions the narrator in relation to the story |
| `brand_identity.emotional_register` | Governs permitted and forbidden emotional beats |
| `audience_personas` | Structures the story arc for the primary persona weight |
| `forbidden_patterns.phrases` | Hard-stops for specific language patterns |

**Injection method:** Narrative voice block injected as prefix to story architecture prompt.

---

### 5.4 Script Engine

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `tone_of_voice` (full) | Complete voice profile for script generation |
| `brand_identity.persona_name` | On-screen identity for first-person passages |
| `brand_identity.voice_notes` (from tone_of_voice.voice_notes) | Extended narrator style directives |
| `forbidden_patterns.phrases` | Literal phrase exclusions enforced per sentence |
| `forbidden_patterns.topics` | Topic exclusions enforced at section level |
| `audience_demographics.education_level` | Calibrates vocabulary and explanation depth |
| `language` | Confirms output language |

**Injection method:** Voice style block injected into script generation system prompt. Forbidden phrases injected as a separate validation prompt after generation.

---

### 5.5 Visual Package Director

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `visual_identity.style_descriptor` | One-line scene style framing |
| `visual_identity.aesthetic_notes` | Extended visual brief for scene planning |
| `visual_identity.lighting_style` | Lighting constraints for image/video prompts |
| `visual_identity.camera_style` | Camera direction constraints |
| `visual_identity.motion_style` | Motion and cut pacing constraints |
| `color_palette` | Color constraints for scene composition prompts |
| `forbidden_patterns.visual_elements` | Visual hard-stops per scene |
| `forbidden_patterns.production_patterns` | Production-level hard-stops |

**Injection method:** Visual style guide injected as prefix to each scene generation prompt. Color palette injected as structured constraint block.

---

### 5.6 Thumbnail Director

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `visual_identity.style_descriptor` | Thumbnail visual style anchor |
| `color_palette` | Color rules enforced on thumbnail composition |
| `typography` | Font choices enforced on title card and overlay text |
| `brand_identity.emotional_register.primary` | Primary emotional hook for thumbnail |
| `forbidden_patterns.visual_elements` | Hard-stops for thumbnail visual choices |
| `forbidden_patterns.brand_violations` | Brand-level hard-stops (verified by BGA) |
| `reference_channels` (what_to_reference only) | Style benchmarks for thumbnail design |

**Injection method:** Thumbnail brief injected as prefix to thumbnail planning prompt.

---

### 5.7 Brand Guardian Agent

**What it needs from the Profile:**

The Brand Guardian receives the **complete visual and brand subset** of the profile. It is the only agent with write-back authority (it records pass/fail verdicts linked to the profile version that governed the run).

| Field | Why |
|---|---|
| `tone_of_voice` (full) | Verifies script and narration register |
| `brand_identity` (full) | Verifies all brand-level commitments |
| `visual_identity` (full) | Verifies visual output against style rules |
| `typography` (full) | Verifies font choices in all produced assets |
| `color_palette` (full) | Verifies color values in all produced assets |
| `forbidden_patterns` (full) | Enforces all exclusions — phrases, topics, visuals, brand |
| `approval_rules.bga_pass1_threshold` | Governs Pass 1 enforcement strictness |
| `approval_rules.bga_pass2_threshold` | Governs Pass 2 enforcement strictness |

**Injection method:** Full brand profile injected as structured evaluation rubric. BGA does not use free-form injection — it uses a typed evaluation schema derived from the Channel Profile.

---

### 5.8 Publishing Agent

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `platform_strategy.enabled_platforms` | Determines which platforms receive this episode |
| `platform_strategy.platform_configs` | Per-platform publishing rules |
| `platform_strategy.cross_post_rules` | How to adapt content per platform |
| `publishing_goals` | Validates cadence compliance |
| `approval_rules.auto_publish` | Determines whether to proceed or hold for approval |
| `monetization_goals.sponsorship_policy` | Validates that any sponsorship content complies |

**Injection method:** Platform config block injected as structured publishing directive. Not a prompt — a typed config object.

---

### 5.9 Analytics / Learning Agent

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `audience_demographics` | Interprets platform analytics against expected demographics |
| `audience_interests` | Weights analytics signals by audience relevance |
| `content_pillars` (with weights) | Benchmarks per-pillar performance |
| `competitors` (monitor=true) | Provides competitive benchmark data |
| `publishing_goals` | Targets against which performance is measured |
| `learning_goals` | Governs which signal types are tracked and at what cadence |
| `learning_goals.auto_apply_threshold` | Governs which improvements auto-apply |

**Injection method:** Goal and benchmark block injected into analytics context. Learning goals injected as structured policy object — not a prompt.

**Write-back:** Analytics / Learning is the only system with authority to write `audience_demographics` and `audience_interests` back to the Channel Profile, subject to approval via the Learning Layer (B6.5). All other agents are read-only on the profile.

---

### 5.10 Agent Director

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `approval_rules` (full) | Routes the run through the correct gate sequence |
| `learning_goals` | Determines which observation hooks are active |
| `archive_policy` | Governs artifact retention after run completion |
| `profile_version` | Captures which profile governed this run (immutable in run record) |

**Injection method:** Agent Director reads the profile directly via the Channel Manager API at run initialization. It does not receive a prompt injection — it uses the profile as a typed config object to set up the run.

---

### 5.11 Provider / Model Director

**What it needs from the Profile:**

| Field | Why |
|---|---|
| `channel_id` | Scopes all model assignments and provider selections to this channel |
| `visual_identity.style_descriptor` | Informs which image/video model is best suited for this channel's aesthetic |
| `language` | Constrains which LLM and voice models are valid for this channel |
| `region` | May constrain which providers are permitted (e.g., GDPR compliance by region) |

**Injection method:** Channel identity and language context injected as channel constraint block into provider selection logic. The Provider / Model Director is not a prompt-based agent — it reads the Channel Profile as a typed config to filter and rank provider options for this channel.

---

## 6. Dashboard Page — Channel Profile

The Channel Profile page is a section within the existing Channel Control Layer of the Dashboard (B5). It is not a separate page — it is a structured, multi-section editor within the channel detail view.

### Page Layout

```
CHANNEL DETAIL  /  The Hypogeum Chronicles  /  Profile
─────────────────────────────────────────────────────────────────────────
[Overview] [Audience] [Brand] [Visual Identity] [Content Strategy]
[Platforms] [Competitors] [Rules] [Goals] [Learning]
─────────────────────────────────────────────────────────────────────────
[Edit Profile]        [Export Profile]        [View Version History]
```

Every tab is read-only by default. **Edit Profile** enters a full-profile edit mode. Individual section editors are available per tab for focused edits.

---

### Tab 1 — Overview

**Purpose:** The operator's first view of the channel's identity. Confirms the channel is correctly described before running any episode.

**Displays:**
```
Channel ID:          hypogeum
Channel Name:        The Hypogeum Chronicles
Status:              active
Profile Version:     1.3.0   (last updated: 2026-06-10 by gawad)

DESCRIPTION
  Every episode investigates an underground ancient space. We combine
  verified archaeology with cinematic storytelling to bring the subterranean
  world to light for a global audience of history enthusiasts.

Niche:         archaeology-history
Industry:      education / entertainment
Language:      English (en)
Region:        US, UK, CA, AU

Profile Completeness:   ████████████░░  87%   (2 optional fields missing)
Last Run:               ep_003 — completed 2026-06-08
Episodes This Month:    2 / 4 (target)
```

---

### Tab 2 — Audience

**Purpose:** Defines and displays the full audience model. Learning agent may propose updates here.

**Displays:**
```
PRIMARY AUDIENCE
  History enthusiasts aged 35–60 with strong interest in ancient
  civilizations, unsolved mysteries, and evidence-based storytelling.

DEMOGRAPHICS
  Age Range:         35–60
  Gender Split:      55% male / 45% female   [Learning: updated 2026-06-01]
  Education:         College-educated or higher
  Viewing Device:    Smart TV (primary), Desktop
  Viewing Context:   Lean-back evening viewing

INTERESTS (ordered by signal strength)
  1. Ancient history      ████████████████  (strong)
  2. Archaeology          ███████████████   (strong)
  3. Mysteries            ████████████      (medium)
  4. Documentaries        ████████████      (medium)
  5. Science              ████████          (moderate)
  6. Travel               ██████            (moderate)

PERSONAS
  The Explorer (45%)    — Motivated by discovery and learning
  The Skeptic (35%)     — Wants evidence and rigor
  The Armchair Traveler (20%) — Attracted by visual richness
```

**Actions:** Edit audience, Accept / Reject pending Learning proposals.

---

### Tab 3 — Brand

**Purpose:** Defines the channel's voice, values, and identity commitments.

**Displays:**
```
BRAND PROMISE
  "Every story is grounded in evidence. Every episode earns its mystery."

NARRATIVE STANCE:   Scholarly investigator

TONE OF VOICE
  Primary:       Authoritative
  Secondary:     Measured, mysterious, reverent
  Register:      Semi-formal
  Forbidden:     Sensationalist, comedic, clickbait, alarmist

EMOTIONAL REGISTER
  Primary:       Awe
  Secondary:     Curiosity, reverence
  Forbidden:     Fear, outrage, sentimentality

BRAND VALUES
  Accuracy · Depth · Wonder · Credibility

FORBIDDEN PATTERNS
  Topics:     Conspiracy theories, supernatural claims w/o attribution,
              modern political events
  Phrases:    "mind-blowing", "you won't believe", "shocking truth"
```

---

### Tab 4 — Visual Identity

**Purpose:** Defines the complete visual rule set consumed by VPD, TD, and BGA.

**Displays:**
```
STYLE
  Descriptor:    cinematic dark archaeology
  Aesthetic:     Low-key dramatic lighting. Amber and gold color temperature.
                 Slow camera movements. Archaeological textures — stone, earth,
                 metal. No modern elements in hero shots.
  Lighting:      Low-key, amber-gold, side-lit
  Camera:        Slow dramatic push-ins, aerial establishing shots
  Motion:        Deliberate pacing, no fast cuts, slow dissolves

COLOR PALETTE
  ■ Antique Gold      #C49A42   (primary)
  ■ Deep Obsidian     #0A0A0A   (background)
  ■ Stone Grey        #6E6E6E   (secondary)
  ■ Parchment         #D4B896   (accent)
  Forbidden:          Any neon, blue-tones in hero shots, white backgrounds

TYPOGRAPHY
  Primary:       Cinzel SemiBold (ALL CAPS) — title cards only
  Secondary:     Raleway Regular (sentence case) — lower thirds, captions
  Forbidden:     Comic Sans, Papyrus, Impact

FORBIDDEN VISUAL ELEMENTS
  ■ Jump cuts
  ■ Meme overlays
  ■ Stock footage logos visible in frame
```

---

### Tab 5 — Content Strategy

**Purpose:** Defines what the channel produces and at what proportions.

**Displays:**
```
CONTENT PILLARS
  ████████████████████  ARCHAEOLOGY  (45%)
    Underground spaces, excavation sites, ancient tools, site discoveries

  ████████████████      HISTORY  (35%)
    Civilization context, period overview, historical figures

  ████████             MYSTERY  (20%)
    Unresolved questions, contested theories — always evidence-first

REFERENCE CHANNELS
  ■ Nexpo — thumbnail composition reference
    "Use: dark atmospheric thumbnails. Avoid: horror-genre tone."
  ■ Veritasium — narrative pacing reference
    "Use: deliberate build. Avoid: science-lab aesthetic."
```

---

### Tab 6 — Platforms

**Purpose:** Defines where the channel publishes and how.

**Displays:**
```
PRIMARY PLATFORM:    YouTube

ENABLED PLATFORMS
  ■ YouTube       long_form     weekly / Thursday / 18:00 UTC
  ■ TikTok        short_form    weekly / Friday   / 12:00 UTC
  ■ Instagram     reel          weekly / Friday   / 14:00 UTC

CROSS-POST RULES
  YouTube → full episode
  TikTok → hook clip from first 60 seconds
  Instagram → still frame + caption

  AI Disclosure:   Required on all platforms
  Auto Thumbnail:  Enabled
  Auto Captions:   Enabled
```

---

### Tab 7 — Competitors

**Purpose:** Defines the competitive landscape for research and analytics.

**Displays:**
```
MONITORED COMPETITORS
  The History Channel        youtube    audience_overlap: medium
    "Our differentiation: evidence-first. No dramatization or reenactments."

  Smithsonian Channel        youtube    audience_overlap: low
    "Our differentiation: specifically underground archaeology vs. general history."

REFERENCE CHANNELS  (not competitors — benchmarks)
  See Content Strategy tab.
```

---

### Tab 8 — Rules

**Purpose:** Defines all governance rules that agents and gates enforce.

**Displays:**
```
APPROVAL RULES
  Brand Guardian:      ✓ ENABLED
  BGA Pass 1:          strict
  BGA Pass 2:          standard
  QA Threshold:        standard
  Compliance Required: ✓ YES
  Human Review:        ✓ REQUIRED before publish
  Auto-Publish:        ✗ DISABLED

ARCHIVE POLICY
  Retain Raw Assets:      ✓ YES
  Retain Intermediate:    ✓ YES
  Retention Days:         indefinite
  Compress After:         90 days
  Delete on Archive:      ✗ NO
```

---

### Tab 9 — Goals

**Purpose:** Defines the strategic targets the channel is working toward.

**Displays:**
```
PUBLISHING GOALS
  Episodes / Month:     4
  Growth Target:        10,000 subscribers by Q4 2026
  Engagement Target:    avg 8% engagement rate
  CTR Target:           avg 6% click-through rate

MONETIZATION GOALS
  Revenue Model:        AdSense · Sponsorship
  Monthly Target:       $2,000 / month by Q3 2026
  Sponsorship Policy:   "History / education / travel adjacent only.
                         No crypto, gambling, supplements."

  CPM Target:           $12.00 USD
```

---

### Tab 10 — Learning

**Purpose:** Defines how the channel learns and improves, and shows pending proposals.

**Displays:**
```
LEARNING CONFIGURATION
  Track Quality:         ✓ YES    (BGA pass rates, QA verdicts)
  Track Cost:            ✓ YES    (cost per episode trend)
  Track Audience:        ✓ YES    (retention, engagement, CTR)
  Track Growth:          ✓ YES    (subscriber velocity)
  Track Providers:       ✗ NO
  Review Cadence:        monthly
  Auto-Apply Threshold:  high_confidence_only

PENDING LEARNING PROPOSALS
  ──────────────────────────────────────────────────────────
  [2026-06-10]  Audience Demographics Update
  Confidence: HIGH    Evidence: 3 episodes / 4,200 sessions
  Proposed: gender split → 60% male / 40% female
  [Accept]  [Reject]  [View Evidence]

  [2026-06-08]  Content Pillar Reweight
  Confidence: MEDIUM  Evidence: Pillar CTR analysis
  Proposed: MYSTERY weight → 25%, HISTORY → 30%
  [Accept]  [Reject]  [View Evidence]
```

---

## 7. Risks and Mitigations

### Risk 1 — Channel Profile becomes too broad

**Failure mode:** The Channel Profile tries to be the single source of truth for everything, and grows to 200+ fields. Agents receive bloated context that degrades output quality. Maintenance burden makes the profile impossible to keep current.

**Mitigation:**
- The schema defined in §4 is the V1 ceiling. No field may be added without a design review.
- Each agent in §5 receives only a **named subset** of the profile — not the full document. The Config Layer (B4) is responsible for field-level projection per agent.
- Profile completeness scoring (shown in the Overview tab) rewards completion of required fields, not optional ones. Optional fields must justify their cost before being added.

---

### Risk 2 — Duplicated brand rules between Channel Profile and Template

**Failure mode:** A Template defines a visual style and tone. A Channel derived from that template redefines them in the Channel Profile. Agents receive conflicting directives.

**Mitigation:**
- The Config Layer (B4) defines a clear resolution order:
  ```
  Runtime Override > Channel Profile > Channel Config > Template Default > Factory Default
  ```
- Template values are **defaults**. Channel Profile values are **overrides**. The Channel Profile always wins over the Template for any field it explicitly sets.
- A field is only considered "explicitly set" if it is non-null and non-empty. An unpopulated Channel Profile field falls through to the template default — it does not override with a blank.
- The Dashboard profile editor shows inherited template values in grey alongside channel overrides in black. Operators see exactly what they are overriding.

---

### Risk 3 — Conflicts between Channel Profile and runtime config

**Failure mode:** An operator sets `auto_publish: false` in the Channel Profile but passes `auto_publish: true` in a run-level override. Agents behave inconsistently across runs.

**Mitigation:**
- Runtime overrides are permitted **only for non-safety fields**. Fields that govern safety, compliance, and approval flow (`bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish`) may **not** be overridden at runtime.
- The Config Layer enforces this at resolution time. Any attempt to override a safety-classified field via runtime config is rejected and logged.
- The Run OS captures the full resolved config at run initialization — including the Channel Profile version and all applicable overrides — before the first agent task executes.

---

### Risk 4 — Conflicts between Channel Profile and template defaults on derived channels

**Failure mode:** A channel cloned from a Template inherits template defaults. The operator never sets Channel Profile fields. The factory runs with a half-populated channel profile — template defaults bleed into agent context with no channel-specific override.

**Mitigation:**
- Channel creation (from template or clone) triggers a **Profile Completion Gate**. A channel cannot be set to `active` until it meets a minimum completeness threshold on required fields (as distinct from optional fields).
- Required fields are explicitly marked in §4. A channel with any required field null cannot produce a run.
- The Dashboard profile completeness score (Overview tab) shows completion as a percentage and lists missing required fields.

---

### Risk 5 — Forbidden patterns conflict across profile layers

**Failure mode:** The Template defines `forbidden_patterns.topics` as a small exclusion list. The Channel adds its own exclusions. An agent receives the template's list but not the channel's extensions, because the Config Layer took the channel value as an override rather than a union.

**Mitigation:**
- `forbidden_patterns` fields are **union-merged**, not replaced, by the Config Layer. The resolved forbidden list is always the superset of all applicable layers.
- This merge behavior applies only to list-typed fields that are additive by nature: `forbidden_patterns.topics`, `forbidden_patterns.phrases`, `forbidden_patterns.visual_elements`, `forbidden_patterns.production_patterns`, `forbidden_patterns.brand_violations`.
- Scalar fields (e.g., `tone_of_voice.primary`) are replaced by the resolution priority order.
- The Config Layer documents this behavior explicitly. Any field that should be union-merged is annotated `merge: union` in the config resolution spec.

---

### Risk 6 — Future V2 Media Entity migration disrupts V1 channels

**Failure mode:** V2 is designed with a different schema for audience, brand, or content strategy. Every V1 Channel Profile must be migrated and the migration breaks existing agent consumption patterns.

**Mitigation:** See §8 (V2 Migration Path). The V2 compatibility design is included in V1 to prevent this class of migration failure.

---

## 8. V2 Migration Path

V2 introduces the Media Entity as the parent of Channels:

```
V2 Hierarchy:
Factory
└── Media Entities    (business entities: a brand, a show, a creator)
    └── Channels      (distribution channels: YouTube, TikTok, podcast)
        └── Runs
```

The Channel Profile is designed to survive this transition without rewriting agent consumption logic.

### 8.1 Future `entity_id` Field

Every Channel Profile in V1 includes:

```
entity_id           string|null     V2 compatibility.
                    Null in V1. Set when this channel is adopted by a V2 Media Entity.
```

When V2 migration runs:
1. A Media Entity record is created for each logical brand group.
2. Each Channel's `entity_id` is populated with the parent Media Entity ID.
3. No other field in the Channel Profile changes.
4. Agent consumption patterns are unchanged — agents still read the Channel Profile.

### 8.2 Profile Inheritance in V2

In V2, the Media Entity will carry its own profile fields — the entity-level audience, brand, and goals that all channels under it share. The resolution order extends to:

```
V2 Resolution Order:
Runtime Override
> Channel Profile (channel-specific overrides)
> Media Entity Profile (entity-level defaults)
> Template Default
> Factory Default
```

Channel Profile fields that are null in V1 become the natural override points in V2. An operator who never set `channel_description` in V1 will inherit the Media Entity's description in V2 without a migration step.

### 8.3 Entity-Level Audience vs. Channel-Level Overrides

In V2:

| Field | Media Entity Level | Channel Level |
|---|---|---|
| `target_audience` | Shared audience description across all channels | Channel-specific refinement (e.g., "YouTube audience skews older; TikTok skews 18–34") |
| `audience_demographics` | Baseline demographics for the brand | Platform-specific demographic override |
| `audience_interests` | Core brand interests | Platform-specific interest ranking |
| `audience_personas` | Brand-level archetypes | Channel-specific persona weights |
| `content_pillars` | Entity-level pillar definitions | Channel-level pillar weights (a TikTok channel may weight MYSTERY higher) |
| `brand_identity` | Shared brand | Channel-specific tone adaptation |
| `visual_identity` | Entity-level visual system | Channel-specific visual adaptation |
| `competitors` | Entity-level competitive awareness | Channel-level platform-specific competitors |
| `monetization_goals` | Entity-level revenue strategy | Channel-level monetization config |

V1 Channel Profiles carry all of these at the channel level. V2 promotes the shared subset to the Media Entity and the channel retains only the channel-specific overrides. The data does not move — the resolution layer changes.

### 8.4 What V2 Does Not Require

V2 migration **does not require:**
- Rewriting agent consumption logic
- Changing the Channel Profile schema (only adding `entity_id`)
- Migrating existing channel run history
- Changing how the Config Layer resolves profiles (only adding a new layer above Channel)
- Changing the Dashboard Channel Profile page (only adding an Entity Profile page above it)

The V2 migration is an additive operation, not a replacement. Every V1 Channel Profile becomes a valid V2 Channel Profile on day one of V2 with `entity_id: null`. Entity adoption is incremental — channels can be adopted into entities one at a time.

---

## 9. Integration with Prior Phases

| Phase | How Channel Profile Integrates |
|---|---|
| B1 — Channel Definition | Channel Profile is the strategic layer within the Channel entity defined in B1. B1 defines the technical schema; B4.75 defines the business schema. Together they form the complete Channel. |
| B2 — Channel Clone Engine | Cloning a channel copies the Channel Profile in full (less `channel_id`, `channel_name`, `entity_id`). The operator reviews and customizes the profile in the clone before activating it. |
| B3 — Template System | Templates define profile defaults. Channel Profile overrides them per the Config Layer resolution order. Forbidden pattern fields are union-merged, not replaced. |
| B3.5 — Factory Entity | The Factory owns all Channel Profiles. Factory-level governance (cost ceilings, module availability) applies as an outer constraint on Channel Profile rules. |
| B4 — Configuration Layer | The Config Layer resolves the complete agent context at run start, projecting only the relevant Channel Profile subset per agent. The Channel Profile is the input; the resolved per-agent context is the output. |
| B4.5 — Module System | The `learning` and `analytics` modules must be enabled for the Learning tab (Dashboard) to show proposals. The `brand_guardian` module must be enabled for `approval_rules.bga_enabled` to have effect. |
| B5 — Dashboard | Channel Profile page is a section within the Channel Control Layer defined in B5. The ten-tab structure defined in §6 is the dashboard surface for the Channel Profile. |
| B6 — Agent OS | Each agent in §5 defines a **capability** in the Agent OS. The Channel Profile subset they receive is part of each capability's input contract. |
| B6.5 — Learning Layer | Analytics / Learning is the only system with write-back authority to `audience_demographics` and `audience_interests`. All write-backs require a Learning Layer approval. |
| B7 — Run OS | Each Run captures `profile_version` at initialization. The Run record is an immutable snapshot of which Channel Profile governed it. |

---

## 10. Open Questions (V1 Deferred)

These questions are noted but deferred to V2 or a future design phase:

1. **Multi-language channels** — If a channel produces in both English and Spanish, does it have one Channel Profile with `language: ["en", "es"]`, or two Channel Profiles, one per language? Deferred to V2.

2. **Audience persona auto-generation** — Should the Learning Layer be able to propose new personas (not just update existing ones)? This requires human approval before a new persona becomes active. Deferred to B6.5 learning intelligence evolution.

3. **Competitor signal ingest** — How does the Analytics agent actually ingest competitor data (views, engagement, topic coverage)? The schema supports competitor monitoring as a flag; the data pipeline to ingest competitor signals is not designed here.

4. **Brand Profile versioning and rollback** — When a Channel Profile is updated, existing in-progress runs continue against the profile version they started with. But what happens if a run fails after a profile update mid-run? The Run OS should capture profile version at start and never re-resolve mid-run. Confirm this invariant in B7.

5. **Sponsorship content policy enforcement** — `monetization_goals.sponsorship_policy` is a free-text field. Who enforces it? A future Compliance Agent could do a policy check at the publishing gate. Not designed here.
