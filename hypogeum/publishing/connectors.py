"""
hypogeum/publishing/connectors.py — Platform Connector Abstraction Layer

Provides a unified interface for all publishing platforms. Each connector
validates a PublishRequest against platform-specific constraints and returns
a structured result — without making any network calls. The actual API calls
are handled by the lower-level clients (youtube.py, meta.py, tiktok.py).

Sub-components:
    PublishRequest     — unified input structure
    PublishResult      — unified output structure
    Platform limits    — per-platform constraint constants
    Validators         — validate_request(platform, request) -> list[str]
    Connector registry — get_connector(platform) -> PlatformConnector

Supported platforms: "youtube", "facebook", "instagram", "tiktok", "email"

Public API:
    validate_request(platform, request) -> list[str]   # errors, empty = ok
    get_connector(platform) -> PlatformConnector
    build_publish_request(
        platform, episode_id, title, description,
        video_path=None, thumbnail_path=None,
        tags=None, channel_id=None, metadata=None,
    ) -> PublishRequest
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Per-platform limits
# ---------------------------------------------------------------------------

PLATFORM_LIMITS: dict[str, dict] = {
    "youtube": {
        "title_max_chars":       100,
        "description_max_chars": 5000,
        "tags_max_count":        500,
        "tags_combined_max_chars": 500,
        "video_max_size_gb":     256,
        "video_formats":         {"mp4", "mov", "avi", "wmv", "flv", "3gp", "webm"},
        "thumbnail_formats":     {"jpg", "jpeg", "png", "gif", "bmp", "webp"},
        "thumbnail_max_size_mb": 2,
        "requires_video":        True,
    },
    "facebook": {
        "title_max_chars":       255,
        "description_max_chars": 63206,
        "tags_max_count":        0,    # no tags field
        "video_max_size_gb":     10,
        "video_formats":         {"mp4", "mov"},
        "requires_video":        True,
    },
    "instagram": {
        "caption_max_chars":     2200,
        "hashtags_max_count":    30,
        "video_max_size_gb":     1,
        "video_duration_max_s":  60,   # Reels
        "video_formats":         {"mp4", "mov"},
        "thumbnail_formats":     {"jpg", "jpeg", "png"},
        "requires_video":        True,
    },
    "tiktok": {
        "title_max_chars":       150,
        "description_max_chars": 2200,
        "hashtags_max_count":    5,
        "video_max_size_gb":     4,
        "video_duration_max_s":  600,
        "video_formats":         {"mp4", "mov", "webm"},
        "requires_video":        True,
    },
    "email": {
        "subject_max_chars":     998,
        "body_max_chars":        100_000,
        "attachments_max_count": 10,
        "attachment_max_size_mb": 25,
        "requires_video":        False,
    },
}


# ---------------------------------------------------------------------------
# Unified data structures
# ---------------------------------------------------------------------------

@dataclass
class PublishRequest:
    """
    Unified publish request passed to any platform connector.

    Not all fields apply to every platform — unused fields are silently
    ignored during validation.
    """
    platform:         str
    episode_id:       str
    title:            str                     = ""
    description:      str                     = ""
    tags:             list[str]               = field(default_factory=list)
    video_path:       Optional[Path]          = None
    thumbnail_path:   Optional[Path]          = None
    channel_id:       Optional[str]           = None
    email_recipients: list[str]               = field(default_factory=list)
    metadata:         dict                    = field(default_factory=dict)


@dataclass
class PublishResult:
    """
    Unified result returned by a connector after validation or publish.

    For dry-run validation: platform_id is empty, status is "validated" or "invalid".
    For live publish: status is "published", "failed", or "skipped".
    """
    platform:     str
    episode_id:   str
    status:       str                # "validated" | "invalid" | "published" | "failed" | "skipped"
    platform_id:  Optional[str]      = None   # platform-assigned video/post ID
    url:          Optional[str]      = None
    errors:       list[str]          = field(default_factory=list)
    warnings:     list[str]          = field(default_factory=list)
    metadata:     dict               = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validator implementations
# ---------------------------------------------------------------------------

def _validate_youtube(req: PublishRequest) -> list[str]:
    limits = PLATFORM_LIMITS["youtube"]
    errors: list[str] = []
    if not req.title:
        errors.append("title is required for YouTube")
    elif len(req.title) > limits["title_max_chars"]:
        errors.append(
            f"YouTube title exceeds {limits['title_max_chars']} chars "
            f"(got {len(req.title)})"
        )
    if req.description and len(req.description) > limits["description_max_chars"]:
        errors.append(
            f"YouTube description exceeds {limits['description_max_chars']} chars"
        )
    if len(req.tags) > limits["tags_max_count"]:
        errors.append(f"YouTube: too many tags ({len(req.tags)} > {limits['tags_max_count']})")
    combined_tags_len = sum(len(t) for t in req.tags)
    if combined_tags_len > limits["tags_combined_max_chars"]:
        errors.append(
            f"YouTube combined tags length {combined_tags_len} "
            f"exceeds {limits['tags_combined_max_chars']}"
        )
    if req.video_path is not None:
        ext = Path(req.video_path).suffix.lstrip(".").lower()
        if ext not in limits["video_formats"]:
            errors.append(f"YouTube: unsupported video format '.{ext}'")
    if req.thumbnail_path is not None:
        ext = Path(req.thumbnail_path).suffix.lstrip(".").lower()
        if ext not in limits["thumbnail_formats"]:
            errors.append(f"YouTube: unsupported thumbnail format '.{ext}'")
    return errors


def _validate_facebook(req: PublishRequest) -> list[str]:
    limits = PLATFORM_LIMITS["facebook"]
    errors: list[str] = []
    if req.title and len(req.title) > limits["title_max_chars"]:
        errors.append(
            f"Facebook title exceeds {limits['title_max_chars']} chars "
            f"(got {len(req.title)})"
        )
    if req.description and len(req.description) > limits["description_max_chars"]:
        errors.append(
            f"Facebook description exceeds {limits['description_max_chars']} chars"
        )
    if req.video_path is not None:
        ext = Path(req.video_path).suffix.lstrip(".").lower()
        if ext not in limits["video_formats"]:
            errors.append(f"Facebook: unsupported video format '.{ext}'")
    return errors


def _validate_instagram(req: PublishRequest) -> list[str]:
    limits = PLATFORM_LIMITS["instagram"]
    errors: list[str] = []
    caption = req.description or req.title
    if caption and len(caption) > limits["caption_max_chars"]:
        errors.append(
            f"Instagram caption exceeds {limits['caption_max_chars']} chars "
            f"(got {len(caption)})"
        )
    hashtag_count = sum(1 for t in req.tags if t.startswith("#"))
    if hashtag_count > limits["hashtags_max_count"]:
        errors.append(
            f"Instagram: too many hashtags ({hashtag_count} > {limits['hashtags_max_count']})"
        )
    if req.video_path is not None:
        ext = Path(req.video_path).suffix.lstrip(".").lower()
        if ext not in limits["video_formats"]:
            errors.append(f"Instagram: unsupported video format '.{ext}'")
    if req.thumbnail_path is not None:
        ext = Path(req.thumbnail_path).suffix.lstrip(".").lower()
        if ext not in limits["thumbnail_formats"]:
            errors.append(f"Instagram: unsupported thumbnail format '.{ext}'")
    return errors


def _validate_tiktok(req: PublishRequest) -> list[str]:
    limits = PLATFORM_LIMITS["tiktok"]
    errors: list[str] = []
    if req.title and len(req.title) > limits["title_max_chars"]:
        errors.append(
            f"TikTok title exceeds {limits['title_max_chars']} chars "
            f"(got {len(req.title)})"
        )
    if req.description and len(req.description) > limits["description_max_chars"]:
        errors.append(
            f"TikTok description exceeds {limits['description_max_chars']} chars"
        )
    hashtag_count = sum(1 for t in req.tags if t.startswith("#"))
    if hashtag_count > limits["hashtags_max_count"]:
        errors.append(
            f"TikTok: too many hashtags ({hashtag_count} > {limits['hashtags_max_count']})"
        )
    if req.video_path is not None:
        ext = Path(req.video_path).suffix.lstrip(".").lower()
        if ext not in limits["video_formats"]:
            errors.append(f"TikTok: unsupported video format '.{ext}'")
    return errors


def _validate_email(req: PublishRequest) -> list[str]:
    limits = PLATFORM_LIMITS["email"]
    errors: list[str] = []
    if not req.email_recipients:
        errors.append("email: at least one recipient is required")
    if not req.title:
        errors.append("email: subject (title) is required")
    elif len(req.title) > limits["subject_max_chars"]:
        errors.append(
            f"email subject exceeds {limits['subject_max_chars']} chars "
            f"(got {len(req.title)})"
        )
    if req.description and len(req.description) > limits["body_max_chars"]:
        errors.append(f"email body exceeds {limits['body_max_chars']} chars")
    attachment_count = sum(1 for x in [req.video_path, req.thumbnail_path] if x is not None)
    if attachment_count > limits["attachments_max_count"]:
        errors.append(
            f"email: too many attachments "
            f"({attachment_count} > {limits['attachments_max_count']})"
        )
    return errors


_VALIDATORS = {
    "youtube":   _validate_youtube,
    "facebook":  _validate_facebook,
    "instagram": _validate_instagram,
    "tiktok":    _validate_tiktok,
    "email":     _validate_email,
}


# ---------------------------------------------------------------------------
# Public API — validate_request
# ---------------------------------------------------------------------------

def validate_request(
    platform: str,
    request:  PublishRequest,
) -> list[str]:
    """
    Validate a PublishRequest against platform-specific constraints.

    No network calls. Pure constraint checking.

    Args:
        platform: Target platform ("youtube", "facebook", "instagram",
                  "tiktok", "email").
        request:  The publish request to validate.

    Returns:
        List of error strings. Empty list means the request is valid.
        Returns ["unknown platform: <name>"] for unrecognised platforms.
    """
    validator = _VALIDATORS.get(platform.lower())
    if validator is None:
        return [f"unknown platform: {platform!r}"]
    try:
        return validator(request)
    except Exception as exc:
        return [f"validation error: {exc}"]


# ---------------------------------------------------------------------------
# Connector classes
# ---------------------------------------------------------------------------

class PlatformConnector:
    """
    Base connector defining the publishing contract.

    Subclasses override validate() to apply platform rules and,
    when production publishing is enabled, publish() to call the API.
    validate() is the only method that runs without network access.
    """

    platform: str = "base"

    def validate(self, request: PublishRequest) -> PublishResult:
        """
        Validate the request and return a PublishResult with status
        "validated" (no errors) or "invalid" (errors present).
        """
        errors = validate_request(self.platform, request)
        status = "invalid" if errors else "validated"
        return PublishResult(
            platform   = self.platform,
            episode_id = request.episode_id,
            status     = status,
            errors     = errors,
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        """
        Perform the actual publish. Default raises NotImplementedError.
        Concrete connectors wire this to the appropriate API client.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.publish() not implemented"
        )


