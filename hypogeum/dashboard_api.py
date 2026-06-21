"""
hypogeum/dashboard_api.py — Phase C: Real Dashboard Foundation

Typed data contracts, state model, and permission definitions for the
factory dashboard. This is the backend API layer that any frontend
(web, CLI, or agent) calls to inspect factory state.

Architecture:
    - All reads. No writes. No network. Non-blocking.
    - Every function returns a typed dataclass or a safe default.
    - Lazy table access: tables that may not exist (reuse_decisions,
      evolution_adjustments) are queried inside try/except.
    - Permission model is pure data — no authentication implementation.
      Phase G (Operations Center) will enforce permissions.

Public API:
    # Summaries (individual sections)
    get_channel_summaries(db_path=None) -> list[ChannelSummary]
    get_run_health(db_path=None)        -> RunHealthSummary
    get_agent_health(db_path=None)      -> list[AgentHealthSummary]
    get_model_usage(db_path=None)       -> list[ModelUsageSummary]
    get_provider_health(db_path=None)   -> list[ProviderHealthSummary]
    get_archive_summary(db_path=None)   -> ArchiveSummary
    get_recall_summary(db_path=None)    -> RecallSummary
    get_learning_summary(db_path=None)  -> LearningSummary
    get_cost_summary(db_path=None)      -> CostSummary
    get_audit_summary(db_path=None)     -> AuditSummary
    get_notifications(db_path=None)     -> list[Notification]

    # Aggregated factory state
    get_factory_state(db_path=None, factory_id="hypogeum") -> FactoryState

    # Permission model
    get_permission(action)                      -> Optional[Permission]
    list_permissions()                          -> list[Permission]
    is_authorized(user_id, action, db_path)     -> bool

    # Text formatter
    format_factory_state(state) -> str

    # OIS signals
    get_active_signals(db_path=None) -> list[dict]

    # Schedules
    get_schedules(db_path=None) -> list[dict]

Data contracts (dataclasses):
    ChannelSummary, RunHealthSummary, AgentHealthSummary, ModelUsageSummary,
    ProviderHealthSummary, ArchiveSummary, RecallSummary, LearningSummary,
    CostSummary, AuditSummary, Notification, Permission, FactoryState
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import list_channel_profiles, load_channel_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"
SCHEMA_VERSION   = "v2"

_PASS_VERDICTS = {"APPROVED", "APPROVED_WITH_FLAGS"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rate(num: int, denom: int) -> float:
    return round(num / denom, 4) if denom > 0 else 0.0


# ===========================================================================
# Data Contracts
# ===========================================================================

@dataclass
class ChannelSummary:
    """Profile completeness and status for one channel."""
    channel_id:           str
    name:                 str
    status:               str          # active | paused | archived
    brand_complete:       bool
    publishing_complete:  bool
    models_complete:      bool
    profile_completeness: float        # 0.0–1.0 (fraction of sub-profiles filled)
    created_at:           str


@dataclass
class RunHealthSummary:
    """Aggregate health across all pipeline runs."""
    total_runs:     int
    complete_count: int
    failed_count:   int
    running_count:  int
    success_rate:   float          # complete / (complete + failed); excludes running
    last_run_at:    Optional[str]
    # RLG intermediate states (default 0 for pre-RLG DBs)
    queued_count:                    int = 0
    paused_count:                    int = 0
    awaiting_review_count:           int = 0
    awaiting_publish_approval_count: int = 0
    publishing_count:                int = 0


@dataclass
class AgentHealthSummary:
    """Per-stage execution health, read from v_stage_performance."""
    stage_name:      str
    total_runs:      int
    success_count:   int
    failure_count:   int
    skip_count:      int
    success_rate:    float         # success / (total - skips)
    avg_duration_ms: Optional[float]
    avg_cost_usd:    Optional[float]


@dataclass
class ModelUsageSummary:
    """Usage and cost for a single model slug across all runs."""
    model_id:       str
    usage_count:    int
    total_cost_usd: float


@dataclass
class ProviderHealthSummary:
    """Asset generation health and cost for a single provider."""
    provider:       str
    total_attempts: int
    failure_count:  int
    failure_rate:   float
    total_cost_usd: float


# ---------------------------------------------------------------------------
# Operational readiness constants
# These are the values for ProviderCatalogEntry.operational_readiness.
# They feed the UI "Row 2: Operational readiness" display.
# ---------------------------------------------------------------------------

OR_READY              = "ready"               # live smoke test passed
OR_READY_VIA_MUAPI    = "ready_via_muapi"     # delegated, MUAPI smoke test passed
OR_NOT_TESTED         = "not_tested"          # no smoke test result on record
OR_SKIPPED_NO_KEY     = "skipped_missing_key" # smoke test skipped — no credential
OR_AUTH_FAILED        = "auth_failed"         # key present, generation returned 401 (credential invalid)
OR_GENERATION_FAILED  = "generation_failed"   # credential valid; generation failed (403/model error)
OR_BILLING_FAILED     = "billing_or_credits_failed"  # 402 / credits exhausted
OR_NEEDS_RETEST       = "needs_retest"        # smoke result stale (>SMOKE_MAX_AGE_DAYS)
OR_MUAPI_DEPENDENT    = "muapi_dependent"     # delegated; MUAPI not yet tested
OR_MUAPI_NOT_READY    = "unavailable_muapi_not_ready"  # delegated; MUAPI test failed
OR_NOT_CONFIGURED     = "not_configured"      # placeholder — direct API not yet supported

# Smoke test results older than this many days are treated as stale.
# Stale failed results show "Needs retest" rather than permanently marking
# the provider as failed — the model list or endpoint may have changed.
SMOKE_MAX_AGE_DAYS = 7


@dataclass
class ProviderCatalogEntry:
    """
    Combined credential status + operational readiness for one provider.

    Two status rows are exposed for dashboard display:

      Row 1 — credential_status: result of the lightweight HTTP probe
        "verified"        — probe succeeded
        "saved"           — key present, never probed
        "missing"         — no credential found
        "failed"          — probe returned error (may be wrong probe endpoint)
        "not_verifiable"  — no probe implemented (freepik)

      Row 2 — operational_readiness: result of Phase 1C smoke test (or derived)
        See OR_* constants above.

    Always present for every known provider regardless of whether it has
    generated any assets yet. This is the correct data source for the Providers
    dashboard page — not asset_versions alone.
    """
    provider:               str
    display_name:           str
    capabilities:           list          # e.g. ["image"], ["llm", "image"], ["video"]
    access_mode:            str           # "direct"|"muapi_delegated"|"direct_placeholder"
    delegate_via:           Optional[str] # "muapi" when access_mode == "muapi_delegated"
    credential_status:      str           # Row 1: verifier probe result
    has_env_var:            bool
    is_configured:          bool          # has_env_var or has_secret_profile
    total_attempts:         int           # from asset_versions; 0 if never used
    failure_count:          int
    failure_rate:           float
    total_cost_usd:         float
    # Row 2: operational readiness (defaults keep backward compatibility)
    operational_readiness:  str                = OR_NOT_TESTED
    operational_note:       Optional[str]      = None
    smoke_test_status:      Optional[str]      = None
    smoke_test_model:       Optional[str]      = None
    smoke_test_latency_ms:  Optional[int]      = None
    credential_key_name:    Optional[str]      = None  # env var that would enable direct access
    smoke_age_days:         Optional[float]    = None  # age of smoke report in days; None = no report


@dataclass
class ArchiveSummary:
    """State of the archive_index table."""
    total_archived:       int
    reuse_eligible_count: int
    asset_types:          dict     # {asset_type: count}
    oldest_at:            Optional[str]
    newest_at:            Optional[str]


@dataclass
class RecallSummary:
    """Reuse decision statistics from the reuse_decisions lazy table."""
    total_decisions: int
    reuse_count:     int
    generate_count:  int
    reuse_rate:      float


@dataclass
class LearningSummary:
    """Aggregate learning signal stats from learning_records."""
    total_records:          int
    signal_types:           dict     # {signal_type: count}
    recent_recommendations: list     # up to 5 most recent recommendation strings


@dataclass
class CostSummary:
    """Cost breakdown across all episodes and providers."""
    total_usd:    float
    by_provider:  dict    # {provider: total_usd}
    by_model:     dict    # {model: total_usd}
    by_episode:   dict    # {episode_id: total_usd}
    run_count:    int
    episode_count: int


@dataclass
class AuditSummary:
    """Result of the most recent acceptance audit check."""
    last_run_at:   Optional[str]
    ready:         bool
    blocker_count: int
    warning_count: int
    detail:        str


@dataclass
class Notification:
    """A dashboard alert surfaced from factory state analysis."""
    level:      str           # "info" | "warning" | "critical"
    category:   str
    message:    str
    channel_id: Optional[str]
    created_at: str


@dataclass
class Permission:
    """
    Definition of one factory action and who may perform it.

    Levels (in ascending authority):
        operator  — day-to-day: approve runs, pause channels
        admin     — structural: clone factory, override budgets, deactivate
        emergency — irreversible: emergency stop

    Phase G (Operations Center) will enforce these at runtime.
    """
    action:                str
    description:           str
    requires_level:        str    # "operator" | "admin" | "emergency"
    requires_confirmation: bool = False


@dataclass
class FactoryState:
    """
    Snapshot of the complete factory state at a point in time.

    All sections are populated from the local DB. No external API calls.
    overall_health: "healthy" | "warning" | "degraded" | "unknown"
    """
    factory_id:      str
    schema_version:  str
    checked_at:      str
    overall_health:  str
    run_health:      RunHealthSummary
    channels:        list
    agent_health:    list
    model_usage:     list
    provider_health:  list
    archive:          ArchiveSummary
    recall:          RecallSummary
    learning:        LearningSummary
    costs:           CostSummary
    audit:           AuditSummary
    notifications:    list
    # Fields with defaults — safe for callers that construct positionally
    factory_name:     str  = ""
    factory_status:   str  = "active"
    active_signals:   list = field(default_factory=list)
    schedules:        list = field(default_factory=list)
    pending_approvals: list = field(default_factory=list)
    provider_catalog: list = field(default_factory=list)  # list[ProviderCatalogEntry]
    metadata_version: str = METADATA_VERSION


# ===========================================================================
# Permission Model (pure data)
# ===========================================================================

PERMISSION_TABLE: dict[str, Permission] = {
    "approve_run": Permission(
        action="approve_run",
        description="Approve an episode run to proceed",
        requires_level="operator",
    ),
    "pause_channel": Permission(
        action="pause_channel",
        description="Pause a channel — stops new runs from being scheduled",
        requires_level="operator",
    ),
    "resume_channel": Permission(
        action="resume_channel",
        description="Resume a paused channel",
        requires_level="operator",
    ),
    "approve_evolution": Permission(
        action="approve_evolution",
        description="Approve an evolution engine recommendation",
        requires_level="operator",
    ),
    "import_package": Permission(
        action="import_package",
        description="Import a brand or identity package into the factory",
        requires_level="operator",
    ),
    "clone_factory": Permission(
        action="clone_factory",
        description="Create a new channel clone from an existing channel",
        requires_level="admin",
    ),
    "override_model": Permission(
        action="override_model",
        description="Override the model assignment for a stage",
        requires_level="admin",
    ),
    "override_cost_ceiling": Permission(
        action="override_cost_ceiling",
        description="Override the budget ceiling for a run",
        requires_level="admin",
        requires_confirmation=True,
    ),
    "deactivate_channel": Permission(
        action="deactivate_channel",
        description="Archive (permanently deactivate) a channel",
        requires_level="admin",
        requires_confirmation=True,
    ),
    "emergency_stop": Permission(
        action="emergency_stop",
        description="Emergency stop — halts all running and queued runs",
        requires_level="emergency",
        requires_confirmation=True,
    ),
}


def get_permission(action: str) -> Optional[Permission]:
    """Return the Permission for an action, or None if not defined."""
    return PERMISSION_TABLE.get(action)


def list_permissions() -> list:
    """Return all defined permissions ordered by requires_level then action."""
    order = {"operator": 0, "admin": 1, "emergency": 2}
    return sorted(
        PERMISSION_TABLE.values(),
        key=lambda p: (order.get(p.requires_level, 99), p.action),
    )


def is_authorized(
    user_id: str,
    action: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Check whether user_id may perform action.

    Single enforcement entry point for all permission-gated dashboard actions.
    Delegates to auth.check_authorization() which enforces the role hierarchy:
        owner > admin > operator > editor > viewer > client

    Returns False on any error (user not found, action not defined, DB unavailable).
    Non-blocking: never raises.
    """
    try:
        from hypogeum.auth import check_authorization
        return check_authorization(user_id, action, db_path=db_path)
    except Exception:
        return False


