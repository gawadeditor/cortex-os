# Credential Manager Design

**Phase:** B5.9  
**Depends on:** B5 (Dashboard Architecture), B5.275 (OIS), B5.35 (Notification System), B5.4 (Approval System), B4.75 (Channel Profile), B4 (Configuration Layer)  
**Feeds into:** Runtime Config Layer, OIS (credential signals), Publishing Pipeline, Analytics Layer, Notification System, Provider/Model Director, Channel Activation

---

## 1. Purpose

The Credential Manager is the secure operating layer for all external authentication material in Factory OS. It manages API keys, OAuth connections, service accounts, webhook secrets, SMTP credentials, and platform tokens — the full set of secrets that connect the factory to external providers and platforms.

Its operating mandate is three-part:

1. **Security:** Raw secret values are never accessible after save. No API endpoint returns a secret value. No log contains a secret value. No export contains a secret value.

2. **Usability:** Non-programmer operators can set up and manage credentials through the dashboard without touching configuration files or the terminal.

3. **Observability:** The factory always knows the health and readiness of every credential it depends on, and surfaces that state before it becomes a blocking problem.

The Credential Manager does not authenticate on behalf of systems. It stores, tests, and resolves credentials at runtime. The calling system authenticates using the resolved credential — the Credential Manager hands over the value in memory and its involvement ends.

---

## 2. Architecture

### 2.1 Inputs

| Input source | What it provides |
|---|---|
| Factory Settings | Factory ID, encryption key reference, storage backend configuration |
| Credential Profiles | Profile definitions, assignments, and metadata managed by operators |
| Provider Registry | List of known providers, their credential type requirements, and test endpoint definitions |
| Platform Connectors | OAuth flow callbacks, token refresh triggers, connector health events |
| Notification System | Credential references for SMTP and Telegram delivery (reads readiness, does not store secrets itself) |
| Publishing Pipeline | Credential readiness checks before publishing runs |
| Runtime Configuration Layer | Credential resolution requests at run time (receives ref, returns resolved value in memory) |
| Dashboard Credentials Page | All credential CRUD operations, OAuth initiation, test triggers |
| User / Role System | Authority checks for all Credential Manager write operations |

### 2.2 Outputs

| Output | Destination |
|---|---|
| `credential_profile` records | Dashboard (Credentials page), Channel Activation, Config Layer |
| `credential_health_status` | Dashboard, OIS, Dashboard Director (Home warning strip) |
| `credential_readiness_status` | Channel Activation gate, Config Layer, PMD |
| `credential_test_results` | Dashboard (after test), audit trail |
| `credential_rotation_warnings` | OIS (as credential_signal, routed to NS) |
| `credential_failure_signals` | OIS (as credential_signal, routed to NS and Home) |
| `sanitized_credential_references` | Config Layer, all API responses (label + type only, never value) |
| Resolved credential value | Runtime memory only, never written anywhere |

### 2.3 The non-output invariant

The Credential Manager has one absolute output constraint: raw secret values are never returned by any interface. This includes:
- API responses (all field names matching value schema are stripped by scrubbing middleware)
- Logs (credential values are replaced with their label)
- Exports (explicitly excluded)
- Dashboard UI (masked display only after initial entry)
- Run records and snapshots (credential references only, never values)
- Notification payloads (scrubbed before submission to NS)

This invariant is enforced at three independent layers: the Credential Manager API itself returns no values; the API gateway has a scrubbing middleware that strips value-shaped fields from all responses; the Dashboard client is stateless about credential values after the form submission response.

---

## 3. Credential Types

Ten credential types are defined. Each type has a distinct storage structure, test method, and lifecycle.

### 3.1 api_key

**What it is:** A static string key issued by a provider.  
**Storage:** Encrypted string, single value.  
**Rotation:** Manual — operator replaces the value when the provider issues a new key.  
**Expiry:** Usually none (unless provider enforces rotation). Credential Manager treats api_keys as non-expiring unless `expires_at` is explicitly set.  
**Test method:** Minimal authenticated API call to provider health or list endpoint.  
**Examples:** OpenRouter API key, MUAPI API key, ElevenLabs API key, Anthropic API key, Perplexity API key.

### 3.2 oauth_connection

**What it is:** An OAuth 2.0 connection consisting of an access token and refresh token pair.  
**Storage:** Encrypted `access_token` + `refresh_token` + `token_expiry` + `scopes` + `platform_user_id` (where available).  
**Rotation:** Automatic — access token is refreshed using the refresh token before expiry. Refresh token itself must be renewed by the operator (re-authorization) when it expires or is revoked.  
**Expiry:** Access tokens typically expire in 1 hour to 24 hours. Refresh tokens may last 6 months to indefinitely depending on platform.  
**Test method:** Token introspection endpoint or lightweight authenticated API call.  
**Examples:** YouTube OAuth connection, Gmail OAuth connection, TikTok OAuth connection, Instagram/Meta OAuth connection.

### 3.3 service_account

**What it is:** A structured credential set associated with a service identity rather than a user. May include a private key file, client_id/client_secret pair, or similar.  
**Storage:** Encrypted JSON object containing provider-specific fields.  
**Rotation:** Manual — operator replaces when service account credentials are regenerated.  
**Expiry:** Varies by provider. Credential Manager stores `expires_at` if the provider defines one.  
**Test method:** Provider-specific authentication flow (e.g., JWT signing + token exchange for Google service accounts).  
**Examples:** Google service account (for YouTube Data API with service account auth), cloud storage service accounts.

### 3.4 webhook_secret

**What it is:** A shared secret used to validate inbound webhook payloads.  
**Storage:** Encrypted string, single value.  
**Rotation:** Manual — operator must coordinate rotation with the webhook sender.  
**Expiry:** Usually none.  
**Test method:** No live test possible without an inbound webhook. Test is limited to format validation (minimum length, character set). Status is `saved_not_tested` by default and must be confirmed by the operator as valid.  
**Examples:** Platform webhook signing secret, publishing confirmation webhook.

### 3.5 smtp_credentials

**What it is:** SMTP connection parameters for email delivery.  
**Storage:** Encrypted object: `host`, `port`, `username`, `password`, `use_tls`, `from_address`.  
**Rotation:** Manual — operator updates when SMTP password changes.  
**Expiry:** Usually none. `expires_at` set if operator knows of a scheduled rotation.  
**Test method:** SMTP EHLO + AUTH handshake. Does not send a test email unless operator requests it. Rate limit safe: a handshake does not consume email quota.  
**Examples:** Gmail SMTP (app password), SendGrid SMTP, custom SMTP relay.

### 3.6 telegram_bot_token

**What it is:** A Telegram Bot API token used to send messages to a configured chat or channel.  
**Storage:** Encrypted object: `bot_token`, `chat_id`, `bot_username` (for display).  
**Rotation:** Manual — operator generates a new token from Telegram's BotFather.  
**Expiry:** Usually none. Tokens are revoked when regenerated.  
**Test method:** `getMe` Telegram Bot API call to verify token validity. Does not send a message.  
**Examples:** Notification Telegram bot, alert channel bot.  
**Security note:** `chat_id` is sensitive — it can be used to send messages to the configured chat. Treated with the same protection as the bot token.

### 3.7 platform_access_token

**What it is:** A long-lived platform API token that does not use OAuth flow. Issued directly by the platform and entered manually.  
**Storage:** Encrypted string.  
**Rotation:** Manual.  
**Expiry:** Platform-defined. Operator sets `expires_at` if known.  
**Test method:** Authenticated API call to platform endpoint.  
**Examples:** TikTok API access token (non-OAuth), custom platform tokens, legacy API credentials.

### 3.8 refresh_token

