# Platform Connector Governance Design

**Phase:** B5.10  
**Depends on:** B5.9 (Credential Manager), B5.275 (OIS), B5.35 (Notification System), B5.5 (Review System), B5.8 (Analytics Layer), B4.75 (Channel Profile)  
**Feeds into:** Publishing Pipeline, Analytics Layer, OIS (connector signals), Dashboard (connector health)

---

## 1. Purpose

The Platform Connector Governance layer governs how Factory OS interacts with external publishing platforms. Where the Credential Manager stores and resolves authentication material, the Connector Governance layer governs the publishing protocol: which connectors exist, how healthy they are, what their platform-specific rules are, how failures are retried, and how cross-channel isolation is enforced.

The Connector Governance layer does not execute publishing directly. The Publishing Pipeline calls the Connector Governance layer to initiate, monitor, and record publishing operations. The Connector Governance layer orchestrates the actual platform API calls through individual platform connector implementations.

### 1.1 Scope

**In scope (B5.10):** Publishing connectors (YouTube, TikTok, Instagram/Meta), connector lifecycle governance, publishing quota management, platform metadata validation rules, retry policy, deduplication, multi-platform publishing orchestration, client and channel isolation, connector health monitoring.

**Out of scope (B5.10):**
- Analytics connectors (governed by B5.8 Analytics Layer)
- Credential storage and OAuth flows (governed by B5.9 Credential Manager)
- Publishing pipeline execution logic (governed by Publishing Pipeline)
- The actual HTTP calls to platform APIs (those are in connector implementations)
- Webhook connectors (inbound — deferred to V2)

---

## 2. Architecture

### 2.1 Connector types

Three connector types are defined. Only publishing connectors are in scope for V1.

| Connector type | Purpose | V1 status |
|---|---|---|
| `publishing_connector` | Push content to external platforms | In scope |
| `analytics_connector` | Pull performance data from platforms | Governed by Analytics Layer (B5.8) |
| `webhook_connector` | Receive inbound events from platforms | Deferred to V2 |

### 2.2 Inputs

| Input | What it provides |
|---|---|
| Publishing Pipeline | Publishing requests: episode_id, channel_id, target_platforms, content package |
| Credential Manager | Connector readiness status, resolved credentials (in memory, at call time) |
| Channel Profile | `platform_strategy` (enabled platforms, platform configs, cross-post rules) |
| OIS | Signal routing for connector health events |
| Analytics Layer | Analytics connector governance (read-only reference for platform API session reuse rules) |
| Dashboard | Connector health display, connector suspension controls |

### 2.3 Outputs

| Output | Destination |
|---|---|
| Publishing results (success/failure per platform) | Publishing Pipeline, Review System |
| `connector_signal` | OIS (for health routing) |
| `publishing_event` records | Analytics Layer (for ingestion into episode analytics), audit trail |
| `connector_health_status` | Dashboard, Channel Activation gate |
| `quota_status` | Dashboard, Publishing Pipeline (pre-publish check) |
| `retry_schedule` | Internal (Connector Governance manages its own retry queue) |

---

## 3. Connector Registry

Every platform connector is registered in the Connector Registry. A registry entry defines the connector's identity, capabilities, and governing rules.

### 3.1 ConnectorRegistryEntry schema

```
ConnectorRegistryEntry {
  connector_type_id:         string            // e.g., "youtube_v3", "tiktok_v2", "instagram_graph"
  platform:                  string            // e.g., "youtube", "tiktok", "instagram"
  display_name:              string            // e.g., "YouTube"
  api_version:               string
  supported_formats:         enum[]            // long_form, short_form, reel, story
  credential_type_required:  string            // from Credential Manager (e.g., "oauth_connection")
  credential_domain:         string            // e.g., "platform_publishing"
  metadata_rules:            MetadataRules     // platform-specific content limits
  quota_rules:               QuotaRules        // daily/hourly publishing limits
  rate_limit_rules:          RateLimitRules    // API rate limits
  retry_policy:              RetryPolicy       // default retry behavior
  supports_scheduling:       boolean           // native scheduled publishing
  supports_thumbnail_upload: boolean
  supports_captions:         boolean
  requires_ai_disclosure:    boolean           // platform requires AI content disclosure
  health_check_endpoint:     string            // lightweight health ping
  deduplication_key:         string[]          // fields used to detect duplicate publish attempts
}
```

### 3.2 Registered connectors (V1)

**YouTube (youtube_v3)**

```
MetadataRules {
  title_max_chars:            100
  description_max_chars:      5000
  tags_max_count:             500
  tags_max_total_chars:       500
  thumbnail_required:         false (recommended)
  thumbnail_max_size_mb:      2
  video_max_size_gb:          128
  supported_video_formats:    [mp4, mov, avi, wmv, flv, mkv]
  max_duration_minutes:       720
  category_required:          true
  privacy_default:            "public"
}

QuotaRules {
  daily_upload_limit:         6           // API quota units cap
  daily_quota_units:          10000       // YouTube Data API v3 daily quota
  upload_cost_units:          1600        // units per upload
  metadata_update_cost_units: 50
  thumbnail_upload_cost_units: 50
  quota_reset_utc:            "00:00"
  quota_warning_threshold:    0.75        // warn when 75% quota used
}

RetryPolicy {
  transient_errors:           [500, 502, 503, 504]
  max_retries:                3
  retry_delays_seconds:       [300, 900, 1800]  // 5min, 15min, 30min
  rate_limit_retry_after:     "Retry-After header"
  auth_error_behavior:        "escalate_immediately"   // never retry auth errors
  content_error_behavior:     "escalate_no_retry"       // never retry 400 content errors
}
```