# ===========================================================================
# Channel Summaries
# ===========================================================================

def get_channel_summaries(db_path: Optional[Path] = None) -> list:
    """
    Return a ChannelSummary for every channel profile in the DB.

    Profile completeness = (brand_present + publishing_present + models_present) / 3.
    Non-blocking: returns [] on any error.
    """
    try:
        profiles = list_channel_profiles(db_path=db_path)
        summaries = []
        for cp in profiles:
            try:
                cfg = load_channel_config(cp.channel_id, db_path=db_path)
                brand_ok     = cfg is not None and cfg.brand      is not None
                pub_ok       = cfg is not None and cfg.publishing is not None
                models_ok    = cfg is not None and cfg.models     is not None
                completeness = _safe_rate(
                    int(brand_ok) + int(pub_ok) + int(models_ok), 3
                )
                summaries.append(ChannelSummary(
                    channel_id           = cp.channel_id,
                    name                 = cp.name,
                    status               = cp.status,
                    brand_complete       = brand_ok,
                    publishing_complete  = pub_ok,
                    models_complete      = models_ok,
                    profile_completeness = completeness,
                    created_at           = cp.created_at,
                ))
            except Exception:
                pass
        return summaries
    except Exception:
        return []


# ===========================================================================
# Run Health
# ===========================================================================

