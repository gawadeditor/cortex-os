# Hypogeum Long-Term Memory Architecture

**Status:** Design document — for review and approval before implementation  
**Date:** 2026-06-08  
**Scope:** Defines the complete architecture for durable asset memory, episode lifecycle management, cross-episode knowledge, and storage across the Mac Mini execution layer and the Synology long-term memory layer.

---

## Purpose

The pipeline currently produces an episode and forgets it. Each run starts cold: no memory of previous assets, no continuity, no cost avoidance, no entity consistency. This document defines the architecture that changes that.

The goal is not a backup system. It is a **memory system** — one that allows the factory to:

- Know what it has already generated (and reuse it)
- Know what it has already said (and stay consistent)
- Know what entities it has depicted (and render them consistently)
- Know what everything cost (and avoid redundant spend)
- Restore any past episode to an active state without loss

The Mac Mini is where episodes are **made**. The Synology is where they are **remembered**.

---

## 1. Mac Mini Execution Layer

The Mac Mini is the hot execution environment. All pipeline agents run here. All active workspace files live here during production.

### Role

- Runs `run_pipeline()` for every episode
- Holds all active workspace files in `.hypogeum-assets/`
- Holds the hot catalog state (JSONL files updated on every run)
- Holds the episode registry (current state for all episodes)
- Syncs completed episodes to Synology (push model)

### What lives permanently on Mac Mini

| Path | Contents | Authoritative? |
|------|----------|----------------|
| `.hypogeum-assets/catalogs/` | Hot JSONL catalogs (all asset types, entities) | Yes — until sync |
| `.hypogeum-assets/registry/` | Episode registry | Shared with Synology |
| `.hypogeum-assets/episodes/{episode_id}/` | Active workspace files | Yes — during Active/Review states |
| `hypogeum/` | Pipeline code | Yes |

### What gets thinned from Mac Mini after archival

After an episode reaches **Archived** state, large binary files can be removed from Mac Mini to reclaim disk space. Records (JSON agent outputs) are kept permanently. Binaries are kept on Synology.

Thinnable:
- `main_{episode_id}.mp4`
- `short_{i}_{episode_id}.mp4`
- `narration/*.mp3`
- `clips/*.mp4` (source clips from MUAPI)
- `music/*.mp3`

Not thinnable (kept permanently on Mac Mini):
- All agent output `*.json` records
- `episode_asset_index_{episode_id}.json`
- Thumbnail `.jpg` files (small, high reuse value)
- `archive_record_{episode_id}.json`
- `asset_archive_record_{episode_id}.json`

---

## 2. Synology Long-Term Memory Layer

The Synology NAS is the cold/warm persistent memory layer. It stores everything the factory has ever produced, searchably, for years.

### Role

- Canonical source of truth for all archived episodes
- Stores binary assets after Mac Mini thinning
- Stores the canonical episode registry
- Stores catalog snapshots per episode (versioned)
- Supports search and retrieval by agents and humans
- Provides restore packages on demand

### Access model

The Synology is accessed from the Mac Mini exclusively. No agents run on Synology. It is a storage target, not a compute target.

**Unresolved design decision #1 — Synology access protocol:**  
Options: rsync over SSH, SMB mount, NFS mount, Synology Drive API, or custom push agent.  
This choice affects sync latency, atomicity guarantees, and error recovery. **Requires approval before implementation.**

### What lives permanently on Synology

| Path | Contents |
|------|----------|
| `registry/episode_registry.jsonl` | Canonical multi-episode registry |
| `catalogs/` | Catalog snapshots (per-sync point) |
| `episodes/{episode_id}/artifacts/` | All agent output JSON records |
| `episodes/{episode_id}/binaries/` | Video, audio, thumbnail binary files |
| `episodes/{episode_id}/workspace_snapshot/` | Point-in-time workspace state at archive |
| `archive/{year}/{month}/{episode_id}/` | Final cold-tier layout after deep archival |

---

## 3. Active Workspace Lifecycle

Every episode progresses through exactly five states. State transitions are recorded in the episode registry.

```
Active → Review → Published → Archive Queue → Archived
```

No episode skips states. No state can be entered unless the previous state's exit conditions are met.

---

### 3.1 State: Active

**Definition:** The episode is being produced. The pipeline is running or paused mid-run.

**Entry condition:** `run_pipeline()` called with this `episode_id`.

**Exit condition:** All 15 pipeline stages complete. `PipelineResult.status == "completed"`.

