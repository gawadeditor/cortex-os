# Notification System Governance

**Phase:** B5.22  
**Depends on:** B5.13 (Authorization — capability checks for preference management), B5.21 (OIS Governance — sole authority on notification eligibility and bypass conditions), B5.35 (Notification System Design — operational behavior this document governs)  
**Supersedes:** B5.35 informal governance assumptions. B5.35 continues to own notification types, recipient resolution algorithms, payload rules, delivery channel mechanics, inbox design, and API contracts. B5.22 owns the formal notification lifecycle, channel governance rules, subscription authority, quiet hours authority, digest authority, escalation delivery authority, retry rules, audit records, retention policy, and the boundary between OIS and the Notification System.  
**Feeds into:** Dashboard (Notification Inbox, bell-icon badge, Home pending-action counts), Operators (via email, Telegram, webhook), B5.13 (capability grants for preference management)

---

## 1. Purpose

Notification System Governance (NSG) is the formal authority over the notification delivery lifecycle. It defines how a `NotificationRequest` from OIS becomes a delivered message, who may govern each step, what records are produced, and what constraints are inviolable.

**The governing principle:** OIS decides. The Notification System delivers. Every delivery decision is traceable to an OIS signal. No notification is generated without an OIS `NotificationRequest`. The Notification System may not independently decide to send, suppress, escalate, or cancel a notification based on its own assessment of urgency.

### 1.1 What NSG does not govern

| Action | Owner |
|---|---|
| Decide whether a signal warrants notification | B5.21 OIS |
| Set signal severity | B5.21 OIS (inherits from emitting system) |
| Suppress or resolve OIS signals | B5.21 OIS |
| Escalate signal classification (WARNING → CRITICAL) | B5.21 OIS |
| Determine which signals bypass quiet hours | B5.21 OIS (§10.2 bypass conditions) |
| Daily notification budget enforcement | B5.21 OIS (§9.2) |
| Signal deduplication | B5.21 OIS (§8) |

### 1.2 What NSG governs exclusively

| Action | NSG authority |
|---|---|
| Notification lifecycle states and transitions | §2 |
| Delivery channel registry and governance rules | §4 |
| Subscription defaults and override authority | §5 |
| Quiet hours policy ownership and scope | §6 |
| Digest schedule ownership and minimum intervals | §7 |
| Escalation delivery to additional recipients/channels | §8 |
| Retry governance (max attempts, backoff, escalation after failure) | §9 |
| Notification audit record schema and immutability | §10 |
| Notification data retention policy | §11 |

### 1.3 The NS receives; it does not re-score

When OIS calls `enqueue(NotificationRequest)`, the Notification System accepts OIS's severity, urgency, and bypass flags as authoritative. It may not:
- Downgrade severity (a BLOCKED notification may not become CRITICAL inside NS)
- Upgrade severity (NS may not escalate signal classification — only OIS may)
- Change the bypass decision (if OIS says bypass quiet hours, NS must bypass; if OIS says respect quiet hours, NS must respect)
- Reject a `NotificationRequest` on content grounds — the only valid rejection is a schema validation error

**NS constraint violations are logged and escalated to OIS** via `submit_signal(signal_type="security_warning", ...)`. An NS that silently drops a BLOCKED notification is a governance violation, not an accepted behavior.

---

## 2. Notification Lifecycle

### 2.1 Lifecycle states

Every notification is in exactly one state at any time.

| State | Category | Description |
|---|---|---|
| `queued` | Active | `NotificationRequest` received from OIS; Notification System has accepted it but not yet begun delivery preparation |
| `preparing` | Active | Recipient resolution and payload assembly in progress |
| `sending` | Active | At least one external delivery attempt is in flight (email sending, Telegram API call) |
| `delivered` | Terminal | All recipients across all required channels have confirmed delivery |
| `partial` | Terminal (partial) | At least one recipient or channel confirmed delivery; at least one failed after all retries |
| `failed` | Terminal (recoverable) | All delivery attempts exhausted on all channels for all recipients; no delivery confirmed |
| `expired` | Terminal | Notification's `expires_at` passed before delivery was confirmed |
| `cancelled` | Terminal | The underlying OIS signal was resolved before delivery completed; NS cancels in-flight delivery |

### 2.2 Lifecycle transition table

| From | To | Trigger | Authority |
|---|---|---|---|
| (none) | `queued` | OIS calls `enqueue(NotificationRequest)` | OIS (via B5.21 routing decision) |
| `queued` | `preparing` | NS begins recipient resolution | System: NS (automatic, within SLA) |
| `queued` | `cancelled` | OIS emits signal resolution before `preparing` begins | System: NS (on OIS signal_resolved event) |
| `queued` | `expired` | `expires_at` passes while queued | System: NS (timer) |
| `preparing` | `sending` | Recipient resolution complete; payload assembled | System: NS (automatic) |
| `preparing` | `cancelled` | OIS signal resolved during preparation | System: NS (on OIS event) |
| `sending` | `delivered` | All recipients on all channels confirmed | System: NS (on delivery callbacks) |
| `sending` | `partial` | Mix of confirmed and exhausted-retry outcomes | System: NS (on final retry exhaustion) |
| `sending` | `failed` | All delivery attempts exhausted; zero confirmations | System: NS (on final retry exhaustion) |
| `sending` | `expired` | `expires_at` passes during sending | System: NS (timer) |
| `sending` | `cancelled` | OIS signal resolved during sending | System: NS — but see §2.3 |
| `failed` | `sending` | Admin+ manually retriggers delivery | Operator (`notification.admin`) |
| `partial` | `sending` | Admin+ retriggers for failed recipients/channels | Operator (`notification.admin`) |