def get_run_health(db_path: Optional[Path] = None) -> RunHealthSummary:
    """
    Aggregate health across all pipeline runs.

    success_rate = complete / (complete + failed); excludes running rows.
    Non-blocking: returns zeroed summary on any error.
    """
    _empty = RunHealthSummary(0, 0, 0, 0, 0.0, None)
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('complete','completed')
                                  THEN 1 ELSE 0 END)              AS complete,
                    SUM(CASE WHEN status='failed'   THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status='running'  THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status='queued'   THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='paused'   THEN 1 ELSE 0 END) AS paused,
                    SUM(CASE WHEN status='awaiting_review'
                                  THEN 1 ELSE 0 END)              AS awaiting_review,
                    SUM(CASE WHEN status='awaiting_publish_approval'
                                  THEN 1 ELSE 0 END)              AS awaiting_publish_approval,
                    SUM(CASE WHEN status='publishing' THEN 1 ELSE 0 END) AS publishing,
                    MAX(started_at)                                AS last_run_at
                FROM runs
            """).fetchone()
        finally:
            conn.close()

        if row is None or row["total"] == 0:
            return _empty

        complete = row["complete"] or 0
        failed   = row["failed"]   or 0
        running  = row["running"]  or 0
        return RunHealthSummary(
            total_runs                    = row["total"],
            complete_count                = complete,
            failed_count                  = failed,
            running_count                 = running,
            success_rate                  = _safe_rate(complete, complete + failed),
            last_run_at                   = row["last_run_at"],
            queued_count                  = row["queued"]                    or 0,
            paused_count                  = row["paused"]                    or 0,
            awaiting_review_count         = row["awaiting_review"]           or 0,
            awaiting_publish_approval_count = row["awaiting_publish_approval"] or 0,
            publishing_count              = row["publishing"]                or 0,
        )
    except Exception:
        return _empty


# ===========================================================================
# Agent Health
# ===========================================================================

def get_agent_health(db_path: Optional[Path] = None) -> list:
    """
    Per-stage success rates from the v_stage_performance view.

    success_rate = successes / (total - skips).
    Non-blocking: returns [] on any error or if no stage data exists.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT stage_name, total_executions, successes, failures,
                       skips, avg_duration_ms, avg_cost_usd
                FROM v_stage_performance
                ORDER BY stage_name
            """).fetchall()
        finally:
            conn.close()

        result = []
        for r in rows:
            non_skip = (r["total_executions"] or 0) - (r["skips"] or 0)
            result.append(AgentHealthSummary(
                stage_name      = r["stage_name"],
                total_runs      = r["total_executions"] or 0,
                success_count   = r["successes"] or 0,
                failure_count   = r["failures"]  or 0,
                skip_count      = r["skips"]     or 0,
                success_rate    = _safe_rate(r["successes"] or 0, non_skip),
                avg_duration_ms = r["avg_duration_ms"],
                avg_cost_usd    = r["avg_cost_usd"],
            ))
        return result
    except Exception:
        return []


# ===========================================================================
# Model Usage
# ===========================================================================

def get_model_usage(db_path: Optional[Path] = None) -> list:
    """
    Usage count and total cost per model slug from the costs table.

    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT model, COUNT(*) AS usage_count, SUM(cost_usd) AS total_cost
                FROM costs
                WHERE model IS NOT NULL
                GROUP BY model
                ORDER BY total_cost DESC
            """).fetchall()
        finally:
            conn.close()

        return [
            ModelUsageSummary(
                model_id       = r["model"],
                usage_count    = r["usage_count"] or 0,
                total_cost_usd = round(r["total_cost"] or 0.0, 6),
            )
            for r in rows
        ]
    except Exception:
        return []


# ===========================================================================
# Provider Health
# ===========================================================================

def get_provider_health(db_path: Optional[Path] = None) -> list:
    """
    Attempt count, failure rate, and total cost per provider from asset_versions.

    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT
                    provider,
                    COUNT(*)                                              AS total,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)    AS failures,
                    SUM(COALESCE(cost_usd, 0))                          AS total_cost
                FROM asset_versions
                GROUP BY provider
                ORDER BY total DESC
            """).fetchall()
        finally:
            conn.close()

        return [
            ProviderHealthSummary(
                provider       = r["provider"],
                total_attempts = r["total"]      or 0,
                failure_count  = r["failures"]   or 0,
                failure_rate   = _safe_rate(r["failures"] or 0, r["total"] or 0),
                total_cost_usd = round(r["total_cost"] or 0.0, 6),
            )
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Provider Catalog — combined credential + usage view
# ---------------------------------------------------------------------------

# Static metadata for every known provider.
# access_mode values:
#   "direct"            — has its own API key and client code
#   "muapi_delegated"   — accessed via the MUAPI aggregator
#   "direct_placeholder"— direct API not yet implemented; future credential slot
# credential_key_name   — the env var that would authenticate this provider
_PROVIDER_CATALOG_META: dict[str, dict] = {
    # --- Content generation (direct) ---
    "openai":     {
        "display_name":      "OpenAI",
        "capabilities":      ["image"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "OPENAI_API_KEY",
    },
    "openrouter": {
        "display_name":      "OpenRouter",
        "capabilities":      ["llm", "image"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "OPENROUTER_API_KEY",
    },
    "muapi":      {
        "display_name":      "MUAPI",
        "capabilities":      ["image", "video"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "MUAPI_API_KEY",
    },
    "elevenlabs": {
        "display_name":      "ElevenLabs",
        "capabilities":      ["audio"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "ELEVENLABS_API_KEY",
    },
    "freepik":    {
        "display_name":      "Freepik",
        "capabilities":      ["image"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "FREEPIK_API_KEY",
    },
    # --- Video providers: accessed via MUAPI aggregator ---
    "seedance":   {
        "display_name":      "Seedance",
        "capabilities":      ["video"],
        "access_mode":       "muapi_delegated",
        "delegate_via":      "muapi",
        "credential_key_name": "MUAPI_API_KEY",
    },
    "veo":        {
        "display_name":      "Veo",
        "capabilities":      ["video"],
        "access_mode":       "muapi_delegated",
        "delegate_via":      "muapi",
        "credential_key_name": "MUAPI_API_KEY",
    },
    "kling":      {
        "display_name":      "Kling",
        "capabilities":      ["video"],
        "access_mode":       "muapi_delegated",
        "delegate_via":      "muapi",
        "credential_key_name": "MUAPI_API_KEY",
    },
    "runway":     {
        "display_name":      "Runway",
        "capabilities":      ["video"],
        "access_mode":       "muapi_delegated",
        "delegate_via":      "muapi",
        "credential_key_name": "MUAPI_API_KEY",
    },
    # --- Official direct providers (Phase 1H / Phase 1J) ---
    "seedance_official": {
        "display_name":        "Seedance 2.0 (BytePlus ModelArk)",
        "capabilities":        ["video"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "BYTEPLUS_ARK_API_KEY",
    },
    "runway_official": {
        "display_name":        "Runway (Official)",
        "capabilities":        ["video"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "RUNWAY_API_KEY",
    },
    "kling_official": {
        "display_name":        "Kling (Official)",
        "capabilities":        ["video"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "KLING_API_KEY",
    },
    "veo_official": {
        "display_name":        "Veo (Google)",
        "capabilities":        ["video"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "GOOGLE_API_KEY",
    },
    # --- Publishing ---
    "youtube":    {
        "display_name":      "YouTube",
        "capabilities":      ["publishing"],
        "access_mode":       "direct",
        "delegate_via":      None,
        "credential_key_name": "YOUTUBE_REFRESH_TOKEN",
    },
    "facebook": {
        "display_name":        "Facebook",
        "capabilities":        ["publishing"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "META_ACCESS_TOKEN",
    },
    "instagram": {
        "display_name":        "Instagram",
        "capabilities":        ["publishing"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "META_ACCESS_TOKEN",
    },
    "tiktok": {
        "display_name":        "TikTok",
        "capabilities":        ["publishing"],
        "access_mode":         "direct",
        "delegate_via":        None,
        "credential_key_name": "TIKTOK_ACCESS_TOKEN",
    },
}


def _load_smoke_test_results() -> dict:
    """
    Load Phase 1C smoke test results from the report file if present.

    Returns {provider: result_dict} or {} if the file doesn't exist.
    Non-blocking.
    """
    providers, _ = _load_smoke_test_results_with_age()
    return providers


def _load_smoke_test_results_with_age() -> tuple[dict, Optional[float]]:
    """
    Load smoke test results and return (providers_dict, age_days).

    age_days is None if the report does not exist or has no timestamp.
    Non-blocking.
    """
    import json as _json
    from datetime import datetime, timezone

    report = _PROJECT_ROOT / ".hypogeum-assets" / "provider_smoke_tests" / "provider_live_smoke_report.json"
    try:
        if not report.exists():
            return {}, None
        data = _json.loads(report.read_text())
        providers = data.get("providers", {})
        generated_at = data.get("generated_at")
        if generated_at:
            try:
                dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
                return providers, age_days
            except Exception:
                pass
        return providers, None
    except Exception:
        return {}, None


def _derive_operational_readiness(
    provider: str,
    meta: dict,
    smoke: dict,
    muapi_readiness: str,
) -> tuple[str, Optional[str]]:
    """
    Return (operational_readiness, note) for one provider.

    Rules:
      direct_placeholder  → OR_NOT_CONFIGURED (no implementation yet)
      muapi_delegated     → inherit from muapi_readiness
      direct (no smoke)   → OR_NOT_TESTED
      direct (smoke hit)  → derive from smoke result
    """
    access_mode = meta.get("access_mode", "direct")

    # Placeholders are never callable
    if access_mode == "direct_placeholder":
        return OR_NOT_CONFIGURED, "Direct API not yet implemented."

    # MUAPI-delegated providers inherit MUAPI's generation readiness
    if access_mode == "muapi_delegated":
        if muapi_readiness == OR_READY:
            return OR_READY_VIA_MUAPI, "Available via MUAPI aggregator."
        if muapi_readiness in (OR_AUTH_FAILED, OR_BILLING_FAILED, OR_GENERATION_FAILED):
            return OR_MUAPI_NOT_READY, f"MUAPI generation not ready: {muapi_readiness}."
        # MUAPI not tested yet — status is muapi_dependent
        return OR_MUAPI_DEPENDENT, "Availability depends on MUAPI generation readiness."

    # Direct provider: derive from smoke test
    if not smoke:
        return OR_NOT_TESTED, None

    status = smoke.get("status", "")
    error  = str(smoke.get("error", ""))
    model  = smoke.get("model", "")
    latency = smoke.get("latency_ms")

    if status == "success":
        note = f"Smoke test passed — model: {model}"
        if latency is not None:
            note += f", {latency} ms"
        return OR_READY, note

    if "skipped" in status:
        return OR_SKIPPED_NO_KEY, smoke.get("note") or "No credential set."

    # Classify error: distinguish credential failures (401) from generation
    # failures (403 = model permission / quota, 5xx = API errors).
    # 403 from a GENERATION endpoint does NOT mean the credential is bad —
    # it means the model/endpoint is not accessible with this account tier.
    # Only 401 is a definitive credential rejection.
    if "402" in error or "billing" in error.lower() or "credit" in error.lower():
        return OR_BILLING_FAILED, f"Billing/credits: {error[:120]}"

    if "401" in error or "not authorized" in error.lower():
        return OR_AUTH_FAILED, f"Credential rejected (401 — check key): {error[:120]}"

    if "403" in error:
        return OR_GENERATION_FAILED, (
            f"Generation forbidden (403 — model access or account tier issue): {error[:120]}"
        )

    if status.startswith("error"):
        return OR_GENERATION_FAILED, f"{status}: {error[:120]}"

    return OR_NOT_TESTED, None


def get_provider_catalog(db_path: Optional[Path] = None) -> list:
    """
    Return a ProviderCatalogEntry for every known provider.

    Merges four sources:
      1. _PROVIDER_CATALOG_META    — static capabilities / access-mode labels
      2. provider_verifier         — credential status (Row 1)
      3. smoke test report         — operational readiness (Row 2)
      4. asset_versions table      — usage stats (0 for providers never used)

    Always includes all providers from _PROVIDER_CATALOG_META — including
    direct_placeholder entries and MUAPI-delegated entries — regardless of
    whether any assets have been generated. This is the correct data source
    for the Providers dashboard page.

    Non-blocking: returns [] on any error.
    """
    try:
        from hypogeum.provider_verifier import get_all_provider_statuses
        statuses = get_all_provider_statuses(db_path)
    except Exception:
        statuses = {}

    smoke_results, smoke_age_days = _load_smoke_test_results_with_age()
    smoke_is_stale = (smoke_age_days is not None and smoke_age_days > SMOKE_MAX_AGE_DAYS)

    # Usage stats from asset_versions
    usage: dict[str, dict] = {}
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT
                    provider,
                    COUNT(*)                                           AS total,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)  AS failures,
                    SUM(COALESCE(cost_usd, 0))                        AS total_cost
                FROM asset_versions
                GROUP BY provider
            """).fetchall()
        finally:
            conn.close()
        for r in rows:
            usage[r["provider"]] = {
                "total":      r["total"]      or 0,
                "failures":   r["failures"]   or 0,
                "total_cost": r["total_cost"] or 0.0,
            }
    except Exception:
        pass

    # Pass 1: build all entries; derive MUAPI's operational_readiness first
    # so delegated providers can inherit it in Pass 2.
    muapi_smoke    = smoke_results.get("muapi", {})
    muapi_st       = statuses.get("muapi", {})
    muapi_readiness, _ = _derive_operational_readiness("muapi", {"access_mode": "direct"}, muapi_smoke, OR_NOT_TESTED)

    catalog: list[ProviderCatalogEntry] = []
    for provider, meta in _PROVIDER_CATALOG_META.items():
        # For MUAPI-delegated providers, use MUAPI's credential status:
        # they all share the MUAPI key and should reflect the same probe result.
        if meta.get("access_mode") == "muapi_delegated":
            st = muapi_st
        else:
            st = statuses.get(provider, {})
        u     = usage.get(provider, {"total": 0, "failures": 0, "total_cost": 0.0})
        total = u["total"]
        fails = u["failures"]

        smoke = smoke_results.get(provider, {})
        op_readiness, op_note = _derive_operational_readiness(
            provider, meta, smoke, muapi_readiness
        )

        is_configured = (
            st.get("has_env_var", False) or st.get("has_secret_profile", False)
        )

        # If the smoke test was skipped because there was no key at that moment,
        # but the provider IS now configured (env var OR saved secret profile),
        # upgrade readiness to OR_NOT_TESTED so the UI doesn't permanently show
        # "Skipped — no key" after a key is connected.
        if op_readiness == OR_SKIPPED_NO_KEY and is_configured:
            op_readiness = OR_NOT_TESTED
            op_note = "Credential saved — run smoke test to confirm generation works."

        # Stale smoke results should not permanently mark a provider as failed.
        # If the report is older than SMOKE_MAX_AGE_DAYS, downgrade failure states
        # to OR_NEEDS_RETEST so the UI prompts re-testing rather than assuming
        # the provider is broken.
        if smoke_is_stale and op_readiness in (OR_AUTH_FAILED, OR_GENERATION_FAILED, OR_BILLING_FAILED):
            age_str = f"{smoke_age_days:.0f}d" if smoke_age_days is not None else "?"
            op_readiness = OR_NEEDS_RETEST
            op_note = f"Smoke result stale ({age_str} old) — re-run smoke test to confirm current status."

        smoke_st      = smoke.get("status")
        smoke_model   = smoke.get("model")
        smoke_latency = smoke.get("latency_ms")

        catalog.append(ProviderCatalogEntry(
            provider              = provider,
            display_name          = meta["display_name"],
            capabilities          = meta["capabilities"],
            access_mode           = meta["access_mode"],
            delegate_via          = meta["delegate_via"],
            credential_status     = st.get("status", "missing"),
            has_env_var           = st.get("has_env_var", False),
            is_configured         = is_configured,
            total_attempts        = total,
            failure_count         = fails,
            failure_rate          = _safe_rate(fails, total),
            total_cost_usd        = round(u["total_cost"], 6),
            operational_readiness = op_readiness,
            operational_note      = op_note,
            smoke_test_status     = smoke_st,
            smoke_test_model      = smoke_model,
            smoke_test_latency_ms = smoke_latency,
            credential_key_name   = meta.get("credential_key_name"),
            smoke_age_days        = round(smoke_age_days, 1) if smoke_age_days is not None else None,
        ))
    return catalog