**TikTok (tiktok_v2)**

```
MetadataRules {
  description_max_chars:      4000
  hashtags_max_count:         20          // effective limit (no hard cap, soft guidance)
  video_max_duration_seconds: 600         // 10 minutes for eligible accounts
  video_max_size_mb:          4096
  supported_video_formats:    [mp4, mov]
  cover_image_required:       false
  external_links_in_desc:     false       // restricted for non-business accounts
}

QuotaRules {
  daily_upload_limit:         50          // approximate; platform-enforced, not quota-unit
  quota_reset_utc:            "00:00"
  quota_warning_threshold:    0.70
}

RetryPolicy {
  transient_errors:           [500, 502, 503]
  max_retries:                3
  retry_delays_seconds:       [300, 900, 1800]
  auth_error_behavior:        "escalate_immediately"
  content_error_behavior:     "escalate_no_retry"
}
```

**Instagram / Meta (instagram_graph)**

```
MetadataRules {
  caption_max_chars:          2200
  hashtags_max_count:         30
  reel_max_duration_seconds:  90
  reel_min_duration_seconds:  3
  video_max_size_mb:          1024        // for reels
  supported_video_formats:    [mp4]
  cover_frame_required:       false
  aspect_ratio:               "9:16"      // required for reels
}

QuotaRules {
  daily_post_limit:           25          // Meta Graph API limit
  quota_reset_utc:            "00:00"
  quota_warning_threshold:    0.70
}

RetryPolicy {
  transient_errors:           [500, 503]
  max_retries:                3
  retry_delays_seconds:       [300, 900, 1800]
  auth_error_behavior:        "escalate_immediately"
  content_error_behavior:     "escalate_no_retry"
}
```

---

## 4. Connector Lifecycle

### 4.1 Connector states per channel-platform pair

A connector state is maintained per `(channel_id, platform)` pair. The same platform can have different connector states for different channels.

| State | Meaning |
|---|---|
| `disconnected` | No valid credential exists for this platform for this channel |
| `connected` | Credential is valid, platform API is reachable, no active publishing |
| `publishing` | An active publishing operation is in progress |
| `rate_limited` | A rate limit was hit; the connector is waiting for the reset window before retrying |
| `retry_pending` | A transient error occurred; retry is scheduled |
| `degraded` | Platform API is returning errors but credential is valid (platform-side issue) |
| `suspended` | Factory-side suspension — operator or Admin has explicitly disabled publishing to this platform for this channel |
| `quota_exhausted` | Daily publishing quota reached; publishing resumes at quota reset |
| `platform_error` | Platform-side permanent error (not credential, not rate limit) — requires investigation |

### 4.2 State transitions

```
disconnected
  └─→ connected          (credential added and tested — via Credential Manager)

connected
  ├─→ publishing         (publish operation initiated by Publishing Pipeline)
  ├─→ suspended          (operator suspends publishing)
  ├─→ degraded           (health check fails but credential valid)
  └─→ disconnected       (credential expires or is revoked)

publishing
  ├─→ connected          (publish succeeded)
  ├─→ rate_limited       (429 received during publish)
  ├─→ retry_pending      (transient error during publish)
  ├─→ platform_error     (permanent error — content rejected)
  └─→ degraded           (platform API degraded mid-publish)

rate_limited
  └─→ connected          (rate limit window elapsed)

retry_pending
  ├─→ connected          (retry succeeded)
  ├─→ rate_limited       (retry hit another 429)
  └─→ platform_error     (max retries exhausted)

degraded
  ├─→ connected          (platform API recovers, health check passes)
  └─→ disconnected       (credential becomes invalid during degradation)

suspended
  └─→ connected          (operator resumes publishing)

quota_exhausted
  └─→ connected          (quota resets at UTC midnight)

platform_error
  └─→ connected          (operator investigates, resolves, and manually resets connector)
```

`platform_error` is the only state that requires manual operator intervention to exit. It is not automatically resolved. The operator investigates the failure, corrects the issue (re-upload with corrected metadata, contact platform support, etc.), and clicks [Reset Connector] on the dashboard to return to `connected`.

---

## 5. Metadata Validation

Before any publish operation is initiated, the Connector Governance layer validates the episode's metadata against the target platform's rules. Validation failures block publishing without making an API call — they are caught before the platform sees the content.

### 5.1 Validation checks

| Check | Fields validated | Failure behavior |
|---|---|---|
| Title length | title char count vs. `title_max_chars` | Block publish — truncation not automatic |
| Description length | description char count vs. `description_max_chars` | Block publish |
| Tag count | tag count vs. `tags_max_count` | Block publish if over limit |
| Tag total characters | sum of all tag chars vs. `tags_max_total_chars` | Block publish |
| Video format | file extension vs. `supported_video_formats` | Block publish |
| Video file size | file size vs. `video_max_size_gb` or `video_max_size_mb` | Block publish |
| Caption/hashtag count | hashtag count in caption vs. `hashtags_max_count` | Warn (not block) — platform soft limits |
| AI disclosure | if `platform.requires_ai_disclosure` and description does not contain disclosure text | Block publish |
| Category ID | if category_required and category not set | Block publish |
| Aspect ratio (reels) | video dimensions vs. `aspect_ratio` | Block publish |
| Duration limits | video duration vs. `max_duration_minutes` / `reel_max_duration_seconds` | Block publish |