### 2.3 Cancellation during active sending

If OIS resolves a signal while the Notification System is mid-send:
- **External channels (email, Telegram):** If the API call has already been submitted and an HTTP response is pending, the send is not cancelled — the message will arrive. NS records the delivery outcome and flags the notification `cancelled` after the response is received. The notification recipient may receive a message about a condition that has already resolved; this is acceptable. NS appends a resolution notice to the inbox entry ("This condition has been resolved — 2 min ago").
- **Inbox:** The inbox entry is immediately updated to show the resolution state. Operators who see the notification see it in a resolved state.

**NS may not kill an in-flight email or Telegram API call.** This is the delivery-layer equivalent of B5.19 Rule 4 (in-flight uploads are non-cancellable).

### 2.4 Delivery SLAs by severity

OIS's bypass flag and severity determine NS's delivery SLA — the maximum time from `queued` to `sending`:

| Severity | `production_blocked` | Quiet hours bypass? | Delivery SLA |
|---|---|---|---|
| `blocked` | true | Always bypass | < 60 seconds |
| `critical` | true | Always bypass | < 60 seconds |
| `critical` | false | OIS bypass flag determines | < 5 minutes |
| `warning` | false | No | < 15 minutes (immediate) or next digest window |
| `info` | false | No | Next digest window |

If NS cannot meet the SLA for a BLOCKED notification (e.g., system overloaded), it emits a `module_unavailable` signal to OIS and records the delay in the audit trail.

---

## 3. Governing Rules

Six rules are inviolable. An NS implementation that violates any of them is non-conformant regardless of other behavior.

**Rule 1 — Inbox always receives.**  
Every notification that reaches `preparing` state is delivered to the Dashboard Inbox. Inbox delivery is attempted before any external channel. External channel failure does not affect inbox delivery. Inbox delivery failure is a critical system fault (NS emits `module_unavailable` signal to OIS).

**Rule 2 — BLOCKED bypasses all delivery constraints.**  
A notification with `production_blocked = true` or `severity = blocked` bypasses quiet hours, daily rate limits, digest batching, and cooldown windows on all channels. This is enforced at the NS level independently of OIS's bypass flag — NS validates: if `production_blocked = true`, apply bypass. This is defense-in-depth; OIS has already set the bypass flag, but NS must not rely solely on OIS having done so correctly.

**Rule 3 — Secrets are never in notification content.**  
Redaction middleware runs on every notification payload before external dispatch (email body, Telegram message, webhook body). Redaction is applied to title, summary, required_action, and any custom fields. Known secret patterns (API keys, OAuth tokens, bearer tokens, password strings) are replaced with `[redacted]`. Redaction is logged per notification (`redaction_applied = true` in the `NotificationAuditEvent`). Any bypass of redaction is a security fault, not an accepted exception.

**Rule 4 — NS does not modify OIS decisions.**  
NS severity, urgency, bypass, and routing decisions arrive from OIS and are immutable within NS. NS may route to fewer channels than permitted (e.g., Telegram not configured) but may not route to channels OIS did not authorize, and may not change the severity or bypass flag.

**Rule 5 — Delivery layer deduplication is independent of OIS deduplication.**  
OIS deduplication prevents duplicate `NotificationRequest` objects from arriving at NS. NS adds a second layer: if the same `correlation_id` (OIS `signal_id`) arrives twice within 5 minutes, NS delivers the first and records the second as `deduplicated` in the audit trail. This protects against race conditions and retry scenarios in OIS itself.

**Rule 6 — Every notification is auditable.**  
Every lifecycle transition produces an immutable `NotificationAuditEvent` (§10). No state change is made without a corresponding audit record. A notification that is cancelled, expired, or fails after all retries is not silently dropped — it remains in the audit trail with a complete lifecycle history.

---

## 4. Channel Governance

### 4.1 Channel registry

The following delivery channels are formally registered. An unregistered channel may not receive notifications.

| Channel | Status | NS-owned config | Operator-owned config | OIS bypass respect |
|---|---|---|---|---|
| `inbox` | **Required** (always active) | None | None — cannot be disabled | N/A (no external delivery; bypasses all limits) |
| `email` | **V1 — optional** (user-enabled) | SMTP provider, sender domain, bounce handling | Address, severity threshold, digest preference | Quiet hours bypassed for BLOCKED; respected otherwise |
| `telegram` | **V1 — optional** (user-enabled) | Bot token (factory credential), per-chat rate limits | Chat ID setup, severity threshold | Quiet hours bypassed for BLOCKED; respected otherwise |
| `webhook` | **Future** | Target URL validation, HMAC signing key | Endpoint URL, severity threshold | Configurable per webhook registration |
| `sms` | **Future** | Provider API key (factory credential) | Phone number | Always bypass (emergency-only channel) |

### 4.2 Channel governance rules

**Inbox:**
- May not be disabled by any operator, including Owner
- Inbox failure is reported to OIS as `module_unavailable` within 60 seconds
- Inbox records are the ground truth for notification history