# ===========================================================================
# Archive Summary
# ===========================================================================

def get_archive_summary(db_path: Optional[Path] = None) -> ArchiveSummary:
    """
    State of the archive_index table: total, eligible, type breakdown, age.

    Non-blocking: returns zeroed summary on any error.
    """
    _empty = ArchiveSummary(0, 0, {}, None, None)
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            totals = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN reuse_eligible=1 THEN 1 ELSE 0 END) AS eligible,
                       MIN(archived_at) AS oldest,
                       MAX(archived_at) AS newest
                FROM archive_index
            """).fetchone()

            type_rows = conn.execute("""
                SELECT asset_type, COUNT(*) AS cnt
                FROM archive_index
                GROUP BY asset_type
            """).fetchall()
        finally:
            conn.close()

        if totals is None or totals["total"] == 0:
            return _empty

        asset_types = {r["asset_type"]: r["cnt"] for r in type_rows}
        return ArchiveSummary(
            total_archived       = totals["total"]    or 0,
            reuse_eligible_count = totals["eligible"] or 0,
            asset_types          = asset_types,
            oldest_at            = totals["oldest"],
            newest_at            = totals["newest"],
        )
    except Exception:
        return _empty


# ===========================================================================
# Recall Summary
# ===========================================================================

def get_recall_summary(db_path: Optional[Path] = None) -> RecallSummary:
    """
    Reuse decision statistics from the reuse_decisions lazy table.

    The table may not exist (created only when should_reuse() is called).
    Non-blocking: returns zeroed summary on any error or missing table.
    """
    _empty = RecallSummary(0, 0, 0, 0.0)
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN decision='reuse'    THEN 1 ELSE 0 END) AS reuse,
                       SUM(CASE WHEN decision='generate' THEN 1 ELSE 0 END) AS generate
                FROM reuse_decisions
            """).fetchone()
        finally:
            conn.close()

        if row is None or row["total"] == 0:
            return _empty

        reuse    = row["reuse"]    or 0
        generate = row["generate"] or 0
        total    = row["total"]    or 0
        return RecallSummary(
            total_decisions = total,
            reuse_count     = reuse,
            generate_count  = generate,
            reuse_rate      = _safe_rate(reuse, total),
        )
    except Exception:
        return _empty