**What it is:** A standalone refresh token stored separately from an OAuth connection, used to re-acquire access tokens programmatically without a full re-authorization flow.  
**Storage:** Encrypted string.  
**Rotation:** Automatic (new refresh tokens replace old ones after use, when platform follows OAuth rotation policy).  
**Expiry:** Long-lived; platform-specific.  
**Test method:** Use refresh token to acquire a new access token. If the token is expired or revoked, the test fails.  
**Note:** In most cases this type is not used standalone — it is a component of `oauth_connection`. It exists as a standalone type for platforms that issue refresh tokens separately from their OAuth flow.

### 3.9 local_environment_secret

**What it is:** A credential value sourced from the local environment (`.env` file or OS environment variable). This type exists as a migration shim only.  
**Storage:** Not stored by the Credential Manager — it reads the value from the environment at resolve time. The Credential Manager stores a `LookupRecord` that maps a credential_id to an environment variable name.  
**Rotation:** Occurs outside the Credential Manager. The Credential Manager detects the change on next health check.  
**Expiry:** Not tracked.  
**Test method:** Environment variable lookup + provider-specific test call.  
**Migration path:** When a `local_environment_secret` is added to the Credential Manager, the dashboard prompts the operator to convert it to an `api_key` or other appropriate type stored in the encrypted store. The `local_environment_secret` type is intentionally second-class — it exists to accommodate the current `.env` state without breaking production while the operator migrates.  
**End state:** No `local_environment_secret` records should exist in a fully configured factory. The Credentials page shows a migration warning for any credential in this type.

### 3.10 external_secret_reference

**What it is:** A pointer to a credential stored in an external secret manager (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager). The Credential Manager stores the reference path and fetches the value at runtime.  
**Storage:** Encrypted reference string (the path/ARN/key, not the secret value itself).  
**Rotation:** Managed externally. Credential Manager triggers a health check after the operator signals that the external secret has been rotated.  
**Expiry:** Managed externally. The Credential Manager does not independently track expiry for external references — it relies on health checks.  
**Test method:** Fetch the secret from the external manager and run the provider-specific test call.  
**V1 scope:** Supported for future-readiness. In V1 with local-first deployment, this type is not required but is defined so the architecture supports it without changes.

---

## 4. Supported Credential Domains

Thirteen credential domains define the full surface of external authentication in Factory OS.

| Domain | Providers | Credential types | Production impact if missing |
|---|---|---|---|
| AI inference (LLM) | Anthropic, OpenAI, OpenRouter | api_key | Blocking — all text generation stops |
| AI inference (vision/multimodal) | OpenRouter, Anthropic | api_key | Blocking — VPD, BGA, QA Agent stop |
| Image generation | MUAPI, Replicate, Stability | api_key | Blocking — visual asset generation stops |
| Video generation | MUAPI (Seedance), Runway | api_key | Blocking — video production stops |
| Voice synthesis | ElevenLabs | api_key | Blocking — audio production stops |
| Research / web access | Perplexity, Brave Search | api_key | Partial — research agents fall back to reduced capability |
| YouTube (publishing) | YouTube Data API v3 | oauth_connection | Blocking for YouTube channels |
| TikTok (publishing) | TikTok API | oauth_connection, platform_access_token | Blocking for TikTok channels |
| Instagram / Meta (publishing) | Meta Graph API | oauth_connection | Blocking for Instagram channels |
| Gmail / Email (notifications) | Gmail API, SMTP relay | oauth_connection, smtp_credentials | Non-blocking — inbox fallback available |
| Telegram (notifications) | Telegram Bot API | telegram_bot_token | Non-blocking — email/inbox fallback |
| Storage / Archive | S3-compatible, GCS, local | service_account, api_key | Partial — asset archiving fails; production continues |
| Analytics connectors | YouTube Analytics API, platform-specific | oauth_connection, service_account | Non-blocking — analytics ingestion pauses; production continues |

**Production-blocking vs. non-blocking:** A credential domain is production-blocking if its failure prevents an episode from being produced or published. Non-blocking domains affect operational visibility or notification delivery but do not stop production.

---

## 5. Credential Profile Model

A channel references credential profiles, not individual credentials. Profiles are logical groups of credentials that serve a specific purpose.

### 5.1 Profile types

| Profile type | Purpose | Contains |
|---|---|---|
| `ai_providers` | All LLM, image, video, and voice generation credentials | API keys for all AI providers in use |
| `platform_publishing` | Publishing credentials for a specific channel's platforms | OAuth connections for YouTube, TikTok, Instagram per channel |
| `platform_analytics` | Analytics API credentials for a channel | OAuth connections or service accounts for analytics APIs |
| `notification_email` | SMTP or Gmail credentials for email delivery | smtp_credentials or gmail oauth_connection |
| `notification_telegram` | Telegram bot credentials | telegram_bot_token |
| `storage_archive` | Storage credentials for asset archiving | service_account or api_key for storage provider |

### 5.2 CredentialProfile schema

```
CredentialProfile {
  credential_profile_id:     string              // stable, immutable slug e.g., "default_ai_providers"
  profile_name:              string              // display name e.g., "Default AI Providers"
  profile_type:              enum(ai_providers, platform_publishing, platform_analytics,
                                   notification_email, notification_telegram, storage_archive)
  provider:                  string | null       // primary provider if single-provider profile
  credential_refs:           CredentialRef[]     // list of credential slots in this profile
  assigned_channels:         string[]            // channel_ids this profile is assigned to
  assigned_modules:          string[]            // module types this profile serves
  status:                    CredentialProfileStatus  // derived from worst credential in profile
  last_tested_at:            timestamp | null    // last time any credential in profile was tested
  health:                    CredentialProfileHealth  // aggregate health across all credentials
  created_by:                string              // user_id
  created_at:                timestamp
  updated_at:                timestamp
  updated_by:                string
}
```

### 5.3 Profile status derivation

The profile's status is derived from the worst-status credential it contains:

```
profile_status = worst_of(
  credential_refs[*].readiness_status
)

Worst to best:
  missing > expired > test_failed > revoked > disabled >
  saved_not_tested > expiring_soon > ready
```

A profile is `ready` only when every credential_ref it contains is `ready` or `expiring_soon`. A profile is `expiring_soon` if the soonest-expiring credential expires within 14 days but all credentials are currently valid.

### 5.4 Example profiles

```
"default_ai_providers"
  type: ai_providers
  credentials:
    - openrouter-prod     (api_key, ready)
    - elevenlabs-prod     (api_key, ready)
    - muapi-prod          (api_key, ready)
    - perplexity-prod     (api_key, ready)
  assigned_channels: all
  status: ready

"hypogeum_youtube"
  type: platform_publishing
  provider: YouTube
  credentials:
    - youtube-hypogeum    (oauth_connection, ready)
  assigned_channels: [hypogeum]
  status: ready

"notification_gmail"
  type: notification_email
  provider: Gmail
  credentials:
    - gmail-notify        (oauth_connection, expiring_soon — 5 days)
  assigned_channels: []       // not channel-specific — factory-wide
  assigned_modules: [notification_system]
  status: expiring_soon

"telegram_alerts"
  type: notification_telegram
  credentials:
    - telegram-hypogeum   (telegram_bot_token, ready)
  assigned_modules: [notification_system]
  status: ready
```

---

## 6. Secret Storage Rules

Six rules govern how secret values are stored and accessed. These rules are architectural constraints, not policy preferences.

**Rule 1 — Write-only after save.** Raw secret values are submitted once (at creation or replacement) and are never returned by any API. The only operation on a secret value after save is resolution in memory at runtime. There is no read endpoint for secret values.

**Rule 2 — No export.** Secret values are excluded from all exports by default and by design. Export endpoints do not accept flags or parameters that would include secrets. An export that somehow contains a secret value is a bug, not a feature.

**Rule 3 — No clone.** Cloning a credential profile copies the profile metadata (name, type, assignments, credential_refs structure) but not the secret values. Cloned profiles are created with empty credential slots that the operator must populate. Cloning a channel or factory does the same — the profile structure is copied; the secrets are not.