class YouTubeConnector(PlatformConnector):
    """Connector for YouTube Data API v3."""
    platform = "youtube"


class FacebookConnector(PlatformConnector):
    """Connector for Meta Graph API (Facebook video)."""
    platform = "facebook"


class InstagramConnector(PlatformConnector):
    """Connector for Meta Graph API (Instagram Reels)."""
    platform = "instagram"


class TikTokConnector(PlatformConnector):
    """Connector for TikTok Content Posting API."""
    platform = "tiktok"


class EmailConnector(PlatformConnector):
    """
    Connector for email distribution.

    Validates subject length, recipient list, and body length.
    publish() is handled by an SMTP client (injected separately).
    """
    platform = "email"


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, PlatformConnector] = {
    "youtube":   YouTubeConnector(),
    "facebook":  FacebookConnector(),
    "instagram": InstagramConnector(),
    "tiktok":    TikTokConnector(),
    "email":     EmailConnector(),
}


def get_connector(platform: str) -> Optional[PlatformConnector]:
    """
    Return the PlatformConnector for the given platform name.

    Args:
        platform: Platform name (case-insensitive).

    Returns:
        PlatformConnector or None if platform is not registered.
    """
    return _REGISTRY.get(platform.lower())


def get_supported_platforms() -> list[str]:
    """Return sorted list of supported platform names."""
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Public API — build_publish_request
# ---------------------------------------------------------------------------

