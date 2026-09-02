# Roadmap Repair Plan — 2026-06-20

## Current State vs Intended State

### What works
- DB schema, all module imports pass acceptance_audit
- `/create` form submits and calls `production_launcher.launch_episode_from_dashboard`
- Credentials for youtube/tiktok/instagram/facebook can be saved via `/platforms/connect/<platform>`
- Finance agent, OIS, acceptance audit all importable

### What is broken

| Gap | Symptom | Impact |
|-----|---------|--------|
| Budget ceiling too low | `FinancePostVPDBlockedError`: $21–30 estimated > $15 ceiling | Pipeline BLOCKS every run |
| `/create` shows fake "Factory ready" | Uses `run_acceptance_audit` (module imports) not real env/finance/OIS checks | Operator thinks factory is ready when it isn't |
| Content providers 404 | seedance/freepik/openrouter/elevenlabs/muapi/veo/kling/runway link to `/platforms/connect/<id>` but route only serves {youtube,tiktok,instagram,facebook} | Cannot enter API keys for any AI provider via UI |
| No real setup/preflight page | `/preflight` shows module-import results, not env vars / ceiling / OIS signals | No single place to diagnose operational blockers |
| OIS signals not surfaced on create | Blocking signals are in DB but not shown before launch | Operator launches into guaranteed failure |
| Topic discovery not wired | `/create` always requires manual text entry | Roadmap requires topic queue first (deferred — lower priority than above) |
| Provider verify not implemented | Saving credentials does not test them | "Connected" badge is a lie — no API call made |

---

## Repair Sequence

### Step 1 — Real Setup/Preflight (THIS COMMIT)

**What**: Add `hypogeum/setup_preflight.py` with `get_setup_preflight_status()`.

Checks:
1. Required env vars: OPENROUTER_API_KEY, PERPLEXITY_API_KEY, MUAPI_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
2. ffmpeg on PATH
3. FINANCE_BUDGET_CEILING_USD configured and > 0
4. Estimated episode cost ($18–$30) vs ceiling — BLOCKER if ceiling < low estimate
5. Active OIS blocking signals (budget_exhaustion, credential_failure, emergency_halt, etc.)
6. At least one active channel in DB

**Routes added**:
- `GET /setup` — HTML page showing all preflight checks with remediation actions
- `GET /api/setup/preflight` — JSON version of above

**Create page**:
- `/create` now calls `get_setup_preflight_status()` in addition to existing checks
- Launch button disabled if any blocker exists
- Shows clear per-blocker remediation action

### Step 2 — Fix Content Provider Routing (THIS COMMIT)

**What**: Content providers (openrouter, elevenlabs, muapi, seedance, veo, kling, runway, freepik) currently link to `/platforms/connect/<id>` which returns 404. Add dedicated content provider connect routes.

Routes added:
- `GET /providers/content/connect/<provider>` — API key entry form
- `POST /providers/content/connect/<provider>/save` — saves credential

Template fix:
- `providers.html` content provider cards now link to `/providers/content/connect/<id>` instead of `/platforms/connect/<id>`

### Step 3 — Fix Budget Ceiling (NEXT — NOT IN THIS COMMIT)

**What the operator must do**: Set `FINANCE_BUDGET_CEILING_USD` to ≥ 30 in the .env file (or environment). Until this is done, every run will BLOCK at the finance gate with `FinancePostVPDBlockedError`.

**Future work**: Add a budget ceiling editor in `/setup` page (POST /api/setup/budget-ceiling) that writes to a local .env or config override file, so the operator can change it from the dashboard without SSH.

### Step 4 — Provider Credential Verification (DEFERRED)

For each content provider, after saving the API key, make a minimal test API call to verify the credential actually works. Return `verified: true/false` and surface this in the UI.

### Step 5 — Topic Discovery Workflow (DEFERRED)

Before `/create` can launch, the topic queue system needs to be the primary entry point. Manual text entry stays as override only. This requires wiring the decision engine topic output → episode queue → create page pre-population.

---

## Test Coverage Added (Step 1 + 2)

- `tests/test_setup_preflight.py`
  - Missing env var appears as blocker
  - All 5 required env vars pass → ready
  - Budget ceiling missing → blocker
  - Budget ceiling below low estimate → blocker
  - Budget ceiling above high estimate → passes
  - OIS blocking signal → blocker
  - `GET /setup` returns 200
  - `GET /api/setup/preflight` returns JSON with `ready`, `blockers`, `env_vars`
  - `GET /create` disabled (button) when blockers exist
  - `GET /providers/content/connect/seedance` returns 200 (not 404)
  - `GET /providers/content/connect/freepik` returns 200 (not 404)
  - `GET /providers/content/connect/openrouter` returns 200 (not 404)
  - `GET /providers/content/connect/unknown` returns 404
  - `POST /providers/content/connect/elevenlabs/save` saves credential