**Rule 4 — No runtime snapshot persistence.** The Config Layer resolves credential values in memory at run start. The resolved value is used for the duration of the run and is not persisted to the run record, the run snapshot, or any log. If a run is interrupted and replayed, the credential is re-resolved from the Credential Manager — not from a prior run record.

**Rule 5 — Log labels only.** All logging within the Credential Manager and all systems that interact with it must use credential labels (e.g., `"youtube-hypogeum"`) rather than values. If a secret value appears in a log, it is a bug. Audit events log which credential was accessed (by label and credential_id), by whom, and when — never the value.

**Rule 6 — Dashboard masks always.** The dashboard displays the last 4 characters of an API key in a masked format (e.g., `••••••••••••4A2F`) for operator identification. It never shows the full value. The edit form for a credential does not pre-populate the value field. To replace a credential, the operator types the new value into an empty field.

### 6.1 Encrypted storage

In V1 (local-first), the Credential Manager stores encrypted credential values in an encrypted local credential store. The encryption key is:
- Derived from a factory-specific key stored in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service via libsecret)
- Falls back to an encrypted file store if no OS keychain is available
- Never stored in `.env`, `config.json`, or any file that could be version-controlled

The `.env` file is read on startup only as a migration detection step. Detected `.env` credentials are flagged as `local_environment_secret` type and the operator is prompted to migrate them to the encrypted store.

---

## 7. Credential Readiness Model

### 7.1 Readiness levels

| Level | Meaning | Color |
|---|---|---|
| `missing` | No credential has been saved for this required slot | Red |
| `saved_not_tested` | Credential saved but never tested (or test result has expired) | Amber |
| `test_failed` | Last connectivity test returned an error | Red |
| `ready` | Last test passed; not within the expiry warning window | Green |
| `expiring_soon` | Last test passed; credential expires within 14 days | Amber |
| `expired` | `expires_at` has passed, OR OAuth refresh has failed | Red |
| `disabled` | Operator has manually disabled this credential (not deleted) | Grey |
| `revoked` | Credential marked as revoked (compromise or decommission) | Red with lock icon |

### 7.2 How readiness affects factory operations

| Operation | `ready` | `expiring_soon` | `saved_not_tested` | `test_failed` / `expired` | `missing` | `disabled` / `revoked` |
|---|---|---|---|---|---|---|
| Channel activation | ✓ | ✓ (warning shown) | ✗ blocked | ✗ blocked | ✗ blocked | ✗ blocked |
| Production run start | ✓ | ✓ (warning in run record) | ✗ blocked | ✗ blocked | ✗ blocked | ✗ blocked |
| Publishing | ✓ | ✓ | ✗ blocked | ✗ blocked | ✗ blocked | ✗ blocked |
| Notifications (external) | ✓ | ✓ | Inbox only | Inbox only | Inbox only | Inbox only |
| Analytics ingestion | ✓ | ✓ | ✗ paused | ✗ paused | ✗ paused | ✗ paused |
| AI provider (inference) | ✓ | ✓ | ✗ blocked | ✗ blocked | ✗ blocked | ✗ blocked |

**`saved_not_tested` blocks production** because an untested credential may be incorrectly entered. The operator must confirm the credential works before the factory can depend on it.

**`expiring_soon` allows production** because the credential is currently valid. The expiry warning creates urgency but does not stop the factory. If a credential expires during a run, the run fails at the point of use — this is treated as a runtime credential failure, not a readiness gate failure.

### 7.3 Credential readiness check for channel activation

Before a channel can be set to `active`, the Credential Manager runs a `CredentialReadinessReport` for the channel:

| Category | Required profiles | Blockers if missing |
|---|---|---|
| AI providers | `ai_providers` (factory-wide default or channel-specific) | All AI production stops |
| Image/video providers | Included in `ai_providers` profile | Visual production stops |
| Voice provider | Included in `ai_providers` profile | Audio production stops |
| Primary publishing platform | `platform_publishing` for each enabled_platform in Channel Profile | Publishing stops for that platform |
| Notification credentials | `notification_email` or `notification_telegram` (if operator-configured) | Not blocking — inbox fallback available |
| Analytics credentials | `platform_analytics` (if `learning_goals.track_audience = true`) | Not blocking — analytics pauses but production continues |

The activation readiness check is a hard gate. The channel cannot be set to `active` if any blocking credential profile is not in `ready` or `expiring_soon` state.

---

## 8. Credential Testing

### 8.1 Test principles

- **Rate limit safe:** No test makes more than one API call. No credential is tested more than once per 5 minutes by the system (operator-triggered tests bypass this limit with a 60-second cooldown).
- **External call minimal:** Tests use the lightest available endpoint (health ping, token introspection, or equivalent). Tests do not consume metered quota (e.g., do not generate an image to test the image API).
- **Timeout:** 10 seconds per test. If the provider does not respond within 10 seconds, the test returns `timeout` as the failure reason.
- **Retry:** One automatic retry after a 5-second delay if the first attempt times out. No retry on authentication errors (wrong key, revoked token) — these should fail immediately.
- **Logged:** Test timestamp, result (pass/fail), failure reason (if any) are logged. The credential value is never logged.

### 8.2 Test methods per credential type

| Credential type | Test method | Success indicator | Failure reason types |
|---|---|---|---|
| `api_key` (OpenRouter, Anthropic, OpenAI) | `GET /models` or `/ping` endpoint | 200 response with valid JSON | 401 (invalid key), 429 (rate limited — treated as transient, not failure), timeout |
| `api_key` (ElevenLabs) | `GET /voices` (list) | 200 with voice list | 401, timeout |
| `api_key` (MUAPI) | `GET /models` or provider health endpoint | 200 | 401, timeout |
| `api_key` (Perplexity) | `GET /models` | 200 | 401, timeout |
| `oauth_connection` (YouTube) | `GET /youtube/v3/channels?mine=true&part=id` | 200 with channel data | 401 (token expired), 403 (insufficient scope), timeout |
| `oauth_connection` (Gmail) | `GET /gmail/v1/users/me/profile` | 200 with email address | 401, 403, timeout |
| `oauth_connection` (TikTok) | `/v2/user/info/` | Valid response | 401, scope error, timeout |
| `oauth_connection` (Instagram/Meta) | `GET /me?fields=id,name` | Valid response | 401, scope error, timeout |
| `service_account` | Provider-specific auth flow | Valid access token issued | Auth failure, timeout |
| `webhook_secret` | Format validation only | Minimum 16 characters, valid characters | Too short, invalid characters |
| `smtp_credentials` | SMTP EHLO + AUTH | AUTH acceptance | Authentication failure, connection refused, timeout |
| `telegram_bot_token` | `getMe` Bot API | `{"ok": true}` | Invalid token (401), timeout |
| `platform_access_token` | Provider-specific minimal call | 200 | 401, timeout |
| `refresh_token` | Token refresh flow | New access token returned | Expired, revoked, timeout |
| `local_environment_secret` | Read environment variable + underlying provider test | Same as underlying type | Variable not set + underlying type failures |
| `external_secret_reference` | Fetch from external manager + underlying provider test | Fetch success + provider test pass | Vault access failure + underlying type failures |

### 8.3 Rate limiting safety for 429 responses

When a test receives a 429 (rate limited) response, this is treated as a transient condition, not a credential failure. The test result is recorded as `rate_limited` (a distinct sub-state of the `saved_not_tested` readiness level). The credential is not marked `test_failed`. An automatic re-test is scheduled for 15 minutes later.

### 8.4 What gets logged per test

```
{
  credential_id:        "cred_<uuid>",
  credential_label:     "youtube-hypogeum",
  credential_type:      "oauth_connection",
  test_triggered_by:    "operator" | "scheduled" | "channel_activation",
  test_started_at:      timestamp,
  test_completed_at:    timestamp,
  result:               "pass" | "fail" | "timeout" | "rate_limited",
  failure_reason:       "401_invalid_token" | "403_scope_insufficient" | ... | null,
  new_readiness_status: "ready" | "test_failed" | "expiring_soon" | ...,
  // credential value: NEVER logged
}
```