**Workspace state (Mac Mini):**
```
.hypogeum-assets/episodes/{episode_id}/
├── validation_scorecard_{episode_id}.json
├── topic_brief_{episode_id}.json
├── research_package_{episode_id}.json
├── narrative_package_{episode_id}.json
├── script_package_{episode_id}.json
├── finance_authorization_{episode_id}.json
├── visual_package_{episode_id}.json
├── thumbnail_package_{episode_id}.json
├── final_render_package_{episode_id}.json
├── qa_approval_report_{episode_id}.json
├── compliance_report_{episode_id}.json
├── publishing_record_{episode_id}.json     (absent until stage 12)
├── archive_record_{episode_id}.json        (absent until stage 13)
├── asset_archive_record_{episode_id}.json  (absent until stage 14)
├── finance_record_{episode_id}.json        (absent until stage 15)
├── episode_asset_index_{episode_id}.json   (absent until stage 14)
├── clips/                                  (MUAPI source clips)
├── narration/                              (ElevenLabs audio)
├── music/                                  (Music tracks)
├── thumbnails/                             (Generated thumbnails)
└── renders/
    ├── main_{episode_id}.mp4
    └── short_{i}_{episode_id}.mp4
```

**Registry entry `episode_status`:** `active`

**Allowed operations:** Pipeline agents may read and write. No other process may write.

---

### 3.2 State: Review

**Definition:** Generation is complete. The episode is awaiting human review before publishing, or review by a future automated review agent.

**Entry condition:** `PipelineResult.status == "completed"` AND  
  `qa_approval_report.verdict` ∈ {`APPROVED`, `WARNING`} AND  
  `compliance_report.verdict` ∈ {`APPROVED`, `WARNING`}

**Exit condition:** Human approval recorded in registry OR automated review agent clears the episode.

**Workspace state:** Same as Active. No files added or removed.

**Registry entry `episode_status`:** `review`

**Allowed operations:** Human may edit metadata (title, description, tags). No regeneration. No pipeline re-run without explicit reset to Active.

**Notes:** This state does not exist yet in the pipeline. The current pipeline runs straight through to publishing without a review gate. This state is a planned addition for unattended production mode.

---

### 3.3 State: Published

**Definition:** The episode has been distributed to at least one platform. `publishing_record.package_metadata.overall_status` ∈ {`published`, `partially_published`}.

**Entry condition:** Publishing Agent completes with non-failed overall_status.

**Exit condition:** Episode is marked for archival (manually or by Archive Queue trigger).

**Workspace state:** All files from Active state, plus:
```
publishing_record_{episode_id}.json
archive_record_{episode_id}.json
asset_archive_record_{episode_id}.json
finance_record_{episode_id}.json
episode_asset_index_{episode_id}.json
```

**Registry entry `episode_status`:** `published`

**Allowed operations:** Read-only for all agents. Human may annotate performance data. No re-run without explicit reset.

---

### 3.4 State: Archive Queue

**Definition:** The episode is queued for sync to Synology. Mac Mini workspace is intact. Synology sync has not yet completed.

**Entry condition:** Manual trigger OR automatic trigger (configurable N days post-publish, or on storage pressure threshold).

**Exit condition:** Synology sync confirmed complete with checksum validation.

**Workspace state:** Same as Published. No changes until sync completes.

**Registry entry `episode_status`:** `archive_queue`

**Allowed operations:** Read-only. No writes. Sync process reads binaries and records.

**Unresolved design decision #2 — Archive Queue trigger:**  
Options: (a) automatic N days after publish, (b) manual only, (c) triggered by disk usage threshold on Mac Mini, (d) triggered by episode count threshold.  
**Requires approval before implementation.**

---

### 3.5 State: Archived

**Definition:** All episode artifacts are confirmed present on Synology. Large binaries may be thinned from Mac Mini.

**Entry condition:** Synology sync complete AND checksum validation pass for all required files.

**Exit condition:** Restore lifecycle is triggered (see §5).

**Workspace state on Mac Mini (after thinning):**
```
.hypogeum-assets/episodes/{episode_id}/
├── [all *.json agent output records — kept]
├── episode_asset_index_{episode_id}.json
├── thumbnails/                             [kept — small, high reuse]
└── [video, audio, clip binaries — removed]
```

**Workspace state on Synology:**
```
episodes/{episode_id}/
├── artifacts/     [all *.json records — full copy]
├── binaries/      [all video, audio, clip binaries]
└── metadata.json  [episode summary for registry queries]
```

**Registry entry `episode_status`:** `archived`

**Allowed operations on Mac Mini:** Read records and thumbnails. Cannot play video locally without restore.

