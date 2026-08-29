"""
hypogeum/channel_config.py — Phase A: Channel Abstraction

Makes channels data, not code. Every piece of identity, brand, publishing
configuration, and model selection that was previously hardcoded in config.py,
world_bible.py, or pipeline.py is expressed here as a stored profile.

A factory can host multiple channels simultaneously. Each channel is a
collection of four profiles:

    BrandProfile        — persona, tone, audience, visuals, voice guidelines
    PublishingProfile   — enabled platforms, scheduling, tags, disclosure rules
    ModelProfile        — AI model slug assignments per pipeline stage
    ChannelProfile      — root entity binding the three profiles together

Architecture:
    All tables are created lazily (not in schema.sql). All functions are
    non-blocking. Every write is idempotent where possible. No network calls.

Public API:
    create_brand_profile(...)    -> BrandProfile
    get_brand_profile(id)        -> Optional[BrandProfile]
    update_brand_profile(id, **) -> bool
    list_brand_profiles()        -> list[BrandProfile]

    create_publishing_profile(...)  -> PublishingProfile
    get_publishing_profile(id)      -> Optional[PublishingProfile]
    update_publishing_profile(id,**) -> bool
    list_publishing_profiles()      -> list[PublishingProfile]

    create_model_profile(...)    -> ModelProfile
    get_model_profile(id)        -> Optional[ModelProfile]
    update_model_profile(id, **) -> bool
    list_model_profiles()        -> list[ModelProfile]

    create_channel_profile(...)  -> ChannelProfile
    get_channel_profile(id)      -> Optional[ChannelProfile]
    update_channel_profile(id,**) -> bool
    list_channel_profiles(...)   -> list[ChannelProfile]
    deactivate_channel(id)       -> bool

    bootstrap_channel(config_dict, db_path=None) -> ChannelProfile
    load_channel_config(channel_id, db_path=None) -> Optional[ChannelConfig]

    create_channel_operations_profile(...) -> ChannelOperationsProfile
    get_channel_operations_profile(channel_id)      -> Optional[ChannelOperationsProfile]
    update_channel_operations_profile(channel_id,**) -> bool

    CHANNEL_CONFIG_SCHEMA        — JSON Schema dict for full channel config

Channel config dict shape (for bootstrap_channel):
    {
      "channel_id": "yt_tech_001",
      "name": "Tech Explained",
      "description": "...",
      "brand": {
        "persona_name": "Alex Chen",
        "tone": "conversational",
        "target_audience": "software engineers 25-40",
        "visual_style": "dark minimal",
        "language": "en",
        "hashtags": ["#tech", "#coding"],
        "color_palette": {"primary": "#0F0F0F"},
        "typography": {"headline_font": "Inter"},
        "voice_guidelines": "...",
        "content_pillars": ["AI", "SWE", "Architecture"]
      },
      "publishing": {
        "enabled_platforms": ["youtube", "tiktok"],
        "schedule": {"cadence": "weekly", "day_of_week": "thursday"},
        "default_tags": ["tech", "tutorial"],
        "ai_disclosure_required": true,
        "auto_thumbnail": true,
        "auto_captions": true
      },
      "models": {
        "episode_producer":    "anthropic/claude-sonnet-4.6",
        "research_director":   "anthropic/claude-opus-4.6",
        "thumbnail_planner":   "anthropic/claude-sonnet-4.6",
        "brand_guardian_p1":   "anthropic/claude-sonnet-4.6",
        "brand_guardian_p2":   "openai/gpt-5.5-pro",
        "narration_voice_id":  "EXAVITQu4vr4xnSDxMaL",
        "image_provider":      "muapi",
        "video_provider":      "muapi"
      }
    }
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"

SUPPORTED_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "email")


# ---------------------------------------------------------------------------
# JSON Schema for full channel config (used by validation)
# ---------------------------------------------------------------------------

CHANNEL_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ChannelConfig",
    "type": "object",
    "required": ["channel_id", "name", "brand", "publishing", "models"],
    "properties": {
        "channel_id":   {"type": "string", "minLength": 1},
        "name":         {"type": "string", "minLength": 1},
        "description":  {"type": "string"},
        "brand": {
            "type": "object",
            "required": ["language"],
            "properties": {
                "persona_name":    {"type": ["string", "null"]},
                "tone":            {"type": ["string", "null"]},
                "target_audience": {"type": ["string", "null"]},
                "visual_style":    {"type": ["string", "null"]},
                "language":        {"type": "string", "default": "en"},
                "hashtags":        {"type": "array", "items": {"type": "string"}},
                "color_palette":   {"type": "object"},
                "typography":      {"type": "object"},
                "voice_guidelines":{"type": ["string", "null"]},
                "content_pillars": {"type": "array", "items": {"type": "string"}},
            },
        },
        "publishing": {
            "type": "object",
            "required": ["enabled_platforms"],
            "properties": {
                "enabled_platforms":   {
                    "type": "array",
                    "items": {"type": "string", "enum": list(SUPPORTED_PLATFORMS)},
                },
                "schedule":            {"type": "object"},
                "default_tags":        {"type": "array", "items": {"type": "string"}},
                "ai_disclosure_required": {"type": "boolean", "default": True},
                "auto_thumbnail":      {"type": "boolean", "default": True},
                "auto_captions":       {"type": "boolean", "default": True},
                # ADR-013 Decision 7, item 3 — logical provider preference,
                # never a live external ID. {} (default) preserves current
                # Factory-default direct-provider routing for every platform.
                "publishing_provider_by_platform": {"type": "object"},
                "analytics_provider_by_platform":  {"type": "object"},
                "provider_alias_by_platform":       {"type": "object"},
            },
        },
        "models": {
            "type": "object",
            "properties": {
                "episode_producer":   {"type": "string"},
                "research_director":  {"type": "string"},
                "thumbnail_planner":  {"type": "string"},
                "brand_guardian_p1":  {"type": "string"},
                "brand_guardian_p2":  {"type": "string"},
                "narration_voice_id": {"type": ["string", "null"]},
                "image_provider":     {"type": "string", "default": "muapi"},
                "video_provider":     {"type": "string", "default": "muapi"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BrandProfile:
    """All brand identity data for one channel. Data, not code."""
    brand_profile_id: str
    name:             str                     # human label, e.g. "Tech Explained Brand"
    persona_name:     Optional[str]  = None   # "Alex Chen"
    tone:             Optional[str]  = None   # "conversational"
    target_audience:  Optional[str]  = None   # "software engineers 25-40"
    visual_style:     Optional[str]  = None   # "dark minimal"
    language:         str            = "en"
    hashtags:         list[str]      = field(default_factory=list)
    color_palette:    dict           = field(default_factory=dict)
    typography:       dict           = field(default_factory=dict)
    voice_guidelines: Optional[str]  = None
    content_pillars:  list[str]      = field(default_factory=list)
    created_at:       str            = ""
    metadata_version: str            = METADATA_VERSION


@dataclass
class PublishingProfile:
    """Platform and scheduling configuration for one channel."""
    publishing_profile_id: str
    name:                  str
    enabled_platforms:     list[str]  = field(default_factory=list)
    schedule:              dict       = field(default_factory=dict)
    default_tags:          list[str]  = field(default_factory=list)
    ai_disclosure_required: bool      = True
    auto_thumbnail:        bool       = True
    auto_captions:         bool       = True
    # ADR-013 Decision 7, item 3: logical provider preference owned by the
    # Channel Package. {platform: provider_id} -- e.g.
    # {"youtube": "youtube_direct"} or a future registered provider_id.
    # Empty dict (default) means "use Factory default routing for every
    # platform" -- see hypogeum.publishing.provider_registry.DEFAULT_PROVIDER_ROUTING.
    # Never stores a live external ID; only a logical provider selection.
    publishing_provider_by_platform: dict = field(default_factory=dict)
    analytics_provider_by_platform:  dict = field(default_factory=dict)
    # {platform: logical_alias} -- resolved operationally via
    # hypogeum.publishing.provider_repository.resolve_active_binding(...,
    # logical_alias=provider_alias_by_platform[platform]). Empty dict means
    # every platform uses the 'default' alias.
    provider_alias_by_platform:      dict = field(default_factory=dict)
    created_at:            str        = ""
    metadata_version:      str        = METADATA_VERSION


@dataclass
class ModelProfile:
    """AI model and provider assignments for one channel's pipeline."""
    model_profile_id:  str
    name:              str
    episode_producer:  str            = "anthropic/claude-sonnet-4.6"
    research_director: str            = "anthropic/claude-opus-4.6"
    thumbnail_planner: str            = "anthropic/claude-sonnet-4.6"
    brand_guardian_p1: str            = "anthropic/claude-sonnet-4.6"
    brand_guardian_p2: str            = "openai/gpt-5.5-pro"
    narration_voice_id: Optional[str] = None
    image_provider:    str            = "muapi"
    video_provider:    str            = "muapi"
    created_at:        str            = ""
    metadata_version:  str            = METADATA_VERSION