---

## 9. OAuth Flow Design

### 9.1 Supported OAuth providers

| Provider | OAuth version | Scopes required (Publishing) | Scopes required (Analytics) |
|---|---|---|---|
| YouTube | OAuth 2.0 | `youtube.upload`, `youtube.readonly`, `youtube.force-ssl` | `yt-analytics.readonly` |
| TikTok | OAuth 2.0 | `video.upload`, `user.info.basic` | `user.insights.creator.readonly` (if available) |
| Instagram / Meta | OAuth 2.0 | `instagram_content_publish`, `instagram_basic`, `pages_read_engagement` | `read_insights` |
| Gmail | OAuth 2.0 | `gmail.send`, `gmail.readonly` (for delivery confirmation) | — |

### 9.2 Connect flow

```
Step 1 — Operator initiates
  Operator clicks [Connect] for a platform on the Credentials page.

Step 2 — State token generation
  Credential Manager generates a cryptographically random state token.
  State token is stored server-side with:
    - factory_id
    - credential_label (operator-provided)
    - credential_profile_id (if known)
    - expiry: 10 minutes
    - initiated_by: user_id

Step 3 — Redirect
  Operator is redirected to the OAuth provider's authorization URL with:
    - client_id (factory's registered OAuth app)
    - redirect_uri (factory's callback URL)
    - scope (required scopes for the use case)
    - state (the state token)
    - access_type=offline (to receive refresh token)
    - prompt=consent (to force scope display)

Step 4 — Scope display
  The OAuth provider shows the operator the permissions being granted.
  The factory's OAuth app description includes the scope being requested.
  The operator approves or denies.

Step 5 — Callback
  Provider redirects to the factory callback URL with code + state.
  Credential Manager validates state token:
    - Exists in store
    - Not expired
    - factory_id matches current factory
    - Consumed (state token is single-use; deleted after validation)

Step 6 — Token exchange
  Credential Manager exchanges the code for access_token + refresh_token.
  Tokens are immediately encrypted and stored.
  Credential record is created with status = saved_not_tested.

Step 7 — Automatic test
  Credential Manager runs the credential test immediately.
  If test passes: status → ready (or expiring_soon if token expiry is within 14 days).
  If test fails: status → test_failed with failure reason.

Step 8 — Operator confirmation
  Dashboard shows: "Connected successfully" or "Connected but test failed — check permissions."
  Operator is returned to the Credentials page.
```

### 9.3 Callback handling

The callback URL must be a factory-registered URL. In local-first V1, this is a localhost URL. In SaaS V2, this is a tenant-scoped URL on the SaaS domain.

PKCE (Proof Key for Code Exchange) is used where supported by the provider to protect the authorization code exchange in local-first deployments.

### 9.4 Token storage

After a successful OAuth flow, the Credential Manager stores:
```
{
  access_token:        encrypted string (short-lived, used for API calls)
  refresh_token:       encrypted string (long-lived, used to get new access tokens)
  token_expiry:        timestamp (when access_token expires)
  refresh_token_expiry: timestamp | null (null if platform does not define refresh token expiry)
  scopes_granted:      string[] (actual scopes granted by the operator)
  platform_user_id:    string | null (platform-provided user/channel ID)
  platform_username:   string | null (platform-provided display name — not sensitive)
}
```

### 9.5 Token refresh

Access token refresh is automatic and transparent:
- Credential Manager checks token expiry before resolving the credential
- If `token_expiry < now + 5 minutes`, a refresh is triggered before returning the resolved value
- Refresh uses the stored refresh_token to acquire a new access_token
- New access_token and updated expiry are written to storage
- If the platform issues a new refresh_token during refresh (platform rotation policy), the new refresh_token replaces the old one
- If refresh fails (expired refresh token, revoked access), the oauth_connection transitions to `expired` status and an OIS `credential_signal` is submitted

### 9.6 Expired token handling

If an OAuth token is expired and cannot be refreshed:
1. Status set to `expired`
2. `credential_signal` submitted to OIS (severity: CRITICAL if this blocks production; WARNING if non-blocking)
3. Dashboard shows: "Authorization expired — reauthorization required" with [Reauthorize] button
4. Production runs that depend on this credential are blocked until reauthorization completes

### 9.7 Permission scope display

The Credentials page shows the scopes granted for each OAuth connection:

```
youtube-hypogeum     YouTube     oauth_connection     ● ready
  Granted scopes:
    ✓ youtube.upload
    ✓ youtube.readonly
    ✓ youtube.force-ssl
  Connected as: Hypogeum Chronicles (channel ID: UCxxxxxx)
  Token expires: 2026-07-15 (in 31 days)
  [Reauthorize]  [Test]  [Disconnect]
```

If the granted scopes do not include all required scopes, a warning is shown: "Missing scope: `youtube.force-ssl`. Publishing may fail. [Reauthorize to grant missing scopes]"

### 9.8 Disconnect flow

Disconnecting an OAuth connection:
1. Operator clicks [Disconnect]
2. Confirmation dialog: "Disconnect [provider] OAuth connection? This will remove the stored tokens. Channels using this connection will be unable to publish until a new connection is established."
3. If confirmed:
   a. Factory calls provider's token revocation endpoint (best-effort — proceeds even if revocation fails)
   b. Tokens deleted from encrypted store
   c. Credential record status set to `revoked`
   d. All channels using this credential's profile are flagged (production blocked if this was a publishing credential)
   e. `credential_signal` emitted to OIS

### 9.9 Reauthorization flow

Reauthorization follows the same flow as the initial connect flow (§9.2), but the existing credential record is updated rather than a new one created. The prior tokens are overwritten on successful token exchange.

---

## 10. Dashboard Credential UX

The Credentials page (B5 §5.5) is Admin+ only. It provides the full credential management surface.

### 10.1 Page layout

```
CREDENTIALS
──────────────────────────────────────────────────────────────────────

[+ Add Credential]   [Import Checklist]

PRODUCTION READINESS  ████████████░░  5 / 6 ready

REQUIRED — PRODUCTION READY  (5 ready, 1 expiring)
┌──────────────────────────────────────────────────────────────────┐
│  openrouter-prod    OpenRouter    api_key      ● ready           │
│                                   tested 2h ago    [Test] [Edit] │
│  muapi-prod         MUAPI         api_key      ● ready           │
│                                   tested 3h ago    [Test] [Edit] │
│  elevenlabs-prod    ElevenLabs    api_key      ● ready           │
│                                   tested 3h ago    [Test] [Edit] │
│  perplexity-prod    Perplexity    api_key      ● ready           │
│                                   tested 5h ago    [Test] [Edit] │
│  youtube-hypogeum   YouTube       oauth         ● ready          │
│                                   tested 6h ago    [Test] [Edit] │
│  gmail-notify       Gmail         oauth         ⚠ expires 5d     │
│                                   tested 1h ago  [Renew] [Test]  │
└──────────────────────────────────────────────────────────────────┘

OPTIONAL — ANALYTICS (not yet connected)
┌──────────────────────────────────────────────────────────────────┐
│  youtube-analytics  YouTube Analytics  oauth   ○ not connected   │
│                                                [Connect]         │
└──────────────────────────────────────────────────────────────────┘

ENV MIGRATION (1 pending)
┌──────────────────────────────────────────────────────────────────┐
│  ⚠ ANTHROPIC_API_KEY from .env has not been migrated.            │
│  Migrate this credential to secure storage.    [Migrate]         │
└──────────────────────────────────────────────────────────────────┘

CREDENTIAL PROFILES
┌──────────────────────────────────────────────────────────────────┐
│  default_ai_providers   ● ready    channels: all    [View]       │
│  hypogeum_youtube       ● ready    channels: hypogeum  [View]    │
│  notification_gmail     ⚠ expiring  modules: notifications [View]│
│  telegram_alerts        ● ready    modules: notifications [View] │
└──────────────────────────────────────────────────────────────────┘

REVOKED  (1)  [Show revoked]
──────────────────────────────────────────────────────────────────────
```