# ===========================================================================
# Learning Summary
# ===========================================================================

def get_learning_summary(db_path: Optional[Path] = None) -> LearningSummary:
    """
    Aggregate learning signal stats from the learning_records table.

    Non-blocking: returns zeroed summary on any error.
    """
    _empty = LearningSummary(0, {}, [])
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM learning_records"
            ).fetchone()

            type_rows = conn.execute("""
                SELECT signal_type, COUNT(*) AS cnt
                FROM learning_records
                GROUP BY signal_type
                ORDER BY cnt DESC
            """).fetchall()

            rec_rows = conn.execute("""
                SELECT recommendation
                FROM learning_records
                WHERE recommendation IS NOT NULL
                ORDER BY recorded_at DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        total = total_row["cnt"] if total_row else 0
        if total == 0:
            return _empty

        signal_types = {r["signal_type"]: r["cnt"] for r in type_rows}
        recent_recs  = [r["recommendation"] for r in rec_rows if r["recommendation"]]
        return LearningSummary(
            total_records          = total,
            signal_types           = signal_types,
            recent_recommendations = recent_recs,
        )
    except Exception:
        return _empty


# ===========================================================================
# Cost Summary
# ===========================================================================

def get_cost_summary(db_path: Optional[Path] = None) -> CostSummary:
    """
    Cost breakdown by provider, model, and episode from the costs table.

    Non-blocking: returns zeroed summary on any error.
    """
    _empty = CostSummary(0.0, {}, {}, {}, 0, 0)
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            total_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs"
            ).fetchone()

            provider_rows = conn.execute("""
                SELECT provider, SUM(cost_usd) AS total
                FROM costs GROUP BY provider ORDER BY total DESC
            """).fetchall()

            model_rows = conn.execute("""
                SELECT model, SUM(cost_usd) AS total
                FROM costs WHERE model IS NOT NULL
                GROUP BY model ORDER BY total DESC
            """).fetchall()

            episode_rows = conn.execute("""
                SELECT episode_id, SUM(cost_usd) AS total
                FROM costs GROUP BY episode_id ORDER BY total DESC
            """).fetchall()

            run_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM runs"
            ).fetchone()["cnt"]

            episode_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM episodes"
            ).fetchone()["cnt"]
        finally:
            conn.close()

        return CostSummary(
            total_usd     = round(total_row["total"] or 0.0, 6),
            by_provider   = {r["provider"]: round(r["total"] or 0.0, 6) for r in provider_rows},
            by_model      = {r["model"]: round(r["total"] or 0.0, 6) for r in model_rows if r["model"]},
            by_episode    = {r["episode_id"]: round(r["total"] or 0.0, 6) for r in episode_rows},
            run_count     = run_count     or 0,
            episode_count = episode_count or 0,
        )
    except Exception:
        return _empty


# ===========================================================================
# Audit Summary
# ===========================================================================

def get_audit_summary(db_path: Optional[Path] = None) -> AuditSummary:
    """
    Run the acceptance audit and return a typed summary.

    Non-blocking: returns a failed summary on any import or runtime error.
    """
    try:
        from hypogeum.acceptance_audit import run_acceptance_audit
        report = run_acceptance_audit(db_path=db_path)
        return AuditSummary(
            last_run_at   = report.checked_at,
            ready         = report.ready,
            blocker_count = len(report.blockers),
            warning_count = len(report.warnings),
            detail        = "ACCEPTED" if report.ready else "REJECTED",
        )
    except Exception as exc:
        return AuditSummary(
            last_run_at   = _now_iso(),
            ready         = False,
            blocker_count = 1,
            warning_count = 0,
            detail        = f"audit import failed: {exc}",
        )


# ===========================================================================
# Notifications
# ===========================================================================

def get_notifications(
    db_path:      Optional[Path]   = None,
    run_health:   Optional[RunHealthSummary] = None,
    audit:        Optional[AuditSummary]     = None,
    provider_health: Optional[list]          = None,
    agent_health: Optional[list]             = None,
    channels:     Optional[list]             = None,
) -> list:
    """
    Derive actionable notifications from factory state.

    Pre-computed summaries can be passed in to avoid redundant DB queries.
    Non-blocking: returns [] on any error.

    Notification levels:
        "critical" — factory cannot function or is actively failing
        "warning"  — degraded state requiring attention
        "info"     — informational, no action required
    """
    try:
        now = _now_iso()
        notes: list[Notification] = []

        rh = run_health   or get_run_health(db_path)
        au = audit         or get_audit_summary(db_path)
        ph = provider_health or get_provider_health(db_path)
        ah = agent_health  or get_agent_health(db_path)
        ch = channels      or get_channel_summaries(db_path)

        # --- Run health ---
        if rh.total_runs > 0:
            if rh.success_rate < 0.5:
                notes.append(Notification(
                    level="critical", category="run_health",
                    message=f"Run success rate critically low: {rh.success_rate:.0%} "
                            f"({rh.complete_count}/{rh.complete_count+rh.failed_count})",
                    channel_id=None, created_at=now,
                ))
            elif rh.success_rate < 0.8:
                notes.append(Notification(
                    level="warning", category="run_health",
                    message=f"Run success rate below target: {rh.success_rate:.0%} "
                            f"({rh.complete_count}/{rh.complete_count+rh.failed_count})",
                    channel_id=None, created_at=now,
                ))

        # --- Audit ---
        if au.blocker_count > 0:
            notes.append(Notification(
                level="critical", category="audit",
                message=f"Acceptance audit has {au.blocker_count} blocker(s) — "
                        "factory not ready for any run",
                channel_id=None, created_at=now,
            ))
        elif au.warning_count > 0:
            notes.append(Notification(
                level="warning", category="audit",
                message=f"Acceptance audit has {au.warning_count} warning(s)",
                channel_id=None, created_at=now,
            ))

        # --- Provider health ---
        for p in ph:
            if p.failure_rate > 0.30:
                notes.append(Notification(
                    level="warning", category="provider_health",
                    message=f"Provider '{p.provider}' failure rate: "
                            f"{p.failure_rate:.0%} ({p.failure_count}/{p.total_attempts})",
                    channel_id=None, created_at=now,
                ))

        # --- Agent health ---
        for a in ah:
            non_skip = a.total_runs - a.skip_count
            if non_skip > 0 and a.success_rate < 0.7:
                notes.append(Notification(
                    level="warning", category="agent_health",
                    message=f"Stage '{a.stage_name}' success rate: "
                            f"{a.success_rate:.0%} ({a.success_count}/{non_skip})",
                    channel_id=None, created_at=now,
                ))

        # --- Channels ---
        if not ch:
            notes.append(Notification(
                level="info", category="channels",
                message="No channels configured — use create_channel() or clone_factory() to get started",
                channel_id=None, created_at=now,
            ))
        else:
            incomplete = [c for c in ch if c.profile_completeness < 1.0]
            for c in incomplete:
                notes.append(Notification(
                    level="info", category="channels",
                    message=f"Channel '{c.name}' profile incomplete "
                            f"({c.profile_completeness:.0%} — missing: "
                            + _missing_profiles(c) + ")",
                    channel_id=c.channel_id, created_at=now,
                ))

        return notes
    except Exception:
        return []


def _missing_profiles(ch: ChannelSummary) -> str:
    missing = []
    if not ch.brand_complete:      missing.append("brand")
    if not ch.publishing_complete: missing.append("publishing")
    if not ch.models_complete:     missing.append("models")
    return ", ".join(missing) if missing else "none"


# ===========================================================================
# Active OIS Signals
# ===========================================================================

def get_active_signals(db_path: Optional[Path] = None) -> list:
    """
    Return open (unresolved) signal records from signal_records.

    Each entry is a plain dict with keys: signal_id, signal_type, severity,
    message, channel_id, source_system, blocking, lifecycle_status, created_at.
    Non-blocking: returns [] on any error.
    """
    try:
        import json as _json
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT signal_id, signal_type, severity, full_record, channel_id,
                       source_system, blocking, lifecycle_status, ingested_at
                FROM signal_records
                WHERE lifecycle_status = 'open'
                ORDER BY ingested_at DESC
            """).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            d = dict(r)
            try:
                full = _json.loads(d.pop("full_record") or "{}")
                d["message"] = full.get("message", "")
            except Exception:
                d.pop("full_record", None)
                d["message"] = ""
            d["created_at"] = d.pop("ingested_at", None)
            result.append(d)
        return result
    except Exception:
        return []