### 5.2 Validation failures

When validation fails, the Connector Governance layer returns a `ValidationFailure` record to the Publishing Pipeline, which routes it to the Review System as a `gate_failure` item (if already in review) or prevents the episode from advancing past the metadata validation checkpoint.

The Publishing Pipeline does not attempt the API call if validation has failed. The episode is returned with the specific validation errors for operator review — similar to a gate failure but at the publishing stage rather than the production stage.

**Truncation is not automatic.** Truncating a title or description without operator review could change meaning. The operator must explicitly fix the metadata before re-attempting publication.

### 5.3 Validation vs. platform enforcement

Validation is a pre-publish check based on known platform rules. Platform rules change occasionally without notice. When a platform rejects content for a rule violation that the Connector Governance layer did not catch (because the rule changed), the response is treated as a `platform_error` rather than a validation failure. The Platform Connector Governance rules are updated when rule changes are detected.

---

## 6. Publishing Quota Management

### 6.1 Quota tracking per channel-platform pair

Quota is tracked per `(channel_id, platform)` pair, matching how platforms enforce quotas (per authenticated connection, which is per channel).

The Connector Governance layer maintains a `QuotaRecord` for each active channel-platform pair:

```
QuotaRecord {
  channel_id:            string
  platform:              string
  quota_date:            date          // current quota day (UTC)
  quota_used:            int           // units used today (YouTube: quota units; others: count)
  quota_limit:           int           // from ConnectorRegistryEntry
  quota_reset_at:        timestamp     // next quota reset
  warning_threshold:     float         // from ConnectorRegistryEntry
  status:                enum(ok, warning, exhausted)
}
```

### 6.2 Pre-publish quota check

Before initiating any publish operation, the Connector Governance layer checks the current `QuotaRecord`:
- If `status = ok`: proceed
- If `status = warning`: proceed, but emit a `connector_signal` (WARNING) to OIS
- If `status = exhausted`: block publish, emit CRITICAL `connector_signal`, return error to Publishing Pipeline with `retry_at = quota_reset_at`

### 6.3 Quota consumption tracking

After each successful platform API operation, the Connector Governance layer updates the `QuotaRecord`:
- YouTube: deducts the API quota units consumed by the operation (1600 for upload, 50 for metadata update, 50 for thumbnail)
- Other platforms: increments the daily operation count

If a publish attempt fails before the platform processes it (timeout, network error), no quota is consumed and the quota record is not updated.

### 6.4 Quota warning and exhaustion signals

| Threshold | Signal to OIS | Severity |
|---|---|---|
| `quota_used >= warning_threshold × quota_limit` | `quota_warning` | WARNING |
| `quota_used >= quota_limit` | `quota_exhausted` | CRITICAL |
| Quota reset (day rollover) | `quota_reset` (auto-resolution) | INFO |

---

## 7. Publishing Orchestration

### 7.1 Multi-platform publishing

When an episode is approved for publishing to multiple platforms, the Connector Governance layer initiates independent publishing operations for each enabled platform. Platform publishing operations are independent — a failure on one platform does not block or cancel publishing on other platforms.

**Orchestration flow:**

```
Publishing Pipeline calls initiate_publishing(episode_id, channel_id, target_platforms, content_package)

For each platform in target_platforms (in parallel):
  1. Check connector state (must be: connected)
  2. Check credential readiness (via Credential Manager)
  3. Validate metadata against platform rules
  4. Check quota (must not be: exhausted)
  5. Initiate platform API call via connector implementation
  6. Track result: success / rate_limited / retry_pending / platform_error

Results are collected independently.
Publishing Pipeline is notified of the aggregate outcome.
```

**Aggregate outcomes:**

| Outcome | All platforms | Meaning |
|---|---|---|
| `all_published` | All ✓ | Complete success |
| `partial_published` | Mix of ✓ and ✗ | Some platforms succeeded; failed platforms may retry |
| `all_failed` | All ✗ | No platform succeeded; investigation required |
| `in_progress` | Some still publishing | Long operations (large video uploads) still running |

The Review System records the publishing outcome with per-platform detail. A `partial_published` outcome creates a `gate_failure` type item on the Review page showing which platforms failed and why.

### 7.2 Sequential dependencies

Some cross-platform publishing has a dependency: a TikTok "clip" post may depend on the full YouTube video being published first (to have a reference URL). These dependencies are declared in the Channel Profile's `cross_post_rules` and enforced by the Connector Governance layer:

- If Platform B depends on Platform A: Platform A is published first; Platform B's publishing is initiated only after Platform A's `publishing_event` is recorded with status `success`.
- Dependency failures: if Platform A fails, Platform B is not attempted. The orchestration records Platform B as `blocked_by_dependency_failure`.

### 7.3 Content package per platform

The Publishing Pipeline provides a per-platform content package to the Connector Governance layer. The Connector Governance layer does not adapt content between platforms — content adaptation is the Publishing Pipeline's responsibility (based on the Channel Profile's `cross_post_rules`).