**Unresolved design decision #3 — Thinning policy:**  
Is thinning automatic on archive confirmation, or manual? Does it thin all binaries or only renders (keeping source clips for potential reuse)?  
**Requires approval before implementation.**

---

## 4. Project Archive Lifecycle

A **project** is a logical grouping of episodes (e.g., a series, a calendar month, or a milestone batch). Project archival is the process of organizing archived episodes into the cold-tier directory structure on Synology.

### Project Archive States

```
Active Episodes → Archived Episodes → Project Bundle → Cold Tier
```

**Project Bundle:** When a set of related episodes are all in `Archived` state, they are bundled into a project manifest and moved into the cold-tier layout on Synology:
```
/volume1/hypogeum/archive/{year}/{month}/{bundle_id}/
├── bundle_manifest.json      (episode list, checksums, creation date)
├── {episode_id_1}/
│   ├── artifacts/
│   └── binaries/
└── {episode_id_2}/
    ├── artifacts/
    └── binaries/
```

**Cold Tier:** Long-term immutable storage. No writes. Only reads via restore.

**Unresolved design decision #4 — Project bundling trigger:**  
What constitutes a project? Options: (a) calendar month, (b) episode count threshold (e.g., every 10 episodes), (c) manual grouping, (d) topic series.  
**Requires approval before implementation.**

---

## 5. Restore Lifecycle

Restore returns an episode from Archived state to a working state on Mac Mini. This is needed when:
- An episode needs to be re-published (takedown and reupload)
- An episode needs to be updated (revised description, new captions)
- An asset from the episode needs to be reused in a new episode
- Debugging requires access to the original pipeline artifacts

### Restore Stages

```
1. Locate  →  2. Pull  →  3. Validate  →  4. Restore State  →  5. Declare Active
```

**1. Locate:** Query episode registry by `episode_id` or metadata search. Returns Synology path.

**2. Pull:** Copy artifacts from Synology to Mac Mini workspace. Scope is configurable:
  - `full` — all binaries + records
  - `records_only` — JSON records only, no binaries
  - `binaries_only` — video/audio only (for reuse without re-running agents)
  - `selective` — named asset_ids only (for asset-level reuse)

**3. Validate:** Checksum all pulled files against the stored checksums in `bundle_manifest.json`.

**4. Restore State:** Set `episode_status` in registry to `restored` (distinct from `active` to prevent accidental pipeline re-run).

**5. Declare Active:** If a full re-run is needed, operator explicitly resets status to `active`. This is a manual step — never automatic.

**Unresolved design decision #5 — Checksum algorithm:**  
Options: MD5 (fast, not cryptographic), SHA-256 (standard, slower), BLAKE3 (fast + secure).  
**Requires approval before implementation.**

---

## 6. Registry Architecture

The **episode registry** is the single index of every episode the factory has ever run. It is the entry point for all lifecycle management, search, and retrieval.

### Registry Format

`episode_registry.jsonl` — append-only JSONL, one line per episode state change.

Each line is a **registry event** (not a snapshot). The current state is derived by reading the latest event for a given `episode_id`.

```json
{
  "event_id": "evt_abc123",
  "episode_id": "ep_042",
  "event_type": "status_changed",
  "from_status": "published",
  "to_status": "archive_queue",
  "recorded_at": "2026-06-08T14:00:00+00:00",
  "operator": "pipeline",
  "metadata": {
    "episode_title": "The Antikythera Mechanism",
    "topic_brief_id": "tb_xyz",
    "publish_date": "2026-06-05T10:00:00+00:00",
    "youtube_video_id": "dQw4w9WgXcQ",
    "total_cost_usd": 1.47
  }
}
```

### Registry Locations

| Location | Role | Write authority |
|----------|------|-----------------|
| Mac Mini `.hypogeum-assets/registry/episode_registry.jsonl` | Hot registry | Pipeline and operator tools |
| Synology `registry/episode_registry.jsonl` | Canonical registry | Sync from Mac Mini |

**Unresolved design decision #6 — Registry sync direction:**  
Is the Mac Mini registry authoritative (push to Synology), or is Synology authoritative (pull to Mac Mini)? This determines conflict resolution if a sync gap occurs.  
Recommended: Mac Mini is authoritative for current state; Synology is authoritative for archived state. Events after `archived` status are written to both simultaneously.  
**Requires approval before implementation.**

### Registry Query Model

The registry supports the following query patterns:
- By `episode_id` → current status + full event history
- By `episode_status` → all episodes in a given state
- By `episode_title` (substring match) → matching episodes
- By `publish_date` range → episodes published in a window
- By `topic_category` → episodes by subject area (requires metadata enrichment)