**Email:**
- Sender address is factory-scoped configuration (Admin+ with `notification.preferences.admin`)
- Maximum: 50 total email notifications/day across all users per factory (factory-wide rate limit)
- Maximum: 3 critical emails/day per user (enforced at NS, reflecting OIS daily budget)
- Per-user rate limit: no more than 20 email notifications/day (preventing any single user from consuming the factory budget)
- HTML emails must include a plaintext fallback
- Every email must include an unsubscribe link (CAN-SPAM / GDPR compliance)
- No images, attachments, or embedded tracking pixels
- Unsubscribe actions are honored within 10 business days; the NS must honor them within 60 seconds of receipt

**Telegram:**
- Bot token is stored in the Credential Manager (B5.9) — never in NS configuration directly
- Per-chat rate limit: 3 messages/hour, 15 messages/day (non-BLOCKED)
- BLOCKED severity: up to 5 messages/hour if blocking condition persists
- Per-factory rate limit: 30 Telegram messages/day across all users
- Message content is limited to 1000 characters
- No internal entity IDs, path segments, or credential-adjacent content in Telegram messages
- If Telegram delivery fails, NS falls back to email for the same notification (if email is configured and the notification type is email-eligible)

**Adding a new channel (extensibility governance):**
A new channel may only be added to the registry by:
1. A design document update to B5.35 specifying the channel's mechanics
2. A B5.22 registry update adding the channel (Owner authorization, Approval System routing — same governance as adding a system actor capability)
3. A B5.13 capability update if the channel requires a new user-facing permission

### 4.3 Channel fallback chain

NS applies a fallback chain when primary channel delivery fails:

```
BLOCKED severity:
  1. Inbox (always, synchronous)
  2. Email (if configured, immediately)
  3. Telegram (if configured, immediately)
  4. If all fail: Owner's inbox receives "[ALL DELIVERY CHANNELS FAILED]" flag

CRITICAL severity:
  1. Inbox (always)
  2. Email (if configured and within daily limit)
  3. Telegram (if configured, type is Telegram-eligible, and within rate limit)
  4. On failure of all external channels: inbox flag only

WARNING/INFO:
  1. Inbox (always)
  2. Digest (batched; no fallback needed — inbox is the delivery)
```

The fallback chain is executed in order. A successful delivery at any step does not prevent delivery at subsequent steps if the notification type warrants multi-channel delivery. Fallback replaces a failed primary channel, not an additional channel.

---

## 5. Subscription Governance

### 5.1 What a subscription controls

A subscription is a per-user, per-notification-type, per-scope configuration that determines whether a given user receives a notification. It is distinct from channel configuration (which governs HOW a user receives notifications) and from quiet hours (which governs WHEN).

Subscription dimensions:
- **By notification type** (which event types the user wants to receive)
- **By channel scope** (receive notifications for all channels or only specific channels)
- **By module scope** (receive notifications for all modules or only specific ones)

### 5.2 Role default subscriptions

Default subscriptions are applied at role assignment and may be modified by the user within their role's permitted scope.

| Role | Default subscription |
|---|---|
| Owner | All notification types, all channels, all modules |
| Admin+ | All notification types, all channels, all modules |
| Operator+ | `production_blocked`, `approval_required`, `publishing_status`, `budget_blocked`, `credential_failure`, `learning_recommendation` |
| Editor | `approval_required`, `publishing_status` (own channels only) |
| Client | `publishing_status` (own channels only, client-safe variant) |
| Viewer | None |

**Role defaults are floor, not ceiling.** A user may expand their subscriptions up to the capability ceiling for their role. They may not subscribe to notification types outside their role's access scope (enforced by `check_authorization()` on preference save).

### 5.3 Required minimum subscriptions

Certain subscriptions are mandatory and may not be unsubscribed from, regardless of user preference:

| Role | Mandatory subscription | Why |
|---|---|---|
| Owner | `production_blocked`, `security_warning`, `emergency_halt`, `budget_blocked` | Owner must be reachable for factory-critical events |
| Admin+ | `production_blocked`, `security_warning` | Admin is the operational escalation target |
| Any user who holds `system.emergency_halt` | `emergency_halt` (resolution notifications) | Authority to resolve requires awareness |

Attempting to unsubscribe from a mandatory subscription is rejected at the API layer with a `subscription_mandatory` error.

### 5.4 Subscription authority

| Action | Capability required | Notes |
|---|---|---|
| Edit own subscriptions within role scope | `notification.preferences.edit` | Any user; role ceiling enforced |
| Edit another user's subscriptions | `notification.preferences.admin` | Admin+ only |
| Expand a user's subscription beyond role ceiling | N/A — blocked | Not possible; role ceiling is enforced structurally |
| Set factory-wide subscription defaults (affects new users) | `notification.preferences.admin` | Admin+ |
| Disable mandatory subscriptions | N/A — blocked | Not possible; mandatory subscriptions cannot be removed |

### 5.5 Subscription scope constraints

A user's subscription scope must not exceed their authorization scope. Examples:
- An Operator scoped to `channel:hypogeum` may subscribe to notifications for that channel only, not for `channel:comedy-shorts`
- A Client user may subscribe only to `publishing_status` for their authorized channels
- If a user's channel scope is revoked, their channel-specific subscriptions are automatically deactivated (not deleted — they are preserved and reactivated if scope is restored)

Subscription scope enforcement calls `check_authorization()` at subscription creation time and at each notification delivery time. If a user's authorization has changed between subscription creation and delivery, NS applies current authorization (not subscription state) to determine delivery.

---

## 6. Quiet Hours Governance

### 6.1 Ownership