The Connector Governance layer receives:
```
ContentPackage {
  platform:               string
  video_file_path:        string          // local or storage path
  title:                  string          // platform-specific adapted title
  description:            string          // platform-specific adapted description
  tags:                   string[]        // platform-specific tags
  thumbnail_file_path:    string | null
  captions_file_path:     string | null   // .srt or .vtt
  category_id:            string | null
  privacy_setting:        string          // "public" | "private" | "unlisted"
  scheduled_publish_at:   timestamp | null  // null = publish immediately
  ai_disclosure_text:     string | null
  platform_metadata:      object          // platform-specific fields
}
```

---

## 8. Retry Policy

### 8.1 Error classification

Every platform API error is classified before retry behavior is determined:

| Error class | HTTP status (typical) | Retry behavior |
|---|---|---|
| `transient` | 500, 502, 503, 504, network timeout | Retry with backoff |
| `rate_limited` | 429 | Retry after `Retry-After` header (or default 15 minutes) |
| `auth_error` | 401, 403 | Never retry — escalate immediately to Credential Manager |
| `quota_error` | 403 with quota-specific message | Block until quota resets — no retry before reset |
| `content_error` | 400, 422 | Never retry — escalate to Review System as permanent failure |
| `platform_error` | Any unclassified error | Retry up to max, then escalate as `platform_error` |

### 8.2 Retry schedule

For `transient` errors:

```
Attempt 1: immediate (at publish initiation — this is the first try)
Attempt 2: 5 minutes after attempt 1 failure
Attempt 3: 15 minutes after attempt 2 failure
Attempt 4: 30 minutes after attempt 3 failure
→ After attempt 4 failure: escalate as platform_error. No further retries.
```

Total retry window: ~50 minutes. If the episode has a scheduled publish time and the retry window extends past it, the operator is notified that the scheduled time was missed.

### 8.3 Auth error on first attempt

If the very first publish attempt returns a 401 or 403, the Connector Governance layer:
1. Does not retry
2. Calls Credential Manager: `test_credential(credential_id)` to confirm the credential state
3. If credential test fails: emits `credential_usage_error` signal to OIS via Credential Manager
4. Sets connector state to `disconnected`
5. Returns `auth_failure` to Publishing Pipeline — the episode is not published and cannot retry until credential is restored

### 8.4 Retry state visibility

During the retry window, the connector state is `retry_pending`. The Publishing Pipeline shows the episode as "Publishing — retry in Xm" on the Production page. The operator can see the retry schedule and, if they wish, cancel the retry and trigger manual investigation.

---

## 9. Deduplication

### 9.1 Deduplication purpose

Deduplication prevents the same episode from being published twice to the same platform-channel pair. This can happen if a publish operation appears to succeed but the publishing_event was not recorded (race condition), or if a retry fires after a success was received but before the record was written.

### 9.2 Deduplication key

For each platform, the deduplication key is `(channel_id, platform, episode_id)`. Before initiating a publish operation, the Connector Governance layer checks whether a `publishing_event` with status `success` already exists for this key.

If a successful publishing_event already exists:
- The duplicate publish is blocked
- Publishing Pipeline receives: `{ status: "already_published", publishing_event_id: "..." }`
- No `platform_error` or failure is recorded — this is not an error

### 9.3 Override for correction publishing

If an episode must be re-published (e.g., the original publish had incorrect metadata and was deleted on the platform by the operator), the operator can set `allow_republish = true` on the publishing request. This bypass is:
- Available to Admin+ only
- Logged in the audit trail with the reason
- Not the default behavior — requires explicit opt-in per publish attempt

---

## 10. Channel and Client Isolation

### 10.1 Channel isolation

The Connector Governance layer enforces that publishing operations for channel A use only channel A's credential profile. This is enforced by:

1. Every `initiate_publishing()` call includes a `channel_id`
2. The Connector Governance layer looks up the credential profile assigned to `(channel_id, platform)` from the Credential Manager
3. The credential resolution uses only credentials from that profile
4. No credential profile assignment outside the channel's own assignments is reachable within a publishing operation

There is no mechanism for channel A's publishing operation to use channel B's credentials, even if an operator configures both channels.

### 10.2 Client isolation

For factories with client channels, client isolation is enforced at two levels:

**Level 1 — Credential isolation:** A client's platform credentials (their YouTube account, their TikTok account) are stored in a credential profile assigned only to their channels. The credential profile is not accessible to other channels.

**Level 2 — Content isolation:** The Connector Governance layer validates that the `channel_id` in the publishing request matches the channel that owns the credential profile being used. If a publishing request for client B's channel somehow references client A's credential profile (misconfiguration), the request is rejected with an `isolation_violation` error and a security_warning signal is emitted to OIS.

**Level 3 — Publishing event isolation:** Publishing events are scoped to `(factory_id, channel_id, platform)`. A client viewing their own channel's publishing history cannot see publishing events for other channels — the API layer enforces this at the query level.

---

## 11. Connector Health Monitoring

### 11.1 Health check cadence

The Connector Governance layer performs periodic health checks on all active channel-platform connectors. Health checks are lightweight — they use the `health_check_endpoint` from the ConnectorRegistryEntry and do not publish content.

| Connector state | Health check frequency |
|---|---|
| `connected` (idle) | Every 6 hours |
| `publishing` | No health check during active publish |
| `degraded` | Every 15 minutes (to detect recovery) |
| `rate_limited` | After the rate limit window expires |
| `suspended` | No health check (operator-suspended) |
| `disconnected` | No health check (no valid credential) |