---

## 7. Asset Catalog Architecture

The asset catalog is a set of JSONL files that index every generated asset across all episodes. It lives on Mac Mini (hot) and is snapshotted to Synology on every archive sync.

### Catalog Files

| File | Contents | One record per |
|------|----------|----------------|
| `asset_catalog.jsonl` | Master index — every asset of every type | Asset |
| `clip_catalog.jsonl` | Detailed video clip records | Video clip |
| `thumbnail_catalog.jsonl` | Detailed thumbnail records | Thumbnail |
| `music_catalog.jsonl` | Detailed music track records | Music track |

### Master Asset Record (`asset_catalog.jsonl`)

```json
{
  "asset_id": "str",
  "asset_type": "clip | thumbnail | music | narration | caption | reference_image",
  "source_episode_id": "str",
  "provider": "muapi | elevenlabs | channel_library | internal",
  "model": "str",
  "prompt": "str",
  "seed": "str | null",
  "cost_usd": 0.0,
  "duration_seconds": null,
  "resolution": "1920x1080 | null",
  "entities": [],
  "locations": [],
  "historical_period": "str | null",
  "style_tags": [],
  "reusable": true,
  "indexed_at": "ISO 8601",
  "file_path_mac": "str | null",
  "file_path_synology": "str | null",
  "checksum_sha256": "str | null",
  "metadata": {}
}
```

**Notes on fields not yet populated by current pipeline:**
- `provider`, `model`, `prompt`, `seed` — require VPD to pass generation provenance through to Assembly (not currently done)
- `cost_usd` per asset — requires Finance Agent to break down per-asset cost (currently only totals)
- `entities`, `locations`, `historical_period` — require scene-level metadata from VPD
- `checksum_sha256` — requires Asset Archive Agent to hash files at write time
- `file_path_synology` — populated after archive sync

The current Asset Archive Agent (partially implemented before this document was written) populates a subset of these fields. Full field coverage is deferred to M7.2.

### Clip Record (`clip_catalog.jsonl`)

```json
{
  "clip_id": "str",
  "asset_id": "str",
  "source_episode_id": "str",
  "clip_type": "main_episode | short",
  "scene_id": "str",
  "derivation_source": "str",
  "provider": "muapi | internal",
  "model": "str",
  "prompt": "str",
  "duration_seconds": null,
  "resolution": "1920x1080",
  "entities": [],
  "historical_period": "str | null",
  "indexed_at": "ISO 8601",
  "reusable": true
}
```

### Thumbnail Record (`thumbnail_catalog.jsonl`)

```json
{
  "thumbnail_id": "str",
  "asset_id": "str",
  "source_episode_id": "str",
  "thumbnail_type": "main | short",
  "short_id": "str | null",
  "format": "jpg | png",
  "resolution": "1280x720",
  "prompt": "str",
  "indexed_at": "ISO 8601",
  "reusable": true
}
```

### Music Record (`music_catalog.jsonl`)

```json
{
  "music_id": "str",
  "log_id": "str",
  "asset_id": "str",
  "source_episode_id": "str",
  "source": "channel_library | ai_generated",
  "category": "Mystery | Discovery | Investigation | ...",
  "section_key": "str",
  "duration_seconds": null,
  "indexed_at": "ISO 8601",
  "reusable": true
}
```

**Unresolved design decision #7 — Catalog format:**  
JSONL is simple and human-readable but slow for large catalogs (full scan for every query). At 50+ episodes with 30+ clips each = 1,500+ clip records. JSONL linear scan is acceptable at this scale. At 500+ episodes, a SQLite backing store would be faster.  
Recommendation: Start with JSONL, add a SQLite index layer at the M7.3 milestone when query performance matters.  
**Requires approval before implementation.**

**Unresolved design decision #8 — Asset deduplication strategy:**  
Should identical binary files (same content, different asset_id) be detected and merged? This requires content-hash comparison at index time.  
Options: (a) no deduplication (simple, wastes storage), (b) SHA-256 hash on write, merge on match (correct, adds write overhead).  
**Requires approval before implementation.**

---

## 8. Entity Catalog Architecture

The entity catalog tracks every named entity the factory has depicted across all episodes. It is the foundation for visual consistency (same rendering of recurring figures) and knowledge graph construction.

### Entity Record (`entity_catalog.jsonl`)

