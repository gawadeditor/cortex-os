# Notification System — Phase B5.35 Design

**Status:** Design only — no implementation  
**Phase:** B5.35 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B5.275 (Operational Intelligence System), B5.3 (Dashboard Director System), B5.25 (Product Experience System)  
**Feeds into:** B5.4 (Approval System)  
**Author:** Factory OS Architect

---

## 1. What the Notification System Is

Three systems share adjacent responsibilities around operator awareness:

| System | Decides |
|---|---|
| Operational Intelligence (B5.275) | Whether a condition deserves notification |
| Notification System (B5.35) | How, when, where, and to whom it is delivered |
| Dashboard Director (B5.3) | How notification counts and summaries appear on the dashboard |

The Notification System does not evaluate signal importance. OIS has already decided that a notification should be sent before calling the Notification System. The Notification System takes that decision and executes delivery — safely, without duplication, without fatigue, and without leaking sensitive information.

**The Notification System owns:**
- Recipient resolution (who should receive this notification)
- Delivery channel selection (email, Telegram, Dashboard Inbox)
- Rate limiting and deduplication at the delivery layer
- Quiet hours enforcement
- Escalation triggers and escalation delivery
- Payload redaction (secrets never leave in notification content)
- Delivery logging and failure tracking
- The Notification Inbox (in-dashboard message center)
- User notification preferences

**The Notification System does not own:**
- Signal priority scoring (OIS)
- Signal classification and routing decisions (OIS)
- Dashboard composition or health scoring (Dashboard Director)
- Approval workflows (Approval System)
- What information is sensitive (Credential Manager defines secrets)
- Engineering execution of changes (Engineering Director)

---

## 2. Notification System Architecture

### 2.1 Signal Flow

```
OIS RoutingDecision
  includes destination: "notification_system"
        │
        ▼
Notification System: create_notification()
  Normalizes OIS signal into NotificationEvent
  Validates payload (no secrets, no stack traces)
        │
        ▼
resolve_recipients()
  Determines who receives this notification
  Applies: role rules, channel scope, factory policy, preferences
        │
        ▼
For each recipient:
  Check delivery channel preferences
  Check quiet hours
  Check rate limits / deduplication
  Check cooldown windows
        │
        ├── Immediate delivery? → deliver_notification()
        │       │
        │       ├── Dashboard Inbox (always — no rate limit)
        │       ├── Email (if enabled + severity threshold met + not in quiet hours)
        │       └── Telegram (if enabled + severity threshold met + not in quiet hours)
        │
        └── Digest delivery? → queue for deliver_digest()
                (non-urgent, batches per digest schedule)
        │
        ▼
Record DeliveryAttempt
  → success: mark delivered, log
  → failure: log failure, schedule retry (exponential backoff)
  → suppressed: log suppression reason
        │
        ▼
Escalation check (async, runs continuously)
  Monitors: unacknowledged critical/blocked notifications
  Triggers: escalation_policy when threshold exceeded
```

### 2.2 Core Guarantees

1. **Dashboard Inbox always receives every notification** — regardless of external delivery suppression, rate limits, or quiet hours. An operator who ignores all external channels will still see all notifications in the inbox.

2. **BLOCKED severity bypasses all limits** — quiet hours, daily rate limits, cooldown windows are all overridden when `severity = blocked`. Production-blocked conditions are always delivered immediately on all enabled channels.

3. **Secrets are never in notification content** — a redaction layer runs on every payload before it leaves the system. No credential value, API key, token, or secret string ever appears in a notification body, subject line, or push content.

4. **Deduplication at the delivery layer is independent of OIS deduplication** — OIS suppresses duplicate signals from reaching the notification routing decision. The Notification System adds a second layer: if the same notification event arrives twice (due to system retry or race condition), only one external delivery is made.

5. **Delivery failures are surfaced, not hidden** — every failed delivery is logged, retried with exponential backoff, and eventually visible in the delivery report. Silent failures are not permitted.

---

## 3. Notification Types

| Type | Definition | Default Severity | Default Recipients |
|---|---|---|---|
| `system_alert` | Factory or infrastructure condition outside normal parameters | WARNING | Owner, Admin |
| `approval_required` | A human decision is needed before an action proceeds | CRITICAL | Role with approval authority |
| `production_blocked` | Production runs cannot proceed due to an unresolved condition | BLOCKED | Owner, Admin, active Operators |
| `credential_failure` | A credential expired, failed a test, or was revoked | CRITICAL | Owner, Admin |
| `provider_failure` | An external provider is unavailable or degraded | WARNING | Owner, Admin |
| `budget_warning` | Spend is approaching the configured ceiling | WARNING | Owner, Admin |
| `budget_blocked` | Spend has reached or exceeded the configured ceiling | BLOCKED | Owner, Admin, active Operators |
| `engineering_change` | Engineering Director applied or rolled back a change | INFO (applied) / WARNING (rolled back) | Owner, Admin |
| `module_health` | A module changed health status (degraded, restored) | WARNING (degraded) / INFO (restored) | Owner, Admin |
| `learning_recommendation` | Learning System has a recommendation awaiting review | INFO | Admin, Operator |
| `publishing_status` | An episode publishing event completed or failed | INFO (success) / CRITICAL (failure) | Operator, Client (own channels) |
| `security_warning` | A permission anomaly or credential access event was detected | CRITICAL | Owner only |

### Type-to-Channel Routing

| Type | Inbox | Email | Telegram |
|---|---|---|---|
| `system_alert` | Yes | If enabled + WARNING threshold | No |
| `approval_required` | Yes | Yes | Yes (if enabled) |
| `production_blocked` | Yes | Yes | Yes |
| `credential_failure` | Yes | Yes | Yes (if enabled) |
| `provider_failure` | Yes | Digest only | No |
| `budget_warning` | Yes | Digest only | No |
| `budget_blocked` | Yes | Yes | Yes |
| `engineering_change` | Yes | Digest only (applied) / Yes (rolled back) | No |
| `module_health` | Yes | Digest only | No |
| `learning_recommendation` | Yes | Digest only | No |
| `publishing_status` (failure) | Yes | Yes | Yes (if enabled) |
| `publishing_status` (success) | Yes | Digest only | No |
| `security_warning` | Yes | Yes | Yes |

---

## 4. Delivery Channels

### 4.1 Dashboard Inbox (V1)

**Purpose:** The primary in-product notification center. Every notification arrives here regardless of external delivery status. The Inbox is the ground-truth notification record.

**Allowed types:** All notification types.

**Risk level:** Minimal. No external transmission. No credential exposure path.

**Rate limits:** None on the inbox itself. External delivery channels are rate-limited; inbox is not.

**Persistence:**
- INFO: retained 30 days, then auto-archived
- WARNING: retained 90 days, then auto-archived
- CRITICAL: retained until acknowledged, then retained 90 days
- BLOCKED: retained until the blocking condition resolves, then retained 90 days

