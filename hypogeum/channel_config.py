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

import json
import uuid
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


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
            default_privacy_state          TEXT    NOT NULL DEFAULT 'private',
            created_at                     TEXT    NOT NULL,
            updated_at                     TEXT    NOT NULL,
            metadata_version                TEXT   NOT NULL DEFAULT 'v1'
        );

        CREATE INDEX IF NOT EXISTS idx_channel_operations_profiles_enabled
            ON channel_operations_profiles(operator_enabled);
    """)
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
    return PublishingProfile(
        publishing_profile_id  = row["publishing_profile_id"],
        name                   = row["name"],
        enabled_platforms      = _uj(row["enabled_platforms_json"], []),
        schedule               = _uj(row["schedule_json"], {}),
        default_tags           = _uj(row["default_tags_json"], []),
        ai_disclosure_required = bool(row["ai_disclosure_required"]),
        auto_thumbnail         = bool(row["auto_thumbnail"]),
        auto_captions          = bool(row["auto_captions"]),
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
        default_privacy_state        = row["default_privacy_state"],
        created_at                   = row["created_at"],
        updated_at                   = row["updated_at"],
        metadata_version             = row["metadata_version"],
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
                    auto_thumbnail, auto_captions, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (profile.publishing_profile_id, name,
                 _j(profile.enabled_platforms),
                 _j(profile.schedule),
                 _j(profile.default_tags),
                 int(ai_disclosure_required),
                 int(auto_thumbnail),
                 int(auto_captions),
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
    "default_privacy_state":       "default_privacy_state",
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

    if default_privacy_state not in _SUPPORTED_PRIVACY_STATES:
        raise ValueError(
            f"default_privacy_state must be one of {_SUPPORTED_PRIVACY_STATES}, "
            f"got {default_privacy_state!r}"
        )

    videos_per_week = production_cadence.get("videos_per_week")
    if videos_per_week is not None and videos_per_week < 1:
        raise ValueError(
            f"production_cadence.videos_per_week must be >= 1, got {videos_per_week}"
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
    default_privacy_state:        str            = "private",
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

    _validate_operations_fields(
        timezone_=timezone,
        enabled_platforms=enabled_platforms,
        blocked_platform_reasons=blocked_platform_reasons,
        max_concurrent_episodes=max_concurrent_episodes,
        cost_ceiling_usd_per_episode=cost_ceiling_usd_per_episode,
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
        default_privacy_state=default_privacy_state,
        created_at=now,
        updated_at=now,
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
                    cost_ceiling_usd_per_episode, default_privacy_state,
                    created_at, updated_at, metadata_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (channel_id, channel_name, timezone, int(operator_enabled),
                 _j(production_cadence), _j(publishing_schedule),
                 _j(enabled_platforms), _j(blocked_platform_reasons),
                 int(discovery_enabled), int(production_enabled), int(publishing_enabled),
                 int(analytics_sync_enabled), int(unattended_operation_enabled),
                 max_concurrent_episodes, _j(retry_policy), _j(failure_halt_policy),
                 cost_ceiling_usd_per_episode, default_privacy_state,
                 now, now, profile.metadata_version),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return profile


def get_channel_operations_profile(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[ChannelOperationsProfile]:
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
        return _row_to_operations(row) if row else None
    except Exception:
        return None


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
        return True
    except Exception:
        return False


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