### 10.2 Add credential flow

1. Select credential domain (dropdown with supported providers)
2. Select credential type (auto-suggested based on domain; operator can override)
3. Enter label (auto-suggested: `{provider}-{purpose}` e.g., `youtube-hypogeum`)
4. For api_key: enter value in masked input field
5. For oauth_connection: click [Authorize with {Provider}] — starts OAuth flow
6. For smtp_credentials: enter host, port, username, masked password, from_address
7. For telegram_bot_token: enter bot_token and chat_id
8. [Save] — stores encrypted, runs automatic test
9. Result confirmation with test outcome

### 10.3 Edit credential metadata

Editable fields (no secret value):
- Label (rename the credential)
- `expires_at` (manually set expiry date for credentials where the provider does not communicate expiry programmatically)
- Assigned profile (link credential to a profile)
- Notes (internal memo — never shown in any system output)

Non-editable fields:
- credential_type (changing type requires delete + recreate)
- credential_id

### 10.4 Replace secret value

Replacing the secret value requires the operator to type the new value into a blank field. The existing value is never pre-populated. After replacement, a test is automatically triggered.

### 10.5 Test connection

[Test] triggers an immediate connectivity test. The test result is displayed inline:
- `● Tested successfully — 1.2s — ready`
- `✗ Test failed — 401 Unauthorized — key may be invalid or revoked`
- `⏱ Test timed out (10s) — provider may be slow or unreachable`

The dashboard shows the test progress indicator while the test runs. Tests that take longer than 10 seconds show a progress bar.

### 10.6 Assign profile to channel

From the Profile detail view:
1. [Assign to Channel] opens a channel picker
2. Operator selects channel(s)
3. Profile is linked to the selected channel(s)
4. Channel activation readiness is immediately recalculated

### 10.7 Revoke / disable credential

- **Disable:** Credential is deactivated temporarily. It can be re-enabled. Channels using this credential are blocked.
- **Revoke:** Dangerous action (two-step confirmation with intent token). Credential cannot be used but record is retained. Channels using this credential are blocked. Used for credential compromise.
- **Delete:** Only available for revoked credentials. Owner-only. Permanent. Removes the record entirely.

---

## 11. Role-Based Access

| Action | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| View Credentials page | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| View credential profiles (labels, health) | ✓ | ✓ | Read via other pages | ✗ | ✗ | ✗ |
| View credential health status | ✓ | ✓ | Home alerts only | ✗ | ✗ | ✗ |
| Add credentials | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Replace credential secret | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Edit credential metadata | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Test credentials | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Assign credential profile to channel | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Disable credential | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Revoke credential | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Delete revoked credential | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Start OAuth connection | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| View readiness check results (channel activation) | ✓ | ✓ | ✓ (result only) | ✗ | ✗ | ✗ |

**Client role:** Clients never see credential labels, values, provider names, or health status for factory-internal credentials (AI providers, internal services). If a client-scoped credential profile exists for their channel (e.g., their own YouTube connection), they may see the health status and label of that specific profile only — with Admin configuration to enable this visibility.

---

## 12. Integration with Runtime Configuration Layer

The Config Layer resolves credential values at run start. The Credential Manager's role in runtime execution is:

**What Config Layer receives from Credential Manager:**
- `credential_profile_id`: which profile the channel is using
- `credential_ref`: which specific credential record within the profile
- `provider_label`: human-readable provider name (for logs)
- `readiness_status`: current readiness at the time of resolution

**What happens at resolution time:**
1. Config Layer calls `resolve_credential_in_memory(credential_ref)`
2. Credential Manager reads the encrypted value from the secure store
3. If `oauth_connection` and token is expiring, refresh is triggered first
4. Credential Manager returns the raw value to the calling system **in memory only**
5. The calling system uses the value for the single API call and does not store it

**What no agent ever receives:**
- Raw credential strings in prompts
- Credential values in agent context blocks
- Credential values in any field of the per-agent context objects

Agents are told which provider and model to use (by name). The actual authentication to that provider happens at the infrastructure layer, below the agent level.

---

## 13. Integration with OIS

The Credential Manager submits `credential_signal` records to OIS for all state changes that warrant operator attention.

### 13.1 Signal types and OIS routing

| Signal subtype | Trigger | Severity to OIS | OIS routing (typical) |
|---|---|---|---|
| `credential_expiring_30d` | `expires_at` is 30 days away | INFO | Passive insight on Credentials page |
| `credential_expiring_14d` | `expires_at` is 14 days away | WARNING | Dashboard alert + digest email |
| `credential_expiring_5d` | `expires_at` is 5 days away | CRITICAL | Immediate notification (bypasses quiet hours) |
| `credential_expired` | `expires_at` has passed or OAuth refresh failed | BLOCKED | Immediate all-channels (production blocked) |
| `credential_test_failed` | Connectivity test returned failure | WARNING | Dashboard notification + email |
| `credential_test_failed_repeated` | 3+ test failures in 24 hours for the same credential | CRITICAL | Escalated notification + Admin/Owner |
| `credential_renewed` | OAuth token refreshed or new key saved and tested | INFO | Auto-resolution signal — dismisses prior expiry alerts |
| `oauth_refresh_failed` | Refresh token exchange failed | CRITICAL | Same as `credential_expired` |
| `credential_missing` | Channel activation check found a required slot empty | CRITICAL | Activation blocked; notification to Admin/Owner |
| `credential_revoked` | Operator revoked a credential | WARNING | Notification with channel impact summary |
| `credential_usage_error` | Provider returned auth error during production use (not during test) | CRITICAL | Immediate — production run may be failing |

OIS applies its own noise rules (deduplication windows, cooldowns). A `credential_expiring_14d` signal is not re-submitted more often than once per 24 hours.

---

## 14. Integration with Notification System

The Notification System uses credentials managed by the Credential Manager for email (SMTP/Gmail) and Telegram delivery.

### 14.1 Credential failure creates a notification paradox

If the notification credential fails, the Notification System cannot deliver notifications about the failure via that channel. This paradox is resolved by the following design:

1. When the Credential Manager detects a notification credential failure, it submits a `credential_signal` to OIS.
2. OIS routes this to the Notification System as a `credential_failure` notification.
3. The Notification System delivers via all available channels. If Gmail fails, it tries Telegram. If Telegram also fails, the notification remains in the Dashboard Inbox.
4. The Dashboard Inbox is always available regardless of notification credential health — it has no external delivery dependency.
5. The Notification System never fails silently on credential failures — the Dashboard Inbox is the fallback of last resort, and it always receives the notification.

### 14.2 Notification System reads credential readiness (it does not own credentials)

When the Notification System is about to deliver via email or Telegram, it calls `get_credential_health(credential_id)` to check readiness before attempting delivery. If the credential is not `ready` or `expiring_soon`, it skips that channel and falls back. It does not attempt delivery through a known-failed credential.

---

## 15. Integration with Provider / Model Director

PMD can only recommend providers and models whose credentials are in `ready` or `expiring_soon` state. This prevents PMD from recommending a provider that the factory cannot authenticate with.

### 15.1 PMD queries credential readiness before generating proposals

When PMD evaluates a provider swap or model swap proposal, it calls `get_channel_credential_readiness(channel_id)` to check whether the target provider has a ready credential. If the credential is `missing`, `test_failed`, or `expired`, PMD does not generate a swap proposal for that provider. Instead, it generates a `credential_requirement` signal:

```
"Model claude-opus-4-7 is available on Anthropic and would improve quality by an estimated 12%
based on benchmark data. However, no Anthropic API key is configured for this factory.
Add an Anthropic API key to evaluate this model."
```

This signal is surfaced on the Credentials page as a "required for PMD recommendation" note, and on the PMD/Providers page as an unmet prerequisite. It is not an approval request — it is informational. The operator decides whether to add the credential.

---

## 16. Integration with Channel Activation