### 11.2 Health signals to OIS

| Health event | Signal to OIS | Severity |
|---|---|---|
| Connector health check passes (previously degraded) | `connector_recovered` | INFO |
| Connector health check fails (first failure) | `connector_degraded` | WARNING |
| Connector health check fails (3+ consecutive failures) | `connector_platform_unavailable` | CRITICAL |
| Connector transitions to `quota_exhausted` | `quota_exhausted` | CRITICAL (if blocks upcoming scheduled publish) |
| Connector transitions to `platform_error` | `connector_platform_error` | CRITICAL |
| Connector transitions to `suspended` (operator action) | `connector_suspended` | INFO |
| All connectors for a channel fail simultaneously | `channel_publishing_blocked` | BLOCKED |

### 11.3 Platform status monitoring

The Connector Governance layer optionally reads public platform status pages (YouTube Status, TikTok Status, Meta Status) to contextually enrich degradation signals. If a platform's public status indicates an incident, the `connector_degraded` signal includes `platform_incident: true`, which OIS uses to suppress repeat notifications (platform-wide incidents are known; repeated notifications are noise).

In V1, public status page reading is optional and best-effort. If the status endpoint is unavailable, this field is `null`.

---

## 12. Scheduling and Publish Windows

### 12.1 Native scheduling vs. factory scheduling

| Approach | How it works | Supported platforms |
|---|---|---|
| Native scheduling | Factory sends content to platform with a future publish time; platform handles the scheduled release | YouTube ✓, TikTok ✓, Instagram ✓ |
| Factory scheduling | Factory waits until the scheduled time and initiates the publish at that moment | All platforms (fallback) |

Native scheduling is preferred where supported — it removes the factory as a dependency at publish time and is more reliable for time-sensitive publishing.

### 12.2 Publish window tolerance

If native scheduling is used and the `scheduled_publish_at` passes but the platform has not confirmed the video is public (API delay), the Connector Governance layer waits up to 15 minutes before marking the publish as `delayed`. After 60 minutes, it is marked `platform_error`.

If factory scheduling is used and the factory is unavailable at the scheduled time (machine off, service restart), the publish is queued as pending on next startup. If the delay exceeds 2 hours past the scheduled time, the operator is notified that the scheduled publish was missed. The operator can choose to publish immediately or cancel.

---

## 13. Integration Points

### 13.1 Publishing Pipeline → Connector Governance Layer

The Publishing Pipeline is the only caller of `initiate_publishing()`. Platform-specific content adaptation (adapting YouTube video format to TikTok short form) happens in the Publishing Pipeline before calling the Connector Governance layer. The Connector Governance layer receives platform-ready content packages.

After the Connector Governance layer completes publishing (or fails), it returns a `PublishingResult` to the Publishing Pipeline. The Publishing Pipeline updates the episode status and signals the Review System.

### 13.2 Connector Governance Layer → Credential Manager

At every publish operation start, the Connector Governance layer calls `resolve_credential_in_memory(credential_ref)` from the Credential Manager. The resolved credential is held in memory for the duration of the publish operation and not persisted anywhere.

If the credential is `expiring_soon`, the Connector Governance layer proceeds with the publish and notes the expiry in the publishing_event record. It does not delay publishing for credential warnings.

If the credential is anything other than `ready` or `expiring_soon`, the publish is blocked and the connector transitions to `disconnected`. The Credential Manager is not called for resolution — the readiness check happens first and prevents the resolution call.

### 13.3 Connector Governance Layer → Analytics Layer

After every successful publish, the Connector Governance layer creates a `publishing_event` record and signals the Analytics Layer that a new episode is now live on a platform. The Analytics Layer uses this event to initialize an `EpisodeAnalytics` record in `pending_stabilization` status and start the stabilization window countdown.

```
publishing_event:
  episode_id
  channel_id
  platform
  published_at
  platform_video_id          // platform's ID for the published content
  native_video_url           // the content's URL on the platform
  publishing_latency_ms      // time from initiation to platform confirmation
  quota_used                 // quota units consumed by this operation
```

### 13.4 Connector Governance Layer → OIS

All connector health events, quota events, and publishing failure events are submitted to OIS as `connector_signal` records. OIS classifies and routes — the Connector Governance layer does not route to NS directly.

### 13.5 Connector Governance Layer → Review System

When a publish fails permanently (`platform_error` after exhausting retries, or `content_error` from metadata validation failure), the Connector Governance layer signals the Review System:

```
review_system.create_review_item(
  review_item_type = "gate_failure",
  episode_id,
  failure_source = "platform_connector",
  failure_type = "content_error" | "platform_error",
  failure_detail = platform_error_message,
  platform = platform_name
)
```

This creates a `gate_failure` item on the Review page (even though it is technically a publishing-stage failure, not a production gate failure). The operator reviews and decides: re-attempt with corrected metadata, or reject the episode.

---

## 14. Data Contracts

### 14.1 ConnectorState