```json
{
  "entity_id": "str",
  "canonical_name": "str",
  "entity_type": "person | place | object | concept | event | institution",
  "source_episodes": ["ep_001", "ep_007"],
  "lock_state": "provisional | promoted | final",
  "reference_image_asset_id": "str | null",
  "visual_description": "str | null",
  "historical_period": "str | null",
  "aliases": [],
  "first_seen_at": "ISO 8601",
  "last_seen_at": "ISO 8601"
}
```

### Upsert Semantics

The entity catalog uses **upsert** (update-or-insert) rather than append. Because the same entity may appear in multiple episodes, there is exactly one record per `entity_id`. On each new episode:

- If `entity_id` exists: `source_episodes` is extended, `last_seen_at` updated, `lock_state` promoted if the new episode provides a stronger lock.
- If `entity_id` is new: a fresh record is appended.

After any upsert, the entity_catalog.jsonl is rewritten in-place. This is the only catalog that is not strictly append-only.

### Entity Identity and Resolution

**Unresolved design decision #9 — Entity ID assignment:**  
Currently, `entity_id` values come directly from the Visual Package Director's locked_entities output. VPD generates these IDs; they are not globally canonical.  
A persistent entity ID must be stable across all episodes. Two options:  
  (a) VPD-assigned IDs are the canonical IDs — requires VPD to be consistent (it currently is not guaranteed to be)  
  (b) A separate Entity Resolution step assigns canonical IDs — matches VPD-assigned IDs to known entities, handles synonyms and aliases  
Option (b) is required for M8.1 Knowledge Graph Agent.  
**Requires approval before implementation.**

**Unresolved design decision #10 — Entity type taxonomy:**  
The entity types listed above are a first draft. A canonical, closed-set taxonomy is needed before implementation.  
**Requires approval before implementation.**

---

## 9. Search and Retrieval Architecture

Search allows agents and operators to find assets and episodes by content, not just by ID. This is what makes the catalog useful.

### Query Operations Required

| Query | Input | Output | Catalog scanned |
|-------|-------|--------|-----------------|
| By asset_id | asset_id string | Single asset record | asset_catalog.jsonl |
| By entity | entity_id | All assets depicting that entity | asset_catalog.jsonl |
| By source episode | episode_id | All assets from that episode | asset_catalog.jsonl |
| By asset type | "clip", "thumbnail", etc. | All assets of that type | asset_catalog.jsonl |
| Reusable candidates | asset_type + filters | Reusable assets matching criteria | asset_catalog.jsonl |
| Entity lookup | entity_id or canonical_name | Entity record | entity_catalog.jsonl |
| Episode lookup | episode_id or title | Episode record + status | episode_registry.jsonl |

### Query Implementation (Current: Linear Scan)

At current scale (1–50 episodes), all queries are implemented as linear scans over JSONL files. No index required. This is the correct approach for M7.2.

### Query Implementation (Future: Index Layer)

At scale (50+ episodes), linear scan over asset_catalog.jsonl becomes slow. A query index layer is needed. Two options:

**Option A — SQLite backing store:**  
On every catalog write, a SQLite database at `.hypogeum-assets/catalogs/index.db` is updated with the same records. Queries run against SQLite. JSONL files remain the append-only source of truth; SQLite is rebuilt from JSONL on corruption.

**Option B — In-memory index at query time:**  
Load the full JSONL into a Python dict at the start of each agent run. No persistent index. Acceptable up to ~100K records.

Recommendation: Option A at M7.3, not before.

**Unresolved design decision #11 — Vector search for reuse matching:**  
The Reuse Recommendation Engine (M7.3) needs to match scene descriptions to existing clips semantically (not just by exact entity or tag match). This requires vector embeddings.  
Options: (a) OpenAI embeddings + cosine similarity over stored vectors, (b) local embeddings (sentence-transformers), (c) no semantic search (tag + entity matching only).  
This is a significant scope decision.  
**Requires approval before implementation.**

---

## 10. Reuse Recommendation Engine Integration

The Reuse Recommendation Engine (M7.3) is not an agent that runs in the current pipeline. It is a **service** called by the Visual Package Director during Phase 1 (scene planning), before Phase 2 (generation).

### Integration Point

```
VPD Phase 1 (scene plan) → Reuse Recommendation Engine → VPD Phase 2 (generation)
```

For each planned scene, VPD Phase 1 produces a scene brief (entities, period, prompt). The Reuse Recommendation Engine queries the asset catalog and returns:

```json
{
  "scene_id": "sc_007",
  "recommendation": "reuse | generate",
  "candidate_asset_id": "clip_abc123 | null",
  "confidence_score": 0.87,
  "match_basis": ["entity:Archimedes", "period:Ancient_Greece", "style:cinematic"],
  "cost_avoided_usd": 0.15,
  "recommendation_reason": "str"
}
```