A channel cannot become `active` if any blocking credential profile assignment is not ready. The Credential Manager provides `get_channel_credential_readiness()` which the Channel Activation gate calls before allowing status change to `active`.

### 16.1 CredentialReadinessReport

The readiness report maps every required credential domain for the channel to its current readiness state:

```
CredentialReadinessReport for channel "hypogeum":

AI Providers (blocking):
  ✓ default_ai_providers (profile)
    openrouter-prod      ready
    elevenlabs-prod      ready
    muapi-prod           ready
    perplexity-prod      ready

Publishing — YouTube (blocking for YouTube):
  ✓ hypogeum_youtube (profile)
    youtube-hypogeum     ready

Notifications (non-blocking):
  ⚠ notification_gmail (profile)
    gmail-notify         expiring_soon (5 days)

Analytics (non-blocking):
  ○ platform_analytics_youtube (profile)
    youtube-analytics    not_connected

OVERALL: channel may be activated
⚠ Warning: gmail-notify expires in 5 days — renew before expiry to maintain email notifications
```

If any blocking domain has a non-ready credential, the activation is denied with a specific list of what needs to be resolved.

---

## 17. Export / Import Rules

### 17.1 Export contents

When a factory configuration is exported:

**Included:**
- Credential profile metadata (profile_id, profile_name, profile_type, assigned_channels, assigned_modules)
- Provider labels (which providers are configured)
- Credential record metadata (label, type, domain, readiness_status at time of export)
- Assignment mappings (which profiles are assigned to which channels and modules)
- Required credential checklist (a list of credential slots that need to be populated in the target environment)

**Excluded (hard exclusions — no override possible):**
- API key values
- OAuth access tokens and refresh tokens
- SMTP passwords
- Telegram bot tokens and chat IDs
- Webhook secrets
- Any field matching the credential value schema

The export file is explicitly labeled: "CREDENTIAL EXPORT — SECRETS EXCLUDED. This export contains credential metadata only. All secret values must be re-entered in the target environment."

### 17.2 Import behavior

When a factory configuration is imported into a new environment:
1. Credential profile structures are created
2. Each credential slot that had a credential record is created with status `missing` (a placeholder with the metadata but no value)
3. The Credentials page shows a "Setup Required" section listing all missing credentials with their labels and types
4. Operator populates each missing credential via the dashboard
5. Activation gate blocks the factory from running until required credentials are populated and tested

Import creates a migration checklist — not a working credential set. The operator must actively provision credentials in the new environment.

---

## 18. Local-First and SaaS Readiness

### 18.1 Local-first V1

**Storage backend:** OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) as the primary encrypted store. An encrypted file-based fallback store is used when no OS keychain is available or detectable.

**Encryption:** AES-256-GCM with a factory-specific key derived at factory initialization and stored in the OS keychain. The encrypted store key itself is never stored in any configuration file.

**.env migration:** On startup, if `.env` contains recognized credential variable names (`OPENROUTER_API_KEY`, `ELEVENLABS_API_KEY`, `ANTHROPIC_API_KEY`, `MUAPI_API_KEY`, etc.), the Credential Manager reads their values and creates `local_environment_secret` placeholder records. The operator is shown a migration prompt on the Credentials page. When the operator completes migration (entering the values into the dashboard for proper encrypted storage), the `local_environment_secret` records are replaced and the operator is prompted to remove the values from `.env` (or rename `.env` to `.env.backup`). The `.env` file is not deleted automatically.

**OAuth redirect URI:** In local-first V1, the OAuth redirect URI is a localhost URL on a randomly selected port. The OAuth connection flow requires a browser on the same machine as the factory. Remote operators cannot complete OAuth flows unless they have a browser session proxied to the factory machine.

### 18.2 SaaS readiness requirements

The Credential Manager is designed so that moving from local-first to SaaS requires only changing the storage backend and the OAuth redirect URIs. The API contracts, data contracts, and governance rules are identical.

**Tenant isolation:** Every credential operation is scoped to a `factory_id`. The storage backend enforces that credential records with factory_id A cannot be read by a session authenticated to factory_id B. This isolation is enforced at the storage layer, not the application layer.

**Per-factory encryption:** Each factory has its own encryption key. Keys are managed by the SaaS key management system (KMS) — not stored in application code or shared configuration.

**Client-safe credential views:** In multi-tenant SaaS, a client-role user can be shown the health status of their own channel's platform credentials (e.g., "Your YouTube connection is active") without seeing factory-internal credentials (AI providers, cost secrets). This scoping is implemented as a `client_visible` flag on credential profiles — only profiles with this flag are included in client-role API responses.

**Cross-tenant isolation:** The Credential Manager API rejects any request that attempts to access a credential from a different factory_id than the authenticated factory. This is enforced at the API middleware layer, before the request reaches the Credential Manager logic.

**Audit trail:** In SaaS, every credential operation (create, read, update, delete, test, resolve) is written to a per-factory audit log with: operation, credential_label, credential_id, actor_user_id, timestamp, source_ip. This audit trail is immutable and retained for the factory's retention period.

---

## 19. Data Contracts

### 19.1 CredentialRecord

```
CredentialRecord {
  credential_id:         string              // stable, immutable
  credential_label:      string              // operator-defined e.g., "youtube-hypogeum"
  credential_type:       enum(api_key, oauth_connection, service_account, webhook_secret,
                               smtp_credentials, telegram_bot_token, platform_access_token,
                               refresh_token, local_environment_secret, external_secret_reference)
  provider:              string              // e.g., "youtube", "openrouter", "elevenlabs"
  domain:                string              // e.g., "ai_inference_llm", "platform_publishing"
  factory_id:            string              // factory this credential belongs to
  readiness_status:      enum(missing, saved_not_tested, test_failed, ready,
                               expiring_soon, expired, disabled, revoked)
  expires_at:            timestamp | null
  last_tested_at:        timestamp | null
  last_test_result:      enum(pass, fail, timeout, rate_limited) | null
  last_test_failure_reason: string | null   // never contains the credential value
  last_used_at:          timestamp | null
  scopes_granted:        string[] | null    // for oauth_connection
  platform_user_id:      string | null      // for oauth_connection — public identifier
  platform_username:     string | null      // for oauth_connection — display name
  client_visible:        boolean            // whether client-role can see this profile's health
  env_variable_name:     string | null      // for local_environment_secret
  external_ref_path:     string | null      // for external_secret_reference
  notes:                 string | null      // internal memo — never exported or logged
  created_by:            string
  created_at:            timestamp
  updated_by:            string
  updated_at:            timestamp
  // credential value: NEVER present in this record's readable form
}
```

### 19.2 CredentialProfile

Defined fully in §5.2.

### 19.3 CredentialRef

```
CredentialRef {
  ref_id:                string
  credential_profile_id: string
  credential_id:         string
  slot_name:             string              // e.g., "primary_llm", "youtube_publish"
  required:              boolean            // if true, profile is not ready without this credential
  readiness_status:      string             // current status of the referenced credential
}
```

### 19.4 CredentialHealth

```
CredentialHealth {
  credential_id:         string
  credential_label:      string
  readiness_status:      string
  last_tested_at:        timestamp | null
  last_test_result:      string | null
  expires_at:            timestamp | null
  expires_in_days:       int | null         // null if no expiry tracked
  test_failure_count_24h: int
  last_used_at:          timestamp | null
  health_checked_at:     timestamp
}
```

### 19.5 CredentialTestResult

```
CredentialTestResult {
  test_id:               string
  credential_id:         string
  credential_label:      string
  credential_type:       string
  triggered_by:          enum(operator, scheduled, channel_activation, oauth_refresh)
  started_at:            timestamp
  completed_at:          timestamp
  duration_ms:           int
  result:                enum(pass, fail, timeout, rate_limited)
  failure_reason:        string | null
  new_readiness_status:  string
  // No credential value in this record
}
```

### 19.6 CredentialAssignment

