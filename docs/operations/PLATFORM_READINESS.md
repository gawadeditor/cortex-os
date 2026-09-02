# PLATFORM_READINESS.md

# Cortex Multi-Platform Publishing Readiness

Version: 1.0

Status: Canonical (operational reference)

Last verified: 2026-07-26, against live provider evidence (debug_token
checks, real non-public smoke tests) — not assumption.

---

# Purpose

This document is the single source of truth for what blocks each
publishing platform from full autonomous operation, who owns closing
each blocker, and the exact steps/commands to do so. It supersedes
scattered per-platform notes in `CURRENT_STATE.md`, which now links here
instead of duplicating this detail.

This document does not describe architecture. Nothing here requires or
implies any change to `CORTEX_ARCHITECTURE.md`, the Interface Contract
Model, the Workflow Model, or any other canonical Constitution document.
Every finding below is either a code fix already applied (with
regression tests) or an external/operator/provider action — no
architectural conflict was found during this assessment.

---

# Executive Summary

Four platforms, four different states:

- **YouTube** — the reference implementation. Fully autonomous. No
  blocker.
- **TikTok** — code-ready and live-validated for Upload-to-Inbox
  (semi-autonomous: draft delivered to the creator's TikTok inbox, a
  human completes the final "Post" tap in-app). Full autonomy (Direct
  Post) is blocked on TikTok's own app review for the `video.publish`
  scope — provider review, not code, not configuration.
- **Instagram** — code-ready, zero defects found. Blocked purely on a
  missing operator configuration value (`META_STAGING_BASE_URL`) — no
  provider review needed, no code change needed.
- **Facebook** — code-ready, zero defects found. Blocked on the live
  credential lacking the `pages_manage_posts` scope. The code already
  requests this scope; a fresh re-authorization may resolve it, or it
  may require Meta App Review — undetermined without performing the
  live OAuth flow, which requires operator/browser action.

None of these four blockers is a software defect. All four platforms'
publishing code — OAuth, token refresh, upload, retry, idempotency,
recovery — has been verified correct by live evidence (see each
platform's own verification work: TikTok found and fixed two real
defects before reaching this state; Instagram and Facebook had none to
find).

---

# Readiness Matrix

| Platform | Code Status | Live Validation | Autonomy Level | Exact Blocker | Blocker Owner | Next Action | Verification Command |
|---|---|---|---|---|---|---|---|
| **YouTube** | ✅ Reference implementation | ✅ Real autonomous publish confirmed (`video_id=8gZux3jYc_I`, 2026-07-25, unattended scheduled run) | Full | None | — | None | `python3 scripts/multiplatform_publishing_smoke.py --episode-id <id> --platforms youtube --execute --confirm-nonpublic-smoke` (with `YOUTUBE_DRY_RUN=false`) |
| **TikTok** | ✅ Fixed (2 real bugs) + capability-gated | ✅ Real Upload-to-Inbox publish confirmed (`v_inbox_file~v2.7666843995985037313`) | Semi (inbox draft, human completes) | `video.publish` scope not granted (app review) | Provider review | See Step 3 checklist below | `YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py --episode-id <id> --platforms tiktok --execute --confirm-nonpublic-smoke` |
| **Instagram** | ✅ No defect found | ⛔ Blocked before container-create | None until configured | `META_STAGING_BASE_URL` unset | Operator (configuration only) | See Step 2 options below | Same command with `--platforms instagram` |
| **Facebook** | ✅ No defect found | ⛔ Blocked at upload (`(#100) No permission to publish the video`) | None until re-authorized | `pages_manage_posts` scope absent from live token | Operator, possibly + Provider review | See Step 1 checklist below | Same command with `--platforms facebook` |

