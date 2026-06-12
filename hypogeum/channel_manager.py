"""
hypogeum/channel_manager.py — Multi-Channel Architecture

Provides channel isolation, identity management, template reuse,
duplication, and prompt regeneration for multi-platform content factories.

Sub-components:
    Channel Schema        — dataclasses + table definitions for channels
    Channel Isolation     — scoped reads/writes per channel
    Channel Template System — reusable per-channel prompt/config templates
    Channel Duplicator    — fork an existing channel with a new identity
    Identity Editor       — update channel identity fields non-destructively
    Prompt Regeneration   — regenerate prompts with channel identity injected

All tables are created lazily (not in schema.sql).

Public API:
    create_channel(
        channel_id, name, platform, identity, db_path=None,
    ) -> Channel

    get_channel(channel_id, db_path=None) -> Optional[Channel]
    list_channels(platform=None, db_path=None) -> list[Channel]
    update_identity(channel_id, identity_patch, db_path=None) -> bool

    create_template(
        template_id, channel_id, template_type, content, db_path=None,
    ) -> ChannelTemplate

    get_templates(channel_id, template_type=None, db_path=None)
        -> list[ChannelTemplate]

    duplicate_channel(
        source_channel_id, new_channel_id, new_name,
        identity_overrides=None, db_path=None,
    ) -> Optional[Channel]

    regenerate_prompt(
        base_prompt, channel_id, db_path=None,
    ) -> str

    get_channel_episodes(channel_id, db_path=None) -> list[str]
    assign_episode_to_channel(
        episode_id, channel_id, db_path=None,
    ) -> bool
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelIdentity:
    """
    Defines the brand/persona identity for a channel.

    Fields are used for prompt injection and metadata tagging.
    All fields are optional — absent fields are simply not injected.
    """
    persona_name:      Optional[str]  = None   # "Dr. Aria Cole"
    tone:              Optional[str]  = None   # "academic", "conversational"
    target_audience:   Optional[str]  = None   # "history enthusiasts 25-45"
    visual_style:      Optional[str]  = None   # "cinematic dark", "bright minimal"
    language:          str            = "en"
    hashtags:          list[str]      = field(default_factory=list)
    extra:             dict           = field(default_factory=dict)


@dataclass
class Channel:
    """One distribution channel (e.g. YouTube long-form, TikTok Shorts)."""
    channel_id:   str
    name:         str
    platform:     str                   # "youtube" | "tiktok" | "instagram" | etc.
    identity:     ChannelIdentity       = field(default_factory=ChannelIdentity)
    active:       bool                  = True
    created_at:   str                   = ""
    metadata_version: str              = "v1"


@dataclass
class ChannelTemplate:
    """Reusable prompt/config template scoped to one channel."""
    template_id:   str
    channel_id:    str
    template_type: str           # "system_prompt" | "title_prompt" | "hook" | etc.
    content:       str
    created_at:    str = ""
    metadata_version: str = "v1"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(conn) -> None:
    """Create channel tables lazily if not present."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id          TEXT    NOT NULL PRIMARY KEY,
            name                TEXT    NOT NULL,
            platform            TEXT    NOT NULL,
            identity_json       TEXT    NOT NULL DEFAULT '{}',
            active              INTEGER NOT NULL DEFAULT 1,
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS channel_templates (
            template_id         TEXT    NOT NULL PRIMARY KEY,
            channel_id          TEXT    NOT NULL,
            template_type       TEXT    NOT NULL,
            content             TEXT    NOT NULL,
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS channel_episodes (
            assignment_id       TEXT    NOT NULL PRIMARY KEY,
            channel_id          TEXT    NOT NULL,
            episode_id          TEXT    NOT NULL,
            assigned_at         TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_channels_platform
            ON channels(platform);
        CREATE INDEX IF NOT EXISTS idx_channel_templates_channel
            ON channel_templates(channel_id);
        CREATE INDEX IF NOT EXISTS idx_channel_episodes_channel
            ON channel_episodes(channel_id);
        CREATE INDEX IF NOT EXISTS idx_channel_episodes_episode
            ON channel_episodes(episode_id);
        """
    )
    conn.commit()


def _identity_to_json(identity: ChannelIdentity) -> str:
    return json.dumps({
        "persona_name":    identity.persona_name,
        "tone":            identity.tone,
        "target_audience": identity.target_audience,
        "visual_style":    identity.visual_style,
        "language":        identity.language,
        "hashtags":        identity.hashtags,
        "extra":           identity.extra,
    })


def _identity_from_json(raw: str) -> ChannelIdentity:
    try:
        d = json.loads(raw or "{}")
    except Exception:
        d = {}
    return ChannelIdentity(
        persona_name    = d.get("persona_name"),
        tone            = d.get("tone"),
        target_audience = d.get("target_audience"),
        visual_style    = d.get("visual_style"),
        language        = d.get("language", "en"),
        hashtags        = d.get("hashtags") or [],
        extra           = d.get("extra") or {},
    )


def _row_to_channel(row) -> Channel:
    return Channel(
        channel_id       = row["channel_id"],
        name             = row["name"],
        platform         = row["platform"],
        identity         = _identity_from_json(row["identity_json"]),
        active           = bool(row["active"]),
        created_at       = row["created_at"],
        metadata_version = row["metadata_version"],
    )


def _row_to_template(row) -> ChannelTemplate:
    return ChannelTemplate(
        template_id      = row["template_id"],
        channel_id       = row["channel_id"],
        template_type    = row["template_type"],
        content          = row["content"],
        created_at       = row["created_at"],
        metadata_version = row["metadata_version"],
    )


# ---------------------------------------------------------------------------
# Public API — Channel CRUD
# ---------------------------------------------------------------------------

def create_channel(
    channel_id: str,
    name:       str,
    platform:   str,
    identity:   Optional[ChannelIdentity] = None,
    db_path:    Optional[Path]            = None,
) -> Channel:
    """
    Create and persist a new channel.

    Non-blocking: on any exception returns a Channel with empty created_at.

    Args:
        channel_id: Caller-supplied unique identifier (e.g. "yt_main").
        name:       Human-readable channel name.
        platform:   Distribution platform ("youtube", "tiktok", etc.).
        identity:   Channel brand identity. Defaults to empty ChannelIdentity.
        db_path:    Override SQLite path.

    Returns:
        Channel dataclass.
    """
    identity  = identity or ChannelIdentity()
    now       = _now_iso()
    channel   = Channel(
        channel_id = channel_id,
        name       = name,
        platform   = platform,
        identity   = identity,
        active     = True,
        created_at = now,
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """
                INSERT INTO channels
                    (channel_id, name, platform, identity_json, active, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (channel_id, name, platform,
                 _identity_to_json(identity), 1, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return channel


def get_channel(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> Optional[Channel]:
    """
    Retrieve a channel by ID.

    Returns None if not found or on any exception.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?",
                [channel_id],
            ).fetchone()
        finally:
            conn.close()
        return _row_to_channel(row) if row else None
    except Exception:
        return None


def list_channels(
    platform: Optional[str] = None,
    db_path:  Optional[Path] = None,
) -> list[Channel]:
    """
    Return all channels, optionally filtered by platform.

    Non-blocking: returns [] on any exception.
    """
    try:
        clauses = ["1=1"]
        params: list = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        where = " AND ".join(clauses)

        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                f"SELECT * FROM channels WHERE {where} ORDER BY created_at",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_channel(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — Identity Editor
# ---------------------------------------------------------------------------

def update_identity(
    channel_id:     str,
    identity_patch: dict,
    db_path:        Optional[Path] = None,
) -> bool:
    """
    Non-destructively update channel identity fields.

    Merges identity_patch into the existing identity — only supplied keys
    are changed. Does not overwrite unmentioned fields.

    Non-blocking: returns False on any exception.

    Args:
        channel_id:     Target channel.
        identity_patch: Dict of fields to update (e.g. {"tone": "serious"}).
        db_path:        Override SQLite path.

    Returns:
        True on success, False on failure.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT identity_json FROM channels WHERE channel_id = ?",
                [channel_id],
            ).fetchone()
            if not row:
                return False
            identity = _identity_from_json(row["identity_json"])

            # Apply patch field-by-field
            for key, val in identity_patch.items():
                if hasattr(identity, key):
                    setattr(identity, key, val)
                else:
                    identity.extra[key] = val

            conn.execute(
                "UPDATE channels SET identity_json = ? WHERE channel_id = ?",
                (_identity_to_json(identity), channel_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API — Channel Template System
# ---------------------------------------------------------------------------

def create_template(
    template_id:   str,
    channel_id:    str,
    template_type: str,
    content:       str,
    db_path:       Optional[Path] = None,
) -> ChannelTemplate:
    """
    Persist a reusable prompt/config template for a channel.

    Non-blocking: returns a ChannelTemplate with empty created_at on failure.

    Args:
        template_id:   Unique template identifier.
        channel_id:    Associated channel.
        template_type: Category ("system_prompt", "title_prompt", "hook", etc.).
        content:       Template text (may include {placeholder} tokens).
        db_path:       Override SQLite path.

    Returns:
        ChannelTemplate dataclass.
    """
    now      = _now_iso()
    template = ChannelTemplate(
        template_id   = template_id,
        channel_id    = channel_id,
        template_type = template_type,
        content       = content,
        created_at    = now,
    )
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            conn.execute(
                """
                INSERT INTO channel_templates
                    (template_id, channel_id, template_type, content, created_at)
                VALUES (?,?,?,?,?)
                """,
                (template_id, channel_id, template_type, content, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return template


def get_templates(
    channel_id:    str,
    template_type: Optional[str] = None,
    db_path:       Optional[Path] = None,
) -> list[ChannelTemplate]:
    """
    Retrieve templates for a channel, optionally filtered by type.

    Non-blocking: returns [] on any exception.
    """
    try:
        clauses = ["channel_id = ?"]
        params: list = [channel_id]
        if template_type:
            clauses.append("template_type = ?")
            params.append(template_type)
        where = " AND ".join(clauses)

        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                f"SELECT * FROM channel_templates WHERE {where} ORDER BY created_at",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_template(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — Channel Duplicator
# ---------------------------------------------------------------------------

def duplicate_channel(
    source_channel_id: str,
    new_channel_id:    str,
    new_name:          str,
    identity_overrides: Optional[dict] = None,
    db_path:           Optional[Path]  = None,
) -> Optional[Channel]:
    """
    Fork an existing channel with a new ID and optional identity overrides.

    Copies all templates from the source channel to the new channel.
    Episode assignments are NOT copied (the new channel starts fresh).

    Non-blocking: returns None on any exception.

    Args:
        source_channel_id:  Channel to clone.
        new_channel_id:     New unique channel identifier.
        new_name:           Name for the forked channel.
        identity_overrides: Identity fields to override on the fork.
        db_path:            Override SQLite path.

    Returns:
        New Channel or None on failure.
    """
    try:
        source = get_channel(source_channel_id, db_path=db_path)
        if source is None:
            return None

        # Clone identity then apply overrides
        identity_dict = {
            "persona_name":    source.identity.persona_name,
            "tone":            source.identity.tone,
            "target_audience": source.identity.target_audience,
            "visual_style":    source.identity.visual_style,
            "language":        source.identity.language,
            "hashtags":        list(source.identity.hashtags),
            "extra":           dict(source.identity.extra),
        }
        if identity_overrides:
            for k, v in identity_overrides.items():
                if k in identity_dict:
                    identity_dict[k] = v
                else:
                    identity_dict["extra"][k] = v

        new_identity = ChannelIdentity(**{
            k: identity_dict[k] for k in
            ("persona_name", "tone", "target_audience",
             "visual_style", "language", "hashtags", "extra")
        })

        new_channel = create_channel(
            new_channel_id, new_name, source.platform,
            identity=new_identity, db_path=db_path,
        )

        # Copy templates
        source_templates = get_templates(source_channel_id, db_path=db_path)
        for tmpl in source_templates:
            new_tid = str(uuid.uuid4())
            create_template(
                new_tid, new_channel_id,
                tmpl.template_type, tmpl.content,
                db_path=db_path,
            )

        return new_channel
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API — Prompt Regeneration
# ---------------------------------------------------------------------------

def regenerate_prompt(
    base_prompt: str,
    channel_id:  str,
    db_path:     Optional[Path] = None,
) -> str:
    """
    Inject channel identity context into a base prompt.

    Appends a concise identity block to the base_prompt so that any LLM
    receiving the prompt is aware of channel persona, tone, audience, and
    visual style. Does not call any external API — purely string work.

    Non-blocking: returns base_prompt unchanged on any exception.

    Args:
        base_prompt: The original prompt text.
        channel_id:  Channel whose identity to inject.
        db_path:     Override SQLite path.

    Returns:
        Prompt string with identity context appended.
    """
    try:
        channel = get_channel(channel_id, db_path=db_path)
        if channel is None:
            return base_prompt

        identity = channel.identity
        lines: list[str] = []

        if identity.persona_name:
            lines.append(f"Persona: {identity.persona_name}")
        if identity.tone:
            lines.append(f"Tone: {identity.tone}")
        if identity.target_audience:
            lines.append(f"Target audience: {identity.target_audience}")
        if identity.visual_style:
            lines.append(f"Visual style: {identity.visual_style}")
        if identity.language and identity.language != "en":
            lines.append(f"Language: {identity.language}")
        if identity.hashtags:
            lines.append(f"Hashtags: {' '.join(identity.hashtags)}")

        if not lines:
            return base_prompt

        identity_block = "\n\n[Channel identity]\n" + "\n".join(lines)
        return base_prompt + identity_block
    except Exception:
        return base_prompt


# ---------------------------------------------------------------------------
# Public API — Channel Isolation (episode assignment)
# ---------------------------------------------------------------------------

def assign_episode_to_channel(
    episode_id: str,
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> bool:
    """
    Associate an episode with a channel (idempotent).

    Non-blocking: returns False on any exception.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            # Idempotent: skip if already assigned
            existing = conn.execute(
                """SELECT 1 FROM channel_episodes
                   WHERE channel_id=? AND episode_id=?""",
                [channel_id, episode_id],
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO channel_episodes
                       (assignment_id, channel_id, episode_id, assigned_at)
                       VALUES (?,?,?,?)""",
                    (str(uuid.uuid4()), channel_id, episode_id, _now_iso()),
                )
                conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def get_channel_episodes(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> list[str]:
    """
    Return episode_ids assigned to a channel, ordered by assignment time.

    Non-blocking: returns [] on any exception.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_tables(conn)
            rows = conn.execute(
                """SELECT episode_id FROM channel_episodes
                   WHERE channel_id=?
                   ORDER BY assigned_at""",
                [channel_id],
            ).fetchall()
        finally:
            conn.close()
        return [r["episode_id"] for r in rows]
    except Exception:
        return []
