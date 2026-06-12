"""
hypogeum/factory_clone.py — Phase B: Factory Clone System

Turns the factory into a reusable product. A new channel can be created,
cloned, or templated without editing any repository files. Everything is
generated from stored configuration.

Sub-components:
    Brand Package System    — exportable / importable brand bundles
    Identity Package System — exportable / importable identity bundles
    Channel Template System — reusable base configs with variable substitution
    Channel Duplicator      — deep-clone an existing channel profile
    create_channel()        — unified one-call channel creation
    clone_factory()         — clone the factory's base channel template

Architecture:
    All tables are created lazily. All functions are non-blocking. No
    network calls. No external APIs. No episode state written.

Public API:
    # Brand packages
    export_brand_package(channel_id, db_path=None) -> dict
    import_brand_package(package, channel_id=None, db_path=None) -> BrandProfile

    # Identity packages
    export_identity_package(channel_id, db_path=None) -> dict
    import_identity_package(package, channel_id=None, db_path=None) -> ChannelProfile

    # Channel templates
    save_channel_template(name, config_dict, description="", db_path=None) -> ChannelTemplate
    get_channel_template(template_name, db_path=None) -> Optional[ChannelTemplate]
    list_channel_templates(db_path=None) -> list[ChannelTemplate]
    render_channel_template(template_name, variables, db_path=None) -> dict

    # Channel duplicator
    duplicate_channel(source_channel_id, new_channel_id, new_name,
                      overrides=None, db_path=None) -> ChannelProfile

    # Main entry points
    create_channel(config_dict, db_path=None) -> ChannelProfile
    clone_factory(source_channel_id, new_channel_id, new_name,
                  brand_overrides=None, model_overrides=None,
                  publishing_overrides=None, db_path=None) -> ChannelProfile

Packages:
    Brand package:    {"package_type": "brand", "version": "v1", "brand": {...},
                       "channel_id": "...", "exported_at": "..."}
    Identity package: {"package_type": "identity", "version": "v1",
                       "channel": {...}, "brand": {...}, "publishing": {...},
                       "models": {...}, "exported_at": "..."}

Templates support {variable} substitution in string fields:
    template: "target_audience": "software engineers in {region}"
    variables: {"region": "Europe"}
    result:    "target_audience": "software engineers in Europe"
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hypogeum.db import get_connection
from hypogeum.channel_config import (
    BrandProfile,
    ChannelConfig,
    ChannelProfile,
    ModelProfile,
    PublishingProfile,
    bootstrap_channel,
    create_brand_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
    get_brand_profile,
    get_channel_profile,
    get_model_profile,
    get_publishing_profile,
    load_channel_config,
    update_brand_profile,
    update_channel_profile,
    update_model_profile,
    update_publishing_profile,
    validate_channel_config,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"
PACKAGE_VERSION  = "v1"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelTemplate:
    """
    A reusable base channel configuration stored in the factory DB.

    Templates support {variable} placeholders in any string field.
    Use render_channel_template() to materialise with concrete values.
    """
    template_id:  str
    name:         str
    description:  str  = ""
    config_json:  str  = "{}"
    created_at:   str  = ""
    metadata_version: str = METADATA_VERSION

    def config(self) -> dict:
        """Deserialise stored config_json."""
        try:
            return json.loads(self.config_json)
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channel_templates (
            template_id         TEXT    NOT NULL PRIMARY KEY,
            name                TEXT    NOT NULL UNIQUE,
            description         TEXT    NOT NULL DEFAULT '',
            config_json         TEXT    NOT NULL DEFAULT '{}',
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_channel_templates_name
            ON channel_templates(name);
    """)
    conn.commit()


def _row_to_template(row) -> ChannelTemplate:
    return ChannelTemplate(
        template_id      = row["template_id"],
        name             = row["name"],
        description      = row["description"] or "",
        config_json      = row["config_json"] or "{}",
        created_at       = row["created_at"],
        metadata_version = row["metadata_version"],
    )