```
ConnectorState {
  state_id:                  string
  channel_id:                string
  platform:                  string
  connector_type_id:         string
  state:                     enum(disconnected, connected, publishing, rate_limited,
                                   retry_pending, degraded, suspended, quota_exhausted,
                                   platform_error)
  credential_profile_id:     string | null
  current_publish_episode_id: string | null    // set during publishing state
  rate_limit_resets_at:      timestamp | null
  quota_status:              QuotaRecord | null
  retry_schedule:            RetrySchedule | null
  last_health_check_at:      timestamp | null
  last_health_check_result:  enum(pass, fail) | null
  degraded_since:            timestamp | null
  platform_error_detail:     string | null     // summary of error, not raw API response
  suspended_by:              string | null     // user_id if suspended
  suspended_at:              timestamp | null
  updated_at:                timestamp
}
```

### 14.2 PublishingEvent

```
PublishingEvent {
  event_id:                  string
  episode_id:                string
  run_id:                    string
  channel_id:                string
  platform:                  string
  status:                    enum(success, failed, retry_pending, partial, blocked_by_dependency)
  initiated_at:              timestamp
  completed_at:              timestamp | null
  platform_video_id:         string | null     // platform's content ID on success
  native_video_url:          string | null
  publishing_latency_ms:     int | null
  quota_used:                int | null
  retry_count:               int
  retry_history:             RetryAttempt[]
  failure_class:             enum(auth_error, transient, rate_limited, content_error,
                                    quota_error, platform_error) | null
  failure_detail:            string | null     // sanitized — no raw API error if it contains sensitive data
  validation_failures:       ValidationFailure[]
  allow_republish_override:  boolean           // true if operator explicitly allowed re-publish
  scheduled_publish_at:      timestamp | null
  published_at:              timestamp | null  // actual platform publish time
  dependency_on:             string | null     // platform this publish depends on (if sequenced)
}
```

### 14.3 ValidationFailure

```
ValidationFailure {
  field:                     string            // e.g., "title", "description", "tags"
  rule_violated:             string            // e.g., "title_max_chars"
  actual_value:              string            // the problematic value
  limit:                     string            // the platform limit
  severity:                  enum(blocking, warning)
  human_description:         string            // operator-readable description
}
```

### 14.4 RetryAttempt

```
RetryAttempt {
  attempt_number:            int
  attempted_at:              timestamp
  result:                    enum(success, transient, rate_limited, auth_error, platform_error)
  http_status:               int | null
  retry_after_seconds:       int | null
}
```

### 14.5 ConnectorSignal

```
ConnectorSignal {
  signal_id:                 string
  channel_id:                string
  platform:                  string
  episode_id:                string | null
  signal_subtype:            enum(connector_degraded, connector_recovered,
                                    connector_platform_unavailable,
                                    connector_platform_error,
                                    connector_suspended, connector_quota_warning,
                                    connector_quota_exhausted, connector_quota_reset,
                                    channel_publishing_blocked,
                                    publish_success, publish_failed,
                                    publish_retry_scheduled,
                                    isolation_violation)
  severity:                  enum(INFO, WARNING, CRITICAL, BLOCKED)
  production_blocked:        boolean
  platform_incident:         boolean           // true if platform public status indicates incident
  detail:                    string            // human-readable description
  emitted_at:                timestamp
}
```

### 14.6 PublishingResult (returned to Publishing Pipeline)

```
PublishingResult {
  episode_id:                string
  channel_id:                string
  overall_status:            enum(all_published, partial_published, all_failed, in_progress)
  per_platform_results:      map<platform, PublishingEvent>
  initiated_at:              timestamp
  completed_at:              timestamp | null
}
```

---

## 15. API Contracts

### 15.1 initiate_publishing()

Called by: Publishing Pipeline.

```
initiate_publishing(
  episode_id,
  channel_id,
  target_platforms,          // list of platforms to publish to
  content_packages,          // map<platform, ContentPackage>
  allow_republish,           // default false
  scheduling_mode            // "immediate" | "native_scheduled"
) → PublishingResult
```

Validates metadata for each platform before initiating. Returns immediately with `in_progress` for large uploads; publishes_events are emitted as operations complete asynchronously.

### 15.2 get_connector_state()

Called by: Dashboard, Publishing Pipeline (pre-publish check), Channel Activation gate.

```
get_connector_state(
  channel_id,
  platform               // optional — returns all platforms if null
) → ConnectorState[]
```

### 15.3 suspend_connector()

Called by: Dashboard (Admin+).

```
suspend_connector(
  channel_id,
  platform,
  reason                 // required — stored in audit trail
) → ConnectorState
```

### 15.4 resume_connector()

Called by: Dashboard (Admin+).

```
resume_connector(
  channel_id,
  platform
) → ConnectorState
```

### 15.5 reset_connector_error()

Called by: Dashboard (Admin+). Only valid for `platform_error` state.

```
reset_connector_error(
  channel_id,
  platform,
  resolution_note        // required — what was done to resolve
) → ConnectorState
```

### 15.6 get_publishing_history()

Called by: Dashboard, Analytics Layer.

```
get_publishing_history(
  channel_id,
  platform,              // optional
  episode_id,            // optional
  date_from,
  date_to,
  page,
  page_size
) → PublishingEvent[]
```

### 15.7 get_quota_status()

Called by: Dashboard, Publishing Pipeline (pre-publish check).

```
get_quota_status(
  channel_id,
  platform
) → QuotaRecord
```

### 15.8 validate_content_package()

Called by: Publishing Pipeline (before initiating a publish to detect failures early).