**Fallback behavior:** If no other delivery channel is configured or available, the inbox is the final delivery guarantee. It is not a fallback — it is the baseline.

---

### 4.2 Email (V1)

**Purpose:** Asynchronous delivery for operators who are not actively watching the dashboard. Suitable for decision-required notifications and critical events.

**Allowed types:** All except `module_health` (degraded-and-restored within minutes is not worth an email) and `learning_recommendation` (digest-appropriate).

**Risk level:** Medium. Email is stored in third-party systems. Payloads must be redacted before delivery. No attachment containing sensitive data.

**Rate limits:**
- Per user: max 3 CRITICAL/day, max 10 total/day
- BLOCKED severity overrides daily limits
- Per factory: max 50 total email notifications/day across all users

**Quiet hours:** Respected. BLOCKED and CRITICAL with `production_blocked = true` override quiet hours.

**Fallback:** If email delivery fails (SMTP error, bounce), the failure is logged and retried. After 3 failures, the notification remains in the inbox with a "[Email delivery failed]" flag. No further email retries until the next scheduled digest window.

**Email format requirements:**
- Subject: `[{severity}] {title} — {factory_branding.dashboard_title}`
- Body: summary, affected entity, required action, dashboard link
- No HTML-only emails — must have plaintext fallback
- No images or attachments in notifications
- Unsubscribe link required per CAN-SPAM / GDPR
- Sender address: configurable per factory (from Settings → Notifications)

---

### 4.3 Telegram (V1)

**Purpose:** Near-real-time delivery for critical and blocked events. Suitable for operators who monitor via mobile or who need faster-than-email alerting.

**Allowed types:** `production_blocked`, `budget_blocked`, `credential_failure` (expired or 5-day warning), `approval_required`, `publishing_status` (failure), `security_warning`, `engineering_change` (HIGH/CRITICAL risk changes and rollbacks only).

Telegram is not for routine notifications. It is reserved for events that require faster response than the next email check.

**Risk level:** Medium-high. Telegram is an external service. Chat IDs are sensitive (stored as credentials). Message content must be short and redacted — Telegram chat logs are not under operator control.

**Rate limits:**
- Per chat: max 3 messages/hour, max 15/day
- BLOCKED severity: up to 5/hour if blocking condition persists
- Per factory: max 30 Telegram messages/day across all users

**Quiet hours:** Respected. BLOCKED overrides.

**Fallback:** If Telegram delivery fails (API error, bot blocked), fall back to email for the same notification. If email also fails, the notification remains in the inbox with delivery failure flags.

**Telegram format requirements:**
- Maximum message length: 1000 characters (Telegram supports 4096 but shorter is better for mobile)
- Format: plain text with minimal Markdown (bold for severity level only)
- No links that expose internal system paths or entity IDs in the URL
- No credential-adjacent content

---

### 4.4 Future Channels

These channels are acknowledged but not designed for V1. They must be considered in the architecture so their addition does not require a rewrite.

| Channel | Status | Notes |
|---|---|---|
| Slack | Future | Webhook-based. Similar rate limits to Telegram. |
| Discord | Future | Webhook-based. Similar to Slack. |
| Generic webhooks | Future | Operator-configured HTTP endpoint. Requires HMAC signing. |
| SMS | Future | Emergency-only (BLOCKED + security_warning). Very high cost, very short messages. |
| Mobile push | Future | Requires native app. Out of scope for web-based Factory OS V1. |

**Architecture forward-compatibility:** The `DeliveryChannel` type is designed as an extensible enum. Adding a new channel requires implementing a new delivery adapter — the core notification pipeline (create → resolve_recipients → deliver → log) does not change.

---

## 5. Recipient Resolution

Recipient resolution determines who receives each notification. It runs immediately after `create_notification()`.

### 5.1 Role-Based Default Recipients

| Notification Type | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| `system_alert` | Yes | Yes | — | — | — | — |
| `approval_required` | If only owner can approve | Yes | If approval is within operator scope | — | — | — |
| `production_blocked` | Yes | Yes | Active operators only | — | — | — |
| `credential_failure` | Yes | Yes | — | — | — | — |
| `provider_failure` | Yes | Yes | — | — | — | — |
| `budget_warning` | Yes | Yes | — | — | — | — |
| `budget_blocked` | Yes | Yes | Active operators only | — | — | — |
| `engineering_change` | Yes | Yes | — | — | — | — |
| `module_health` | Yes | Yes | — | — | — | — |
| `learning_recommendation` | Yes | Yes | Yes | — | — | — |
| `publishing_status` (failure) | Yes | Yes | Yes | — | — | Own channels only |
| `publishing_status` (success) | — | — | Yes | — | — | Own channels only |
| `security_warning` | Yes | — | — | — | — | — |

"Active operators only" means Operators who have queued a run within the past 7 days, or who have a run currently active. Inactive Operators do not receive production_blocked notifications.

### 5.2 Channel Scope Filtering

For channel-scoped notifications (publishing events, channel-specific budget events), recipients are filtered to:
- Operators assigned to that channel
- Admins and Owners (always factory-wide)
- Clients assigned to that channel (receiving only client-safe notification variants)

A notification about "Channel A publishing failed" does not go to Operators who are only assigned to Channel B.

### 5.3 Client-Safe Notification Variants

Notifications delivered to Client-role users must:
- Not mention internal system names ("Factory OS", "Engineering Director", "OIS")
- Not include technical error codes or internal entity IDs
- Not mention other channels or factory-wide operations
- Use plain language appropriate for a non-technical client audience

The Notification System maintains client-safe payload variants for every notification type that can reach Clients. The variant strips internal references and replaces technical descriptions with plain language.

Example:

| Audience | Publishing Failure Message |
|---|---|
| Internal (Admin/Operator) | "YouTube publishing failed for ep_005 — OAuth token rejected (error 401). Credential 'youtube-hypogeum' may have expired." |
| Client | "Your episode 'The Antikythera Mechanism' could not be published to YouTube. Your agency is looking into it and will update you shortly." |

### 5.4 Factory Policy Overrides

Factory notification policy can override user preferences for specific types:

- `security_warning`: always delivered to Owner on all channels — no user preference can suppress this
- `production_blocked`: always delivered to Owner and Admin via Inbox — no user preference can remove these from the Inbox
- `credential_failure` with expiry ≤ 5 days: always delivered via Inbox to Admin and Owner — external delivery follows preferences, but inbox delivery cannot be suppressed
- BLOCKED severity: always delivered to Owner via all their enabled channels — daily limits do not apply

---

## 6. Notification Severity

### 6.1 INFO

**Definition:** Something happened that the operator may want to know but that requires no action.

**External delivery:** Inbox only by default. User may opt in to email digest.  
**Dismissal:** Auto-archived after 30 days. Operator may archive anytime.  
**Escalation:** None. INFO notifications do not escalate.  
**Retry on failure:** Single retry only. If inbox delivery fails, log and drop.  
**Quiet hours:** Always observed. No override for INFO.