VPD uses this output to decide whether to skip generation for a scene.

### Matching Criteria (Priority Order)

1. **Exact entity match** — asset depicts the same named entity as the scene brief
2. **Period + style match** — asset is from the same historical period with compatible visual style
3. **Prompt similarity** — asset prompt is semantically similar to the scene brief (requires vector search)
4. **Tag match** — asset has overlapping style/category tags

### Reusability Policy

An asset is eligible for reuse if:
- `reusable == true` in asset_catalog
- Not already used in the same episode (prevents exact duplicate footage in one episode)
- Not used more than N times across all episodes (prevents over-repetition — N is configurable)

**Unresolved design decision #12 — Reuse cap per asset:**  
What is the maximum number of times a clip can be reused across different episodes before it is retired?  
**Requires approval before implementation.**

---

## 11. Future Graphify Integration Point

Graphify is listed as a deferred capability in the roadmap. Based on context, it is assumed to be a knowledge graph construction tool or service that consumes entity and relationship data from the pipeline.

### Assumed Role

Graphify receives structured entity/relationship data from the pipeline and builds a persistent, queryable knowledge graph. This graph powers:
- Topic discovery (what adjacent topics have not been covered?)
- Continuity checking (has the factory contradicted itself about this claim?)
- Relationship mapping (which entities are connected, how strongly?)

### Integration Architecture

The Graphify integration point is the **Knowledge Graph Agent (M8.1)**. That agent reads from:
- `entity_catalog.jsonl` — all known entities
- `research_package_{episode_id}.json` — claims, sources, relationships
- `narrative_package_{episode_id}.json` — narrative structure and entity emphasis
- `script_package_{episode_id}.json` — exact claims made in narration

The Knowledge Graph Agent writes to Graphify's ingestion API (or file format). The format and API are not yet defined.

### Design Constraints

- The entity catalog (§8) must use canonical entity IDs that Graphify can recognise. This is the same requirement as design decision #9.
- The relationship schema must be agreed between hypogeum and Graphify before M8.1 implementation.
- Graphify may run on Synology (as a background service) or on Mac Mini. This is unresolved.

**Unresolved design decision #13 — Graphify definition:**  
What is Graphify exactly? Is it an external tool, an internal agent, a graph database (Neo4j, etc.), or a file format?  
**This question must be answered before M8.1 can be scoped.**

---

## 12. Storage Layout on Synology

### Volume Structure

```
/volume1/hypogeum/
├── registry/
│   └── episode_registry.jsonl
│
├── catalogs/
│   ├── asset_catalog.jsonl
│   ├── entity_catalog.jsonl
│   ├── clip_catalog.jsonl
│   ├── thumbnail_catalog.jsonl
│   ├── music_catalog.jsonl
│   └── snapshots/
│       └── {YYYY-MM-DD}/
│           ├── asset_catalog.jsonl
│           └── entity_catalog.jsonl
│
├── episodes/
│   └── {episode_id}/
│       ├── metadata.json
│       ├── artifacts/
│       │   ├── validation_scorecard_{episode_id}.json
│       │   ├── topic_brief_{episode_id}.json
│       │   ├── research_package_{episode_id}.json
│       │   ├── narrative_package_{episode_id}.json
│       │   ├── script_package_{episode_id}.json
│       │   ├── finance_authorization_{episode_id}.json
│       │   ├── visual_package_{episode_id}.json
│       │   ├── thumbnail_package_{episode_id}.json
│       │   ├── final_render_package_{episode_id}.json
│       │   ├── qa_approval_report_{episode_id}.json
│       │   ├── compliance_report_{episode_id}.json
│       │   ├── publishing_record_{episode_id}.json
│       │   ├── archive_record_{episode_id}.json
│       │   ├── asset_archive_record_{episode_id}.json
│       │   ├── finance_record_{episode_id}.json
│       │   └── episode_asset_index_{episode_id}.json
│       └── binaries/
│           ├── renders/
│           │   ├── main_{episode_id}.mp4
│           │   └── short_{i}_{episode_id}.mp4
│           ├── clips/
│           │   └── {asset_id}.mp4
│           ├── narration/
│           │   └── {segment_id}.mp3
│           ├── music/
│           │   └── {track_id}.mp3
│           └── thumbnails/
│               └── {asset_id}.jpg
│
└── archive/
    └── {YYYY}/
        └── {MM}/
            └── {bundle_id}/
                ├── bundle_manifest.json
                └── {episode_id}/        (symlinked from episodes/)
```