```
validate_content_package(
  channel_id,
  platform,
  content_package
) → { valid: boolean, validation_failures: ValidationFailure[] }
```

### 15.9 get_connector_registry()

Called by: Dashboard (provider setup checklist), Channel Activation.

```
get_connector_registry(
  platform               // optional — returns all if null
) → ConnectorRegistryEntry[]
```

---

## 16. Dashboard Visibility

### 16.1 Connectors on Credentials page

The Credentials page (B5.9) shows connector health as a derived view:

```
PLATFORM CONNECTORS  (The Hypogeum Chronicles)

  YouTube    ● connected      last published: ep_003 (2026-06-08)   [Suspend] [Test]
  TikTok     ● connected      last published: ep_003 (2026-06-08)   [Suspend] [Test]
  Instagram  ✗ platform_error  ep_004 failed — video rejected (400)  [View Error] [Reset]
```

### 16.2 Connector status on Production page

Each published episode in the Production history table shows per-platform publishing status:

```
ep_003  The Antikythera Mechanism  published
  YouTube:   ✓ published 2026-06-08 18:03 UTC  (video ID: dQw4w9WgXcQ)
  TikTok:    ✓ published 2026-06-08 18:07 UTC
  Instagram: ✓ published 2026-06-08 18:09 UTC
```

For partial failures:
```
ep_004  The Acropolis Tunnels      partial
  YouTube:   ✓ published
  TikTok:    ✗ failed — rate limited, retry at 19:30 UTC
  Instagram: ✗ platform_error — video rejected (see error detail)
             [View Error]  [Retry TikTok]
```

### 16.3 Quota status on Credentials page

```
PUBLISHING QUOTA  (Today — 2026-06-14)

  YouTube (hypogeum):   2 / 6 uploads used   ██░░░░  33%
  TikTok (hypogeum):    3 / 50 posts used     ██░░░░   6%
  Instagram (hypogeum): 1 / 25 posts used     █░░░░░   4%
```

### 16.4 Role-based connector visibility

| Section | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| Connector health status | ✓ | ✓ | Home alerts | ✗ | ✗ | Own channels (label + status only) |
| Publishing history | ✓ | ✓ | ✓ (Production page) | ✓ (read) | ✓ (read) | Own channels (no URLs) |
| Quota status | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Suspend / Resume | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Reset platform error | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |

**Client visibility:** Clients may see the publishing status of their own channel's episodes (published / pending / failed) but do not see internal factory details (quota, connector state label, platform error detail, video URLs if the client does not already have access to them via the platform).

---

## 17. Local-First and SaaS Readiness

### 17.1 Local-first V1

In local-first V1, the Connector Governance layer operates as a single-factory, single-machine system. All connector state is stored locally. OAuth redirect URIs are localhost URLs. Scheduling uses the local machine's clock and process scheduler.

**Limitation:** If the local machine is off when a scheduled native publish is due and the factory uses factory-scheduling mode (not native scheduling), the publish is missed. The operator is notified on next startup. This is acceptable in V1 for a single-machine operation.

**Recommendation:** Use native scheduling mode where supported to eliminate the local machine dependency at publish time.

### 17.2 SaaS readiness

For SaaS deployment, the Connector Governance layer requires:
- Connector state storage in a shared, durable data store (not local files)
- Factory scheduling runs on a reliable cloud scheduler (not a local cron)
- Per-factory, per-channel connector state isolation (same scope as Credential Manager)
- OAuth redirect URIs on the SaaS domain
- Audit trail for all connector operations
- Client connector views scoped per-tenant without cross-tenant leakage

These requirements are consistent with the Credential Manager's SaaS readiness requirements (B5.9 §18.2). The Connector Governance layer uses the same `factory_id` scoping model.

---

## 18. Risk Register

### Risk 1 — Duplicate publish to same platform

**Failure mode:** A publish operation times out. The Connector Governance layer schedules a retry. The original request, delayed by the platform, is actually processed. Both the original and the retry publish successfully. The same episode appears twice on the operator's YouTube channel.