Quiet hours policy is owned by the user (per-user configuration). The Notification System enforces quiet hours at delivery time. OIS determines whether a bypass condition applies.

**Three-layer model:**

```
OIS (bypass decision)
  └─→ NS policy check (quiet hours active for this user?)
        └─→ Channel delivery (respects or bypasses per OIS flag)
```

NS does not decide whether a bypass is warranted — it receives that decision from OIS via the `production_blocked` flag and the OIS bypass conditions defined in B5.21 §10.2. NS enforces the result.

### 6.2 Quiet hours configuration

Per-user, per-factory-timezone:
- Start time: HH:MM (24-hour, factory timezone)
- End time: HH:MM (24-hour, factory timezone)
- Enabled / disabled
- Scope: applies to email and Telegram only — Inbox never observes quiet hours

Factory Admin+ may set a factory-wide quiet hours default (applied to new users). Users may override the factory default for their own account.

**Quiet hours constraints:**
- Minimum quiet window: 1 hour (cannot set start = end)
- Maximum quiet window: 12 hours (prevents operators from going permanently dark; enforced at configuration save)
- A factory may not set a factory-wide quiet hours default that covers more than 10 hours for any timezone

### 6.3 What quiet hours affect

| Delivery mode | Quiet hours respected? | Exception |
|---|---|---|
| Dashboard Inbox | **Never** (inbox always delivers) | — |
| Email — BLOCKED | **No** (always delivers) | OIS bypass |
| Email — CRITICAL with `production_blocked = true` | **No** (always delivers) | OIS bypass |
| Email — CRITICAL without `production_blocked` | **Yes** — queued until quiet hours end | Delivered at end of quiet window |
| Email — WARNING / INFO | **Yes** — queued until quiet hours end (or included in next digest) | — |
| Telegram — BLOCKED | **No** (always delivers) | OIS bypass |
| Telegram — CRITICAL with `production_blocked = true` | **No** (always delivers) | OIS bypass |
| Telegram — CRITICAL without `production_blocked` | **Yes** — queued until quiet hours end | — |
| Telegram — WARNING / INFO | **Yes** — not sent during quiet hours; digest only | — |

### 6.4 Notifications queued during quiet hours

CRITICAL notifications received during quiet hours are:
1. Immediately delivered to Inbox (quiet hours do not apply to Inbox)
2. Marked `pending_external_delivery` in the inbox with a "[Will deliver at 07:00]" label
3. Held in the NS delivery queue until quiet hours end
4. Delivered to external channels at the moment quiet hours end (delivery at exactly the configured end time, not batched into a digest)

If a CRITICAL notification's `expires_at` passes during the quiet hours window, it is not delivered externally. It remains in Inbox as `expired`. A `NotificationAuditEvent` is recorded: `quiet_hours_expiry`.

---

## 7. Digest Governance

### 7.1 What digest governs

Digest is a delivery mode that batches multiple INFO and non-urgent WARNING notifications into a single summary message, delivered on a schedule rather than individually.

**Digest applies to:** INFO severity, non-urgent WARNING (i.e., WARNING with no `time_sensitivity_hours < 24` flag from OIS)  
**Digest does not apply to:** CRITICAL, BLOCKED — always immediate  
**Digest channel:** Email only. Telegram does not have a digest mode (Telegram messages are always immediate or not sent).

### 7.2 Digest configuration

Per-user:
- Enabled / disabled (default: enabled)
- Frequency: `hourly`, `daily` (default), `weekly`
- Delivery time: for daily/weekly, a configured time in the factory timezone (default: 08:00 factory timezone)
- Minimum interval: NS enforces a 1-hour minimum between digests regardless of configured frequency

Factory Admin+ may set a factory-wide digest default. Users may override it.

### 7.3 Digest content rules

A digest email includes:
- All INFO and non-urgent WARNING notifications accumulated since the previous digest
- Grouped by severity: WARNING items first, INFO items second
- Within each group: ordered by `composite_score` descending
- Maximum items per digest email: 25. If more than 25 notifications have accumulated, the digest includes the 25 highest-scored items and a count: "... and 14 more in your Dashboard Inbox"
- Each item: severity emoji, title, age ("3h ago"), and one `dashboard_link`

**Digest does not include:** CRITICAL or BLOCKED items. If a CRITICAL notification arrived since the last digest and was immediately delivered, the digest may include a brief "Earlier CRITICAL:" summary line as a reference, but the primary delivery was the immediate notification, not the digest.

### 7.4 Digest authority

| Action | Authority |
|---|---|
| User enables/disables own digest | `notification.preferences.edit` |
| User changes own digest frequency | `notification.preferences.edit` |
| Admin+ sets factory-wide digest default | `notification.preferences.admin` |
| Admin+ disables digest for all users (factory policy) | `notification.preferences.admin` — records a `NotificationAuditEvent` |
| Disabling digest for a specific user without their consent | Not permitted without `notification.preferences.admin` |

A factory may not disable digest entirely without providing an alternative mechanism for INFO notification accumulation. If digest is disabled factory-wide, INFO notifications must accumulate in the Inbox only (no external delivery). This is acceptable behavior.

---

## 8. Escalation Delivery Governance

### 8.1 Two distinct escalation concepts

**OIS escalation** (B5.21 §7, §8.4): Signal severity increases (WARNING → CRITICAL). OIS re-classifies the signal and re-routes a new `NotificationRequest` to NS. This is a signal lifecycle change, not a notification delivery change.