Relevant implementation files: `hypogeum/publishing/youtube.py`,
`hypogeum/publishing/tiktok.py`, `hypogeum/publishing/meta.py`,
`hypogeum/publishing/staging.py`, `hypogeum/agents/publishing_agent.py`,
`run_dashboard.py` (`meta_oauth_authorize`/`meta_oauth_callback`, lines
2566-2821). Relevant tests: `tests/test_publishing_tiktok.py`,
`tests/test_publishing_meta.py`, `tests/test_publishing_agent.py`,
`tests/test_multiplatform_publishing_smoke.py`. Relevant decision
record: `DECISION_LOG.md` ADR-011 (TikTok capability-gated publishing).

---

# Step 1 — Facebook: Operator Action Checklist

## What's already true (no action needed)

- `hypogeum/publishing/meta.py::upload_facebook_video()` is correct:
  three-phase chunked upload, wrapped in `retry_video_upload()`.
- `hypogeum/agents/publishing_agent.py` STEP 8 idempotency guard is
  correct (checks `publishing_outcomes` before every attempt).
- `run_dashboard.py`'s `_META_SCOPES` constant already requests
  `pages_manage_posts` (verified at line 2553-2558) — the code-side fix
  from the prior sprint (commit `16e0ce8`) is in place.
- The OAuth callback (`meta_oauth_callback()`, line 2601) already
  attempts to exchange the user token for a genuine **Page** access
  token (Step 2.5, line 2714) specifically so Facebook publishing rights
  come from the Page relationship, not just the scope grant.

## What is not yet determined

The currently active credential (confirmed live via `debug_token`) is
already type `PAGE` (meaning the Page-token exchange in Step 2.5 above
did succeed) — **and it still lacks `pages_manage_posts`**. This is
important: it means the Page-token mechanism alone was not sufficient
for this app, which points toward Meta requiring the scope to be
explicitly present in the review-approved permission list for this app,
not merely inferred from Page ownership. A fresh re-authorization
**might** simply pick up the scope (if the only issue was that the
existing token predates the code fix) — or Meta's consent dialog may
silently omit the scope again if the app's current Access Level for
`pages_manage_posts` requires App Review. Only performing the live
re-authorization will tell us which.

## Exact operator checklist

1. **Dashboard route to use**: navigate to `/platforms/meta/authorize`
   in a browser while logged into the Dashboard (this redirects to
   Facebook's own OAuth dialog — no dashboard button click needed beyond
   visiting this URL, but check the `/providers` page for a "Connect
   Meta" link if one exists).
2. **Meta account/page prerequisites**: the Facebook account performing
   this authorization must be an **Admin** (or a role with page-content
   permissions) on Page `1176612708858828` ("The Hypogeum"), and must be
   listed as a **Developer, Admin, or Tester** on the Meta App
   (`META_APP_ID=1621137892324338`) in developers.facebook.com → App
   Roles — if the account isn't in the App's Roles list, the OAuth
   dialog may not offer `pages_manage_posts` at all regardless of App
   Review status (Meta apps in Development Mode only grant
   non-Advanced-Access permissions to Registered Test Users/Roles).
3. **Permissions that must appear on the consent screen**: watch for
   explicit listing of "Manage your Page" or the raw scope name
   `pages_manage_posts` in the Facebook consent dialog. If it is
   **absent** from the dialog entirely, that is itself the answer: the
   app is not currently authorized by Meta to request that scope from
   this user, and App Review is required (see below) — re-authorizing
   again will not help until that's resolved.
4. **Do not click through this authorization yet** — this checklist is
   for your review; I have not initiated this OAuth flow, per your
   instruction not to alter credentials without explicit approval.
5. **How to verify the new token's scopes** (after authorizing, if you
   choose to):
   ```
   python3 -c "
   import sys; sys.path.insert(0, '.')
   from hypogeum.credential_manager import resolve_oauth_credential
   import requests
   tok = resolve_oauth_credential(providers=['facebook','instagram'], label='meta-oauth-token')
   r = requests.get('https://graph.facebook.com/debug_token', params={
       'input_token': tok,
       'access_token': '<META_APP_ID>|<META_APP_SECRET>',
   })
   print(r.json()['data'].get('scopes'), r.json()['data'].get('type'))
   "
   ```
   `pages_manage_posts` must appear in the printed scope list.