def build_publish_request(
    platform:         str,
    episode_id:       str,
    title:            str                = "",
    description:      str                = "",
    tags:             Optional[list[str]] = None,
    video_path:       Optional[Path]     = None,
    thumbnail_path:   Optional[Path]     = None,
    channel_id:       Optional[str]      = None,
    email_recipients: Optional[list[str]] = None,
    metadata:         Optional[dict]     = None,
) -> PublishRequest:
    """
    Build a PublishRequest with sensible defaults.

    Args:
        platform:         Target platform.
        episode_id:       Episode identifier.
        title:            Video/post/email title.
        description:      Video description / post caption / email body.
        tags:             List of tags or hashtags.
        video_path:       Path to the video file.
        thumbnail_path:   Path to the thumbnail image.
        channel_id:       Associated channel (from channel_manager).
        email_recipients: Email addresses (email platform only).
        metadata:         Arbitrary additional metadata.

    Returns:
        PublishRequest dataclass.
    """
    return PublishRequest(
        platform         = platform.lower(),
        episode_id       = episode_id,
        title            = title,
        description      = description,
        tags             = tags or [],
        video_path       = Path(video_path) if video_path else None,
        thumbnail_path   = Path(thumbnail_path) if thumbnail_path else None,
        channel_id       = channel_id,
        email_recipients = email_recipients or [],
        metadata         = metadata or {},
    )