**NS escalation delivery** (this section): A notification of fixed severity goes unacknowledged beyond a time threshold. NS sends the same notification to additional recipients or channels. The signal severity does not change — only the delivery expands.

These are completely distinct mechanisms with different ownership. OIS escalation is a signal-classification decision. NS escalation is a delivery-expansion decision. They may co-occur (OIS upgrades the signal while NS is also escalating delivery), but they are governed independently.

### 8.2 Escalation delivery authority

NS may expand delivery recipients and channels based on time-based thresholds. It may not:
- Change notification severity
- Modify the `NotificationRequest` content
- Create a new OIS signal
- Suppress the original notification in favor of the escalated one

Escalation delivery is additive: the original recipients continue to see the notification; new recipients are added.

### 8.3 Escalation threshold governance

| Condition | Threshold | Escalation action | Authority |
|---|---|---|---|
| CRITICAL unacknowledged | > 4 hours | Deliver to Owner via all enabled channels | System: NS (automatic) |
| BLOCKED unacknowledged | > 2 hours | Deliver to Owner via all channels (ignore quiet hours) | System: NS (automatic) |
| `approval_required` pending | > 24 hours | Deliver to Admin+ and Owner | System: NS (automatic) |
| Production blocked continuously | > 4 hours | Re-notify Owner + Admin+ via all channels | System: NS (automatic) |
| `credential_failure` (expired) unresolved | > 24 hours | Escalate to Owner; add in-dashboard banner | System: NS (automatic) |
| All delivery channels fail (BLOCKED) | Immediately after final retry | Owner inbox receives `[ALL DELIVERY CHANNELS FAILED]` flag | System: NS (automatic) |
| Engineering rollback failed | Immediate | CRITICAL escalation to Owner on all channels | System: NS (on receipt of this notification type) |

**Escalation thresholds may be adjusted by Admin+** (`notification.preferences.admin`) within bounds: minimum 30 minutes, maximum 48 hours. The BLOCKED and all-channels-failed escalations may not be adjusted — they are hardcoded minimums.

### 8.4 Escalation snooze

The escalation recipient may snooze further escalation:
- Maximum snooze: 2 hours
- Maximum consecutive snoozes: 2 (4 hours total)
- BLOCKED with `production_blocked = true`: maximum 1 snooze (2 hours)
- After maximum snoozes: escalation continues; snooze option is removed from the inbox entry

A snooze is recorded as a `NotificationAuditEvent` with event_type `escalation_snoozed`. Snooze does not resolve the underlying condition.

### 8.5 Escalation stop conditions

NS stops escalating when:
1. The underlying OIS signal transitions to `resolved` or `auto_resolved`
2. Any operator with appropriate authority acknowledges the notification
3. The escalation maximum window has elapsed (default: 24 hours from first notification, configurable by Admin+ between 4 hours and 72 hours)

After the maximum window, the notification remains active in the Inbox but no further external escalation deliveries are sent. NS records `escalation_max_reached` in the audit trail.

---

## 9. Retry Governance

### 9.1 Retry authority

NS owns retry decisions for external channel delivery failures. OIS is not involved in retries — once OIS has called `enqueue()`, NS is responsible for all subsequent delivery attempts.

### 9.2 Retry policy by channel and severity

| Channel | Severity | Max retries | Backoff | Escalation after exhaustion |
|---|---|---|---|---|
| Email | BLOCKED | Aggressive: every 10 minutes until delivered or condition resolves | None (fixed interval) | All-channels-failed flag to Owner inbox |
| Email | CRITICAL | 3 retries | 5min, 30min, 2h | Attempt Telegram fallback; then inbox flag |
| Email | WARNING | 3 retries | 5min, 15min, 1h | Inbox flag only (no further action) |
| Email | INFO | 1 retry | 5min | Inbox flag (email dropped; inbox already has it) |
| Telegram | BLOCKED | 5 retries | 2min intervals | Fall back to email; then all-channels-failed flag |
| Telegram | CRITICAL | 3 retries | 5min, 30min, 2h | Fall back to email |
| Telegram | WARNING / INFO | 1 retry | 5min | No further action (digest equivalent in inbox) |

### 9.3 Retry governing rules

1. A retry uses the same `notification_id`. It is not a new notification — it is a new `NotificationDeliveryAttempt` against the same notification record.
2. Retries do not reset the quiet hours check: if quiet hours expired during the retry backoff and the notification is now past quiet hours, the next retry delivers without quiet hours constraint.
3. Retries do not consume additional daily notification budget quota — only the first attempt counts against the daily limit. A retry is not a new notification.
4. If the underlying OIS signal is resolved before a retry fires, the retry is cancelled. A `NotificationAuditEvent` with event_type `retry_cancelled_signal_resolved` is recorded.
5. No retry window may exceed 24 hours from the first delivery attempt. A notification that has been retrying for 24 hours without success transitions to `failed` state regardless of remaining retry count.

---

## 10. Notification Audit Records

### 10.1 Immutability rule

Every notification lifecycle transition and delivery event produces an immutable `NotificationAuditEvent`. No audit event may be updated or deleted. Corrections are additive (new event appended). The audit trail for each notification must make it possible to reconstruct the complete delivery history: who received it, on which channels, when, and with what outcome.

### 10.2 NotificationAuditEvent schema