### Catalog Snapshots

Catalog snapshots are point-in-time copies of the shared JSONL catalogs, taken once per archive sync cycle. They allow rollback if catalog corruption is detected. Snapshots older than 90 days may be pruned.

### `metadata.json` per Episode

A lightweight per-episode summary for quick registry queries without reading full artifact JSON:

```json
{
  "episode_id": "ep_042",
  "episode_title": "The Antikythera Mechanism",
  "status": "archived",
  "published_at": "2026-06-05T10:00:00+00:00",
  "archived_at": "2026-06-08T09:00:00+00:00",
  "total_cost_usd": 1.47,
  "youtube_video_id": "abc123",
  "asset_count": 18,
  "synology_path": "/volume1/hypogeum/episodes/ep_042"
}
```

---

## 13. Storage Layout on Mac Mini

```
/Users/gawadeditor/hypogeum-brain/
├── hypogeum/                              (pipeline code — git tracked)
├── tests/
├── docs/
│
└── .hypogeum-assets/
    ├── registry/
    │   └── episode_registry.jsonl
    │
    ├── catalogs/
    │   ├── asset_catalog.jsonl
    │   ├── entity_catalog.jsonl
    │   ├── clip_catalog.jsonl
    │   ├── thumbnail_catalog.jsonl
    │   └── music_catalog.jsonl
    │
    └── episodes/
        └── {episode_id}/
            │
            ├── [agent output *.json records — permanent]
            ├── episode_asset_index_{episode_id}.json
            │
            ├── thumbnails/                [permanent — small, reuse value]
            │   └── {asset_id}.jpg
            │
            ├── renders/                   [thinnable after archive]
            │   ├── main_{episode_id}.mp4
            │   └── short_{i}_{episode_id}.mp4
            │
            ├── clips/                     [thinnable after archive]
            │   └── {asset_id}.mp4
            │
            ├── narration/                 [thinnable after archive]
            │   └── {segment_id}.mp3
            │
            └── music/                     [thinnable after archive]
                └── {track_id}.mp3
```

**Notes:**
- `.hypogeum-assets/` is not git-tracked (in `.gitignore`)
- `catalogs/` and `registry/` live at the `.hypogeum-assets/` level — shared across all episodes
- Individual episode directories contain both the current pipeline output structure and the new lifecycle metadata (episode_asset_index, archive_record)

**Unresolved design decision #14 — .hypogeum-assets location:**  
Currently hardcoded relative to the pipeline source root. If the source code moves (new machine, new path), `.hypogeum-assets/` must move with it or the path must be configurable via environment variable.  
Recommendation: Add `HYPOGEUM_ASSETS_ROOT` env var, defaulting to the current relative path.  
**Requires approval before implementation.**

---

## 14. Metadata Ownership and Synchronization Rules

### Ownership Table

| Metadata type | Owned by | Written by | Read by |
|---------------|----------|------------|---------|
| Episode registry events | Mac Mini (hot) → Synology (canonical) | Pipeline agents + operator tools | All agents, sync service |
| Agent output records (`*.json`) | Mac Mini until archived; Synology after | Pipeline agents only | All agents, Restore lifecycle |
| Asset catalog (JSONL) | Mac Mini (hot) | Asset Archive Agent | Reuse Recommendation Engine, KG Agent |
| Entity catalog (JSONL) | Mac Mini (hot), upserted by Asset Archive Agent | Asset Archive Agent | KG Agent, VPD (entity continuity) |
| Catalog snapshots | Synology | Sync service | Restore lifecycle, audit |
| Episode `metadata.json` | Synology | Sync service (derived from registry + artifacts) | Search queries |
| Bundle manifests | Synology | Sync service | Restore lifecycle |
| Checksums | Generated on Mac Mini at archive time | Archive sync service | Restore lifecycle (validation) |

### Synchronization Rules

**Rule 1 — Catalogs are authoritative on Mac Mini until archived.**  
Synology catalog snapshots are read-only historical records, not the live working catalog. Agents always read from Mac Mini.

**Rule 2 — Synology is authoritative for Archived episode binaries.**  
Once an episode is in `Archived` state and its binaries have been thinned from Mac Mini, Synology is the only copy. No write operations on Synology binary files.

**Rule 3 — Registry sync is append-only in both directions.**  
Registry events are never deleted or modified, only appended. Sync merges event logs by `event_id`. A conflict (same `event_id`, different content) is a data corruption event and must halt sync with an alert.