def _deep_substitute(obj: Any, variables: dict) -> Any:
    """
    Recursively substitute {variable} placeholders in any string value.

    Leaves non-string values untouched. Missing variable keys are left
    as-is (not replaced, not errored).
    """
    if isinstance(obj, str):
        def replacer(m):
            key = m.group(1)
            return str(variables.get(key, m.group(0)))
        return re.sub(r"\{(\w+)\}", replacer, obj)
    if isinstance(obj, dict):
        return {k: _deep_substitute(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_substitute(i, variables) for i in obj]
    return obj


def _brand_to_dict(bp: BrandProfile) -> dict:
    return {
        "persona_name":    bp.persona_name,
        "tone":            bp.tone,
        "target_audience": bp.target_audience,
        "visual_style":    bp.visual_style,
        "language":        bp.language,
        "hashtags":        bp.hashtags,
        "color_palette":   bp.color_palette,
        "typography":      bp.typography,
        "voice_guidelines":bp.voice_guidelines,
        "content_pillars": bp.content_pillars,
    }


def _publishing_to_dict(pp: PublishingProfile) -> dict:
    return {
        "enabled_platforms":     pp.enabled_platforms,
        "schedule":              pp.schedule,
        "default_tags":          pp.default_tags,
        "ai_disclosure_required":pp.ai_disclosure_required,
        "auto_thumbnail":        pp.auto_thumbnail,
        "auto_captions":         pp.auto_captions,
    }


def _model_to_dict(mp: ModelProfile) -> dict:
    return {
        "episode_producer":   mp.episode_producer,
        "research_director":  mp.research_director,
        "thumbnail_planner":  mp.thumbnail_planner,
        "brand_guardian_p1":  mp.brand_guardian_p1,
        "brand_guardian_p2":  mp.brand_guardian_p2,
        "narration_voice_id": mp.narration_voice_id,
        "image_provider":     mp.image_provider,
        "video_provider":     mp.video_provider,
    }


# ===========================================================================
# Brand Package System
# ===========================================================================

def export_brand_package(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Export a channel's brand profile as a portable package dict.

    The package can be stored, versioned, shared, or imported into a
    different channel or factory instance.

    Returns:
        Package dict. Empty dict on any error.
    """
    try:
        cfg = load_channel_config(channel_id, db_path=db_path)
        if cfg is None or cfg.brand is None:
            return {}
        return {
            "package_type": "brand",
            "version":      PACKAGE_VERSION,
            "channel_id":   channel_id,
            "exported_at":  _now_iso(),
            "brand":        _brand_to_dict(cfg.brand),
        }
    except Exception:
        return {}


def import_brand_package(
    package:    dict,
    channel_id: Optional[str] = None,
    db_path:    Optional[Path] = None,
) -> Optional[BrandProfile]:
    """
    Import a brand package and create a new BrandProfile.

    If channel_id is provided, the new BrandProfile is also attached
    to the existing channel (overwrites brand_profile_id).

    Non-blocking: returns None on any error.
    """
    try:
        if package.get("package_type") != "brand":
            return None
        brand_data = package.get("brand", {})
        if not isinstance(brand_data, dict):
            return None

        bp = create_brand_profile(
            name             = f"Imported brand ({_now_iso()[:10]})",
            persona_name     = brand_data.get("persona_name"),
            tone             = brand_data.get("tone"),
            target_audience  = brand_data.get("target_audience"),
            visual_style     = brand_data.get("visual_style"),
            language         = brand_data.get("language", "en"),
            hashtags         = brand_data.get("hashtags", []),
            color_palette    = brand_data.get("color_palette", {}),
            typography       = brand_data.get("typography", {}),
            voice_guidelines = brand_data.get("voice_guidelines"),
            content_pillars  = brand_data.get("content_pillars", []),
            db_path          = db_path,
        )

        if channel_id:
            update_channel_profile(channel_id, db_path=db_path,
                                   brand_profile_id=bp.brand_profile_id)
        return bp
    except Exception:
        return None


# ===========================================================================
# Identity Package System
# ===========================================================================

def export_identity_package(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Export a full channel identity as a portable package dict.

    Includes channel metadata, brand, publishing, and model profiles.
    The package is self-contained enough to recreate the channel on
    a fresh factory instance.

    Returns:
        Package dict. Empty dict on any error.
    """
    try:
        cfg = load_channel_config(channel_id, db_path=db_path)
        if cfg is None:
            return {}

        package: dict = {
            "package_type": "identity",
            "version":      PACKAGE_VERSION,
            "channel_id":   channel_id,
            "exported_at":  _now_iso(),
            "channel": {
                "name":        cfg.channel.name,
                "description": cfg.channel.description,
                "status":      cfg.channel.status,
            },
            "brand":      _brand_to_dict(cfg.brand)      if cfg.brand      else None,
            "publishing": _publishing_to_dict(cfg.publishing) if cfg.publishing else None,
            "models":     _model_to_dict(cfg.models)     if cfg.models     else None,
        }
        return package
    except Exception:
        return {}


def import_identity_package(
    package:    dict,
    channel_id: Optional[str] = None,
    db_path:    Optional[Path] = None,
) -> Optional[ChannelProfile]:
    """
    Import a full identity package and bootstrap a new channel.

    The channel_id to use for the import can be overridden — this allows
    the same identity package to be used for multiple channel instances.

    Non-blocking: returns None on any error.
    """
    try:
        if package.get("package_type") != "identity":
            return None

        target_id   = channel_id or package.get("channel_id") or str(uuid.uuid4())
        ch_data     = package.get("channel", {})
        brand_data  = package.get("brand") or {}
        pub_data    = package.get("publishing") or {}
        model_data  = package.get("models") or {}

        config_dict = {
            "channel_id":  target_id,
            "name":        ch_data.get("name", target_id),
            "description": ch_data.get("description", ""),
            "brand": {
                "language": brand_data.get("language", "en"),
                **{k: v for k, v in brand_data.items() if k != "language"},
            },
            "publishing": {
                "enabled_platforms": pub_data.get("enabled_platforms", []),
                **{k: v for k, v in pub_data.items()
                   if k != "enabled_platforms"},
            },
            "models": model_data,
        }

        errors = validate_channel_config(config_dict)
        if errors:
            return None

        return bootstrap_channel(config_dict, db_path=db_path)
    except Exception:
        return None


# ===========================================================================
# Channel Template System
# ===========================================================================

def save_channel_template(
    name:        str,
    config_dict: dict,
    description: str           = "",
    db_path:     Optional[Path] = None,
) -> ChannelTemplate:
    """
    Save a reusable channel template to the factory DB.

    The config_dict may contain {variable} placeholders in any string
    field. Use render_channel_template() to instantiate with values.

    Non-blocking: returns a ChannelTemplate with empty created_at on fail.

    Args:
        name:        Unique template name (e.g. "tech_explainer_en").
        config_dict: Base channel config dict (may contain placeholders).
        description: Human description of when to use this template.
        db_path:     Override SQLite path.

    Returns:
        ChannelTemplate.
    """
    template = ChannelTemplate(
        template_id  = str(uuid.uuid4()),
        name         = name,
        description  = description,
        config_json  = json.dumps(config_dict, ensure_ascii=False),
        created_at   = _now_iso(),
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """INSERT OR REPLACE INTO channel_templates
                   (template_id, name, description, config_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (template.template_id, name, description,
                 template.config_json, template.created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return template


def get_channel_template(
    template_name: str,
    db_path:       Optional[Path] = None,
) -> Optional[ChannelTemplate]:
    """Return a stored template by name. None if not found."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM channel_templates WHERE name=?",
                [template_name],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_template(row) if row else None
    except Exception:
        return None


def list_channel_templates(
    db_path: Optional[Path] = None,
) -> list[ChannelTemplate]:
    """Return all stored templates ordered by name."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM channel_templates ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_template(r) for r in rows]
    except Exception:
        return []


def render_channel_template(
    template_name: str,
    variables:     dict,
    db_path:       Optional[Path] = None,
) -> dict:
    """
    Materialise a channel template by substituting {variable} placeholders.

    Returns the rendered config dict ready for bootstrap_channel().
    Returns {} if the template is not found.

    Args:
        template_name: Name of the stored template.
        variables:     Dict of {key: value} substitutions.
        db_path:       Override SQLite path.
    """
    tmpl = get_channel_template(template_name, db_path=db_path)
    if tmpl is None:
        return {}
    base = tmpl.config()
    return _deep_substitute(base, variables)


# ===========================================================================
# Channel Duplicator
# ===========================================================================

def duplicate_channel(
    source_channel_id: str,
    new_channel_id:    str,
    new_name:          str,
    overrides:         Optional[dict] = None,
    db_path:           Optional[Path] = None,
) -> Optional[ChannelProfile]:
    """
    Deep-clone an existing channel profile into a new channel.

    Copies brand, publishing, and model data — new profile records are
    created so the clone is fully independent of the source. Episode
    history is NOT copied.

    The overrides dict can selectively replace brand, publishing, or
    model fields after cloning:
        overrides = {
            "brand":      {"tone": "casual"},
            "publishing": {"enabled_platforms": ["tiktok"]},
            "models":     {"narration_voice_id": "new_voice"},
        }

    Non-blocking: returns None if source channel not found or on error.

    Args:
        source_channel_id: Channel to clone from.
        new_channel_id:    New unique channel identifier.
        new_name:          Human name for the cloned channel.
        overrides:         Dict of section → field overrides (optional).
        db_path:           Override SQLite path.

    Returns:
        New ChannelProfile or None on failure.
    """
    try:
        src = load_channel_config(source_channel_id, db_path=db_path)
        if src is None:
            return None

        # Check new channel doesn't already exist
        existing = get_channel_profile(new_channel_id, db_path=db_path)
        if existing:
            return existing

        overrides = overrides or {}
        brand_ov  = overrides.get("brand", {})
        pub_ov    = overrides.get("publishing", {})
        mdl_ov    = overrides.get("models", {})

        # --- Brand ---
        if src.brand:
            brand_data = {**_brand_to_dict(src.brand), **brand_ov}
        else:
            brand_data = {"language": "en", **brand_ov}

        brand = create_brand_profile(
            name             = f"{new_name} Brand",
            persona_name     = brand_data.get("persona_name"),
            tone             = brand_data.get("tone"),
            target_audience  = brand_data.get("target_audience"),
            visual_style     = brand_data.get("visual_style"),
            language         = brand_data.get("language", "en"),
            hashtags         = brand_data.get("hashtags", []),
            color_palette    = brand_data.get("color_palette", {}),
            typography       = brand_data.get("typography", {}),
            voice_guidelines = brand_data.get("voice_guidelines"),
            content_pillars  = brand_data.get("content_pillars", []),
            db_path          = db_path,
        )

        # --- Publishing ---
        if src.publishing:
            pub_data = {**_publishing_to_dict(src.publishing), **pub_ov}
        else:
            pub_data = {"enabled_platforms": [], **pub_ov}

        publishing = create_publishing_profile(
            name                   = f"{new_name} Publishing",
            enabled_platforms      = pub_data.get("enabled_platforms", []),
            schedule               = pub_data.get("schedule", {}),
            default_tags           = pub_data.get("default_tags", []),
            ai_disclosure_required = bool(pub_data.get("ai_disclosure_required", True)),
            auto_thumbnail         = bool(pub_data.get("auto_thumbnail", True)),
            auto_captions          = bool(pub_data.get("auto_captions", True)),
            db_path                = db_path,
        )

        # --- Models ---
        if src.models:
            mdl_data = {**_model_to_dict(src.models), **mdl_ov}
        else:
            mdl_data = mdl_ov

        models = create_model_profile(
            name               = f"{new_name} Models",
            episode_producer   = mdl_data.get("episode_producer",   "anthropic/claude-sonnet-4.6"),
            research_director  = mdl_data.get("research_director",  "anthropic/claude-opus-4.6"),
            thumbnail_planner  = mdl_data.get("thumbnail_planner",  "anthropic/claude-sonnet-4.6"),
            brand_guardian_p1  = mdl_data.get("brand_guardian_p1",  "anthropic/claude-sonnet-4.6"),
            brand_guardian_p2  = mdl_data.get("brand_guardian_p2",  "openai/gpt-5.5-pro"),
            narration_voice_id = mdl_data.get("narration_voice_id"),
            image_provider     = mdl_data.get("image_provider",     "muapi"),
            video_provider     = mdl_data.get("video_provider",     "muapi"),
            db_path            = db_path,
        )

        return create_channel_profile(
            channel_id            = new_channel_id,
            name                  = new_name,
            description           = src.channel.description,
            brand_profile_id      = brand.brand_profile_id,
            publishing_profile_id = publishing.publishing_profile_id,
            model_profile_id      = models.model_profile_id,
            db_path               = db_path,
        )
    except Exception:
        return None


# ===========================================================================
# Main entry points
# ===========================================================================

def create_channel(
    config_dict: dict,
    db_path:     Optional[Path] = None,
) -> ChannelProfile:
    """
    Unified one-call channel creation. Alias for bootstrap_channel().

    A new channel is fully operational after this call — no code changes
    needed. The pipeline resolves all configuration from the stored profile.

    Args:
        config_dict: A dict conforming to CHANNEL_CONFIG_SCHEMA.
        db_path:     Override SQLite path.

    Returns:
        ChannelProfile. Raises ValueError on invalid config.
    """
    return bootstrap_channel(config_dict, db_path=db_path)


def clone_factory(
    source_channel_id:    str,
    new_channel_id:       str,
    new_name:             str,
    brand_overrides:      Optional[dict] = None,
    model_overrides:      Optional[dict] = None,
    publishing_overrides: Optional[dict] = None,
    db_path:              Optional[Path] = None,
) -> Optional[ChannelProfile]:
    """
    Clone a factory channel to produce a new, independent channel instance.

    This is the one-click factory cloning entry point. It combines the
    channel duplicator with a clear three-way override API:

        clone_factory(
            source_channel_id="hypogeum_template",
            new_channel_id="tech_explained",
            new_name="Tech Explained",
            brand_overrides={"persona_name": "Alex Chen", "tone": "casual"},
            model_overrides={"narration_voice_id": "EXAVITQu4vr4xnSDxMaL"},
            publishing_overrides={"enabled_platforms": ["youtube", "tiktok"]},
        )

    Non-blocking: returns None on failure.

    Args:
        source_channel_id:    The template/base channel to clone from.
        new_channel_id:       Unique ID for the new channel.
        new_name:             Display name for the new channel.
        brand_overrides:      Brand fields to override post-clone.
        model_overrides:      Model fields to override post-clone.
        publishing_overrides: Publishing fields to override post-clone.
        db_path:              Override SQLite path.

    Returns:
        New ChannelProfile or None on failure.
    """
    overrides: dict = {}
    if brand_overrides:
        overrides["brand"] = brand_overrides
    if model_overrides:
        overrides["models"] = model_overrides
    if publishing_overrides:
        overrides["publishing"] = publishing_overrides

    return duplicate_channel(
        source_channel_id = source_channel_id,
        new_channel_id    = new_channel_id,
        new_name          = new_name,
        overrides         = overrides,
        db_path           = db_path,
    )