**Mitigation:** Deduplication check (§9) prevents the retry from initiating if a `publishing_event` with `status: success` already exists for the same `(channel_id, platform, episode_id)` key. However, the race condition window (between original success and the retry's deduplication check) is narrow. An additional safeguard: after every successful publish, the Connector Governance layer calls `get_platform_video_status(platform_video_id)` to verify the video is live on the platform before initiating a retry. If the video is found on the platform, the retry is cancelled and the existing `publishing_event` is updated with the `platform_video_id`.

---

### Risk 2 — Cross-channel credential use

**Failure mode:** A factory manages channels for two clients. An operator accidentally assigns Client A's YouTube credential profile to Client B's channel. Client B's episodes are published to Client A's YouTube account.

**Mitigation:** Channel isolation (§10) validates that the `channel_id` in the publish request matches the channel that owns the credential profile. If the credential profile's `assigned_channels` does not include the requesting channel, the publish is blocked with an `isolation_violation` error. This check happens before the credential is resolved — the wrong credential is never accessed. An `isolation_violation` signal is submitted to OIS as a security_warning, alerting the Owner.

---

### Risk 3 — OAuth token expires mid-upload

**Failure mode:** A large video upload to YouTube takes 45 minutes. The OAuth access token expires after 1 hour. The token expires 55 minutes into the 45-minute upload. YouTube's upload API rejects the final chunk authentication.

**Mitigation:** For long upload operations, the Connector Governance layer refreshes the access token proactively at 5 minutes before expiry. The refresh happens in parallel with the upload — the upload is not interrupted. The new token is used for subsequent API calls (such as the metadata update after upload). If the token expires before the proactive refresh can execute (e.g., the token was already close to expiry when the upload started), the upload fails at the expired-token point. The Connector Governance layer retries from the beginning (not mid-upload, unless the platform supports resumable upload continuation) using the refreshed token. YouTube's Data API supports resumable uploads which can be continued after token refresh — the connector implementation should use resumable upload mode.

---

### Risk 4 — Platform rule changes break publishing

**Failure mode:** YouTube changes its title character limit from 100 to 80. The Connector Governance layer still validates against 100 chars. An episode with a 90-character title passes validation, is submitted to YouTube, and is rejected with a 400 error.

**Mitigation:** Platform rule violations discovered at publish time are treated as `content_error` class (no retry). The error is surfaced to the Review System and the operator with the specific YouTube error message. The ConnectorRegistryEntry rules are updated as soon as rule changes are detected. In V1, rule changes are detected reactively (when a publish fails for a new rule) and updated manually. In V2, a platform rules monitoring agent would detect changes proactively. Operators are encouraged to use titles with headroom below published limits to reduce sensitivity to rule boundary changes.

---

### Risk 5 — Quota exhaustion breaks scheduled publishing

**Failure mode:** An operator queues 8 episodes for publication over the next 3 days. The YouTube upload quota is 6 per day. The Connector Governance layer publishes 6 on day 1 and the next 2 fail with quota exhaustion. The scheduled publication times are missed.

**Mitigation:** The Connector Governance layer checks `quota_status` before each publish and emits a `connector_quota_warning` signal when 75% of the daily quota is used. If the quota would be exhausted by the current publish queue, the Publishing Pipeline is notified with a warning: "Publishing queue exceeds daily YouTube quota. Scheduled publish for ep_007 and ep_008 will be delayed until quota resets at midnight UTC." The operator can reschedule or accept the delay. Quota exhaustion is not a failure — it is a planned constraint surfaced proactively.

---

### Risk 6 — Platform degradation blocks all publishing silently

**Failure mode:** YouTube's API is degraded (5xx errors on upload endpoint) but the platform status page is not yet updated. The Connector Governance layer retries 3 times, exhausts retries, and marks the connector as `platform_error`. The operator is not watching the dashboard and does not notice for 4 hours. An episode that was supposed to publish at 18:00 UTC is still unpublished at 22:00 UTC.

**Mitigation:** `platform_error` after retry exhaustion emits a CRITICAL `connector_signal` to OIS immediately. OIS routes this as a CRITICAL notification (bypasses quiet hours — production is impacted). The notification includes: "YouTube publishing failed for ep_007 after 3 retries. YouTube API may be degraded. Check platform status and retry when resolved." The Home page shows a CRITICAL alert in the Pending Actions section. The operator is notified by all configured channels within minutes of the failure. Additionally, the Connector Governance layer reads the YouTube platform status page (if available) to include `platform_incident: true` in the signal — giving the operator immediate context that the issue is platform-side rather than factory-side.

---

### Risk 7 — Content_package mismatch between platforms

**Failure mode:** The Publishing Pipeline sends the same `title` for YouTube (100 char limit) and TikTok (shorter effective limit for first line display). TikTok displays a truncated title. The operator's intended title hook does not appear on TikTok.

**Mitigation:** The Connector Governance layer validates each platform's content package independently against that platform's rules. Metadata validation (§5) runs per-platform, not once globally. The Publishing Pipeline is responsible for per-platform content adaptation (based on `cross_post_rules` in the Channel Profile). If the Publishing Pipeline sends an identical package to both platforms without adaptation, that is a Publishing Pipeline configuration issue — the Connector Governance layer validates what it receives, it does not adapt content.

---

## 19. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Connector health shown on Credentials page and Production page. Publishing history visible on Production page per-episode. Home page alerts include connector health events routed via OIS. |
| B5.9 — Credential Manager | All credential resolution for publishing uses Credential Manager's `resolve_credential_in_memory()`. Credential health is checked before publishing. Auth errors during publishing call Credential Manager to re-test and update readiness. |
| B5.275 — OIS | All connector health events, publishing failures, quota events, and isolation violations are submitted as `connector_signal` to OIS. OIS routes to NS, Home, and Dashboard. |
| B5.35 — Notification System | NS delivers connector failure and quota notifications. It does not interact with connectors directly — it receives notifications via OIS routing. |
| B5.5 — Review System | Publishing failures (content_error, platform_error) create review items on the Review page. The Review System calls the connector to check the current connector state when displaying the failure. |
| B5.8 — Analytics Layer | Every successful `publishing_event` initializes an `EpisodeAnalytics` record in the Analytics Layer. Publishing events are the trigger for analytics stabilization window countdown. |
| B4.75 — Channel Profile | `platform_strategy.enabled_platforms` determines which connectors are required for a channel. `platform_strategy.platform_configs` provides publishing schedules and platform-specific defaults. `cross_post_rules` governs sequential dependencies between platforms. |
| Publishing Pipeline | The Publishing Pipeline is the sole caller of `initiate_publishing()`. It handles content adaptation before calling the Connector Governance layer and processes `PublishingResult` to update episode status. |
