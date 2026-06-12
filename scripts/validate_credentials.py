"""
scripts/validate_credentials.py — Production onboarding Task 1

Validate core API credentials for all four pipeline providers:
  1. OpenRouter  (GET /models — free, no generation)
  2. Perplexity  (minimal query, max_tokens=50 — real call, inexpensive)
  3. MUAPI       (GET /predictions/<probe_id>/result — auth probe, no generation)
  4. ElevenLabs  (GET /v1/models + GET /v1/voices/<voice_id> — read-only, free)

Rules:
  - Reads all env vars from the Config system (never os.getenv directly)
  - Never prints or logs secret values
  - Never submits paid generation jobs
  - Reports missing vars, auth failures, quota warnings, and next actions

Exit codes:
  0 — all four providers ready
  1 — one or more providers not ready (see table)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Bootstrap — ensure project root is on sys.path regardless of invocation cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from hypogeum.config import Config, ConfigError
    from hypogeum.openrouter import check_health as openrouter_check_health
    from hypogeum.perplexity import check_perplexity_health as perplexity_check_health
except ImportError as exc:
    print(f"\nFATAL: Cannot import hypogeum package: {exc}")
    print(f"Expected project root at: {_ROOT}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask(value: Optional[str]) -> str:
    """Return a safe display form of a secret value. Never prints actual content."""
    if not value:
        return "[NOT SET]"
    if len(value) <= 8:
        return "[SET — too short, may be invalid]"
    return f"[SET — {len(value)} chars, ends ...{value[-4:]}]"


def _status_icon(status: str) -> str:
    return {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}.get(status, status.upper())


def _elapsed(t0: float) -> str:
    return f"{(time.monotonic() - t0) * 1000:.0f}ms"


# ---------------------------------------------------------------------------
# Check 1: OpenRouter
# ---------------------------------------------------------------------------

def check_openrouter(config: Config) -> dict:
    result = {
        "provider": "OpenRouter",
        "status": "fail",
        "details": [],
        "warnings": [],
        "next_action": None,
    }

    if not config.OPENROUTER_API_KEY:
        result["details"].append("OPENROUTER_API_KEY not set")
        result["next_action"] = "Set OPENROUTER_API_KEY in .env (obtain from openrouter.ai → Account → API Keys)"
        return result

    t0 = time.monotonic()
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        result["details"].append("Connection failed — network unreachable")
        result["next_action"] = "Check internet connectivity, then retry"
        return result
    except requests.exceptions.Timeout:
        result["details"].append("Request timed out after 15s")
        result["next_action"] = "Check internet connectivity, then retry"
        return result

    elapsed = _elapsed(t0)

    if resp.status_code == 401:
        result["details"].append(f"HTTP 401 — authentication failed ({elapsed})")
        result["next_action"] = "OPENROUTER_API_KEY is invalid. Regenerate at openrouter.ai → Account → API Keys"
        return result

    if resp.status_code != 200:
        result["details"].append(f"HTTP {resp.status_code} ({elapsed})")
        result["next_action"] = f"Unexpected response. Check OpenRouter status page."
        return result

    try:
        data = resp.json()
        models = data.get("data", [])
        model_ids = {m["id"] for m in models}
    except Exception:
        result["details"].append("Response JSON unparseable")
        result["next_action"] = "Check OpenRouter API status"
        return result

    # Check both required models
    sonnet = config.OPENROUTER_SONNET_MODEL_ID
    opus   = config.OPENROUTER_OPUS_MODEL_ID
    missing_models = [m for m in (sonnet, opus) if m not in model_ids]

    if missing_models:
        result["status"] = "warn"
        result["details"].append(f"Auth OK — {len(models)} models available ({elapsed})")
        result["warnings"].append(f"Required models not found: {missing_models}")
        result["next_action"] = f"Verify model slugs in .env: OPENROUTER_SONNET_MODEL_ID, OPENROUTER_OPUS_MODEL_ID"
        return result

    # Check account balance via /api/v1/auth/key
    balance_info = ""
    try:
        key_resp = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            timeout=10,
        )
        if key_resp.status_code == 200:
            key_data = key_resp.json().get("data", {})
            limit = key_data.get("limit")
            usage = key_data.get("usage", 0)
            label = key_data.get("label", "")
            if limit is not None:
                remaining = limit - usage
                balance_info = f" | credit limit ${limit:.2f}, used ${usage:.4f}, remaining ${remaining:.4f}"
                if remaining < 1.0:
                    result["warnings"].append(f"Low credit balance: ${remaining:.4f} remaining")
            else:
                balance_info = f" | usage-based (no limit set)"
            if label:
                balance_info += f" | key label: {label!r}"
    except Exception:
        balance_info = " | (balance check unavailable)"

    result["status"] = "ok"
    result["details"].append(
        f"Auth OK — {len(models)} models — {sonnet} ✓ — {opus} ✓ ({elapsed}){balance_info}"
    )
    return result


# ---------------------------------------------------------------------------
# Check 2: Perplexity
# ---------------------------------------------------------------------------

def check_perplexity(config: Config) -> dict:
    result = {
        "provider": "Perplexity",
        "status": "fail",
        "details": [],
        "warnings": [],
        "next_action": None,
    }

    if not config.PERPLEXITY_API_KEY:
        result["details"].append("PERPLEXITY_API_KEY not set")
        result["next_action"] = "Set PERPLEXITY_API_KEY in .env (obtain from perplexity.ai → Settings → API)"
        return result

    # Use the existing health check (minimal query, max_tokens=50)
    t0 = time.monotonic()
    try:
        status = perplexity_check_health(config, episode_id="onboarding", agent="credential_check")
    except Exception as exc:
        result["details"].append(f"Health check raised exception: {type(exc).__name__}: {exc}")
        result["next_action"] = "Investigate error above. Check PERPLEXITY_API_KEY and network."
        return result

    elapsed = _elapsed(t0)

    if status == "unavailable":
        # Probe the status code directly to give a better error message
        try:
            probe = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.PERPLEXITY_MODEL,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=12,
            )
            if probe.status_code == 401:
                result["details"].append(f"HTTP 401 — authentication failed ({elapsed})")
                result["next_action"] = "PERPLEXITY_API_KEY is invalid. Regenerate at perplexity.ai → Settings → API"
            elif probe.status_code == 402:
                result["details"].append(f"HTTP 402 — credits exhausted ({elapsed})")
                result["next_action"] = "Top up Perplexity credits at perplexity.ai → Billing"
                result["status"] = "warn"
            else:
                result["details"].append(f"HTTP {probe.status_code} — unavailable ({elapsed})")
                result["next_action"] = "Check Perplexity API status. Retry after a few minutes."
        except Exception:
            result["details"].append(f"Unavailable — connection error ({elapsed})")
            result["next_action"] = "Check internet connectivity and PERPLEXITY_API_KEY"
        return result

    if status == "degraded":
        result["status"] = "warn"
        result["details"].append(f"Auth OK — model {config.PERPLEXITY_MODEL} — slow response ({elapsed})")
        result["warnings"].append("Response exceeded 10s threshold. May affect research pipeline latency.")
        result["next_action"] = "Monitor; retry if latency persists. Not a blocker."
        return result

    result["status"] = "ok"
    result["details"].append(f"Auth OK — model {config.PERPLEXITY_MODEL} — query answered ({elapsed})")
    return result


# ---------------------------------------------------------------------------
# Check 3: MUAPI (auth probe — no generation submitted)
# ---------------------------------------------------------------------------

def check_muapi(config: Config) -> dict:
    result = {
        "provider": "MUAPI",
        "status": "fail",
        "details": [],
        "warnings": [],
        "next_action": None,
    }

    if not config.MUAPI_API_KEY:
        result["details"].append("MUAPI_API_KEY not set")
        result["next_action"] = "Set MUAPI_API_KEY in .env (obtain production key from MUAPI dashboard)"
        return result

    # Auth probe: GET /api/v1/predictions/<probe_id>/result
    # 401 = bad key (unauthenticated)
    # 404 = good key (prediction does not exist — expected)
    # This probe does NOT submit a generation job. No billing occurs.
    probe_id = "onboarding-auth-probe-000000000000"
    probe_url = f"{config.MUAPI_BASE_URL}/predictions/{probe_id}/result"

    t0 = time.monotonic()
    try:
        resp = requests.get(
            probe_url,
            headers={
                "x-api-key": config.MUAPI_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        result["details"].append("Connection failed — network unreachable")
        result["next_action"] = "Check internet connectivity, then retry"
        return result
    except requests.exceptions.Timeout:
        result["details"].append("Request timed out after 15s")
        result["next_action"] = "Check internet connectivity, then retry"
        return result

    elapsed = _elapsed(t0)

    if resp.status_code == 401:
        result["details"].append(f"HTTP 401 — authentication failed ({elapsed})")
        result["next_action"] = "MUAPI_API_KEY is invalid. Obtain production key from MUAPI dashboard."
        return result

    if resp.status_code == 402:
        result["status"] = "warn"
        result["details"].append(f"HTTP 402 — quota/billing issue ({elapsed})")
        result["warnings"].append("MUAPI returned 402. Account may have no credits.")
        result["next_action"] = "Check MUAPI account balance. Top up before running the visual pipeline."
        return result

    if resp.status_code == 403:
        result["details"].append(f"HTTP 403 — forbidden ({elapsed})")
        result["next_action"] = "MUAPI_API_KEY may be a sandbox key. Obtain a production key."
        return result

    if resp.status_code in (404, 422):
        # 404 = probe prediction not found — correct, proves auth succeeded
        # 422 = validation error on the probe ID format — also proves auth layer was reached
        result["status"] = "ok"
        result["details"].append(
            f"Auth OK — HTTP {resp.status_code} on probe (prediction not found — expected) ({elapsed})"
        )
        result["details"].append(
            f"Base URL: {config.MUAPI_BASE_URL} | Tier 1: {config.MUAPI_T1_MODEL} | "
            f"Tier 2: {config.MUAPI_T2_MODEL} | Tier 3: {config.MUAPI_T3_MODEL}"
        )
        # Credit warning threshold check
        if config.MUAPI_CREDIT_WARNING_THRESHOLD is not None:
            result["details"].append(
                f"Credit warning threshold: ${config.MUAPI_CREDIT_WARNING_THRESHOLD:.2f} (live balance checked at generation time)"
            )
        return result

    if resp.status_code == 200:
        # Unexpected — probe ID should not match a real prediction
        result["status"] = "ok"
        result["details"].append(f"Auth OK — HTTP 200 on probe ({elapsed}) [unexpected but auth passed]")
        return result

    # Any other status: report but treat as unclear
    result["status"] = "warn"
    result["details"].append(f"HTTP {resp.status_code} on probe ({elapsed}) — auth unclear")
    result["warnings"].append(f"Unexpected status {resp.status_code}. Key may be valid but service state is unknown.")
    result["next_action"] = "Retry in a few minutes. Check MUAPI status page."
    return result


# ---------------------------------------------------------------------------
# Check 4: ElevenLabs (read-only — GET /v1/models + GET /v1/voices/<voice_id>)
# ---------------------------------------------------------------------------

def check_elevenlabs(config: Config) -> dict:
    result = {
        "provider": "ElevenLabs",
        "status": "fail",
        "details": [],
        "warnings": [],
        "next_action": None,
    }

    if not config.ELEVENLABS_API_KEY:
        result["details"].append("ELEVENLABS_API_KEY not set")
        result["next_action"] = "Set ELEVENLABS_API_KEY in .env (obtain from elevenlabs.io → Profile → API Keys)"
        return result

    voice_id = config.ELEVENLABS_VOICE_ID
    if not voice_id:
        result["details"].append("ELEVENLABS_VOICE_ID not set")
        result["next_action"] = "ELEVENLABS_VOICE_ID should be j9jfwdrw7BRfcR43Qohk — check .env"
        return result

    base_url = "https://api.elevenlabs.io"
    headers = {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }

    # --- Step 1: GET /v1/models — proves auth, lists available models ---
    t0 = time.monotonic()
    try:
        models_resp = requests.get(f"{base_url}/v1/models", headers=headers, timeout=15)
    except requests.exceptions.ConnectionError:
        result["details"].append("Connection failed — network unreachable")
        result["next_action"] = "Check internet connectivity, then retry"
        return result
    except requests.exceptions.Timeout:
        result["details"].append("GET /v1/models timed out after 15s")
        result["next_action"] = "Check internet connectivity, then retry"
        return result

    elapsed1 = _elapsed(t0)

    if models_resp.status_code == 401:
        result["details"].append(f"HTTP 401 on GET /v1/models — authentication failed ({elapsed1})")
        result["next_action"] = "ELEVENLABS_API_KEY is invalid. Regenerate at elevenlabs.io → Profile → API Keys"
        return result

    if models_resp.status_code != 200:
        result["details"].append(f"HTTP {models_resp.status_code} on GET /v1/models ({elapsed1})")
        result["next_action"] = "Check ElevenLabs API status. Retry."
        return result

    try:
        models = models_resp.json()
        model_ids = [m.get("model_id") for m in models if m.get("model_id")]
        target_model = config.ELEVENLABS_MODEL_ID
        model_found = target_model in model_ids
    except Exception:
        models = []
        model_ids = []
        model_found = False

    # --- Step 2: GET /v1/voices/<voice_id> — confirms pinned voice exists ---
    t1 = time.monotonic()
    try:
        voice_resp = requests.get(
            f"{base_url}/v1/voices/{voice_id}",
            headers=headers,
            timeout=15,
        )
    except Exception as exc:
        result["details"].append(f"Models OK — voice check failed: {exc}")
        result["status"] = "warn"
        result["warnings"].append("Could not verify pinned voice ID")
        result["next_action"] = "Verify ELEVENLABS_VOICE_ID=j9jfwdrw7BRfcR43Qohk is accessible on this account"
        return result

    elapsed2 = _elapsed(t1)

    if voice_resp.status_code == 404:
        result["details"].append(f"Auth OK (models: {len(model_ids)}) — voice {voice_id} NOT FOUND ({elapsed2})")
        result["warnings"].append(f"Pinned voice j9jfwdrw7BRfcR43Qohk returned 404")
        result["next_action"] = (
            "The pinned voice is not accessible on this ElevenLabs account. "
            "Either the voice ID is wrong, or it is a shared/cloned voice not available to this key. "
            "Confirm voice ownership at elevenlabs.io → Voice Library."
        )
        result["status"] = "fail"
        return result

    if voice_resp.status_code == 401:
        result["details"].append(f"HTTP 401 on voice check ({elapsed2})")
        result["next_action"] = "ELEVENLABS_API_KEY may have changed. Regenerate."
        return result

    if voice_resp.status_code != 200:
        result["status"] = "warn"
        result["details"].append(f"Auth OK — voice check HTTP {voice_resp.status_code} ({elapsed2})")
        result["warnings"].append("Voice availability uncertain")
        result["next_action"] = "Verify ELEVENLABS_VOICE_ID at elevenlabs.io → Voice Library"
        return result

    try:
        voice_data = voice_resp.json()
        voice_name = voice_data.get("name", "unknown")
        voice_category = voice_data.get("category", "unknown")
    except Exception:
        voice_name = "unknown"
        voice_category = "unknown"

    # --- Step 3: GET /v1/user/subscription — quota/character balance ---
    quota_info = ""
    quota_warning = None
    try:
        sub_resp = requests.get(f"{base_url}/v1/user/subscription", headers=headers, timeout=10)
        if sub_resp.status_code == 200:
            sub = sub_resp.json()
            char_count = sub.get("character_count", 0)
            char_limit = sub.get("character_limit", 0)
            tier = sub.get("tier", "unknown")
            next_reset = sub.get("next_character_count_reset_unix", None)
            remaining = char_limit - char_count if char_limit else None
            if remaining is not None:
                pct_used = (char_count / char_limit * 100) if char_limit else 0
                quota_info = f" | tier: {tier} | chars used: {char_count:,}/{char_limit:,} ({pct_used:.1f}%) | remaining: {remaining:,}"
                if pct_used >= 90:
                    quota_warning = f"Character quota {pct_used:.1f}% consumed ({remaining:,} remaining). Consider upgrading plan before production run."
                elif pct_used >= 75:
                    quota_warning = f"Character quota {pct_used:.1f}% consumed. Monitor before long production run."
            else:
                quota_info = f" | tier: {tier} | chars used: {char_count:,} (no limit)"
    except Exception:
        quota_info = " | (subscription info unavailable)"

    model_note = f"{target_model} ✓" if model_found else f"{target_model} NOT FOUND in model list"
    result["status"] = "ok"
    result["details"].append(
        f"Auth OK — {len(model_ids)} models — {model_note} ({elapsed1})"
    )
    result["details"].append(
        f"Voice j9jfwdrw7BRfcR43Qohk ✓ — name: {voice_name!r} — category: {voice_category} ({elapsed2}){quota_info}"
    )
    if quota_warning:
        result["warnings"].append(quota_warning)
        result["status"] = "warn"

    if not model_found:
        result["warnings"].append(f"ELEVENLABS_MODEL_ID={target_model!r} not found in model list. Check model ID.")
        result["status"] = "warn"
        result["next_action"] = f"Verify ELEVENLABS_MODEL_ID in .env. Available: {model_ids[:5]}"

    return result


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------

def render_report(results: list[dict]) -> int:
    """Print the provider readiness report. Returns exit code (0=all ok, 1=any fail)."""

    print()
    print("=" * 70)
    print("  Hypogeum Brain — Credential Validation")
    print("  Production Onboarding Task 1")
    print("=" * 70)
    print()

    # Summary table
    print(f"  {'Provider':<15}  {'Status':<8}  Details")
    print(f"  {'-'*15}  {'-'*8}  {'-'*40}")

    any_fail = False
    for r in results:
        icon = _status_icon(r["status"])
        first_detail = r["details"][0] if r["details"] else ""
        print(f"  {r['provider']:<15}  {icon:<8}  {first_detail}")
        for d in r["details"][1:]:
            print(f"  {'':15}  {'':8}  {d}")
        for w in r["warnings"]:
            print(f"  {'':15}  {'WARN':<8}  ⚠  {w}")
        if r["status"] == "fail":
            any_fail = True

    print()

    # Missing / invalid credentials
    failing = [r for r in results if r["status"] == "fail"]
    if failing:
        print("-" * 70)
        print("  FAILURES — NEXT ACTIONS REQUIRED")
        print("-" * 70)
        for r in failing:
            print(f"\n  {r['provider']}")
            print(f"    → {r['next_action']}")
        print()

    # Warnings
    warned = [r for r in results if r["status"] == "warn"]
    if warned:
        print("-" * 70)
        print("  WARNINGS")
        print("-" * 70)
        for r in warned:
            for w in r["warnings"]:
                print(f"  {r['provider']}: {w}")
            if r["next_action"]:
                print(f"    → {r['next_action']}")
        print()

    # Task 2 gate
    print("-" * 70)
    all_cp_ready = all(r["status"] in ("ok", "warn") for r in results)
    if all_cp_ready:
        print("  Task 2 (YouTube OAuth): READY TO BEGIN")
        print("  All critical-path credentials are valid.")
    else:
        failing_names = [r["provider"] for r in results if r["status"] == "fail"]
        print(f"  Task 2 (YouTube OAuth): CAN BEGIN IN PARALLEL")
        print(f"  Note: resolve {failing_names} credentials before Episode Zero.")
    print("=" * 70)
    print()

    return 1 if any_fail else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("\nLoading configuration...")

    try:
        config = Config()
    except ConfigError as exc:
        # OPENROUTER_API_KEY is always-required — Config.__init__ raises if missing.
        # Show a full report with all four providers marked as blocked.
        print()
        missing_var = exc.var_name
        print(f"  {missing_var}: [NOT SET]")
        print()
        print("=" * 70)
        print("  Hypogeum Brain — Credential Validation")
        print("  Production Onboarding Task 1")
        print("=" * 70)
        print()
        print(f"  {'Provider':<15}  {'Status':<8}  Details")
        print(f"  {'-'*15}  {'-'*8}  {'-'*40}")
        print(f"  {'OpenRouter':<15}  {'FAIL':<8}  {missing_var} not set — Config cannot load")
        print(f"  {'Perplexity':<15}  {'SKIP':<8}  Blocked by OpenRouter config failure")
        print(f"  {'MUAPI':<15}  {'SKIP':<8}  Blocked by OpenRouter config failure")
        print(f"  {'ElevenLabs':<15}  {'SKIP':<8}  Blocked by OpenRouter config failure")
        print()
        print("-" * 70)
        print("  FAILURES — NEXT ACTIONS REQUIRED")
        print("-" * 70)
        print(f"\n  OpenRouter")
        print(f"    → Set {missing_var} in .env (obtain from openrouter.ai → Account → API Keys)")
        print(f"    → Then re-run: python3 scripts/validate_credentials.py")
        print()
        print("-" * 70)
        print("  Task 2 (YouTube OAuth): CAN BEGIN IN PARALLEL")
        print("  YouTube OAuth setup does not require OpenRouter credentials.")
        print("=" * 70)
        print()
        return 1

    print(f"  OPENROUTER_API_KEY  {_mask(config.OPENROUTER_API_KEY)}")
    print(f"  PERPLEXITY_API_KEY  {_mask(config.PERPLEXITY_API_KEY)}")
    print(f"  MUAPI_API_KEY       {_mask(config.MUAPI_API_KEY)}")
    print(f"  ELEVENLABS_API_KEY  {_mask(config.ELEVENLABS_API_KEY)}")
    print(f"  ELEVENLABS_VOICE_ID {config.ELEVENLABS_VOICE_ID or '[NOT SET]'}")
    print("\nRunning provider checks...\n")

    results = []

    print("  [1/4] OpenRouter...", end=" ", flush=True)
    results.append(check_openrouter(config))
    print(_status_icon(results[-1]["status"]))

    print("  [2/4] Perplexity...", end=" ", flush=True)
    results.append(check_perplexity(config))
    print(_status_icon(results[-1]["status"]))

    print("  [3/4] MUAPI...", end=" ", flush=True)
    results.append(check_muapi(config))
    print(_status_icon(results[-1]["status"]))

    print("  [4/4] ElevenLabs...", end=" ", flush=True)
    results.append(check_elevenlabs(config))
    print(_status_icon(results[-1]["status"]))

    return render_report(results)


if __name__ == "__main__":
    sys.exit(main())