# ===========================================================================
# Schedules
# ===========================================================================

def get_schedules(db_path: Optional[Path] = None) -> list:
    """
    Return schedule rows from the schedules table.

    Each entry is a plain dict with keys: schedule_id, channel_id, workflow,
    cadence, day_of_week, time_of_day, timezone, status, next_run_at, last_run_at.
    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT schedule_id, scope, channel_id, cadence,
                       status, next_run_at, last_run_at, label
                FROM schedules
                ORDER BY channel_id, cadence
            """).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# Overall Health
# ===========================================================================

def _compute_overall_health(
    run_health:     RunHealthSummary,
    audit:          AuditSummary,
    active_signals: Optional[list] = None,
) -> str:
    # Active blocking OIS signals always degrade the factory regardless of run history
    if active_signals:
        if any(s.get("blocking") for s in active_signals):
            return "degraded"
    if run_health.total_runs == 0:
        return "unknown"
    if run_health.success_rate < 0.5:
        return "degraded"
    if run_health.success_rate < 0.8 or audit.blocker_count > 0:
        return "warning"
    return "healthy"


# ===========================================================================
# Factory State (aggregated)
# ===========================================================================

def _get_factory_identity(factory_id: str, db_path: Optional[Path]) -> tuple:
    """Return (factory_name, factory_status) from the factories table. Never raises."""
    try:
        from hypogeum.factory_entity import get_factory
        rec = get_factory(factory_id=factory_id, db_path=db_path)
        if rec is not None:
            return rec.factory_name, rec.status
    except Exception:
        pass
    return factory_id, "active"