```
NotificationAuditEvent {
  audit_event_id:         string            // stable, globally unique, immutable
  notification_id:        string            // the notification this event belongs to
  correlation_id:         string            // OIS signal_id
  factory_id:             string
  channel_id:             string | null     // affected channel if channel-scoped
  episode_id:             string | null     // affected episode if episode-scoped

  event_type:             enum(
    notification_queued,
    notification_preparing,
    notification_sending,
    notification_delivered,
    notification_partial,
    notification_failed,
    notification_expired,
    notification_cancelled,
    delivery_attempt,               // one per attempt per channel per recipient
    delivery_success,               // confirmed delivery from channel
    delivery_failure,               // failed delivery with reason
    delivery_suppressed,            // quiet hours, rate limit, or channel constraint
    delivery_deduplicated,          // second arrival of same correlation_id
    retry_scheduled,
    retry_fired,
    retry_cancelled_signal_resolved,
    escalation_triggered,
    escalation_delivered,
    escalation_snoozed,
    escalation_max_reached,
    escalation_stopped,
    digest_queued,
    digest_delivered,
    quiet_hours_held,
    quiet_hours_expiry,
    quiet_hours_released,
    inbox_delivered,
    inbox_acknowledged,
    inbox_dismissed,
    inbox_archived,
    redaction_applied,
    mandatory_subscription_enforced,
    subscription_rejected,
    preference_changed,
    all_channels_failed
  )

  actor:                  string            // "system:ns" or user_id
  actor_type:             enum(system, operator)
  recipient_user_id:      string | null     // which user this event concerns (null = factory-wide)
  delivery_channel:       string | null     // "inbox" | "email" | "telegram" | "webhook" | null
  severity:               enum(info, warning, critical, blocked)
  production_blocked:     boolean
  lifecycle_state_before: string
  lifecycle_state_after:  string
  detail:                 string            // human-readable description
  failure_reason:         string | null     // for delivery_failure events
  failure_code:           string | null     // e.g., "smtp_bounce", "telegram_rate_limited"
  external_message_id:    string | null     // email Message-ID or Telegram message_id if available
  occurred_at:            timestamp
  recorded_at:            timestamp
}
```

### 10.3 Required audit events per notification lifecycle

Every notification MUST produce at minimum:
1. `notification_queued` — on receipt from OIS
2. `inbox_delivered` — when Inbox delivery completes
3. One `delivery_attempt` per external channel per recipient
4. One terminal event: `notification_delivered`, `notification_partial`, `notification_failed`, `notification_expired`, or `notification_cancelled`

Missing any of these four events in the audit trail is a conformance failure.

---

## 11. Notification Retention Policy

### 11.1 Standard retention

| Record type | Retention period | After expiry |
|---|---|---|
| `NotificationEvent` (main record) | 365 days | Soft-deleted; retained in audit store for 5 years |
| `NotificationAuditEvent` | 365 days minimum; 5 years for BLOCKED and CRITICAL | Never hard-deleted |
| `NotificationDeliveryAttempt` | 90 days | Purged; summary counts retained in `NotificationEvent` |
| Inbox entries (Dashboard Inbox) | Operator-controlled; system auto-archives after 90 days (WARNING) / 30 days (INFO) | Archived entries remain searchable for 1 year |
| `NotificationRecipient` | 365 days | Anonymized after 365 days (user_id replaced with role identifier) |

### 11.2 Extended retention (mandatory for governance audit)

The following records are retained indefinitely (no expiry, no purge by standard archive policy):
- All `NotificationAuditEvent` records where `event_type = all_channels_failed`
- All `NotificationAuditEvent` records where `severity = blocked`
- All records associated with notifications whose `correlation_id` maps to an OIS `emergency_halt` or `unsafe_override_attempt` signal

These records are excluded from B5.12 standard archive policy purges.

### 11.3 Retention authority

| Action | Capability required |
|---|---|
| View notification audit trail | `system.audit.view` (factory-scoped) |
| Export notification audit records | `system.audit.export` (factory-scoped) |
| Configure retention periods | Owner only (`factory.danger_zone`) — only to extend, not to shorten |
| Request deletion of personal notification data (GDPR) | Handled via user account deletion flow; audit records anonymized, not deleted |

---

## 12. Authority Matrix

### 12.1 Human user capabilities (B5.13)

The following capabilities govern Notification System actions. Existing capabilities from B5.13 §3.13 apply; B5.22 adds one new capability.

| Capability | Exists in B5.13? | Description |
|---|---|---|
| `notification.preferences.edit` | ✓ §3.13 | Edit own notification preferences (delivery channels, quiet hours, digest, subscriptions) |
| `notification.preferences.admin` | ✓ §3.13 | Edit notification preferences for any user; set factory-wide defaults |
| `notification.admin` | **New — §12.2** | Administrative NS actions: retriger failed deliveries, view full audit trail, manage channel credentials |

### 12.2 New capability: `notification.admin`

B5.22 introduces one new capability that must be added to B5.13's catalog:

| Capability | Description | Scope | Minimum role |
|---|---|---|---|
| `notification.admin` | Administrative Notification System actions: re-trigger failed/partial deliveries, view full delivery audit trail, manage NS channel credentials (Telegram bot token, SMTP config) | factory | Admin+ |

Role matrix addition:

| Capability | Owner | Admin+ | Operator+ | Operator | Editor | Client |
|---|---|---|---|---|---|---|
| `notification.admin` | ✓ | ✓ | — | — | — | — |

### 12.3 Decision authority boundary table