```
CredentialAssignment {
  assignment_id:         string
  credential_profile_id: string
  assigned_to_type:      enum(channel, module, factory)
  assigned_to_id:        string             // channel_id, module_id, or factory_id
  assigned_by:           string
  assigned_at:           timestamp
  active:                boolean
}
```

### 19.7 OAuthConnection

```
OAuthConnection {
  connection_id:         string
  credential_id:         string
  provider:              string
  factory_id:            string
  scopes_granted:        string[]
  platform_user_id:      string | null
  platform_username:     string | null      // display name — not sensitive
  access_token_expiry:   timestamp
  refresh_token_expiry:  timestamp | null
  state:                 enum(active, expired, refresh_failed, revoked, disconnected)
  // access_token, refresh_token: encrypted at rest — never in this record's readable form
  connected_at:          timestamp
  last_refreshed_at:     timestamp | null
  last_refresh_error:    string | null
}
```

### 19.8 CredentialReadinessReport

```
CredentialReadinessReport {
  channel_id:            string
  computed_at:           timestamp
  overall_status:        enum(ready, expiring_soon, blocked)
  blocking_issues:       CredentialHealth[]  // credentials that prevent activation
  warnings:              CredentialHealth[]  // expiring_soon credentials
  domains: {
    ai_providers:        { status, profiles: CredentialProfile[] }
    platform_publishing: { status, profiles: CredentialProfile[] }
    platform_analytics:  { status, profiles: CredentialProfile[] }
    notifications:       { status, profiles: CredentialProfile[], non_blocking: true }
    storage:             { status, profiles: CredentialProfile[], non_blocking: true }
  }
}
```

### 19.9 CredentialSignal

```
CredentialSignal {
  signal_id:             string
  credential_id:         string
  credential_label:      string
  signal_subtype:        enum(credential_expiring_30d, credential_expiring_14d,
                               credential_expiring_5d, credential_expired,
                               credential_test_failed, credential_test_failed_repeated,
                               credential_renewed, oauth_refresh_failed,
                               credential_missing, credential_revoked,
                               credential_usage_error)
  severity:              enum(INFO, WARNING, CRITICAL, BLOCKED)
  affected_channels:     string[]           // channels that will be impacted
  expires_in_days:       int | null
  production_blocked:    boolean
  emitted_at:            timestamp
  // No credential values in any signal field
}
```

---

## 20. API Contracts

### 20.1 create_credential()

Called by: Dashboard (Admin+).

```
create_credential(
  credential_label,
  credential_type,
  provider,
  domain,
  secret_value,          // write-only — never returned
  expires_at,            // optional
  notes                  // optional
) → { credential_id, readiness_status, test_result }
```

Automatically runs a test after creation. Returns `readiness_status` based on test result.

### 20.2 replace_credential_secret()

Called by: Dashboard (Admin+).

```
replace_credential_secret(
  credential_id,
  new_secret_value       // write-only — the prior value is overwritten; not retrievable
) → { credential_id, readiness_status, test_result }
```

Automatically runs a test after replacement.

### 20.3 update_credential_metadata()

Called by: Dashboard (Admin+).

```
update_credential_metadata(
  credential_id,
  label,                 // optional
  expires_at,            // optional
  notes                  // optional
) → CredentialRecord     // without secret value
```

### 20.4 delete_credential()

Called by: Dashboard (Owner only). Only valid for credentials in `revoked` state.

```
delete_credential(
  credential_id,
  confirm_intent_token   // required — two-step deletion
) → { deleted: boolean }
```

### 20.5 disable_credential()

Called by: Dashboard (Admin+).

```
disable_credential(
  credential_id,
  reason                 // optional — internal note
) → { credential_id, readiness_status: "disabled" }
```

### 20.6 test_credential()

Called by: Dashboard (Admin+), Channel Activation gate, scheduled health checks.

```
test_credential(
  credential_id,
  triggered_by           // "operator" | "scheduled" | "channel_activation"
) → CredentialTestResult
```

Rate limit: 60-second cooldown per credential for operator-triggered tests. Scheduled tests: once per configured interval (default 6 hours). No rate limit for channel_activation-triggered tests.

### 20.7 create_credential_profile()

Called by: Dashboard (Admin+).

```
create_credential_profile(
  profile_name,
  profile_type,
  credential_refs,       // initial list of credential_ids to include
  assigned_channels,
  assigned_modules
) → CredentialProfile
```

### 20.8 update_credential_profile()

Called by: Dashboard (Admin+).

```
update_credential_profile(
  credential_profile_id,
  profile_name,
  credential_refs,
  assigned_channels,
  assigned_modules
) → CredentialProfile
```

### 20.9 assign_credential_profile()

Called by: Dashboard (Admin+).

```
assign_credential_profile(
  credential_profile_id,
  assigned_to_type,      // "channel" | "module"
  assigned_to_id
) → CredentialAssignment
```

### 20.10 unassign_credential_profile()

Called by: Dashboard (Admin+).

```
unassign_credential_profile(
  assignment_id
) → { unassigned: boolean }
```

### 20.11 get_credential_health()

Called by: Dashboard, NS (before delivery attempts), PMD.

```
get_credential_health(
  credential_id
) → CredentialHealth
```

### 20.12 get_channel_credential_readiness()

Called by: Channel Activation gate, Dashboard, PMD.

```
get_channel_credential_readiness(
  channel_id
) → CredentialReadinessReport
```

### 20.13 start_oauth_connection()

Called by: Dashboard (Admin+).

```
start_oauth_connection(
  provider,
  credential_label,
  credential_profile_id,  // optional — pre-assign to profile after connection
  scopes                  // optional — override default scopes
) → { redirect_url, state_token }
```

### 20.14 complete_oauth_connection()

Called by: OAuth callback handler.

```
complete_oauth_connection(
  code,
  state
) → { credential_id, readiness_status, scopes_granted, platform_username }
```

### 20.15 refresh_oauth_token()

Called by: Config Layer (automatic, before resolution if expiry is imminent), Dashboard (manual via [Renew] button).

```
refresh_oauth_token(
  credential_id
) → { access_token_expiry, readiness_status, refreshed_at }
```

### 20.16 disconnect_oauth_connection()

Called by: Dashboard (Admin+). Dangerous action — requires intent token.

```
disconnect_oauth_connection(
  credential_id,
  confirm_intent_token
) → { disconnected: boolean, revocation_attempted: boolean }
```

---

## 21. Risk Register

### Risk 1 — Secret leakage through API response

**Failure mode:** A developer adds a `GET /credentials/{id}` endpoint that returns the full credential record including the secret value. The endpoint passes code review because the response model accidentally includes a `value` field. Secret values are returned in API responses.

**Mitigation:** Three independent layers prevent this. (1) The Credential Manager's internal API does not expose a value-return method — the architecture has no `get_secret_value()` function accessible to the API layer. (2) The API gateway runs a scrubbing middleware that strips any field matching the credential value schema pattern (`api_key`, `token`, `secret`, `password`, `value` at any nesting level) before returning any API response. (3) The CredentialRecord data contract used in all API responses does not contain a value field — the readable CredentialRecord and the encrypted storage record are distinct types that never merge.

---

### Risk 2 — Raw credential appearing in logs

**Failure mode:** An AI provider returns an error that includes a reflected API key in the error message (some providers do this). The error is logged. The log now contains the credential value.

**Mitigation:** All error messages from external providers are scrubbed through the same log redaction pipeline before writing. Any string matching a known credential format pattern (begins with `sk-`, `ya29.`, `Bearer `, `AIza`, etc.) is replaced with `[REDACTED_CREDENTIAL]` before log writing. The log line records the credential label and credential_id instead. Provider error messages are truncated at 500 characters before log writing to limit the surface area for reflected secrets.

---

### Risk 3 — Exported credentials

**Failure mode:** An operator exports the factory configuration to migrate to a new machine. The export file contains API keys. The operator emails the export file. API keys are leaked.