---

### 6.2 WARNING

**Definition:** Something is suboptimal or approaching a threshold. Action is recommended but not immediately required.

**External delivery:** Inbox + daily digest email. Immediate email/Telegram only if user has configured WARNING threshold for those channels.  
**Dismissal:** Operator dismisses manually. Not auto-dismissed. After 90 days: auto-archived.  
**Escalation:** Escalates to CRITICAL if unresolved after 7 days.  
**Retry on failure:** 3 retries with exponential backoff (5min, 15min, 1h). After 3 failures: log failure, keep in inbox with delivery failure flag.  
**Quiet hours:** Observed. Not overridden.

---

### 6.3 CRITICAL

**Definition:** Action is required. Without it, production impact or significant degradation is imminent or occurring.

**External delivery:** Inbox + immediate email (if enabled, up to daily limit) + Telegram (if enabled and type is Telegram-eligible). Bypasses digest batching.  
**Dismissal:** Manual only. CRITICAL notifications cannot be auto-dismissed. They can be acknowledged (operator has seen them) but remain visible until the underlying condition resolves.  
**Escalation:** Escalates per `EscalationPolicy` if unacknowledged for > 4 hours.  
**Retry on failure:** 3 retries (5min, 30min, 2h). If email fails, attempt Telegram if enabled. After all retries exhausted: log and alert in inbox.  
**Quiet hours:** Observed for external delivery. The inbox always receives CRITICAL regardless of quiet hours. If a CRITICAL notification arrives during quiet hours, it is queued for delivery at quiet hours end AND marked as "pending delivery" in the inbox immediately.

---

### 6.4 BLOCKED

**Definition:** Production is currently prevented. Immediate action required.

**External delivery:** Inbox + immediate email + immediate Telegram (if enabled). Bypasses all daily limits and quiet hours. Overrides deduplication cooldowns.  
**Dismissal:** Cannot be dismissed while the blocking condition is active. Once the condition resolves, the notification transitions to CRITICAL (resolved) and can be acknowledged.  
**Escalation:** Escalates per `EscalationPolicy` if unacknowledged for > 2 hours.  
**Retry on failure:** Aggressive retry every 10 minutes until delivery confirmed or condition resolves. If all channels fail: log escalation to Owner's inbox with "[ALL DELIVERY CHANNELS FAILED]" flag.  
**Quiet hours:** No quiet hours for BLOCKED. All channels deliver immediately regardless of time.

---

## 7. Notification Payload Rules

### 7.1 Required Fields (every notification)

```
Notification Payload — Required Fields

notification_id:          string      UUID, unique per notification event
correlation_id:           string      OIS signal_id that triggered this notification
notification_type:        string      From the NotificationType enum
severity:                 string      "info" | "warning" | "critical" | "blocked"
title:                    string      ≤80 characters. Operator-readable headline.
summary:                  string      ≤300 characters. Context in 1-3 sentences.
source_system:            string      Which system generated the original signal
source_entity_id:         string|null The specific entity involved (channel_id, etc.)
source_entity_name:       string|null Human-readable entity name
affected_entity_desc:     string      Plain English: "The Hypogeum Chronicles channel"
required_action:          string|null What the operator should do. null if no action needed.
dashboard_link:           string      Deep link to the relevant dashboard page
created_at:               datetime    When this notification was created
expires_at:               datetime|null When this notification auto-resolves (if applicable)
```

### 7.2 Forbidden Content (every notification)

The following must NEVER appear in any notification payload, email body, Telegram message, or digest content:

| Category | Examples | Reason |
|---|---|---|
| Raw API keys | OpenRouter key, MUAPI key, ElevenLabs key | Secret leakage |
| OAuth tokens | YouTube access_token, refresh_token | Secret leakage |
| Credentials in any form | `api_key: "sk-..."`, `token: "ya29..."` | Secret leakage |
| Full stack traces | Python tracebacks with file paths and line numbers | Internal exposure |
| Raw log content | Application log lines, agent prompt content | Internal exposure |
| Unredacted external API errors | `"OpenAI API error: Invalid key sk-proj-..."` | Secret leakage |
| Private user data | Internal user IDs, email addresses beyond what's needed | Privacy |
| Sensitive export data | Channel export JSON content, config file content | Internal exposure |

### 7.3 Redaction Middleware

Every notification payload passes through a redaction middleware before delivery. The middleware:

1. Scans all string fields for API key patterns (regex for common key formats: `sk-`, `ya29.`, long base64 strings ≥32 chars)
2. Replaces detected secrets with `[REDACTED]`
3. Truncates any field named `stack_trace`, `traceback`, `log_content` to: "Internal error details withheld. Error ID: {error_id}"
4. Validates that no field in the `CredentialManager` credential value schema appears in the payload
5. Logs any redaction event (a notification that triggered redaction is flagged for review)

Redaction is not optional and cannot be disabled per-notification or per-channel. It runs on all payloads including Dashboard Inbox entries.

### 7.4 Title and Summary Length Constraints

| Channel | Title max | Summary max |
|---|---|---|
| Dashboard Inbox | 120 chars | 500 chars |
| Email subject | 80 chars | (body has no limit but must be concise) |
| Telegram | 80 chars | 600 chars (total message ≤1000 chars) |

If the generated title or summary exceeds the channel limit, the Notification System truncates with "..." rather than failing delivery. Truncation is logged.

---

## 8. Delivery Policy

### 8.1 Immediate Delivery

Applies to: CRITICAL, BLOCKED severity; `production_blocked`, `approval_required`, `credential_failure` types regardless of severity.

On arrival at the Notification System:
1. `create_notification()` runs immediately
2. `resolve_recipients()` runs immediately
3. `deliver_notification()` called within 60 seconds of receipt
4. Dashboard Inbox updated first (synchronous)
5. Email and Telegram dispatched concurrently (asynchronous)
6. Delivery logged

---

### 8.2 Batched Delivery

Applies to: Multiple notifications of the same type arriving within a 15-minute window that share the same `source_system` and `source_entity_id`.

Instead of 5 separate emails, one batched email groups them:

```
Email Subject: [WARNING] 3 Agent Performance Alerts — Hypogeum Media Factory

• Script Engine: gate_pass_rate dropped below threshold (4 runs)
• Research Director: high retry rate detected (30-run window)
• Script Engine: model cost anomaly detected

[→ View all agents in Agent Performance]
```

Batching is applied only to external channels (email, Telegram). Dashboard Inbox receives individual entries for each notification.

---

### 8.3 Digest Delivery

Applies to: INFO severity notifications and non-urgent WARNING notifications where the user has configured digest mode.

Digest schedule options: hourly, daily (default), weekly.

A digest groups all accumulated INFO/WARNING notifications since the last digest:

```
Email Subject: Factory OS Daily Digest — 4 updates — 2026-06-13

WARNINGS (1)
  ⚠ Archive module upgrade available (v1.2.0)
     [→ Upgrade in Modules]

INFO (3)
  ℹ ep_005 published to YouTube · 3h ago
  ℹ Learning: schedule recommendation applied · 1d ago
  ℹ ep_004 published to YouTube · 2d ago

[→ Open Dashboard Inbox]
```

Digest delivery does not apply to CRITICAL or BLOCKED — those are always immediate.

---

### 8.4 Suppressed Delivery

Applies to: Notifications within a cooldown window, notifications exceeding rate limits, or notifications during quiet hours (non-BLOCKED).

Suppressed deliveries are:
- Always delivered to Dashboard Inbox (suppression applies only to external channels)
- Logged with suppression reason
- Counted in the delivery report as "suppressed — [reason]"
- Not counted against daily notification limits

---

### 8.5 Escalation Delivery

When an escalation policy triggers (see §11), the Notification System sends an escalation notification to additional recipients and/or additional channels. The escalation notification includes:

- Reference to the original notification
- How long the original has been unacknowledged
- Who the escalation is going to and why
- The specific action required to stop further escalation

Escalation deliveries bypass quiet hours and are always immediate.

---

### 8.6 Quiet Hours

Quiet hours define a window per user during which external notifications (email, Telegram) are suppressed. Dashboard Inbox is never quiet.

Rules:
- Quiet hours are configurable per user in notification preferences
- Default: no quiet hours (all hours active)
- During quiet hours: external deliveries are queued; they are sent at quiet hours end
- If an external notification is queued and the underlying condition resolves before quiet hours end: the notification is converted to INFO severity and sent in the next digest

Override conditions (quiet hours ignored):
- `severity = blocked`
- `notification_type = production_blocked` or `security_warning`
- Escalation delivery

---

### 8.7 Critical Override

Factory policy can designate specific notification types as "always deliver, always immediate, always all channels" regardless of user preferences and quiet hours. This is reserved for:

- `security_warning`
- `production_blocked`
- `budget_blocked`
- Any notification with `severity = blocked`

Critical override settings are in Factory Settings → Notification Policy. They cannot be disabled by individual user preferences.

---

## 9. Deduplication and Rate Limiting

### 9.1 Deduplication at the Delivery Layer

OIS deduplication prevents duplicate signals from generating duplicate notifications. The Notification System adds a second layer for cases where OIS retry or race conditions result in the same notification event arriving twice.

**External delivery deduplication grouping key:**
```
(notification_type, source_system, entity_id, issue_code, recipient_user_id, delivery_channel)
```

If a delivery attempt for this grouping key was made within the cooldown window and was successful, the new attempt is suppressed with reason "recent_delivery_duplicate."

**External delivery cooldown windows:**

| Delivery Channel | INFO | WARNING | CRITICAL | BLOCKED |
|---|---|---|---|---|
| Email | 24h | 12h | 2h | 30min |
| Telegram | 24h | 6h | 1h | 15min |

Dashboard Inbox has no cooldown — all events are recorded.

### 9.2 Per-User Rate Limits

| Channel | INFO | WARNING | CRITICAL | BLOCKED |
|---|---|---|---|---|
| Email | 2/day (digest only) | 5/day | 3/day | No limit |
| Telegram | Digest only | 3/day | 5/day | No limit |

Once a per-user limit is reached:
- The notification is delivered to Dashboard Inbox
- The notification is queued for the next digest (if digest is enabled)
- A "[Daily limit reached]" flag appears in the inbox entry

### 9.3 Per-Factory Rate Limits

| Channel | Daily Total |
|---|---|
| Email | 50 across all users |
| Telegram | 30 across all users |

Per-factory limits prevent a large user base from generating excessive API calls to email or Telegram services. If the factory limit is reached, priority is given to BLOCKED > CRITICAL > WARNING > INFO. INFO and WARNING for later recipients are queued for digest.

### 9.4 Digest Deduplication