| Decision | OIS authority | NS authority | No one may |
|---|---|---|---|
| Notification eligibility | **OIS only** | Receives; does not reconsider | Bypass OIS to create a notification |
| Signal severity | **OIS only** | Reflects; does not change | Downgrade or upgrade severity within NS |
| Quiet hours bypass | **OIS only** (bypass conditions) + NS (Rule 2 for BLOCKED) | Enforces; cannot override OIS bypass | Suppress a BLOCKED notification on quiet hours grounds |
| Daily notification budget | **OIS only** | Tracks count; reports to OIS | Exceed OIS budget or grant NS its own budget |
| Signal suppression | **OIS only** | Does not suppress signals | Suppress an OIS signal from within NS |
| Delivery channel selection | OIS does not specify channels | **NS only** (per operator subscription and channel config) | OIS may not mandate a specific delivery channel |
| Notification formatting | OIS provides title/summary | **NS only** (formats per channel) | OIS may not write email bodies or Telegram messages |
| Escalation delivery (recipient expansion) | Not involved | **NS only** (per §8 thresholds) | NS may not change severity as part of escalation |
| Retry decisions | Not involved after enqueue | **NS only** | OIS may not re-enqueue after NS has accepted delivery |
| Audit records | OIS audit (signal lifecycle) | **NS audit** (notification delivery lifecycle) | Neither may modify the other's audit records |

---

## 13. Integration Boundaries

### 13.1 OIS → NS (the only source of notification decisions)

OIS is the only system authorized to call NS's `enqueue()` API. No other governance system (B5.16 REG, B5.17 RLG, B5.18 CALOG, B5.19 PG, B5.20 RSG) may call NS directly. All of those systems emit signals to OIS; OIS decides whether to notify and calls NS.

**Exception — Approval System (B5.4):** B5.35 §13.2 describes the Approval System calling `create_notification()` and `resolve_notification()` directly. Under B5.22 governance, this is permissible only because B5.4 does not decide whether to notify (OIS has already done so for the `approval_request` signal); B5.4 is providing resolution metadata. The Approval System's direct call is for `resolve_notification()` only — to mark an approval notification resolved when the decision is made. It does not call `enqueue()`.

**Corrected boundary:**
- `enqueue()` — OIS only
- `resolve_notification()` — OIS (signal resolved) OR B5.4 (approval decision recorded) — the latter is a lifecycle update, not a new routing decision

### 13.2 NS → OIS (failure signals only)

NS calls OIS's `submit_signal()` in exactly two cases:
1. Inbox delivery failure: `signal_type = "module_unavailable"`, identifying NS as the unavailable module
2. All-channels-failed for BLOCKED severity: `signal_type = "alert"`, severity = `critical`, informing OIS that the BLOCKED notification could not be delivered externally

NS may not submit signals to OIS for any other reason. NS does not evaluate signal importance; it does not create new signals based on delivery patterns.

### 13.3 Dashboard → NS

The Dashboard reads NS for:
- Inbox entries (`get_notification_inbox()`)
- Unread badge count
- Home page pending-action notification counts

The Dashboard does not write to NS. All notification creation flows through OIS → NS.

### 13.4 NS does not call governance systems

NS may not call B5.16 REG, B5.17 RLG, B5.18 CALOG, B5.19 PG, B5.20 RSG, or B5.21 OIS for anything other than the specific failure-signal submission in §13.2. NS is a terminal delivery system. It delivers and records. It does not query production state, channel state, run state, or review state.

---

## 14. Dashboard Visibility

### 14.1 Notification Inbox (existing, B5.35 §11)

The Dashboard Inbox is the primary NS surface. Every notification appears here. B5.35 §11 governs the Inbox layout and behavior. B5.22 adds governance rules to it:

- Inbox entries are the ground truth for notification delivery (Rule 1)
- An Inbox entry may not be deleted by any operator action — it may be archived but remains accessible to `system.audit.view` holders
- Inbox entries for BLOCKED notifications may not be archived while the blocking condition is active; they can only be acknowledged

### 14.2 NS health on Dashboard

A compact NS health indicator appears in the System Health panel (governed by B5.275 / Dashboard Director):

```
NOTIFICATION SYSTEM
  Status:         ● Healthy
  Last delivered: 4 minutes ago
  Queued:         0
  Failed (24h):   1  [View →]
  Suppressed (24h): 37 (quiet hours: 12, rate limit: 25)
  Channels:       Email ● Telegram ●
```

If NS transitions to `unhealthy` (Inbox delivery failing, or queue depth > 50 pending for > 10 minutes), a `module_unavailable` signal is submitted to OIS. OIS routes an alert to Home.

### 14.3 Delivery failure visibility

When a notification reaches `failed` or `partial` state, the Inbox entry shows:
```
⚠ Delivery issue: Email delivery failed (3 attempts)
  Last attempt: Jun 14 16:42 UTC — smtp_bounce
  Inbox: ✓ delivered · Email: ✗ failed · Telegram: ● not configured
  [Retry email →]   (requires notification.admin)
```

---

## 15. Risk Register

### Risk 1 — OIS bypass flag not set; BLOCKED notification silently respects quiet hours (MEDIUM)

**Failure mode:** An OIS bug fails to set `production_blocked = true` on an `emergency_halt` notification. NS, receiving a CRITICAL notification without the bypass flag, respects the operator's quiet hours. The operator does not receive the Telegram message at 02:00. They wake up to find production was halted for 7 hours.