def _get_pending_approvals(db_path: Optional[Path]) -> list:
    """Return pending approval records for the factory state snapshot."""
    try:
        from hypogeum.approvals import list_pending_approvals
        return list_pending_approvals(db_path=db_path)
    except Exception:
        return []


def get_factory_state(
    db_path:    Optional[Path] = None,
    factory_id: str            = "hypogeum",
) -> FactoryState:
    """
    Assemble a complete snapshot of factory state from all data sources.

    All sub-queries are non-blocking. A partial failure in any section
    returns an empty/zeroed section rather than aborting the whole snapshot.

    Args:
        db_path:    Override SQLite path.
        factory_id: Stable identifier for this factory instance.

    Returns:
        FactoryState dataclass.
    """
    checked_at = _now_iso()

    factory_name, factory_status = _get_factory_identity(factory_id, db_path)
    run_health      = get_run_health(db_path)
    channels        = get_channel_summaries(db_path)
    agent_health    = get_agent_health(db_path)
    model_usage     = get_model_usage(db_path)
    provider_health   = get_provider_health(db_path)
    provider_catalog  = get_provider_catalog(db_path)
    archive           = get_archive_summary(db_path)
    recall          = get_recall_summary(db_path)
    learning        = get_learning_summary(db_path)
    costs           = get_cost_summary(db_path)
    audit           = get_audit_summary(db_path)
    active_signals  = get_active_signals(db_path)
    schedules       = get_schedules(db_path)
    notifications   = get_notifications(
        db_path=db_path,
        run_health=run_health,
        audit=audit,
        provider_health=provider_health,
        agent_health=agent_health,
        channels=channels,
    )

    overall_health = _compute_overall_health(run_health, audit, active_signals)

    return FactoryState(
        factory_id        = factory_id,
        schema_version    = SCHEMA_VERSION,
        checked_at        = checked_at,
        overall_health    = overall_health,
        run_health        = run_health,
        channels          = channels,
        agent_health      = agent_health,
        model_usage       = model_usage,
        provider_health   = provider_health,
        provider_catalog  = provider_catalog,
        archive           = archive,
        recall            = recall,
        learning          = learning,
        costs             = costs,
        audit             = audit,
        notifications     = notifications,
        factory_name      = factory_name,
        factory_status    = factory_status,
        active_signals    = active_signals,
        schedules         = schedules,
        pending_approvals = _get_pending_approvals(db_path),
    )