If a notification has already been delivered immediately (CRITICAL email) and is also included in a digest (because it wasn't resolved before the next digest window), the digest item is marked as "already notified" and displayed at reduced prominence, or omitted if it is resolved.

---

## 10. Notification Preferences

Users configure their notification preferences in the Notification Inbox → Settings. Factory policy overrides apply as described in §5.4.

### 10.1 Preference Scope

Per-user preferences control:

**Delivery channels:**
- Dashboard Inbox: always enabled, cannot be disabled
- Email: enabled/disabled, email address, severity threshold
- Telegram: enabled/disabled, chat_id setup flow, severity threshold

**Digest:**
- Enabled/disabled
- Frequency: hourly / daily / weekly
- Which notification types to include in digest (default: INFO and non-urgent WARNING)

**Quiet hours:**
- Enabled/disabled
- Start time: HH:MM in factory timezone
- End time: HH:MM in factory timezone

**Subscriptions:**
- Per notification type: enabled/disabled for this user
- Per channel scope: receive notifications only for specific channels
- Per module scope: receive notifications only for specific modules

### 10.2 Subscription Defaults by Role

| Role | Default Subscription |
|---|---|
| Owner | All types, all channels, all modules |
| Admin | All types, all channels, all modules |
| Operator | production_blocked, approval_required, publishing_status, budget_blocked, learning_recommendation |
| Editor | approval_required, publishing_status |
| Client | publishing_status (own channels only, client-safe variant) |
| Viewer | None by default |

Users may expand their subscriptions within their role's permitted scope. Users may not subscribe to notification types outside their role's access (e.g., Operators cannot subscribe to `security_warning`).

### 10.3 Preference UI (Inbox Settings)

```
NOTIFICATION PREFERENCES
──────────────────────────────────────────────────────────────────────
DELIVERY CHANNELS

  Dashboard Inbox      ● Always active (cannot be disabled)

  Email                [✓ Enabled]
    Address:           muhamed.fcp@gmail.com     [Edit]
    Severity threshold: Critical and above        [Edit]
    [Send test email]

  Telegram             [○ Not configured]
    [→ Connect Telegram Bot]

DIGEST
  Enabled:             [✓ Yes]
  Frequency:           Daily at 08:00 UTC        [Edit]
  Include:             Warnings, Info             [Edit]

QUIET HOURS
  Enabled:             [✓ Yes]
  From:                22:00  To: 07:00  (factory timezone)  [Edit]
  Note: Production blocked events override quiet hours.

SUBSCRIPTIONS
  Receive notifications for:
  [✓] All channels  (or specify: The Hypogeum Chronicles [✓] test-channel-2 [○])
  [✓] All modules   (or specify: Analytics [✓] Archive [✓] Learning [✓])

  By type:
  [✓] system_alert         [✓] approval_required    [✓] production_blocked
  [✓] credential_failure   [✓] provider_failure     [✓] budget_warning
  [✓] budget_blocked       [✓] engineering_change   [✓] module_health
  [✓] learning_recommendation  [✓] publishing_status  [✓] security_warning

[Save preferences]
──────────────────────────────────────────────────────────────────────
```

---

## 11. Notification Inbox

The Notification Inbox is the in-dashboard notification center. It is distinct from the Review page (which handles approval workflows). The Inbox handles message awareness, delivery history, and notification lifecycle management.

### 11.1 Inbox Structure

```
NOTIFICATION INBOX
──────────────────────────────────────────────────────────────────────
[◉ 3 unread]    Filter: [All ▾]  Severity: [All ▾]  Status: [Unread ▾]

──── BLOCKED ──────────────────────────────────────────────────────────
[⛔ UNREAD]  Gmail credential expired — publishing blocked
  Source: Credential Manager  ·  6h ago  ·  production_blocked
  Your Gmail OAuth credential has expired. Publishing runs will fail
  until it is renewed.
  [→ Renew in Credentials]  [Acknowledge]

──── CRITICAL ─────────────────────────────────────────────────────────
[✗ UNREAD]  Budget at 94% of monthly ceiling ($282/$300)
  Source: Cost Ledger  ·  3h ago  ·  budget_warning
  At current run rate, the monthly ceiling will be reached after
  approximately 1 more episode.
  [→ Adjust in Settings]  [Acknowledge]

[✗ UNREAD]  ep_005 awaiting your approval  (2h)
  Source: Acceptance Audit  ·  2h ago  ·  approval_required
  All gates passed with 3 non-blocking warnings.
  [→ Review episode]  [Acknowledge]

──── WARNING ──────────────────────────────────────────────────────────
[⚠ READ]   Archive module upgrade available (v1.2.0 → v1.3.0)
  Source: Module Manager  ·  2 days ago  ·  module_health
  This is a minor upgrade. No breaking changes.
  [→ Upgrade in Modules]  [Dismiss]

──── INFO ─────────────────────────────────────────────────────────────
[ℹ READ]   ep_005 published to YouTube successfully
  Source: Platform Connector  ·  18h ago  ·  publishing_status
  [Archive]

[ℹ READ]   Learning system: schedule recommendation applied
  Source: Learning System  ·  2 days ago  ·  engineering_change
  Publish time updated to Thursday 18:00 UTC (was 16:00 UTC).
  [Archive]

──────────────────────────────────────────────────────────────────────
[Mark all read]  [Archive all read resolved]  [Inbox Settings →]
Showing 5 of 12 notifications  [Load more]
──────────────────────────────────────────────────────────────────────
```

### 11.2 Inbox Features

**Unread tracking:** Every notification starts as unread. Becomes read when the operator scrolls it into view (passive read) or explicitly clicks [Acknowledge] (active read). Active acknowledgment is recorded with a timestamp and the user_id.

**Acknowledge vs. Dismiss vs. Archive:**
- Acknowledge: "I have seen this." Marks as read. Does not resolve or dismiss. CRITICAL and BLOCKED stay visible.
- Dismiss: "I don't need to see this anymore." Only available for WARNING and INFO. Removes from default view. Moves to dismissed section.
- Archive: "This is resolved and I want to clear it." Moves to archived section. Available for any resolved notification.

**Filters:**
- Status: Unread / Acknowledged / All
- Severity: All / Blocked / Critical / Warning / Info
- Source: All / [system name]
- Entity: All / [specific channel or module]
- Date range

**Resolved notifications:**
When a notification's underlying condition resolves, the inbox entry is updated:
- BLOCKED entry transitions to CRITICAL (resolved) with a green checkmark
- "Resolved at: 2026-06-13 15:22 UTC" note added
- [Archive] button becomes available

### 11.3 Inbox vs. Review Page

| Aspect | Notification Inbox | Review Page |
|---|---|---|
| Purpose | Message awareness, delivery history | Approval workflow execution |
| Contains | All notification types | Approval-type items only |
| Actions available | Acknowledge, Dismiss, Archive | Approve, Reject, Defer |
| Executes decisions | Never | Yes (approval decisions) |
| Role scope | Per user's notification preferences | Per approval authority |

An `approval_required` notification appears in BOTH: in the Inbox as a notification (awareness), and on the Review page as an approval panel (action). The Inbox entry links to the Review page. The Review page is where the operator actually approves or rejects.

---

## 12. Escalation Rules

Escalation adds recipients or delivery channels when a condition persists without operator action.

### 12.1 Default Escalation Policies

| Trigger | Threshold | Escalation Action |
|---|---|---|
| CRITICAL notification unacknowledged | > 4 hours | Escalate to Owner via all enabled channels |
| BLOCKED notification unacknowledged | > 2 hours | Escalate to Owner via all channels (ignoring quiet hours) |
| `approval_required` pending | > 24 hours | Escalate to Admin + Owner |
| Production remains blocked | > 4 hours continuous | Re-notify Owner + Admin via all channels; increment severity in inbox |
| `credential_failure` persists | > 24 hours (expired credential) | Escalate to Owner; add in-dashboard banner (not just inbox) |
| Provider failure repeats | 3+ failures within 1 hour | Escalate to Admin |
| `budget_blocked` | Immediate | Escalates to Owner on first occurrence (no threshold) |
| Engineering rollback failed | Immediate | Critical escalation to Owner on all channels |

### 12.2 Escalation Notification Format

```
[⛔ ESCALATION]  Gmail credential expired — 6h unacknowledged

This was flagged BLOCKED 6 hours ago (at 09:14 UTC). It has not been
acknowledged. Production publishing runs remain blocked.

ESCALATING TO: Mohamed (Owner) — all channels

Required action: Renew Gmail OAuth credential
[→ Renew in Credentials]

[Acknowledge — stop escalating]  [Snooze 2h]
```

### 12.3 Snooze

Escalation can be snoozed by the escalation recipient:
- Maximum snooze: 2 hours
- Maximum 2 consecutive snoozes (4 hours total)
- After max snoozes: escalation continues without snooze option
- BLOCKED with `production_blocked = true`: max 1 snooze (2 hours)

### 12.4 Escalation Stop Conditions

Escalation stops when:
- The underlying condition is resolved
- An operator with appropriate authority acknowledges the notification
- The maximum escalation window has passed (configurable, default 24 hours — after which the notification remains but escalation ceases)

---

## 13. Integration Contracts

### 13.1 OIS → Notification System

OIS calls `notification_system.enqueue()` when its routing decision includes `destination = "notification_system"`. OIS passes:

```
NotificationEnqueueRequest {
  correlation_id:           string        // OIS signal_id
  notification_type:        string        // OIS-determined type
  severity:                 string        // from OIS PriorityScore
  title:                    string        // short, operator-readable
  summary:                  string        // 1-3 sentence detail
  source_system:            string
  source_entity_id:         string | null
  source_entity_name:       string | null
  affected_entity_desc:     string        // plain English
  required_action:          string | null
  dashboard_link:           string
  production_blocked:       boolean       // from OIS signal
  expires_at:               datetime | null
}
```

The Notification System does not re-score, re-classify, or re-route. It takes OIS's decisions as authoritative and handles delivery. If the Notification System disagrees with a routing decision (e.g., the notification type is not on the allowed list for the Telegram channel), it delivers to the alternative channels and logs the routing constraint.

### 13.2 Approval System → Notification System

When the Approval System creates a new approval request, it calls:
```
notification_system.create_notification(
  notification_type = "approval_required",
  correlation_id = approval_id,
  ...
)
```

When the Approval System resolves an approval (approved, rejected, or auto-expired):
```
notification_system.resolve_notification(
  correlation_id = approval_id,
  resolution_type = "approved" | "rejected" | "expired",
  resolved_at = datetime
)
```

The Notification System updates the inbox entry as resolved. The Approval System does not send notifications directly — all notification delivery goes through the Notification System.

### 13.3 Dashboard Director → Notification System

Dashboard Director reads notification state from the Notification System to produce dashboard summaries. It calls:
```
notification_system.get_notification_inbox(user_id)
  → inbox items (unread count, critical count, pending approvals)
```

Dashboard Director does not deliver notifications. It reads notification state to compose the Home page's unread badge count and the Inbox page.

### 13.4 Credential Manager → Notification System

Credential Manager is a signal source — it calls `OIS.submit_signal()` for credential events. OIS routes to the Notification System. The Notification System receives a sanitized notification payload — no credential values are ever in the payload.

When generating a credential-related notification, the payload must reference the credential by its `label` (e.g., "openrouter-prod") and `provider` (e.g., "OpenRouter") — never by its value. The redaction middleware validates this before delivery.

---

## 14. Data Contracts

### `NotificationEvent`

```typescript
NotificationEvent {
  notification_id:              string      // UUID
  correlation_id:               string      // OIS signal_id
  notification_type:            NotificationType
  severity:                     "info" | "warning" | "critical" | "blocked"
  title:                        string      // ≤80 chars
  summary:                      string      // ≤300 chars
  source_system:                string
  source_entity_id:             string | null
  source_entity_name:           string | null
  affected_entity_desc:         string
  required_action:              string | null
  dashboard_link:               string
  production_blocked:           boolean
  created_at:                   datetime
  expires_at:                   datetime | null
  resolved:                     boolean
  resolved_at:                  datetime | null
  resolution_type:              "operator_action" | "auto_resolved" |
                                "escalation" | "expired" | null
  client_safe_title:            string | null     // Client-role variant
  client_safe_summary:          string | null
  client_safe_dashboard_link:   string | null
  redaction_applied:            boolean           // true if middleware redacted anything
}
```

---

### `NotificationRecipient`

```typescript
NotificationRecipient {
  recipient_id:           string
  notification_id:        string
  user_id:                string
  user_role:              string
  delivery_channels:      DeliveryChannel[]   // which channels were selected
  delivery_status:        "pending" | "delivered" | "failed" |
                          "suppressed" | "queued_for_digest"
  inbox_delivered:        boolean             // Inbox is always attempted
  inbox_delivered_at:     datetime | null
  acknowledged:           boolean
  acknowledged_at:        datetime | null
  dismissed:              boolean
  dismissed_at:           datetime | null
  archived:               boolean
  archived_at:            datetime | null
  escalated:              boolean
  escalated_at:           datetime | null
  escalation_reason:      string | null
}
```

---

### `NotificationDeliveryAttempt`

```typescript
NotificationDeliveryAttempt {
  attempt_id:           string
  notification_id:      string
  recipient_id:         string
  delivery_channel:     "inbox" | "email" | "telegram" | "digest"
  attempt_number:       number          // 1 = first attempt
  attempted_at:         datetime
  status:               "success" | "failed" | "suppressed" | "queued"
  failure_reason:       string | null
  failure_code:         string | null   // e.g., "smtp_bounce", "telegram_bot_blocked"
  next_retry_at:        datetime | null
  message_id:           string | null   // external message ID (email Message-ID, Telegram message_id)
}
```

---

### `NotificationPreference`

```typescript
NotificationPreference {
  user_id:                        string
  updated_at:                     datetime
  inbox_enabled:                  true              // always true, immutable
  email_enabled:                  boolean
  email_address:                  string | null
  email_severity_threshold:       "info" | "warning" | "critical" | "blocked"
  telegram_enabled:               boolean
  telegram_chat_id:               string | null
  telegram_severity_threshold:    "info" | "warning" | "critical" | "blocked"
  digest_enabled:                 boolean
  digest_frequency:               "hourly" | "daily" | "weekly"
  digest_delivery_time:           string            // "08:00" in factory timezone
  quiet_hours_enabled:            boolean
  quiet_hours_start:              string | null     // "22:00"
  quiet_hours_end:                string | null     // "07:00"
  subscriptions:                  NotificationSubscription[]
}

NotificationSubscription {
  subscription_id:    string
  notification_type:  NotificationType | "all"
  scope_type:         "factory" | "channel" | "module"
  scope_id:           string | null
  enabled:            boolean
  delivery_channels:  DeliveryChannel[]   // null = use global preference
}
```

---

### `NotificationDigest`

```typescript
NotificationDigest {
  digest_id:            string
  factory_id:           string
  recipient_user_id:    string
  period_start:         datetime
  period_end:           datetime
  notification_count:   number
  critical_count:       number
  warning_count:        number
  info_count:           number
  items:                DigestItem[]
  delivered_via:        "email" | "telegram"
  delivered_at:         datetime | null
  delivery_attempt_id:  string | null
}

DigestItem {
  notification_id:    string
  notification_type:  NotificationType
  severity:           string
  title:              string            // ≤80 chars
  dashboard_link:     string
  created_at:         datetime
  resolved:           boolean
  already_notified:   boolean           // true if an immediate delivery was already sent
}
```

---

### `EscalationPolicy`

```typescript
EscalationPolicy {
  policy_id:                  string
  factory_id:                 string
  escalation_trigger:         EscalationTrigger
  trigger_threshold_hours:    number | null     // for time-based triggers
  trigger_threshold_count:    number | null     // for count-based triggers
  additional_recipient_roles: string[]          // minimum role to add as recipient
  additional_channels:        DeliveryChannel[] // additional delivery channels
  snooze_allowed:             boolean
  max_snooze_count:           number
  max_snooze_hours:           number | null
  max_escalation_hours:       number            // when to stop escalating (default 24)
}
```

---

### `DeliveryResult`

```typescript
DeliveryResult {
  notification_id:        string
  delivery_channel:       DeliveryChannel
  recipient_count:        number
  successful_count:       number
  failed_count:           number
  suppressed_count:       number
  queued_for_digest:      number
  delivery_attempts:      NotificationDeliveryAttempt[]
  completed_at:           datetime
  all_delivered:          boolean
}
```

---

## 15. API Contracts

### `create_notification()`

```
create_notification(
  correlation_id:           string,
  notification_type:        NotificationType,
  severity:                 Severity,
  title:                    string,
  summary:                  string,
  source_system:            string,
  source_entity_id:         string | null,
  source_entity_name:       string | null,
  affected_entity_desc:     string,
  required_action:          string | null,
  dashboard_link:           string,
  production_blocked:       boolean,
  expires_at:               datetime | null
) → NotificationEvent
```

Called by OIS (via the routing decision) and by the Approval System (for approval-required notifications). This is the primary entry point.

---

### `resolve_recipients()`

```
resolve_recipients(
  notification_event:   NotificationEvent,
  factory_id:           string
) → NotificationRecipient[]
```

Runs role rules, channel scope, factory policy, and user preferences to produce the recipient list and their delivery channels.

---

### `deliver_notification()`

```
deliver_notification(
  notification_id:    string,
  recipient_ids:      string[],
  force_channels:     DeliveryChannel[] | null   // null = use resolved preferences
) → DeliveryResult
```

Executes delivery to all resolved recipients. BLOCKED severity sets `force_channels = all_enabled` regardless of preferences. Called after `resolve_recipients()`.

---

### `deliver_digest()`

```
deliver_digest(
  factory_id:         string,
  recipient_user_id:  string,
  period_start:       datetime,
  period_end:         datetime
) → NotificationDigest
```

Assembles and delivers a digest for one recipient covering the specified period. Called by the scheduled digest job.

---

### `acknowledge_notification()`

```
acknowledge_notification(
  notification_id:  string,
  user_id:          string
) → NotificationRecipient{
  acknowledged:       true,
  acknowledged_at:    datetime
}
```

Records that the operator has seen and acknowledged the notification. Stops escalation timer.

---

### `dismiss_notification()`

```
dismiss_notification(
  notification_id:  string,
  user_id:          string
) → NotificationRecipient{
  dismissed:        true,
  dismissed_at:     datetime
}
```

Only valid for INFO and WARNING. CRITICAL and BLOCKED cannot be dismissed until resolved.

---

### `archive_notification()`

```
archive_notification(
  notification_id:  string,
  user_id:          string
) → NotificationRecipient{
  archived:         true,
  archived_at:      datetime
}
```

Available for any resolved notification.

---

### `retry_failed_delivery()`

```
retry_failed_delivery(
  delivery_attempt_id:  string
) → NotificationDeliveryAttempt{
  attempt_number:     number,   // incremented
  attempted_at:       datetime,
  status:             "success" | "failed"
}
```

Manually triggers a retry for a failed delivery attempt. Used when: operator fixes a delivery issue (e.g., corrects email address) and wants immediate re-delivery rather than waiting for the retry schedule.

---

### `get_notification_inbox()`

```
get_notification_inbox(
  user_id:          string,
  filters:          InboxFilters | null
) → InboxResult{
  items:            NotificationInboxItem[],
  total_count:      number,
  unread_count:     number,
  critical_count:   number,
  blocked_count:    number,
  has_more:         boolean
}

InboxFilters {
  status:           "unread" | "acknowledged" | "dismissed" | "archived" | "all" | null
  severity:         Severity[] | null
  source_system:    string[] | null
  entity_id:        string | null
  date_from:        datetime | null
  date_to:          datetime | null
  page:             number
  page_size:        number    // default 20, max 100
}
```

---

### `get_notification_preferences()`

```
get_notification_preferences(
  user_id: string
) → NotificationPreference
```

---

### `update_notification_preferences()`

```
update_notification_preferences(
  user_id:      string,
  preferences:  Partial<NotificationPreference>
) → NotificationPreference
```

Factory policy overrides are applied server-side after the user's preferences are saved. The returned object reflects the effective preferences (user preference + factory override).

---

### `generate_delivery_report()`

```
generate_delivery_report(
  factory_id:     string,
  period_start:   datetime,
  period_end:     datetime
) → DeliveryReport{
  period_start:             datetime,
  period_end:               datetime,
  total_notifications:      number,
  by_type:                  map<NotificationType, number>,
  by_severity:              map<Severity, number>,
  delivery_success_rate:    number,          // 0.0-1.0
  inbox_delivery_rate:      number,
  email_delivery_rate:      number | null,
  telegram_delivery_rate:   number | null,
  suppressed_count:         number,
  failed_count:             number,
  escalated_count:          number,
  digest_count:             number,
  redaction_triggered_count: number,
  failed_deliveries:        NotificationDeliveryAttempt[]   // only failed attempts
}
```

---

## 16. Dashboard Visibility

### Notification Inbox Page

The Notification Inbox is reachable via the sidebar (bell icon) and as a dedicated page (accessible from any page via `Cmd+K` → "Notification Inbox").

The sidebar shows an unread badge count on the bell icon:
- Count = unread notifications scoped to the current user's role and permissions
- Badge color: red if any BLOCKED or CRITICAL unread; amber if only WARNING; grey if only INFO

```
SIDEBAR BADGE EXAMPLE:
  🔔 5   ← badge in red (has critical/blocked)
```

### Home Page Integration

Dashboard Director reads notification state to compose:
- Pending Actions section: shows BLOCKED and CRITICAL items (from inbox)
- Unread count: total unread notification count for the current user

### Settings → Notifications

```
SETTINGS  /  Notifications
──────────────────────────────────────────────────────────────────────
DELIVERY STATUS
  Email:     ● healthy  ·  Last sent: 3h ago  ·  [Test email]
  Telegram:  ○ Not configured  ·  [Connect]

DELIVERY REPORT  (last 30 days)
  Sent:         48   (12 critical, 22 warning, 14 info)
  Suppressed:   312  (noise floor: 280, rate limit: 32)
  Failed:       2    [View failed]
  Redactions:   0    (no credential leakage detected)

ESCALATION HISTORY
  2 escalations in last 30 days:
  2026-06-10  Gmail credential warning  ·  escalated after 4h  ·  resolved
  2026-06-05  Budget blocked  ·  escalated immediately  ·  resolved

[→ Notification Preferences]  [→ Full delivery report]
──────────────────────────────────────────────────────────────────────
```

---

## 17. Risks and Mitigations

### Risk 1 — Notification fatigue

**Failure mode:** The operator receives 15 emails in a day from the factory. They create an email filter that sends all factory emails to trash. Critical notifications are missed.

**Mitigation:**
- Daily email limit of 10 total, 3 critical. BLOCKED overrides.
- Digest mode available: user opts to receive all non-critical in one daily summary.
- Per-type subscriptions: user disables notification types they don't care about (e.g., `learning_recommendation` for Operators who trust the auto-apply system).
- The delivery report (Settings → Notifications) shows suppressed count, helping operators calibrate their preferences — "312 signals were suppressed last month; you saw 48." This transparency builds trust.

---

### Risk 2 — Missed critical notifications

**Failure mode:** A BLOCKED notification arrives during the operator's quiet hours. Quiet hours mask the notification externally. The operator wakes up to find a credential has been expired for 8 hours and no episodes published during peak hours.

**Mitigation:**
- BLOCKED severity overrides quiet hours for all channels.
- The Dashboard Inbox receives BLOCKED notifications immediately regardless of quiet hours. The operator checks the inbox when they wake up.
- The "[Daily limit reached]" flag in the inbox means the operator knows external delivery was capped — they're not surprised.
- Escalation policy: BLOCKED unacknowledged for >2h escalates to Owner on all channels.

---

### Risk 3 — Secret leakage

**Failure mode:** The Credential Manager's test failure notification includes the credential value in its error message: "Test failed for key sk-proj-12345abcde: invalid authentication."

**Mitigation:**
- Redaction middleware runs on every payload before any delivery, including Dashboard Inbox.
- API key pattern detection (regex for common formats) catches values even if the field name is not "key" or "secret."
- Redaction is logged: if middleware triggered, the `redaction_applied = true` flag is recorded. A daily count of redaction events is shown in Settings → Notifications. Any positive count should prompt a review of which system is emitting un-redacted content.
- The Notification System never directly reads credential values. It only receives events from OIS, which has already processed the Credential Manager's signal. The Credential Manager never puts values in signals — it uses the label and provider name only.

---

### Risk 4 — Client sees internal notification

**Failure mode:** A Client-role user receives a notification mentioning "Factory OS," "Engineering Director," or another internal system name. The client realizes this is a generic platform and not a custom solution built for them.

**Mitigation:**
- Client-safe payload variants are defined for every notification type accessible to Client role.
- `resolve_recipients()` identifies Client-role recipients and switches to the `client_safe_title`, `client_safe_summary`, and `client_safe_dashboard_link` fields.
- Notification types that only apply internally (`engineering_change`, `security_warning`, `system_alert`) are not in the Client role's subscription scope — they cannot be delivered to Clients regardless of preferences.
- Before any new notification type is added, the delivery matrix (§3) must explicitly define whether Client role can receive it and what variant they receive.

---

### Risk 5 — Duplicated notifications

**Failure mode:** OIS retries a routing decision. The Notification System receives the same notification event twice within 30 seconds. The operator receives two identical emails.

**Mitigation:**
- Deduplication grouping key at the delivery layer is independent of OIS deduplication (defense in depth).
- `create_notification()` is idempotent on `correlation_id` — if the same `correlation_id` (OIS signal_id) arrives within the notification's active window, the second call returns the existing `notification_id` rather than creating a new one.
- External channel cooldown windows (§9.1) prevent duplicate email delivery even if two `NotificationEvent` records exist for the same logical condition.

---

### Risk 6 — Notification loops

**Failure mode:** Engineering Director executes a change. Notification System notifies. Operator sees notification and approves something. Approval System triggers a new change. Engineering Director executes. Notification System notifies. Loop continues.

**Mitigation:**
- Each notification in the loop is a genuinely distinct event (different `notification_id`, different `correlation_id`, different timestamp). Deduplication does not suppress them — they are not duplicates.
- Rate limiting is the protection: the email and Telegram daily limits mean only 3 CRITICAL and 10 total emails/day can be sent. Even a legitimate sequence of events cannot flood external channels.
- The `engineering_change` notification type is INFO/WARNING severity by default (only HIGH-risk rollbacks escalate to WARNING). Most engineering change notifications are inbox-only.

---

### Risk 7 — Overuse of Telegram/email for non-urgent events

**Failure mode:** An operator configures Telegram to receive all notification types including INFO. The factory publishes episodes weekly. After each publication, the operator gets a Telegram message. After a few weeks, they block the bot.

**Mitigation:**
- Telegram is not a default channel. Operators must explicitly enable and configure it.
- Telegram is only allowed for specific notification types (§3) — `publishing_status` (success) is not Telegram-eligible. Success notifications are inbox-only or digest-only.
- The severity threshold for Telegram defaults to "critical" when the user first enables it. The user must explicitly lower the threshold to receive WARNING or INFO via Telegram.

---

### Risk 8 — Delivery failure hidden

**Failure mode:** An email delivery fails (SMTP relay error). The failure is logged internally but the operator has no visibility. They believe their email notifications are working. A CRITICAL event occurs and no email arrives.

**Mitigation:**
- The Dashboard Inbox entry for every notification includes a delivery status indicator: "Email: delivered ✓" or "Email: failed ✗ [2 retries remaining]."
- Settings → Notifications shows a delivery report with failed delivery count and links to the failed attempts.
- If an email delivery has failed all retries, a WARNING notification is generated (about the failed notification) in the Dashboard Inbox: "Email delivery failed for: [notification title]. Check your email configuration in Notification Preferences."
- The `generate_delivery_report()` output is accessible to Admins and can be reviewed periodically.

---

### Risk 9 — Quiet hours hiding critical events

**Failure mode:** A credential expires during the operator's quiet hours. Quiet hours suppress the email. The next morning, the operator discovers the factory has been unable to publish for 8 hours.

**Mitigation:**
- As stated in §6.4, BLOCKED severity overrides quiet hours completely for all channels.
- As stated in §8.6, CRITICAL with `production_blocked = true` also overrides quiet hours.
- For CRITICAL without production impact (e.g., budget at 94%): external delivery is queued until quiet hours end, but the Dashboard Inbox is updated immediately. When the operator checks the dashboard in the morning, the CRITICAL notification is at the top of the inbox.
- The PES Risk 9 mitigation applies here: the Dashboard Inbox is the baseline ground truth. External channels are supplements, not the primary delivery surface.

---

## 18. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.275 — Operational Intelligence | OIS calls `notification_system.create_notification()` when its routing decision includes the notification_system destination. OIS severity maps directly to NS severity. NS never re-scores. |
| B5.3 — Dashboard Director | Dashboard Director reads `get_notification_inbox()` to populate the Home page unread badge and pending action counts. DD does not deliver notifications. |
| B5.25 — PES | Inbox component follows PES C5 (Alerts), C12 (Confirmation Dialogs for dismissal). Notification preferences page follows PES C3 (Forms). PES P6 (every alert has action) enforced in notification payload — `dashboard_link` is required. |
| B5 — Dashboard Architecture | The Notification Inbox is a sub-section of the Dashboard (accessible via sidebar bell icon). It is not in the primary nav (not one of the 6 Core pages). It is reachable from any page via the bell icon and global search. |
| B4.9 — Engineering Director | Engineering Director calls `notification_system.create_notification()` for engineering_change events. Rollback failures trigger immediate CRITICAL escalation. |
| B5.4 — Approval System (next) | Approval System calls `create_notification()` for approval_required and calls `resolve_notification()` when approval is decided. Notification System does not execute approvals — it delivers the notification and records the lifecycle. |
| Credential Manager | Credential Manager emits `credential_signal` to OIS → OIS routes to NS. The credential label and provider name are in the notification. Credential values are never present. |
| Platform Connectors | Platform Connectors emit publishing events → OIS → NS. Client-role users receive client-safe variants of publishing_status notifications. |