**Mitigation:** NS Rule 2 (§3) is defense-in-depth: NS independently validates that if `severity = blocked`, bypass applies regardless of OIS's flag. Additionally, `notification_type = "emergency_halt"` is on NS's internal hardcoded-bypass list (independently of OIS). The inbox always delivers. The failure mode degrades to "operator didn't see Telegram" not "operator wasn't notified at all."

### Risk 2 — Escalation delivery and OIS signal escalation create duplicate notifications (LOW)

**Failure mode:** OIS upgrades a WARNING signal to CRITICAL (signal escalation). At the same time, NS's 4-hour unacknowledged escalation fires (delivery escalation). The operator receives two escalation notifications in close succession: one from OIS's re-routing (new severity), one from NS's threshold firing.

**Mitigation:** NS deduplicates by `correlation_id`. When OIS re-routes a severity-upgraded signal, it sends a new `NotificationRequest` with the same `correlation_id` but a new severity. NS updates the existing notification's severity and records the upgrade as a `NotificationAuditEvent`, rather than creating a new notification. The escalation delivery timer resets when a severity upgrade arrives — the "CRITICAL for 4 hours unacknowledged" threshold starts from when the severity became CRITICAL, not from the original WARNING creation time.

### Risk 3 — Telegram bot token leaked via notification content (HIGH)

**Failure mode:** An operator-configured Telegram channel is somehow included in a notification payload (e.g., a signal about credential issues includes the credential detail). The Telegram chat receives a message containing the bot token.

**Mitigation:** Rule 3 (§3) — redaction middleware runs on every payload before external dispatch. Known secret patterns are replaced with `[redacted]`. The Telegram bot token itself is stored in the Credential Manager (B5.9), not in NS configuration — NS accesses it only to make the API call, and it is never included in message content. Redaction is logged per notification; a zero-redaction policy for messages about credentials is flagged as anomalous.

### Risk 4 — Mandatory subscription for Owner cannot be enforced if Owner account is deleted (LOW)

**Failure mode:** The only Owner-role user account is deleted (or deactivated) while a BLOCKING condition is active. NS escalates to Owner, finds no active Owner account, and the `[ALL DELIVERY CHANNELS FAILED]` flag goes to an empty inbox.

**Mitigation:** B5.13's user access revocation flow must verify that at least one active Owner account remains after a revocation. B5.22 adds a pre-condition check: when NS resolves Owner recipients for an escalation notification and finds no active Owner, it submits a `security_warning` signal to OIS (severity: critical) with detail "no active Owner account found during escalation delivery." This is a factory configuration defect, not an NS defect, but NS must surface it.

---

## 16. Local-First and SaaS Readiness

### 16.1 Local-first V1

NS stores all `NotificationEvent`, `NotificationRecipient`, `NotificationDeliveryAttempt`, and `NotificationAuditEvent` records in the local factory database. Inbox delivery is synchronous. External delivery (email, Telegram) is async with a local queue.

`get_notification_inbox()` is a hot path (Dashboard bell badge). Target: < 20ms (local database read, indexed on `(factory_id, recipient_user_id, resolved = false)`).

Email sending uses a configured SMTP server. Telegram uses the Telegram Bot API. Both are external calls with timeout handling (max: 10 seconds per attempt before retry).

### 16.2 SaaS readiness

**Per-factory isolation:** All notification records are scoped to `factory_id`. No notification data crosses tenant boundaries.

**Audit log immutability:** In SaaS, `NotificationAuditEvent` records are written to an append-only audit log store (separate from the operational database). Row-level constraints prevent UPDATE or DELETE on audit records.

**Multi-channel delivery at scale:** A factory with 10 channels running simultaneously may generate hundreds of notifications per day. NS applies factory-wide rate limits (§4.2) before any per-user logic. Rate limit state is stored in a factory-scoped atomic counter, not per-user.

---

## 17. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.13 — Authorization | `check_authorization()` called on: preference save (validate user has `notification.preferences.edit`), subscription expansion (validate new subscription is within role ceiling), admin actions (`notification.preferences.admin`, `notification.admin`). B5.22 adds one new capability: `notification.admin`. |
| B5.21 — OIS Governance | OIS is the sole source of `NotificationRequest` objects (via `enqueue()`). OIS bypass conditions (B5.21 §10.2) are authoritative over quiet hours. OIS signal resolution drives NS `notification_cancelled` transitions. NS submits `module_unavailable` signals to OIS on critical delivery failure. |
| B5.35 — Notification System Design | B5.35 governs operational behavior (notification types, recipient resolution algorithms, payload rules, inbox design, full API contracts). B5.22 governs the formal lifecycle, authority, audit records, and boundary rules on top of B5.35. Neither supersedes the other. |
| B5.4 — Approval System | Approval System calls NS's `resolve_notification()` when an approval decision is made. It does not call `enqueue()`. All initial approval notifications route through OIS → NS. |
| B5.18 — CALOG | CALOG emits channel lifecycle signals to OIS. OIS routes relevant ones to NS. NS delivers to subscribed operators. CALOG does not call NS directly. |
| B5.19 — PG | PG emits publication signals to OIS. OIS routes to NS. `content_hold` notifications route to Owner. PG does not call NS directly. |
| B5.20 — RSG | RSG emits review signals to OIS. `unsafe_override_attempt` is immediately routed to NS for Owner notification (bypass all limits). RSG does not call NS directly. |
| Dashboard Director | Dashboard reads NS inbox via `get_notification_inbox()`. Updates bell badge and Home notification counts. Does not write to NS. |