# ===========================================================================
# Text Formatter
# ===========================================================================

def format_factory_state(state: FactoryState) -> str:
    """
    Render a FactoryState to a terminal-friendly text report.

    No network calls. No DB access. Pure formatting.
    """
    lines = [
        f"Factory State — {state.factory_name or state.factory_id}",
        f"  Factory ID:   {state.factory_id}",
        f"  Status:       {state.factory_status}",
        f"  Checked at:   {state.checked_at}",
        f"  Schema:       {state.schema_version}",
        f"  Health:       {state.overall_health.upper()}",
        "",
        "Run Health",
        f"  Total runs:   {state.run_health.total_runs}",
        f"  Complete:     {state.run_health.complete_count}",
        f"  Failed:       {state.run_health.failed_count}",
        f"  Running:      {state.run_health.running_count}",
        f"  Success rate: {state.run_health.success_rate:.0%}",
        f"  Last run:     {state.run_health.last_run_at or 'never'}",
        "",
        f"Channels ({len(state.channels)})",
    ]
    if state.channels:
        for ch in state.channels:
            lines.append(
                f"  [{ch.status:8s}] {ch.channel_id:30s} "
                f"{ch.profile_completeness:.0%} complete"
            )
    else:
        lines.append("  (none configured)")

    lines += ["", f"Agent Health ({len(state.agent_health)} stages)"]
    for a in state.agent_health[:10]:
        lines.append(
            f"  {a.stage_name:28s}  {a.success_rate:5.0%}  "
            f"({a.success_count}/{a.total_runs - a.skip_count})"
        )
    if len(state.agent_health) > 10:
        lines.append(f"  … {len(state.agent_health) - 10} more")

    lines += ["", f"Provider Health ({len(state.provider_health)} providers)"]
    for p in state.provider_health:
        lines.append(
            f"  {p.provider:20s}  fail={p.failure_rate:.0%}  "
            f"cost=${p.total_cost_usd:.4f}"
        )

    lines += [
        "",
        "Costs",
        f"  Total:        ${state.costs.total_usd:.4f}",
        f"  Episodes:     {state.costs.episode_count}",
        f"  Runs:         {state.costs.run_count}",
    ]
    for prov, cost in sorted(state.costs.by_provider.items()):
        lines.append(f"  {prov:22s}  ${cost:.4f}")

    lines += [
        "",
        "Archive",
        f"  Total:        {state.archive.total_archived}",
        f"  Reuse eligible: {state.archive.reuse_eligible_count}",
    ]

    lines += [
        "",
        "Recall",
        f"  Decisions:    {state.recall.total_decisions}",
        f"  Reuse rate:   {state.recall.reuse_rate:.0%}",
    ]

    lines += [
        "",
        "Learning",
        f"  Records:      {state.learning.total_records}",
    ]

    lines += [
        "",
        "Audit",
        f"  Ready:        {'YES' if state.audit.ready else 'NO'}",
        f"  Blockers:     {state.audit.blocker_count}",
        f"  Warnings:     {state.audit.warning_count}",
        f"  Detail:       {state.audit.detail}",
    ]

    if state.notifications:
        lines += ["", f"Notifications ({len(state.notifications)})"]
        for n in state.notifications:
            lines.append(f"  [{n.level.upper():8s}] [{n.category}] {n.message}")
    else:
        lines += ["", "Notifications: none"]

    if state.pending_approvals:
        lines += ["", f"Pending Approvals ({len(state.pending_approvals)})"]
        for a in state.pending_approvals:
            run_short = getattr(a, "run_id", "?")[:12]
            lines.append(
                f"  [{getattr(a,'approval_type','?'):20s}] "
                f"run={run_short}... ep={getattr(a,'episode_id','?')}"
            )
    else:
        lines += ["", "Pending Approvals: none"]

    if state.active_signals:
        lines += ["", f"Active OIS Signals ({len(state.active_signals)})"]
        for s in state.active_signals:
            blocking = " [BLOCKING]" if s.get("blocking") else ""
            lines.append(
                f"  [{s.get('severity','?'):8s}] {s.get('signal_type','?')}"
                f"{blocking}: {str(s.get('message',''))[:60]}"
            )
    else:
        lines += ["", "Active OIS Signals: none"]

    if state.schedules:
        lines += ["", f"Schedules ({len(state.schedules)})"]
        for sc in state.schedules:
            ch = sc.get("channel_id") or sc.get("scope", "?")
            lines.append(
                f"  [{sc.get('status','?'):8s}] {ch:20s} "
                f"{sc.get('cadence','?'):10s} next={sc.get('next_run_at') or 'unset'}"
            )
    else:
        lines += ["", "Schedules: none"]

    return "\n".join(lines)