6. **Exact smoke-test command to run afterward**:
   ```
   YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py \
       --episode-id <episode_id> --platforms facebook \
       --execute --confirm-nonpublic-smoke
   ```
7. **Expected success evidence**: `[OK] facebook_main — visibility=unpublished`
   with a real `video_id` in the ledger, and `schema validation: PASS`.
8. **Expected failure evidence if App Review is still required**: the
   identical error as today — `HTTP 400`, `message='(#100) No permission
   to publish the video'`, `code=100`. If you see this again after a
   fresh re-authorization that did show `pages_manage_posts` on the
   consent screen, that is a genuinely new, different signal worth a
   fresh investigation (would suggest the scope was granted but the
   Page-token derivation failed) — otherwise, absence of the scope on
   the consent screen is the same root cause as today, just confirmed
   twice.

## If App Review is required

Meta's Page Public Content Access / `pages_manage_posts` permission is
Advanced Access on most apps created after Meta tightened its Pages API
policies — it typically requires:
- A live-mode Meta app (not just Development Mode).
- A **Business Verification** of the Meta Business Manager account
  owning the app.
- A **Use Case** submission in App Review explaining exactly how
  `pages_manage_posts` is used (in this case: "autonomous publishing of
  produced video content to the operator's own Facebook Page").
- Screenshots or a screen-recording demonstrating the actual publish
  flow.

This is a **manual, operator-owned, provider-review process** — I
cannot initiate, submit, or influence it. It is not a checklist item I
can execute.

---

# Step 2 — Instagram: Staging Configuration Options

## Code review finding

`hypogeum/publishing/staging.py` defines a `StagingProvider` Protocol
with exactly two required methods (`stage_file()`, `delete_staged_file()`)
and one concrete implementation, `UrlStagingProvider`, which is a
**generic HTTP bridge**: it `POST`s the local file to
`{META_STAGING_BASE_URL}/upload` (multipart, field name `file`) and
expects either a JSON body `{"url": "..."}` or a plain-text body
containing the public URL; cleanup is a plain `DELETE {url}`. This
contract is **not incomplete** — it is deliberately storage-agnostic by
design (the module's own docstring: "cloud bucket (S3/R2) can be added
later as a deployment-time configuration change, not a code change").
No new `StagingProvider` implementation is needed for either option
below; both options work with the existing code unchanged.

## Option 1 — Simplest reliable temporary option (completes the smoke test)

**Required infrastructure**: a tiny local HTTP server implementing the
two-endpoint contract above, plus a public tunnel to it (e.g. `ngrok`,
Cloudflare Tunnel, or any similar service). This is the fastest path to
a real, non-public Instagram smoke test.

- **Environment variable**: `META_STAGING_BASE_URL=https://<your-tunnel-subdomain>.<provider>.app` (no trailing slash; the code appends `/upload`).
- **URL/HTTPS requirement**: must be HTTPS (Instagram's Graph API
  requires it) — every common tunnel provider issues this automatically.
- **File lifecycle**: files exist only for the duration of one
  container-create + poll cycle; `publishing_agent.py`/the smoke script
  both call `delete_staged_file()` unconditionally after the attempt
  (success or failure) — a short server-side TTL (e.g. auto-delete after
  10 minutes) is a reasonable safety net in case cleanup is ever skipped.
- **Security risks**: the tunnel exposes your local machine to the
  public internet for as long as it runs — use a random, unguessable
  object key/path per file (never sequential IDs) so unrelated parties
  cannot enumerate or guess URLs to pre-release content; stop the tunnel
  when not actively testing.
- **Validation steps**: `curl -F file=@test.mp4 https://<tunnel>/upload`
  should return a URL; `curl <that_url>` should return the file's bytes;
  `curl -X DELETE <that_url>` should remove it.
- **Exact Instagram smoke-test command** (after `META_STAGING_BASE_URL`
  is set and exported):
  ```
  YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py \
      --episode-id <episode_id> --platforms instagram \
      --execute --confirm-nonpublic-smoke
  ```
  Expected success evidence: `[OK] instagram_short_first —
  visibility=container_only` with a real `creation_id` and terminal
  status `FINISHED` in the ledger (recall: `publish_instagram_container()`
  is deliberately never called by the smoke script — this validates the
  full real upload/container path without going public, exactly as
  designed).

## Option 2 — Recommended production option (unattended operation)

**Required infrastructure**: a small, permanently-deployed bridge
service implementing the identical two-endpoint contract, backed by
durable object storage instead of local disk + a tunnel (tunnels are not
reliable for unattended 24/7 autonomous operation — free-tier URLs
rotate, connections drop). A minimal serverless function (Cloudflare
Worker + R2, or AWS Lambda + API Gateway + S3, or any equivalent the
operator already has an account for) fronting a bucket is sufficient —
this requires no new class in this codebase; `UrlStagingProvider`
already speaks exactly this HTTP contract.

- **Environment variable**: `META_STAGING_BASE_URL=https://<permanent-bridge-domain>` — set once, never rotates.
- **URL/HTTPS requirement**: same as Option 1, permanently valid TLS
  certificate (managed automatically by any of the providers above).
- **File lifecycle**: the bucket/object store should have a lifecycle
  policy auto-expiring objects after e.g. 24 hours, as a durable backstop
  independent of the application's own `delete_staged_file()` call (the
  same defense-in-depth principle the smoke script already applies with
  its own cleanup-always-attempted pattern).
- **Security risks**: same enumeration risk as Option 1 — random object
  keys, no public bucket listing. Additionally: the bridge's `/upload`
  endpoint should require the same credential/token scheme the rest of
  this pipeline already uses for provider calls (not left fully open),
  since production content (pre-release episodes) would flow through it
  continuously rather than only during manual test sessions.
- **Validation steps**: identical `curl` sequence as Option 1, run once
  after deployment and again after any redeploy.
- **Exact Instagram smoke-test command**: identical to Option 1.

## Recommendation

No existing connected provider in this codebase (MUAPI, asset registry,
etc.) already exposes public HTTPS URLs for local assets — confirmed by
code search (`grep` for public-URL patterns across `muapi.py`,
`asset_registry.py`: no matches). Nothing in this codebase can be
pointed at directly without one of the two options above.

---

# Step 3 — TikTok: Direct Post App-Review Checklist

## What's already true (no action needed, do not touch)

Upload-to-Inbox is live, validated, idempotent, and retry-wrapped
(`ADR-011`). This checklist is **only** for upgrading to Direct Post; it
does not require or suggest any change to the working Upload-to-Inbox
path.

## Operator checklist (sourced from TikTok's own developer documentation, verified live 2026-07-26)

1. **Required TikTok developer product**: **Content Posting API**, with
   its **Direct Post** configuration specifically enabled (distinct from
   the currently-enabled Upload-to-Inbox-only configuration).
2. **Requested scope**: `video.publish` (in addition to the currently
   granted `video.upload`, `video.list`, `user.info.basic`,
   `user.info.profile` — all of which should remain).
3. **App review prerequisite — unaudited-client restriction**: TikTok's
   own documentation states plainly: "All content posted by unaudited
   clients will be restricted to private viewing mode. ...your API
   client must undergo an audit to verify compliance with our Terms of
   Service" — this audit is submitted via
   developers.tiktok.com/application/content-posting-api (an
   authenticated developer-portal page; I could not fetch its contents
   without a logged-in session — this must be completed by the operator
   directly).
4. **Redirect URI requirements** (TikTok Login Kit, general
   requirement): must be an absolute HTTPS URL, static (no query
   parameters), under 512 characters, maximum 10 URIs registered per
   app.
5. **Privacy policy / terms of service**: TikTok's App Review Guidelines
   require a valid, active Privacy Policy and Terms of Service, visible
   on a fully-developed official website (not a bare landing/login
   page), with both links reachable without menu navigation.
6. **Evidence/demo material likely required**: 1-5 demo videos (up to 50
   MB each) showing the complete end-to-end integration flow — in this
   case, the actual TikTok upload happening from this pipeline — with a
   clear explanation of how the Content Posting API "enriches the user
   experience," per TikTok's stated review principle ("only request and
   demonstrate features your app actually uses").
7. **Post-approval token re-authorization**: once TikTok approves
   `video.publish` for the app, the existing credential must still be
   **re-authorized** (the currently active token was issued under the
   old scope grant and will not retroactively gain the new scope) —
   analogous to the Facebook re-authorization step above, via whatever
   TikTok's own OAuth consent flow is for this app (not yet automated in
   this codebase's Dashboard — TikTok credential setup is currently
   manual per `run_dashboard.py`'s own setup instructions text: "Go to
   developers.tiktok.com → Manage Apps → your app → Keys & Tokens").
8. **Exact scope verification** (after re-authorization):
   ```
   python3 -c "
   import sqlite3, json
   conn = sqlite3.connect('.hypogeum-assets/hypogeum.db')
   row = conn.execute(\"SELECT credential_data FROM secret_profiles WHERE provider='tiktok' AND status='active'\").fetchone()
   print(json.loads(row[0]).get('scope'))
   "
   ```
   `video.publish` must appear in the printed scope string —
   `resolve_tiktok_publish_mode()` (`hypogeum/publishing/tiktok.py`) will
   then automatically switch to Direct Post on its own; no code change
   is required when this happens.
9. **Exact Direct Post smoke-test command**:
   ```
   YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py \
       --episode-id <episode_id> --platforms tiktok \
       --execute --confirm-nonpublic-smoke
   ```
   The smoke script's existing `_SAFE_TIKTOK_PRIVACY = "SELF_ONLY"`
   guard (unchanged, still in effect) means even a successful Direct
   Post smoke test stays private to the account, exactly like today's
   Upload-to-Inbox smoke test.
   Expected success evidence: `[OK] tiktok_short_first —
   visibility=self_only` with a real `post_id`, status `published` (not
   `uploaded_to_inbox`) in the ledger — the capability-gated dispatch
   added in `ADR-011` will select this path automatically once
   `resolve_tiktok_publish_mode()` sees `video.publish` in scope.

This entire checklist is provider review + operator infrastructure
(website, legal docs, redirect URIs) — none of it is executable by me,
and none of it requires any change to the working Upload-to-Inbox code.

---

# Definition of Done (per platform)

- **YouTube**: already met (autonomous publish confirmed live).
- **TikTok**: Direct Post Definition of Done — `video.publish` appears
  in the live credential's scope (Step 3 item 8), followed by one
  successful `status=published` smoke test (Step 3 item 9). Until then,
  Upload-to-Inbox's own Definition of Done (already met) stands as this
  platform's current production mode.
- **Instagram**: Definition of Done — `META_STAGING_BASE_URL` set to a
  working endpoint (Step 2, either option), followed by one successful
  `status=skipped, visibility=container_only, terminal_status=FINISHED`
  smoke test (container-only, matching the platform's existing safety
  design — full publish was never in scope for the smoke test itself).
- **Facebook**: Definition of Done — `pages_manage_posts` appears in the
  live credential's scope (Step 1 item 5), followed by one successful
  `status=published, visibility=unpublished` smoke test (Step 1 item 6).

---

# Authority

This document is the canonical, current-as-of-2026-07-26 record of
multi-platform publishing readiness. `CURRENT_STATE.md`'s Publishing —
Multi-Platform section points here for detail rather than duplicating
it. Update this document (not `CURRENT_STATE.md`) whenever a platform's
live status changes.