@dataclass
class ChannelProfile:
    """
    Root entity for one channel. Binds the three sub-profiles together.

    A channel can run in the factory without any code changes — everything
    the pipeline needs is resolved through this profile.
    """
    channel_id:            str
    name:                  str
    description:           str            = ""
    brand_profile_id:      Optional[str]  = None
    publishing_profile_id: Optional[str]  = None
    model_profile_id:      Optional[str]  = None
    status:                str            = "active"   # active|paused|archived
    created_at:            str            = ""
    metadata_version:      str            = METADATA_VERSION


@dataclass
class ChannelConfig:
    """
    Fully resolved channel configuration — all sub-profiles joined.

    This is what the pipeline receives at runtime. No env vars or
    hardcoded defaults needed; everything comes from the stored profiles.
    """
    channel:     ChannelProfile
    brand:       Optional[BrandProfile]       = None
    publishing:  Optional[PublishingProfile]  = None
    models:      Optional[ModelProfile]       = None


@dataclass
class ChannelOperationsProfile:
    """
    Runtime operational settings for one channel — a sibling profile to
    Brand/Publishing/Model, layered over an existing ChannelProfile.

    operator_enabled represents operator INTENT ("I want this channel to
    run autonomously"). It is distinct from hypogeum.calog's channel state
    machine, which remains the sole gate-enforcement authority (credentials,
    budget, OIS-clear, profile-complete). Future autonomous execution
    requires BOTH operator_enabled == True AND calog reporting the channel
    runnable — this profile does not replace or duplicate that check.
    """
    channel_id:                   str
    channel_name:                 str
    timezone:                     str
    operator_enabled:             bool

    production_cadence:           dict
    publishing_schedule:          dict

    enabled_platforms:            dict
    blocked_platform_reasons:     dict

    discovery_enabled:            bool
    production_enabled:           bool
    publishing_enabled:           bool
    analytics_sync_enabled:       bool
    unattended_operation_enabled: bool

    max_concurrent_episodes:      int
    retry_policy:                 dict
    failure_halt_policy:          dict

    cost_ceiling_usd_per_episode: Optional[float]
    default_privacy_state:        str

    created_at:                   str
    updated_at:                   str
    metadata_version:             str = METADATA_VERSION

    # Phase 3 (2026-08-25): per-channel weekly paid-video budget ceiling in
    # USD. None = not configured -- the weekly enforcement point
    # (hypogeum.weekly_budget, via hypogeum.muapi's paid video boundary)
    # fails closed on None exactly as on any other missing or malformed
    # value; it never treats None as unlimited. Values <= 0 are rejected at
    # write time by _validate_operations_fields().
    weekly_video_budget_usd:      Optional[float] = None

    # Optional, per-deliverable-type schedule override (ADR — autonomous
    # runtime slice). Empty dict (default) means "no per-deliverable
    # policy configured" -- hypogeum.publishing.schedule_compiler falls
    # back to the single publishing_schedule slot for every deliverable,
    # exactly as before this field existed. Keyed by an opaque
    # deliverable_id (e.g. 'main_episode', 'short_0') this module assigns
    # no meaning to -- the actual keys and values are entirely
    # channel-declared (see channel_packages/{channel_id}/ for the
    # versioned, reviewable definition of what should be installed here).
    # Each entry: {"day_of_week": ..., "time_of_day": ..., "timezone": ...}
    # (same shape hypogeum.scheduler.compute_next_publish_slot() already
    # consumes for the single-slot case).
    deliverable_schedule_policy:  dict = field(default_factory=dict)

    # Optional, per-BUNDLE (not per-deliverable) schedule policy. Empty
    # dict (default) means "no bundle-cycle policy configured" --
    # hypogeum.publishing.schedule_compiler falls back to
    # deliverable_schedule_policy / the single publishing_schedule slot,
    # exactly as before this field existed. Keyed by an opaque
    # bundle_cycle_id this module assigns no meaning to. Each entry:
    #   {
    #     "anchor_deliverable_id": <opaque deliverable_id>,
    #     "required_deliverable_ids": [<opaque deliverable_id>, ...],  # optional
    #     "deliverables": {
    #         <opaque deliverable_id>: {
    #             "day_of_week": ..., "time_of_day": ..., "timezone": ...,
    #             "anchor_relation": "after" | "before",  # optional, default "after"
    #         }, ...
    #     },
    #   }
    # Factory Core assigns no meaning to bundle_cycle_id, deliverable_id
    # values, day_of_week, or the number of members -- a channel with a
    # single-deliverable "bundle" or a Shorts-only bundle anchored on a
    # Short is exactly as valid as Hypogeum's 4-member, main-anchored one.
    # See channel_packages/{channel_id}/ for the versioned, reviewable
    # definition of what should be installed here.
    bundle_cycle_policy:          dict = field(default_factory=dict)

    # Optional, per-(platform, deliverable CLASS) distribution policy.
    # Empty dict (default) means "no platform-distribution policy
    # configured" -- callers that need per-platform routing (e.g.
    # hypogeum.publishing.buffer_multi_platform, once installed) must fail
    # closed rather than guess a default matrix. Keyed by an opaque
    # platform string (Factory Core assigns no meaning to which platform
    # names appear, or how many) -> deliverable CLASS
    # ('main_episode' | 'shorts', see
    # hypogeum.publishing.deliverable_ids.classify_deliverable_class() --
    # the one place Factory Core defines what a "deliverable class" is) ->
    # {"enabled": bool}. A platform/class combination absent from this
    # dict is treated as NOT enabled (fail closed on omission, never an
    # implicit "everything is allowed" default). See
    # channel_packages/{channel_id}/ for the versioned, reviewable
    # definition of what should be installed here -- this field never
    # carries a Hypogeum-specific default itself.
    platform_deliverable_policy: dict = field(default_factory=dict)

    # Optional, channel-declared list of deliverable/bundle definitions --
    # the generic Channel Package contract that replaces run_worker.py's
    # previous hardcoded Hypogeum-only bundle shape (see
    # run_worker.py::_resolve_bundle_definitions_for_channel()'s own
    # docstring, and hypogeum/buffer.py's SCHEMA GAP note, both of which
    # this field closes). Empty list (default) means "no bundle
    # definitions declared" -- a channel with none configured is never
    # buffer-gated at all (falls through to queue-depth-only discovery
    # refill, unaffected), exactly the same fail-closed-by-omission
    # behavior the stand-in already used. Each entry:
    #   {"bundle_id": <opaque str>, "target_buffer_count": <int>}
    # Factory Core assigns no meaning to bundle_id or how many entries a
    # channel declares -- hypogeum.buffer.DeliverableBundleDefinition is
    # built from exactly these two fields, unchanged.
    deliverable_bundle_definitions: list = field(default_factory=list)

    # Optional, per-(deliverable CLASS, platform) evidence-based
    # publication-slot optimizer policy (hypogeum.schedule_optimizer).
    # Empty dict (default) means "analytics scheduling not configured" --
    # the optimizer is never consulted; scheduling behaves exactly as
    # before this field existed (deliverable_schedule_policy /
    # bundle_cycle_policy / publishing_schedule only). Keyed by an opaque
    # deliverable_class ('main_episode' | 'shorts') -> platform ->
    #   {"enabled": bool, "evidence_window_days": int,
    #    "min_sample_size": int, "min_confidence": float,
    #    "allowed_days": [<'monday'..'sunday'>, ...],
    #    "allowed_hours": [<0-23>, ...],
    #    "min_spacing_hours": int}.
    # A (deliverable_class, platform) combination absent from this dict,
    # or present with "enabled": false, is never optimized -- the
    # existing static schedule layers are consulted unchanged. See
    # hypogeum.schedule_optimizer.optimize_publication_slot()'s own
    # docstring for exactly how each of these fields is used, and its
    # fail-closed fallback contract when evidence is insufficient.
    analytics_scheduling_policy: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    from hypogeum.db import resolve_db_path as _r  # noqa: PLC0415

    return _r(db_path, module_default=_DEFAULT_DB_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _uj(raw: Optional[str], default=None):
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except Exception:
        return default if default is not None else {}


def _ensure_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS brand_profiles (
            brand_profile_id    TEXT    NOT NULL PRIMARY KEY,
            name                TEXT    NOT NULL,
            persona_name        TEXT,
            tone                TEXT,
            target_audience     TEXT,
            visual_style        TEXT,
            language            TEXT    NOT NULL DEFAULT 'en',
            hashtags_json       TEXT    NOT NULL DEFAULT '[]',
            color_palette_json  TEXT    NOT NULL DEFAULT '{}',
            typography_json     TEXT    NOT NULL DEFAULT '{}',
            voice_guidelines    TEXT,
            content_pillars_json TEXT   NOT NULL DEFAULT '[]',
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS publishing_profiles (
            publishing_profile_id   TEXT    NOT NULL PRIMARY KEY,
            name                    TEXT    NOT NULL,
            enabled_platforms_json  TEXT    NOT NULL DEFAULT '[]',
            schedule_json           TEXT    NOT NULL DEFAULT '{}',
            default_tags_json       TEXT    NOT NULL DEFAULT '[]',
            ai_disclosure_required  INTEGER NOT NULL DEFAULT 1,
            auto_thumbnail          INTEGER NOT NULL DEFAULT 1,
            auto_captions           INTEGER NOT NULL DEFAULT 1,
            publishing_provider_by_platform_json TEXT NOT NULL DEFAULT '{}',
            analytics_provider_by_platform_json  TEXT NOT NULL DEFAULT '{}',
            provider_alias_by_platform_json      TEXT NOT NULL DEFAULT '{}',
            created_at              TEXT    NOT NULL,
            metadata_version        TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS model_profiles (
            model_profile_id    TEXT    NOT NULL PRIMARY KEY,
            name                TEXT    NOT NULL,
            episode_producer    TEXT    NOT NULL DEFAULT 'anthropic/claude-sonnet-4.6',
            research_director   TEXT    NOT NULL DEFAULT 'anthropic/claude-opus-4.6',
            thumbnail_planner   TEXT    NOT NULL DEFAULT 'anthropic/claude-sonnet-4.6',
            brand_guardian_p1   TEXT    NOT NULL DEFAULT 'anthropic/claude-sonnet-4.6',
            brand_guardian_p2   TEXT    NOT NULL DEFAULT 'openai/gpt-5.5-pro',
            narration_voice_id  TEXT,
            image_provider      TEXT    NOT NULL DEFAULT 'muapi',
            video_provider      TEXT    NOT NULL DEFAULT 'muapi',
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS channel_profiles (
            channel_id              TEXT    NOT NULL PRIMARY KEY,
            name                    TEXT    NOT NULL,
            description             TEXT    NOT NULL DEFAULT '',
            brand_profile_id        TEXT,
            publishing_profile_id   TEXT,
            model_profile_id        TEXT,
            status                  TEXT    NOT NULL DEFAULT 'active',
            created_at              TEXT    NOT NULL,
            metadata_version        TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE INDEX IF NOT EXISTS idx_channel_profiles_status
            ON channel_profiles(status);
        CREATE INDEX IF NOT EXISTS idx_channel_profiles_brand
            ON channel_profiles(brand_profile_id);

        CREATE TABLE IF NOT EXISTS channel_operations_profiles (
            channel_id                    TEXT    NOT NULL PRIMARY KEY,
            channel_name                  TEXT    NOT NULL,
            timezone                      TEXT    NOT NULL DEFAULT 'UTC',
            operator_enabled               INTEGER NOT NULL DEFAULT 1,
            production_cadence_json       TEXT    NOT NULL DEFAULT '{}',
            publishing_schedule_json      TEXT    NOT NULL DEFAULT '{}',
            enabled_platforms_json        TEXT    NOT NULL DEFAULT '{}',
            blocked_platform_reasons_json TEXT    NOT NULL DEFAULT '{}',
            discovery_enabled              INTEGER NOT NULL DEFAULT 1,
            production_enabled             INTEGER NOT NULL DEFAULT 1,
            publishing_enabled             INTEGER NOT NULL DEFAULT 1,
            analytics_sync_enabled         INTEGER NOT NULL DEFAULT 1,
            unattended_operation_enabled   INTEGER NOT NULL DEFAULT 0,
            max_concurrent_episodes        INTEGER NOT NULL DEFAULT 1,
            retry_policy_json              TEXT    NOT NULL DEFAULT '{}',
            failure_halt_policy_json       TEXT    NOT NULL DEFAULT '{}',
            cost_ceiling_usd_per_episode   REAL,
            weekly_video_budget_usd        REAL,
            default_privacy_state          TEXT    NOT NULL DEFAULT 'private',
            created_at                     TEXT    NOT NULL,
            updated_at                     TEXT    NOT NULL,
            metadata_version                TEXT   NOT NULL DEFAULT 'v1'
        );

        CREATE INDEX IF NOT EXISTS idx_channel_operations_profiles_enabled
            ON channel_operations_profiles(operator_enabled);
    """)
    conn.commit()
    _apply_publishing_profile_column_migrations(conn)
    _apply_channel_operations_profile_column_migrations(conn)


# SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS. This table is
# created by _ensure_tables() above, not by hypogeum.db.initialize_schema()
# (channel_config.py's tables are a separate, lazily-created initialization
# path -- see this module's docstring), so it cannot use hypogeum.db's own
# _V2_COLUMN_MIGRATIONS map. Mirrors that same idempotent
# PRAGMA-table_info-then-ALTER pattern, scoped locally to this module's own
# tables (ADR-013 Decision 7, item 3 -- provider preference columns).
_PUBLISHING_PROFILE_COLUMN_MIGRATIONS: list[tuple[str, str]] = [
    ("publishing_provider_by_platform_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("analytics_provider_by_platform_json",  "TEXT NOT NULL DEFAULT '{}'"),
    ("provider_alias_by_platform_json",      "TEXT NOT NULL DEFAULT '{}'"),
]


def _apply_publishing_profile_column_migrations(conn) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(publishing_profiles)").fetchall()
    }
    for col_name, col_def in _PUBLISHING_PROFILE_COLUMN_MIGRATIONS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE publishing_profiles ADD COLUMN {col_name} {col_def}")
    conn.commit()


# Same idempotent PRAGMA-table_info-then-ALTER pattern as
# _apply_publishing_profile_column_migrations() above, scoped to
# channel_operations_profiles. Adds the optional per-deliverable-type
# schedule override column (see ChannelOperationsProfile.deliverable_schedule_policy's
# own docstring) without touching any existing row's other values.
_CHANNEL_OPERATIONS_PROFILE_COLUMN_MIGRATIONS: list[tuple[str, str]] = [
    ("deliverable_schedule_policy_json", "TEXT"),
    ("bundle_cycle_policy_json", "TEXT"),
    ("platform_deliverable_policy_json", "TEXT"),
    # 2026-08-23 (autonomous weekly runtime): channel-agnostic replacement
    # for run_worker.py's hardcoded Hypogeum bundle shape (see
    # _resolve_bundle_definitions_for_channel()'s own docstring for the
    # gap this closes) and the opt-in evidence-based scheduling policy
    # consumed by hypogeum.schedule_optimizer. Both default to
    # NULL/empty -- a channel with neither configured behaves exactly as
    # before either field existed (no bundle gating, static
    # deliverable_schedule_policy/publishing_schedule only).
    ("deliverable_bundle_definitions_json", "TEXT"),
    ("analytics_scheduling_policy_json", "TEXT"),
    # Phase 3 (2026-08-25): per-channel weekly paid-video budget ceiling in
    # USD. NULL means "no weekly ceiling configured" -- the enforcement
    # point (hypogeum.weekly_budget via hypogeum.muapi's paid video
    # boundary) fails closed on NULL exactly as on any other missing or
    # malformed value, so a channel is never silently unlimited. Values
    # <= 0 are rejected at write time by _validate_operations_fields().
    ("weekly_video_budget_usd", "REAL"),
]


def _apply_channel_operations_profile_column_migrations(conn) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(channel_operations_profiles)").fetchall()
    }
    for col_name, col_def in _CHANNEL_OPERATIONS_PROFILE_COLUMN_MIGRATIONS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE channel_operations_profiles ADD COLUMN {col_name} {col_def}")
    conn.commit()


# ---------------------------------------------------------------------------
# Row → dataclass converters
# ---------------------------------------------------------------------------

def _row_to_brand(row) -> BrandProfile:
    return BrandProfile(
        brand_profile_id = row["brand_profile_id"],
        name             = row["name"],
        persona_name     = row["persona_name"],
        tone             = row["tone"],
        target_audience  = row["target_audience"],
        visual_style     = row["visual_style"],
        language         = row["language"] or "en",
        hashtags         = _uj(row["hashtags_json"], []),
        color_palette    = _uj(row["color_palette_json"], {}),
        typography       = _uj(row["typography_json"], {}),
        voice_guidelines = row["voice_guidelines"],
        content_pillars  = _uj(row["content_pillars_json"], []),
        created_at       = row["created_at"],
        metadata_version = row["metadata_version"],
    )


def _row_to_publishing(row) -> PublishingProfile:
    keys = row.keys()
    return PublishingProfile(
        publishing_profile_id  = row["publishing_profile_id"],
        name                   = row["name"],
        enabled_platforms      = _uj(row["enabled_platforms_json"], []),
        schedule               = _uj(row["schedule_json"], {}),
        default_tags           = _uj(row["default_tags_json"], []),
        ai_disclosure_required = bool(row["ai_disclosure_required"]),
        auto_thumbnail         = bool(row["auto_thumbnail"]),
        auto_captions          = bool(row["auto_captions"]),
        publishing_provider_by_platform = (
            _uj(row["publishing_provider_by_platform_json"], {})
            if "publishing_provider_by_platform_json" in keys else {}
        ),
        analytics_provider_by_platform = (
            _uj(row["analytics_provider_by_platform_json"], {})
            if "analytics_provider_by_platform_json" in keys else {}
        ),
        provider_alias_by_platform = (
            _uj(row["provider_alias_by_platform_json"], {})
            if "provider_alias_by_platform_json" in keys else {}
        ),
        created_at             = row["created_at"],
        metadata_version       = row["metadata_version"],
    )


def _row_to_model(row) -> ModelProfile:
    return ModelProfile(
        model_profile_id  = row["model_profile_id"],
        name              = row["name"],
        episode_producer  = row["episode_producer"],
        research_director = row["research_director"],
        thumbnail_planner = row["thumbnail_planner"],
        brand_guardian_p1 = row["brand_guardian_p1"],
        brand_guardian_p2 = row["brand_guardian_p2"],
        narration_voice_id= row["narration_voice_id"],
        image_provider    = row["image_provider"],
        video_provider    = row["video_provider"],
        created_at        = row["created_at"],
        metadata_version  = row["metadata_version"],
    )


def _row_to_channel(row) -> ChannelProfile:
    return ChannelProfile(
        channel_id            = row["channel_id"],
        name                  = row["name"],
        description           = row["description"] or "",
        brand_profile_id      = row["brand_profile_id"],
        publishing_profile_id = row["publishing_profile_id"],
        model_profile_id      = row["model_profile_id"],
        status                = row["status"],
        created_at            = row["created_at"],
        metadata_version      = row["metadata_version"],
    )


def _row_to_operations(row) -> ChannelOperationsProfile:
    keys = row.keys()
    return ChannelOperationsProfile(
        channel_id                   = row["channel_id"],
        channel_name                 = row["channel_name"],
        timezone                     = row["timezone"],
        operator_enabled              = bool(row["operator_enabled"]),
        production_cadence           = _uj(row["production_cadence_json"], {}),
        publishing_schedule          = _uj(row["publishing_schedule_json"], {}),
        enabled_platforms            = _uj(row["enabled_platforms_json"], {}),
        blocked_platform_reasons     = _uj(row["blocked_platform_reasons_json"], {}),
        discovery_enabled            = bool(row["discovery_enabled"]),
        production_enabled           = bool(row["production_enabled"]),
        publishing_enabled           = bool(row["publishing_enabled"]),
        analytics_sync_enabled       = bool(row["analytics_sync_enabled"]),
        unattended_operation_enabled = bool(row["unattended_operation_enabled"]),
        max_concurrent_episodes      = row["max_concurrent_episodes"],
        retry_policy                 = _uj(row["retry_policy_json"], {}),
        failure_halt_policy          = _uj(row["failure_halt_policy_json"], {}),
        cost_ceiling_usd_per_episode = row["cost_ceiling_usd_per_episode"],
        weekly_video_budget_usd      = (
            row["weekly_video_budget_usd"] if "weekly_video_budget_usd" in keys else None
        ),
        default_privacy_state        = row["default_privacy_state"],
        created_at                   = row["created_at"],
        updated_at                   = row["updated_at"],
        metadata_version             = row["metadata_version"],
        deliverable_schedule_policy  = (
            _uj(row["deliverable_schedule_policy_json"], {})
            if "deliverable_schedule_policy_json" in keys else {}
        ),
        bundle_cycle_policy          = (
            _uj(row["bundle_cycle_policy_json"], {})
            if "bundle_cycle_policy_json" in keys else {}
        ),
        platform_deliverable_policy  = (
            _uj(row["platform_deliverable_policy_json"], {})
            if "platform_deliverable_policy_json" in keys else {}
        ),
        deliverable_bundle_definitions = (
            _uj(row["deliverable_bundle_definitions_json"], [])
            if "deliverable_bundle_definitions_json" in keys else []
        ),
        analytics_scheduling_policy  = (
            _uj(row["analytics_scheduling_policy_json"], {})
            if "analytics_scheduling_policy_json" in keys else {}
        ),
    )


# ===========================================================================
# BrandProfile CRUD
# ===========================================================================

def create_brand_profile(
    name:              str,
    persona_name:      Optional[str] = None,
    tone:              Optional[str] = None,
    target_audience:   Optional[str] = None,
    visual_style:      Optional[str] = None,
    language:          str           = "en",
    hashtags:          Optional[list[str]] = None,
    color_palette:     Optional[dict]      = None,
    typography:        Optional[dict]      = None,
    voice_guidelines:  Optional[str] = None,
    content_pillars:   Optional[list[str]] = None,
    brand_profile_id:  Optional[str] = None,
    db_path:           Optional[Path] = None,
) -> BrandProfile:
    """Create and persist a BrandProfile. Non-blocking."""
    profile = BrandProfile(
        brand_profile_id = brand_profile_id or str(uuid.uuid4()),
        name             = name,
        persona_name     = persona_name,
        tone             = tone,
        target_audience  = target_audience,
        visual_style     = visual_style,
        language         = language,
        hashtags         = hashtags or [],
        color_palette    = color_palette or {},
        typography       = typography or {},
        voice_guidelines = voice_guidelines,
        content_pillars  = content_pillars or [],
        created_at       = _now_iso(),
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT INTO brand_profiles
                   (brand_profile_id, name, persona_name, tone, target_audience,
                    visual_style, language, hashtags_json, color_palette_json,
                    typography_json, voice_guidelines, content_pillars_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile.brand_profile_id, name, persona_name, tone, target_audience,
                 visual_style, language,
                 _j(profile.hashtags), _j(profile.color_palette),
                 _j(profile.typography), voice_guidelines,
                 _j(profile.content_pillars), profile.created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return profile


def get_brand_profile(
    brand_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[BrandProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM brand_profiles WHERE brand_profile_id=?",
                [brand_profile_id],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_brand(row) if row else None
    except Exception:
        return None


def update_brand_profile(
    brand_profile_id: str,
    db_path: Optional[Path] = None,
    **kwargs,
) -> bool:
    """Update individual fields. Only provided kwargs are changed."""
    _field_map = {
        "name":             "name",
        "persona_name":     "persona_name",
        "tone":             "tone",
        "target_audience":  "target_audience",
        "visual_style":     "visual_style",
        "language":         "language",
        "hashtags":         ("hashtags_json",         _j),
        "color_palette":    ("color_palette_json",    _j),
        "typography":       ("typography_json",        _j),
        "voice_guidelines": "voice_guidelines",
        "content_pillars":  ("content_pillars_json",  _j),
    }
    try:
        assignments, params = [], []
        for k, v in kwargs.items():
            mapping = _field_map.get(k)
            if mapping is None:
                continue
            if isinstance(mapping, tuple):
                col, fn = mapping
                assignments.append(f"{col}=?")
                params.append(fn(v))
            else:
                assignments.append(f"{mapping}=?")
                params.append(v)
        if not assignments:
            return True
        params.append(brand_profile_id)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                f"UPDATE brand_profiles SET {', '.join(assignments)} "
                f"WHERE brand_profile_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def list_brand_profiles(db_path: Optional[Path] = None) -> list[BrandProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM brand_profiles ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_brand(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# PublishingProfile CRUD
# ===========================================================================

def create_publishing_profile(
    name:                  str,
    enabled_platforms:     Optional[list[str]] = None,
    schedule:              Optional[dict]       = None,
    default_tags:          Optional[list[str]]  = None,
    ai_disclosure_required: bool               = True,
    auto_thumbnail:        bool                 = True,
    auto_captions:         bool                 = True,
    publishing_provider_by_platform: Optional[dict] = None,
    analytics_provider_by_platform:  Optional[dict] = None,
    provider_alias_by_platform:      Optional[dict] = None,
    publishing_profile_id: Optional[str]        = None,
    db_path:               Optional[Path]       = None,
) -> PublishingProfile:
    profile = PublishingProfile(
        publishing_profile_id  = publishing_profile_id or str(uuid.uuid4()),
        name                   = name,
        enabled_platforms      = enabled_platforms or [],
        schedule               = schedule or {},
        default_tags           = default_tags or [],
        ai_disclosure_required = ai_disclosure_required,
        auto_thumbnail         = auto_thumbnail,
        auto_captions          = auto_captions,
        publishing_provider_by_platform = publishing_provider_by_platform or {},
        analytics_provider_by_platform  = analytics_provider_by_platform or {},
        provider_alias_by_platform      = provider_alias_by_platform or {},
        created_at             = _now_iso(),
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT INTO publishing_profiles
                   (publishing_profile_id, name, enabled_platforms_json,
                    schedule_json, default_tags_json, ai_disclosure_required,
                    auto_thumbnail, auto_captions,
                    publishing_provider_by_platform_json,
                    analytics_provider_by_platform_json,
                    provider_alias_by_platform_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile.publishing_profile_id, name,
                 _j(profile.enabled_platforms),
                 _j(profile.schedule),
                 _j(profile.default_tags),
                 int(ai_disclosure_required),
                 int(auto_thumbnail),
                 int(auto_captions),
                 _j(profile.publishing_provider_by_platform),
                 _j(profile.analytics_provider_by_platform),
                 _j(profile.provider_alias_by_platform),
                 profile.created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return profile


def get_publishing_profile(
    publishing_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[PublishingProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM publishing_profiles WHERE publishing_profile_id=?",
                [publishing_profile_id],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_publishing(row) if row else None
    except Exception:
        return None


def update_publishing_profile(
    publishing_profile_id: str,
    db_path: Optional[Path] = None,
    **kwargs,
) -> bool:
    _field_map = {
        "name":                  "name",
        "enabled_platforms":     ("enabled_platforms_json",  _j),
        "schedule":              ("schedule_json",           _j),
        "default_tags":          ("default_tags_json",       _j),
        "ai_disclosure_required":("ai_disclosure_required",  int),
        "auto_thumbnail":        ("auto_thumbnail",          int),
        "auto_captions":         ("auto_captions",           int),
        "publishing_provider_by_platform": ("publishing_provider_by_platform_json", _j),
        "analytics_provider_by_platform":  ("analytics_provider_by_platform_json",  _j),
        "provider_alias_by_platform":      ("provider_alias_by_platform_json",      _j),
    }
    try:
        assignments, params = [], []
        for k, v in kwargs.items():
            mapping = _field_map.get(k)
            if mapping is None:
                continue
            if isinstance(mapping, tuple):
                col, fn = mapping
                assignments.append(f"{col}=?")
                params.append(fn(v))
            else:
                assignments.append(f"{mapping}=?")
                params.append(v)
        if not assignments:
            return True
        params.append(publishing_profile_id)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                f"UPDATE publishing_profiles SET {', '.join(assignments)} "
                f"WHERE publishing_profile_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def list_publishing_profiles(db_path: Optional[Path] = None) -> list[PublishingProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM publishing_profiles ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_publishing(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# ModelProfile CRUD
# ===========================================================================

def create_model_profile(
    name:               str,
    episode_producer:   str            = "anthropic/claude-sonnet-4.6",
    research_director:  str            = "anthropic/claude-opus-4.6",
    thumbnail_planner:  str            = "anthropic/claude-sonnet-4.6",
    brand_guardian_p1:  str            = "anthropic/claude-sonnet-4.6",
    brand_guardian_p2:  str            = "openai/gpt-5.5-pro",
    narration_voice_id: Optional[str]  = None,
    image_provider:     str            = "muapi",
    video_provider:     str            = "muapi",
    model_profile_id:   Optional[str]  = None,
    db_path:            Optional[Path] = None,
) -> ModelProfile:
    profile = ModelProfile(
        model_profile_id  = model_profile_id or str(uuid.uuid4()),
        name              = name,
        episode_producer  = episode_producer,
        research_director = research_director,
        thumbnail_planner = thumbnail_planner,
        brand_guardian_p1 = brand_guardian_p1,
        brand_guardian_p2 = brand_guardian_p2,
        narration_voice_id= narration_voice_id,
        image_provider    = image_provider,
        video_provider    = video_provider,
        created_at        = _now_iso(),
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT INTO model_profiles
                   (model_profile_id, name, episode_producer, research_director,
                    thumbnail_planner, brand_guardian_p1, brand_guardian_p2,
                    narration_voice_id, image_provider, video_provider, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (profile.model_profile_id, name, episode_producer, research_director,
                 thumbnail_planner, brand_guardian_p1, brand_guardian_p2,
                 narration_voice_id, image_provider, video_provider,
                 profile.created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return profile


def get_model_profile(
    model_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ModelProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM model_profiles WHERE model_profile_id=?",
                [model_profile_id],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_model(row) if row else None
    except Exception:
        return None


def update_model_profile(
    model_profile_id: str,
    db_path: Optional[Path] = None,
    **kwargs,
) -> bool:
    _allowed = {
        "name", "episode_producer", "research_director", "thumbnail_planner",
        "brand_guardian_p1", "brand_guardian_p2", "narration_voice_id",
        "image_provider", "video_provider",
    }
    try:
        assignments, params = [], []
        for k, v in kwargs.items():
            if k not in _allowed:
                continue
            assignments.append(f"{k}=?")
            params.append(v)
        if not assignments:
            return True
        params.append(model_profile_id)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                f"UPDATE model_profiles SET {', '.join(assignments)} "
                f"WHERE model_profile_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def list_model_profiles(db_path: Optional[Path] = None) -> list[ModelProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM model_profiles ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_model(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# ChannelProfile CRUD
# ===========================================================================

def create_channel_profile(
    channel_id:            str,
    name:                  str,
    description:           str           = "",
    brand_profile_id:      Optional[str] = None,
    publishing_profile_id: Optional[str] = None,
    model_profile_id:      Optional[str] = None,
    db_path:               Optional[Path] = None,
) -> ChannelProfile:
    profile = ChannelProfile(
        channel_id            = channel_id,
        name                  = name,
        description           = description,
        brand_profile_id      = brand_profile_id,
        publishing_profile_id = publishing_profile_id,
        model_profile_id      = model_profile_id,
        status                = "active",
        created_at            = _now_iso(),
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT INTO channel_profiles
                   (channel_id, name, description, brand_profile_id,
                    publishing_profile_id, model_profile_id, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (channel_id, name, description, brand_profile_id,
                 publishing_profile_id, model_profile_id,
                 "active", profile.created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return profile


def get_channel_profile(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ChannelProfile]:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM channel_profiles WHERE channel_id=?",
                [channel_id],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_channel(row) if row else None
    except Exception:
        return None


def update_channel_profile(
    channel_id: str,
    db_path: Optional[Path] = None,
    **kwargs,
) -> bool:
    _allowed = {
        "name", "description", "brand_profile_id",
        "publishing_profile_id", "model_profile_id", "status",
    }
    try:
        assignments, params = [], []
        for k, v in kwargs.items():
            if k not in _allowed:
                continue
            assignments.append(f"{k}=?")
            params.append(v)
        if not assignments:
            return True
        params.append(channel_id)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                f"UPDATE channel_profiles SET {', '.join(assignments)} "
                f"WHERE channel_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def list_channel_profiles(
    status:  Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[ChannelProfile]:
    try:
        clauses, params = ["1=1"], []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                f"SELECT * FROM channel_profiles WHERE {where} ORDER BY created_at",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_channel(r) for r in rows]
    except Exception:
        return []


def deactivate_channel(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    return update_channel_profile(channel_id, db_path=db_path, status="archived")


# ===========================================================================
# ChannelOperationsProfile CRUD
#
# Unlike the sibling profile types above, this one hard-validates (raises
# ValueError) rather than silently ignoring bad input — operational settings
# that gate autonomous execution must not fail open.
# ===========================================================================

_SUPPORTED_PRIVACY_STATES = ("private", "unlisted", "public")

_OPERATIONS_FIELD_MAP: dict[str, Any] = {
    "channel_name":                 "channel_name",
    "timezone":                     "timezone",
    "operator_enabled":             ("operator_enabled", int),
    "production_cadence":          ("production_cadence_json", _j),
    "publishing_schedule":         ("publishing_schedule_json", _j),
    "enabled_platforms":           ("enabled_platforms_json", _j),
    "blocked_platform_reasons":    ("blocked_platform_reasons_json", _j),
    "discovery_enabled":           ("discovery_enabled", int),
    "production_enabled":          ("production_enabled", int),
    "publishing_enabled":          ("publishing_enabled", int),
    "analytics_sync_enabled":      ("analytics_sync_enabled", int),
    "unattended_operation_enabled": ("unattended_operation_enabled", int),
    "max_concurrent_episodes":     "max_concurrent_episodes",
    "retry_policy":                ("retry_policy_json", _j),
    "failure_halt_policy":         ("failure_halt_policy_json", _j),
    "cost_ceiling_usd_per_episode": "cost_ceiling_usd_per_episode",
    "weekly_video_budget_usd":     "weekly_video_budget_usd",
    "default_privacy_state":       "default_privacy_state",
    "deliverable_schedule_policy": ("deliverable_schedule_policy_json", _j),
    "bundle_cycle_policy":         ("bundle_cycle_policy_json", _j),
    "platform_deliverable_policy": ("platform_deliverable_policy_json", _j),
    "deliverable_bundle_definitions": ("deliverable_bundle_definitions_json", _j),
    "analytics_scheduling_policy": ("analytics_scheduling_policy_json", _j),
}


def _validate_timezone_name(tz: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid timezone: {tz!r}") from exc


def _validate_hhmm(value: str, field_name: str) -> None:
    import re
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value)):
        raise ValueError(f"{field_name} must match HH:MM (24h), got {value!r}")


def _validate_operations_fields(
    timezone_: str,
    enabled_platforms: dict,
    blocked_platform_reasons: dict,
    max_concurrent_episodes: int,
    cost_ceiling_usd_per_episode: Optional[float],
    default_privacy_state: str,
    production_cadence: dict,
    publishing_schedule: dict,
    weekly_video_budget_usd: Optional[float] = None,
) -> None:
    """Hard validation for ChannelOperationsProfile — raises ValueError, never warns."""
    _validate_timezone_name(timezone_)

    bad = [p for p in enabled_platforms if p not in SUPPORTED_PLATFORMS]
    if bad:
        raise ValueError(f"enabled_platforms has unsupported platform(s): {bad}")

    bad = [p for p in blocked_platform_reasons if p not in SUPPORTED_PLATFORMS]
    if bad:
        raise ValueError(f"blocked_platform_reasons has unsupported platform(s): {bad}")

    # A platform enabled=True and a platform appearing in blocked_platform_reasons
    # are mutually exclusive by construction — checking from either direction
    # (blocked-implies-not-enabled, enabled-implies-not-blocked) is the same
    # constraint, so one pass over blocked_platform_reasons covers both.
    for platform, reason in blocked_platform_reasons.items():
        if enabled_platforms.get(platform, False):
            raise ValueError(
                f"platform {platform!r} cannot be both enabled and blocked"
            )
        if not str(reason or "").strip():
            raise ValueError(f"blocked_platform_reasons[{platform!r}] must be non-empty")

    if max_concurrent_episodes < 1:
        raise ValueError(
            f"max_concurrent_episodes must be >= 1, got {max_concurrent_episodes}"
        )

    if cost_ceiling_usd_per_episode is not None and cost_ceiling_usd_per_episode <= 0:
        raise ValueError(
            "cost_ceiling_usd_per_episode must be null (inherit factory/env policy) "
            f"or > 0, got {cost_ceiling_usd_per_episode}"
        )

    # Phase 3: same hard posture as the episode ceiling — a weekly budget
    # that is zero or negative can never mean "unlimited"; it is a config
    # error, rejected at write time. (None = not configured, which the
    # runtime enforcement point fails closed on.)
    if weekly_video_budget_usd is not None and weekly_video_budget_usd <= 0:
        raise ValueError(
            "weekly_video_budget_usd must be null (weekly enforcement fails "
            f"closed on unconfigured channels) or > 0, got {weekly_video_budget_usd}"
        )

    if default_privacy_state not in _SUPPORTED_PRIVACY_STATES:
        raise ValueError(
            f"default_privacy_state must be one of {_SUPPORTED_PRIVACY_STATES}, "
            f"got {default_privacy_state!r}"
        )

    # Cadence: a channel produces long-form videos, Shorts, or both. Either
    # count may be present; when present it must be a non-negative int (bools
    # rejected). videos_per_week == 0 is legal ONLY for a shorts-only channel
    # (shorts_per_week >= 1) -- an explicit zero with nothing else configured
    # is a config error, not "produce nothing".
    videos_per_week = production_cadence.get("videos_per_week")
    shorts_per_week = production_cadence.get("shorts_per_week")
    for name, value in (("videos_per_week", videos_per_week),
                        ("shorts_per_week", shorts_per_week)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"production_cadence.{name} must be a non-negative integer, got {value!r}"
            )
    if videos_per_week == 0 and not (isinstance(shorts_per_week, int) and shorts_per_week >= 1):
        raise ValueError(
            "production_cadence.videos_per_week=0 is only valid for a shorts-only "
            "channel; set shorts_per_week >= 1 (or omit videos_per_week entirely)"
        )

    time_of_day = publishing_schedule.get("time_of_day")
    if time_of_day is not None:
        _validate_hhmm(time_of_day, "publishing_schedule.time_of_day")


def create_channel_operations_profile(
    channel_id:                   str,
    channel_name:                 str,
    timezone:                     str            = "UTC",
    operator_enabled:             bool           = True,
    production_cadence:           Optional[dict] = None,
    publishing_schedule:          Optional[dict] = None,
    enabled_platforms:            Optional[dict] = None,
    blocked_platform_reasons:     Optional[dict] = None,
    discovery_enabled:            bool           = True,
    production_enabled:           bool           = True,
    publishing_enabled:           bool           = True,
    analytics_sync_enabled:       bool           = True,
    unattended_operation_enabled: bool           = False,
    max_concurrent_episodes:      int            = 1,
    retry_policy:                 Optional[dict] = None,
    failure_halt_policy:          Optional[dict] = None,
    cost_ceiling_usd_per_episode: Optional[float] = None,
    weekly_video_budget_usd:      Optional[float] = None,
    default_privacy_state:        str            = "private",
    deliverable_schedule_policy:  Optional[dict] = None,
    bundle_cycle_policy:          Optional[dict] = None,
    platform_deliverable_policy:  Optional[dict] = None,
    deliverable_bundle_definitions: Optional[list] = None,
    analytics_scheduling_policy:  Optional[dict] = None,
    db_path:                      Optional[Path] = None,
) -> ChannelOperationsProfile:
    """
    Create and persist a ChannelOperationsProfile.

    Requires channel_id to already exist in channel_profiles (raises
    ValueError otherwise) — operational settings only make sense layered
    over an existing canonical channel, never freestanding.

    Hard-validates (raises ValueError; never silently warns) timezone,
    enabled/blocked platform consistency, max_concurrent_episodes,
    cost_ceiling_usd_per_episode, default_privacy_state, and
    production_cadence.videos_per_week / publishing_schedule.time_of_day
    when present.

    Fails closed on persistence: any INSERT/read-back failure raises
    RuntimeError (never silently swallowed). Returns the row actually read
    back from the database — not the in-memory object — and only after
    confirming it matches the intended channel_id and field values. A
    caller can trust that a returned profile really was persisted.
    """
    if get_channel_profile(channel_id, db_path=db_path) is None:
        raise ValueError(
            f"channel_id {channel_id!r} does not exist in channel_profiles — "
            f"create the canonical ChannelProfile first"
        )

    enabled_platforms = enabled_platforms or {}
    blocked_platform_reasons = blocked_platform_reasons or {}
    production_cadence = production_cadence or {}
    publishing_schedule = publishing_schedule or {}
    retry_policy = retry_policy or {}
    failure_halt_policy = failure_halt_policy or {}
    deliverable_schedule_policy = deliverable_schedule_policy or {}
    bundle_cycle_policy = bundle_cycle_policy or {}
    platform_deliverable_policy = platform_deliverable_policy or {}
    deliverable_bundle_definitions = deliverable_bundle_definitions or []
    analytics_scheduling_policy = analytics_scheduling_policy or {}

    _validate_operations_fields(
        timezone_=timezone,
        enabled_platforms=enabled_platforms,
        blocked_platform_reasons=blocked_platform_reasons,
        max_concurrent_episodes=max_concurrent_episodes,
        cost_ceiling_usd_per_episode=cost_ceiling_usd_per_episode,
        weekly_video_budget_usd=weekly_video_budget_usd,
        default_privacy_state=default_privacy_state,
        production_cadence=production_cadence,
        publishing_schedule=publishing_schedule,
    )

    now = _now_iso()
    profile = ChannelOperationsProfile(
        channel_id=channel_id,
        channel_name=channel_name,
        timezone=timezone,
        operator_enabled=operator_enabled,
        production_cadence=production_cadence,
        publishing_schedule=publishing_schedule,
        enabled_platforms=enabled_platforms,
        blocked_platform_reasons=blocked_platform_reasons,
        discovery_enabled=discovery_enabled,
        production_enabled=production_enabled,
        publishing_enabled=publishing_enabled,
        analytics_sync_enabled=analytics_sync_enabled,
        unattended_operation_enabled=unattended_operation_enabled,
        max_concurrent_episodes=max_concurrent_episodes,
        retry_policy=retry_policy,
        failure_halt_policy=failure_halt_policy,
        cost_ceiling_usd_per_episode=cost_ceiling_usd_per_episode,
        weekly_video_budget_usd=weekly_video_budget_usd,
        default_privacy_state=default_privacy_state,
        created_at=now,
        updated_at=now,
        deliverable_schedule_policy=deliverable_schedule_policy,
        bundle_cycle_policy=bundle_cycle_policy,
        platform_deliverable_policy=platform_deliverable_policy,
        deliverable_bundle_definitions=deliverable_bundle_definitions,
        analytics_scheduling_policy=analytics_scheduling_policy,
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT INTO channel_operations_profiles
                   (channel_id, channel_name, timezone, operator_enabled,
                    production_cadence_json, publishing_schedule_json,
                    enabled_platforms_json, blocked_platform_reasons_json,
                    discovery_enabled, production_enabled, publishing_enabled,
                    analytics_sync_enabled, unattended_operation_enabled,
                    max_concurrent_episodes, retry_policy_json, failure_halt_policy_json,
                    cost_ceiling_usd_per_episode, weekly_video_budget_usd, default_privacy_state,
                    created_at, updated_at, metadata_version,
                    deliverable_schedule_policy_json, bundle_cycle_policy_json,
                    platform_deliverable_policy_json,
                    deliverable_bundle_definitions_json, analytics_scheduling_policy_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (channel_id, channel_name, timezone, int(operator_enabled),
                 _j(production_cadence), _j(publishing_schedule),
                 _j(enabled_platforms), _j(blocked_platform_reasons),
                 int(discovery_enabled), int(production_enabled), int(publishing_enabled),
                 int(analytics_sync_enabled), int(unattended_operation_enabled),
                 max_concurrent_episodes, _j(retry_policy), _j(failure_halt_policy),
                 cost_ceiling_usd_per_episode, weekly_video_budget_usd, default_privacy_state,
                 now, now, profile.metadata_version,
                 _j(deliverable_schedule_policy), _j(bundle_cycle_policy),
                 _j(platform_deliverable_policy),
                 _j(deliverable_bundle_definitions), _j(analytics_scheduling_policy)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"create_channel_operations_profile: INSERT failed for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    # Read back through a fresh connection — proves the write is actually
    # durable, not merely consistent within the same transaction/session.
    try:
        persisted = get_channel_operations_profile(channel_id, db_path=db_path)
    except Exception as exc:
        raise RuntimeError(
            f"create_channel_operations_profile: read-back failed for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if persisted is None:
        raise RuntimeError(
            f"create_channel_operations_profile: row not found after commit "
            f"for channel_id={channel_id!r} (INSERT reported success but the "
            f"row is not readable)"
        )

    if persisted != profile:
        raise RuntimeError(
            f"create_channel_operations_profile: persisted row does not match "
            f"intended values for channel_id={channel_id!r} "
            f"(expected={profile!r}, got={persisted!r})"
        )

    return persisted


def get_channel_operations_profile(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ChannelOperationsProfile]:
    """
    Returns None only when no row exists for channel_id. Any other failure
    (connection, schema, corrupt row) raises RuntimeError — never silently
    converted to None, since a caller must be able to tell "no profile"
    apart from "couldn't read the profile."
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM channel_operations_profiles WHERE channel_id=?",
                [channel_id],
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"get_channel_operations_profile: read failed for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if row is None:
        return None

    try:
        return _row_to_operations(row)
    except Exception as exc:
        raise RuntimeError(
            f"get_channel_operations_profile: corrupt row for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc


def update_channel_operations_profile(
    channel_id: str,
    db_path: Optional[Path] = None,
    **kwargs,
) -> bool:
    """
    Update individual fields. Unlike the sibling *_profile update functions
    in this module, unknown fields are REJECTED (raises ValueError) rather
    than silently ignored, and the merged result is re-validated the same
    way create_channel_operations_profile() is — this profile requires hard
    validation throughout, not just at creation.

    Fails closed on persistence: database errors raise RuntimeError (never
    silently return False). After commit, the row is read back and every
    requested field is verified against what was actually persisted —
    returns True only once that verification passes.
    """
    unknown = set(kwargs) - set(_OPERATIONS_FIELD_MAP)
    if unknown:
        raise ValueError(f"unknown ChannelOperationsProfile field(s): {sorted(unknown)}")

    if not kwargs:
        return True

    current = get_channel_operations_profile(channel_id, db_path=db_path)
    if current is None:
        raise ValueError(f"no ChannelOperationsProfile exists for channel_id={channel_id!r}")

    _validate_operations_fields(
        timezone_=kwargs.get("timezone", current.timezone),
        enabled_platforms=kwargs.get("enabled_platforms", current.enabled_platforms),
        blocked_platform_reasons=kwargs.get(
            "blocked_platform_reasons", current.blocked_platform_reasons
        ),
        max_concurrent_episodes=kwargs.get(
            "max_concurrent_episodes", current.max_concurrent_episodes
        ),
        cost_ceiling_usd_per_episode=kwargs.get(
            "cost_ceiling_usd_per_episode", current.cost_ceiling_usd_per_episode
        ),
        weekly_video_budget_usd=kwargs.get(
            "weekly_video_budget_usd", current.weekly_video_budget_usd
        ),
        default_privacy_state=kwargs.get(
            "default_privacy_state", current.default_privacy_state
        ),
        production_cadence=kwargs.get("production_cadence", current.production_cadence),
        publishing_schedule=kwargs.get("publishing_schedule", current.publishing_schedule),
    )

    assignments, params = [], []
    for k, v in kwargs.items():
        mapping = _OPERATIONS_FIELD_MAP[k]
        if isinstance(mapping, tuple):
            col, fn = mapping
            assignments.append(f"{col}=?")
            params.append(fn(v))
        else:
            assignments.append(f"{mapping}=?")
            params.append(v)
    assignments.append("updated_at=?")
    params.append(_now_iso())
    params.append(channel_id)

    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                f"UPDATE channel_operations_profiles SET {', '.join(assignments)} "
                f"WHERE channel_id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"update_channel_operations_profile: UPDATE failed for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        after = get_channel_operations_profile(channel_id, db_path=db_path)
    except Exception as exc:
        raise RuntimeError(
            f"update_channel_operations_profile: read-back failed for "
            f"channel_id={channel_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if after is None:
        raise RuntimeError(
            f"update_channel_operations_profile: row disappeared after update "
            f"for channel_id={channel_id!r}"
        )

    for k, v in kwargs.items():
        if getattr(after, k) != v:
            raise RuntimeError(
                f"update_channel_operations_profile: field {k!r} did not persist "
                f"as expected for channel_id={channel_id!r} "
                f"(expected={v!r}, got={getattr(after, k)!r})"
            )

    return True


class ChannelPackagePolicyDriftError(RuntimeError):
    """
    A versioned Channel Package policy artifact (e.g.
    platform_distribution_policy_v1.json) could not be located, read,
    parsed, or its live sha256 no longer matches what
    channel_package_manifest.json has registered for it. Always fails
    closed -- never silently syncs an unverified or stale file into the
    runtime channel_operations_profiles row.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def load_platform_distribution_policy(
    channel_id: str, *, packages_root: Optional[Path] = None,
) -> dict:
    """
    Reads channel_packages/{channel_id}/platform_distribution_policy_v1.json
    fresh from disk (never cached) and verifies its sha256 against the
    'platform_distribution_policy' artifact entry registered in that
    channel's own channel_package_manifest.json -- fixed 2026-08-23, see
    docs/operations/audits/ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md
    item 8: the versioned file is the authoritative source for
    platform_deliverable_policy, never the runtime database, mirroring
    the exact drift-detection discipline hypogeum.workflow_executor
    already applies to workflow_definition artifacts.

    Raises ChannelPackagePolicyDriftError (never returns a partial or
    stale result) if the manifest, the artifact file, or the checksum
    match fails in any way.
    """
    from hypogeum.workflow_executor import (  # noqa: PLC0415 -- avoid a module-load-order cycle
        channel_package_dir,
        load_channel_package_manifest,
    )

    try:
        manifest = load_channel_package_manifest(channel_id, packages_root=packages_root)
    except Exception as exc:
        raise ChannelPackagePolicyDriftError(
            f"channel_package_manifest.json unreadable for channel_id={channel_id!r}: {exc}"
        ) from exc

    entry = next(
        (a for a in manifest.get("artifacts", []) if a.get("role") == "platform_distribution_policy"),
        None,
    )
    if entry is None:
        raise ChannelPackagePolicyDriftError(
            f"no 'platform_distribution_policy' artifact entry registered in "
            f"channel_package_manifest.json for channel_id={channel_id!r}"
        )

    artifact_path = channel_package_dir(channel_id, packages_root=packages_root) / entry["path"]
    try:
        raw = artifact_path.read_bytes()
    except OSError as exc:
        raise ChannelPackagePolicyDriftError(
            f"platform_distribution_policy artifact unreadable at {artifact_path}: {exc}"
        ) from exc

    live_checksum = hashlib.sha256(raw).hexdigest()
    expected_checksum = entry.get("sha256")
    if live_checksum != expected_checksum:
        raise ChannelPackagePolicyDriftError(
            f"live platform_distribution_policy checksum {live_checksum!r} for "
            f"channel_id={channel_id!r} no longer matches the checksum "
            f"{expected_checksum!r} registered in channel_package_manifest.json -- "
            f"the file changed without the manifest being updated to match"
        )

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChannelPackagePolicyDriftError(
            f"platform_distribution_policy artifact at {artifact_path} is not valid JSON: {exc}"
        ) from exc

    if "platform_deliverable_policy" not in doc:
        raise ChannelPackagePolicyDriftError(
            f"platform_distribution_policy artifact at {artifact_path} has no "
            f"'platform_deliverable_policy' key"
        )

    return doc


def sync_platform_distribution_policy_to_runtime(
    channel_id: str, *, packages_root: Optional[Path] = None, db_path: Optional[Path] = None,
) -> ChannelOperationsProfile:
    """
    The sole sanctioned path from the versioned, checksum-verified
    platform_distribution_policy Channel Package artifact into the
    runtime channel_operations_profiles row. A direct
    update_channel_operations_profile(platform_deliverable_policy=...)
    call that bypasses this function -- and therefore bypasses the
    versioned file -- must not be used to change this policy; doing so
    is exactly how the runtime database drifted ahead of
    platform_distribution_policy_v1.json in the first place (TikTok's
    consent fields were added directly to the database on 2026-08-23,
    never reflected back into the versioned file until this fix folded
    them in).

    Fails closed via load_platform_distribution_policy() before ever
    touching the database -- a missing/unreadable/checksum-mismatched
    artifact never partially syncs.

    default_privacy_state (2026-08-24): when the file declares this key,
    it is synced too -- the channel's own scheduled-release visibility
    (e.g. "public" for a normal release), never a Factory-Core-hardcoded
    value. Omitted from the file: left untouched at whatever the runtime
    row already has, exactly like any other field this function does not
    mention.
    """
    policy_doc = load_platform_distribution_policy(channel_id, packages_root=packages_root)
    sync_fields = {"platform_deliverable_policy": policy_doc["platform_deliverable_policy"]}
    if "default_privacy_state" in policy_doc:
        sync_fields["default_privacy_state"] = policy_doc["default_privacy_state"]
    update_channel_operations_profile(channel_id, db_path=db_path, **sync_fields)
    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        raise RuntimeError(
            f"sync_platform_distribution_policy_to_runtime: no ChannelOperationsProfile "
            f"found after sync for channel_id={channel_id!r}"
        )
    return profile


# ===========================================================================
# Channel bootstrap workflow
# ===========================================================================

def validate_channel_config(config_dict: dict) -> list[str]:
    """
    Validate a channel config dict against CHANNEL_CONFIG_SCHEMA.

    Pure Python validation — no JSON Schema library required.
    Returns a list of error strings. Empty = valid.
    """
    errors: list[str] = []

    if not isinstance(config_dict, dict):
        return ["config must be a dict"]

    for req in ("channel_id", "name", "brand", "publishing", "models"):
        if req not in config_dict:
            errors.append(f"missing required field: '{req}'")

    if not config_dict.get("channel_id", "").strip():
        errors.append("channel_id must be a non-empty string")
    if not config_dict.get("name", "").strip():
        errors.append("name must be a non-empty string")

    pub = config_dict.get("publishing", {})
    if isinstance(pub, dict):
        platforms = pub.get("enabled_platforms", [])
        if not isinstance(platforms, list):
            errors.append("publishing.enabled_platforms must be a list")
        else:
            bad = [p for p in platforms if p not in SUPPORTED_PLATFORMS]
            if bad:
                errors.append(f"unsupported platforms: {bad}")
        for _field in ("publishing_provider_by_platform", "analytics_provider_by_platform",
                       "provider_alias_by_platform"):
            _val = pub.get(_field)
            if _val is not None and not isinstance(_val, dict):
                errors.append(f"publishing.{_field} must be an object")
            elif isinstance(_val, dict):
                _bad_keys = [k for k in _val if k not in SUPPORTED_PLATFORMS]
                if _bad_keys:
                    errors.append(f"publishing.{_field} has unsupported platform keys: {_bad_keys}")

    brand = config_dict.get("brand", {})
    if isinstance(brand, dict) and "language" not in brand:
        errors.append("brand.language is required")

    return errors


def bootstrap_channel(
    config_dict: dict,
    db_path: Optional[Path] = None,
) -> ChannelProfile:
    """
    Create a fully configured channel from a config dict.

    Creates BrandProfile, PublishingProfile, ModelProfile, and
    ChannelProfile in one atomic workflow. Idempotent for the channel_id
    — if the channel already exists it is returned as-is.

    Args:
        config_dict: A dict conforming to CHANNEL_CONFIG_SCHEMA.
        db_path:     Override SQLite path.

    Returns:
        ChannelProfile. On any error, a ChannelProfile with empty
        profile IDs is returned and errors are silently absorbed.

    Raises:
        ValueError: If config_dict fails validation.
    """
    errors = validate_channel_config(config_dict)
    if errors:
        raise ValueError(f"Invalid channel config: {'; '.join(errors)}")

    channel_id = config_dict["channel_id"]

    # Idempotent — return existing channel if already bootstrapped
    existing = get_channel_profile(channel_id, db_path=db_path)
    if existing:
        return existing

    brand_cfg = config_dict.get("brand", {})
    pub_cfg   = config_dict.get("publishing", {})
    mdl_cfg   = config_dict.get("models", {})
    name      = config_dict["name"]

    brand = create_brand_profile(
        name              = f"{name} Brand",
        persona_name      = brand_cfg.get("persona_name"),
        tone              = brand_cfg.get("tone"),
        target_audience   = brand_cfg.get("target_audience"),
        visual_style      = brand_cfg.get("visual_style"),
        language          = brand_cfg.get("language", "en"),
        hashtags          = brand_cfg.get("hashtags", []),
        color_palette     = brand_cfg.get("color_palette", {}),
        typography        = brand_cfg.get("typography", {}),
        voice_guidelines  = brand_cfg.get("voice_guidelines"),
        content_pillars   = brand_cfg.get("content_pillars", []),
        db_path           = db_path,
    )

    publishing = create_publishing_profile(
        name                   = f"{name} Publishing",
        enabled_platforms      = pub_cfg.get("enabled_platforms", []),
        schedule               = pub_cfg.get("schedule", {}),
        default_tags           = pub_cfg.get("default_tags", []),
        ai_disclosure_required = bool(pub_cfg.get("ai_disclosure_required", True)),
        auto_thumbnail         = bool(pub_cfg.get("auto_thumbnail", True)),
        auto_captions          = bool(pub_cfg.get("auto_captions", True)),
        publishing_provider_by_platform = pub_cfg.get("publishing_provider_by_platform", {}),
        analytics_provider_by_platform  = pub_cfg.get("analytics_provider_by_platform", {}),
        provider_alias_by_platform      = pub_cfg.get("provider_alias_by_platform", {}),
        db_path                = db_path,
    )

    models = create_model_profile(
        name               = f"{name} Models",
        episode_producer   = mdl_cfg.get("episode_producer",   "anthropic/claude-sonnet-4.6"),
        research_director  = mdl_cfg.get("research_director",  "anthropic/claude-opus-4.6"),
        thumbnail_planner  = mdl_cfg.get("thumbnail_planner",  "anthropic/claude-sonnet-4.6"),
        brand_guardian_p1  = mdl_cfg.get("brand_guardian_p1",  "anthropic/claude-sonnet-4.6"),
        brand_guardian_p2  = mdl_cfg.get("brand_guardian_p2",  "openai/gpt-5.5-pro"),
        narration_voice_id = mdl_cfg.get("narration_voice_id"),
        image_provider     = mdl_cfg.get("image_provider",     "muapi"),
        video_provider     = mdl_cfg.get("video_provider",     "muapi"),
        db_path            = db_path,
    )

    return create_channel_profile(
        channel_id            = channel_id,
        name                  = name,
        description           = config_dict.get("description", ""),
        brand_profile_id      = brand.brand_profile_id,
        publishing_profile_id = publishing.publishing_profile_id,
        model_profile_id      = models.model_profile_id,
        db_path               = db_path,
    )


def load_channel_config(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ChannelConfig]:
    """
    Load a fully resolved ChannelConfig for the given channel.

    Joins channel_profiles with all three sub-profiles. Returns None
    if the channel does not exist. Non-blocking.
    """
    try:
        channel = get_channel_profile(channel_id, db_path=db_path)
        if channel is None:
            return None

        brand = (get_brand_profile(channel.brand_profile_id, db_path=db_path)
                 if channel.brand_profile_id else None)
        publishing = (get_publishing_profile(channel.publishing_profile_id, db_path=db_path)
                      if channel.publishing_profile_id else None)
        models = (get_model_profile(channel.model_profile_id, db_path=db_path)
                  if channel.model_profile_id else None)

        return ChannelConfig(
            channel    = channel,
            brand      = brand,
            publishing = publishing,
            models     = models,
        )
    except Exception:
        return None


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


def compute_channel_package_identity(channel_id: str, db_path: Optional[Path] = None) -> dict:
    """
    The canonical, generic Channel Package identity for channel_id --
    a real content-addressed checksum of the DB-backed brand/publishing/
    model/channel profiles (via load_channel_config()) folded together
    with the versioned Channel Package directory on disk (channel_packages/
    {channel_id}/, if present -- its manifest plus every artifact file it
    lists, e.g. a registered workflow definition). Neither half is
    optional once a package directory exists: a change to either the DB
    profiles or the on-disk artifacts changes this checksum.

    Returns {"available": bool, "checksum": Optional[str],
    "version": Optional[str], "reason": Optional[str]}.

    "available" requires the channel_profiles row to exist; the package
    directory folding in is best-effort (a channel with no
    channel_packages/{channel_id}/ directory yet -- e.g. a
    Channel-Package-file migration still pending -- still resolves
    "available": True from the DB profiles alone, since that half is
    real, already-bootstrapped evidence on its own).

    version is the profile rows' own stored metadata_version field
    (their storage-schema version, the only "version"-labeled field
    that actually exists here; content freshness is what the checksum
    verifies, not this field -- disclosed explicitly, never presented as
    more than it is).
    """
    try:
        channel_config = load_channel_config(channel_id, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 -- any lookup failure is UNVERIFIABLE, never a crash
        return {"available": False, "checksum": None, "version": None,
                "reason": f"channel config lookup failed: {type(exc).__name__}: {exc}"}

    if channel_config is None or channel_config.channel is None:
        return {"available": False, "checksum": None, "version": None,
                "reason": f"no channel_profiles row found for channel_id={channel_id!r}"}

    payload = {
        "channel": asdict(channel_config.channel),
        "brand": asdict(channel_config.brand) if channel_config.brand else None,
        "publishing": asdict(channel_config.publishing) if channel_config.publishing else None,
        "models": asdict(channel_config.models) if channel_config.models else None,
    }

    package_dir_checksum = None
    try:
        from hypogeum.workflow_executor import compute_channel_package_directory_checksum  # noqa: PLC0415 -- avoid a module-load-order cycle
        package_dir_checksum = compute_channel_package_directory_checksum(channel_id)
    except Exception:  # noqa: BLE001 -- no package directory yet is not an error, just nothing to fold in
        pass
    payload["channel_package_directory_checksum"] = package_dir_checksum

    checksum = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    version = channel_config.channel.metadata_version
    return {"available": True, "checksum": checksum, "version": version, "reason": None}