**Mitigation:** The export function has no parameter, flag, or configuration that includes secrets. The export endpoint returns a CredentialMetadataExport that is a declared type — it cannot accidentally include secret fields because those fields do not exist in the export type. The export file is explicitly labeled with a header: "CREDENTIAL EXPORT — SECRETS EXCLUDED." A hash of the factory_id is included in the export for verification; the actual factory_id is not labeled as such to reduce information leakage in the export file itself.

---

### Risk 4 — Cloned credentials

**Failure mode:** An operator clones a channel to create a second channel. The clone operation copies the credential profile assignments. The operator assumes the clone has working credentials. The new channel uses the same credential profile but has no channel-specific OAuth connection for its new YouTube channel. Publishing fails.

**Mitigation:** Cloning copies profile structure references, not secret values. The clone's credential readiness report shows `missing` for any platform-specific OAuth connections (YouTube, TikTok, etc.) because those are per-channel connections that must be separately authorized. The clone cannot be activated until its required credentials are ready — the activation gate blocks this explicitly. The cloning workflow shows a "Credentials checklist for new channel" page before the clone is finalized.

---

### Risk 5 — Credential assigned to wrong channel

**Failure mode:** An operator manages two client channels: `client_a_youtube` and `client_b_youtube`. They accidentally assign the `client_a_youtube` credential profile to `client_b_channel`. Client A's publishing credentials are used for Client B's content.

**Mitigation:** Credential profile assignment is a write operation logged in the audit trail. The assignment UI shows a confirmation that explicitly names the channel and credential profile being assigned. For client channels, the assignment confirmation includes: "You are assigning Client A's YouTube connection to Client B's channel. This will publish Client B's content under Client A's YouTube account." The operator must confirm this unusual cross-assignment. The Channels page shows which credential profile is assigned to each channel — the assignment is visible and reviewable. OIS does not detect wrong-channel assignments (the system cannot know operator intent), but the audit trail enables investigation after the fact.

---

### Risk 6 — OAuth token expiry breaking publishing

**Failure mode:** A YouTube OAuth access token expires at 02:00 UTC. A scheduled episode is queued to publish at 03:00 UTC. The auto-refresh should have worked, but the refresh token was silently revoked by Google (this happens if the user has not used the OAuth connection in 6 months, or if they revoke access from Google's account settings). The episode fails to publish.

**Mitigation:** Automatic refresh runs before token expiry, not at expiry. The refresh window is 5 minutes before expiry for scheduled runtime refreshes. A proactive health check runs once per hour for all OAuth credentials — if the refresh fails during the health check, the credential transitions to `expired` immediately and a CRITICAL signal is emitted to OIS. The 1-hour check gives the operator hours of warning before a scheduled publication. Additionally, the Notification System sends a `credential_failure` notification immediately on refresh failure — not just at the next digest cycle. The notification includes: "YouTube OAuth token for [channel] could not be refreshed. Reauthorize before [scheduled publish time] to prevent publishing failure."

---

### Risk 7 — Client sees internal credentials

**Failure mode:** A client-role user assigned to their YouTube channel navigates to the Credentials page URL directly. They see the factory's internal OpenRouter API key and ElevenLabs API key.

**Mitigation:** The Credentials page is hidden from all non-Admin roles (§11 role table). The API endpoint `get_channel_credential_readiness()` filters its response for client-role callers: it returns only credential profiles where `client_visible = true`. Internal AI provider credentials have `client_visible = false` by default (this flag cannot be changed via normal API calls — it requires a factory settings change with Admin authority). The dashboard client does not render the Credentials page for non-Admin roles — but the API layer enforces the same restriction even if the frontend is bypassed.

---

### Risk 8 — Failed credential tests create false confidence

**Failure mode:** An operator adds a new MUAPI API key and clicks [Test]. The test returns a 429 (rate limited) response. The system marks it as `rate_limited` and schedules a re-test. The operator sees the credential listed without a red failure indicator and assumes it is working. It is actually untested, and the rate limit may have been masking an invalid key.

**Mitigation:** `rate_limited` is displayed distinctly from `ready` on the dashboard — it shows as amber with a clock icon ("⏱ rate limited — re-testing in 15 minutes") rather than green. The credential remains in `saved_not_tested` readiness until a definitive pass test succeeds. Channel activation requires `ready` or `expiring_soon` — `rate_limited` does not satisfy the activation gate. The operator is shown the specific status and what it means rather than a generic amber indicator.

---

### Risk 9 — Local .env becoming permanent

**Failure mode:** The operator sets up the factory by configuring `.env` with all credentials. The migration prompt appears. The operator dismisses it repeatedly. Six months later, the factory is running entirely on `.env` credentials. The `.env` file is committed to version control accidentally. All credentials are leaked.

**Mitigation:** `local_environment_secret` credentials are shown in a dedicated "ENV MIGRATION" section on the Credentials page with a persistent warning banner (not dismissible — not a one-time notice). They are excluded from the "Production Ready" count — the Production Readiness score counts only credentials in the encrypted store. A factory where any `local_environment_secret` credential is a required production credential shows `ENV MIGRATION PENDING` in the factory status. The operator sees this on every dashboard session start. Additionally, the Credential Manager emits a weekly `credential_signal` (INFO level) for each `local_environment_secret` credential that has not been migrated — these appear in the notification digest.

---

### Risk 10 — SaaS tenant isolation failure

**Failure mode:** A bug in the SaaS multi-tenant deployment allows factory B's Config Layer to call `resolve_credential_in_memory()` with a credential_id that belongs to factory A. Factory B temporarily has access to factory A's credentials during a resolution call.

**Mitigation:** Every Credential Manager API endpoint (including the in-memory resolution endpoint) validates that the requested credential_id belongs to the authenticated factory_id. This check is the first operation in every handler — before any database query, before any cache lookup. The check is not performed by the calling application layer — it is performed by the Credential Manager itself. A credential resolution call from factory B for factory A's credential_id returns `403 Forbidden`, not the secret value. The attempted cross-tenant access is logged as a security_warning signal and emitted to OIS, which routes it as a CRITICAL security alert to the Owner of factory A.

---

## 22. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Credentials page (§5.5) is the UI surface for the Credential Manager. Secret scrubbing middleware (§3.3) operates at the dashboard API layer. Dangerous action confirmation tokens (§3.6) apply to credential revocation and deletion. |
| B5.25 — PES | Credential forms follow PES C3 (forms) and C6 (confirmation dialogs for dangerous actions). Error states follow PES conventions. Empty states use C7 format. |
| B5.275 — OIS | All credential state changes that warrant operator attention are submitted as `credential_signal` to OIS. OIS classifies and routes. The Credential Manager does not deliver notifications. |
| B5.35 — Notification System | NS reads credential readiness before attempting delivery. Credential failure creates a notification paradox handled by inbox-first fallback (§14.1). NS never stores credential values — it reads health status only. |
| B5.4 — Approval System | Credential operations do not require Approval System decisions in V1. They are direct Admin+ actions. This may change in SaaS multi-tenant if credential additions require Owner approval. |
| B4.75 — Channel Profile | Channel Profile's `platform_strategy.enabled_platforms` determines which platform publishing credential profiles are required for channel activation. Channel Profile does not store credentials. |
| B4 — Config Layer | Config Layer receives `credential_ref` + `provider_label` + `readiness_status` — never the raw value. Resolution happens via `resolve_credential_in_memory()` called at run start, in the Config Layer's infrastructure scope. |
| Analytics Layer (B5.8) | Analytics Layer checks credential readiness before batch ingestion. Failed ingestion due to credential expiry is reported via IngestionHealthRecord and triggers a `credential_signal` through OIS. |
| Publishing Pipeline | Publishing Pipeline checks credential readiness before initiating a publish. Credential failure during publishing is treated as a CRITICAL runtime error with immediate OIS signal. |
| Provider / Model Director | PMD only recommends providers with ready credentials. Missing credentials generate a `credential_requirement` note, not a swap proposal. |