**Rule 4 — Sync is atomic per episode.**  
A sync operation either completes all files for an episode or none. Partial syncs are detected by the absence of the terminal sentinel file (`metadata.json`). Partial syncs are retried, not left in a broken state.

**Rule 5 — Agent output records are immutable after sealing.**  
Once an agent writes a `*.json` record and the pipeline stage completes, that record is immutable. No agent may overwrite another agent's output. Pipeline resume (checkpoint) reads existing records but never overwrites them.

**Rule 6 — Entity catalog rewrites are transactional.**  
Because the entity catalog uses rewrite (not append), concurrent writes would corrupt it. The Asset Archive Agent is the only writer of the entity catalog. No other agent may write to it. Reads are always allowed.

**Unresolved design decision #15 — Sync conflict resolution:**  
If the Mac Mini catalog and Synology catalog diverge (e.g., a sync gap during a run), which takes precedence? Mac Mini should always win for current-state data, but a merge strategy for historical records must be defined.  
**Requires approval before implementation.**

**Unresolved design decision #16 — Multi-machine scenarios:**  
This architecture assumes a single Mac Mini. If a second machine is introduced (e.g., for parallel episode production), the catalog architecture breaks — two machines would append to separate local JSONL files. This would require Synology to become the authoritative catalog store, not a replica.  
This scenario should be explicitly out of scope for M7.2 but must be noted as a design constraint.  
**Requires approval before implementation.**

---

## Summary of Unresolved Design Decisions

The following decisions require approval before any implementation work begins.

| # | Decision | Options | Impact |
|---|----------|---------|--------|
| 1 | Synology access protocol | rsync/SSH, SMB, NFS, Synology Drive API | Sync service implementation |
| 2 | Archive Queue trigger | Manual, N days post-publish, disk threshold, episode count | Trigger logic implementation |
| 3 | Mac Mini thinning policy | Automatic on archive confirmation vs manual | Thin service implementation |
| 4 | Project bundling trigger | Calendar month, episode count, manual, series | Bundle logic implementation |
| 5 | Checksum algorithm | MD5, SHA-256, BLAKE3 | File validation at all lifecycle stages |
| 6 | Registry sync direction | Mac Mini authoritative, Synology authoritative, bidirectional merge | Registry sync service |
| 7 | Catalog format at scale | JSONL (current), SQLite index, hybrid | Query performance at 50+ episodes |
| 8 | Asset deduplication | None, SHA-256 content hash | Storage efficiency, write overhead |
| 9 | Entity ID assignment | VPD-assigned, canonical resolver | Entity continuity across episodes |
| 10 | Entity type taxonomy | First draft in §8; closed-set required | Entity catalog schema |
| 11 | Vector search for reuse matching | OpenAI embeddings, local embeddings, tag-only | Reuse Recommendation Engine scope |
| 12 | Reuse cap per asset | N uses maximum; N to be defined | Reuse recommendation logic |
| 13 | Graphify definition | External tool, internal agent, graph DB, file format | M8.1 scoping |
| 14 | HYPOGEUM_ASSETS_ROOT configurability | Env var vs hardcoded path | Portability, multi-machine future |
| 15 | Sync conflict resolution | Mac Mini wins, Synology wins, merge strategy | Sync service robustness |
| 16 | Multi-machine scope | In-scope for M7.2 or explicitly deferred | Architecture constraints |

---

## What Is Not This Document

This document does not define:
- Pipeline agent implementation details (schemas, checks, verdicts)
- The Publication Archive Recorder (`archive_agent.py`) — that is already implemented and is not M7.2
- The Asset Archive Agent implementation — that is deferred pending this document's approval
- The sync service implementation — that is a separate design document
- Content identity rules — that is `docs/channel-identity.md`
- The Knowledge Graph schema — that is a Graphify integration document

---

## Implementation Sequence (Pending Approval of All Design Decisions)

Once all 16 decisions are resolved, the recommended implementation sequence is:

```
1.  HYPOGEUM_ASSETS_ROOT env var  (Design decision #14)
2.  Episode registry JSONL + writer  (foundation for all lifecycle management)
3.  Asset Archive Agent (M7.2)  (catalogs, episode index — depends on registry)
4.  Archive sync service  (depends on decisions #1, #2, #3, #5, #6)
5.  Restore lifecycle  (depends on sync service)
6.  Reuse Recommendation Engine (M7.3)  (depends on M7.2 + decision #11, #12)
7.  Knowledge Graph Agent (M8.1)  (depends on M7.2 + decision #9, #10, #13)
```

Nothing in this sequence should be started until the design decisions it depends on are approved.
