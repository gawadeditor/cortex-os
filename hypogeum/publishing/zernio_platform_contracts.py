"""
hypogeum/publishing/zernio_platform_contracts.py — Verified, versioned
Zernio-per-platform publishing contract data (media constraints, caption/
title field mapping, privacy requirements, platform-specific settings).

Provider-specific (Zernio), NOT channel-specific: nothing here mentions
"hypogeum" or any Hypogeum editorial decision. Which platforms a channel
actually uses, and which deliverable classes go to which platform, is a
Channel Package concern (ChannelOperationsProfile.platform_deliverable_policy) —
this module only records what ZERNIO'S OWN API requires/supports for a
platform once a caller has already decided to target it.

Source of truth: docs.zernio.com/platforms/{tiktok,instagram,facebook}
and docs.zernio.com/posts/create-post, read live on 2026-08-22 (see
docs/operations/checkpoints/HYPOGEUM_ZERNIO_CROSS_PLATFORM_READINESS_2026-08-22.md
for the full audit trail and exact quotes). YouTube's contract is not
duplicated here — it is already implicitly encoded in
hypogeum.publishing.zernio._build_publish_body()/hypogeum.publishing.
zernio_media_staging (title/content top-level fields, no platform-specific
settings object, no required privacy field).

Every field below is either a confirmed value from the live documentation
fetch, or explicitly `None`/marked unconfirmed when the documentation did
not state it — this module never guesses. A caller (readiness) that needs
a field this contract does not confirm must fail closed, never assume a
server-side default is acceptable for autonomous submission.

Public API:
    PlatformContract (dataclass)
    ZERNIO_PLATFORM_CONTRACTS: dict[str, PlatformContract]
    get_platform_contract(platform) -> Optional[PlatformContract]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class PlatformContract:
    platform: str
    verified: bool  # False => this platform must fail closed everywhere
    duration_seconds_range: Optional[Tuple[float, float]]  # (min, max), None = unconfirmed
    max_size_bytes: Optional[int]
    aspect_ratio: Optional[str]  # e.g. "9:16"
    caption_field: str  # always "content" (top-level) for every verified platform
    caption_max_chars: Optional[int]
    title_supported: bool
    title_field: Optional[str]  # dotted path within the request body, or None
    privacy_required: bool
    privacy_field: Optional[str]  # dotted path, or None when not required/not documented
    privacy_allowed_values: Optional[Tuple[str, ...]]
    scheduling_supported: bool
    thumbnail_supported: bool
    thumbnail_field: Optional[str]
    # Platform-specific settings object this contract requires to be
    # present (with these exact sub-keys, all True/set) before an
    # autonomous submission is safe — e.g. TikTok's mandatory consent
    # flags. Keys not listed here are optional per the documentation.
    required_settings: dict = field(default_factory=dict)
    settings_body_key: Optional[str] = None  # e.g. "tiktokSettings", root-level
    known_gaps: Tuple[str, ...] = ()


ZERNIO_PLATFORM_CONTRACTS: dict = {
    "tiktok": PlatformContract(
        platform="tiktok",
        verified=True,
        duration_seconds_range=(3.0, 600.0),
        max_size_bytes=4 * 1024 * 1024 * 1024,
        aspect_ratio="9:16",
        caption_field="content",
        caption_max_chars=2200,
        title_supported=False,
        title_field=None,
        privacy_required=True,
        privacy_field="tiktokSettings.privacy_level",
        privacy_allowed_values=("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"),
        scheduling_supported=True,
        thumbnail_supported=True,
        thumbnail_field="tiktokSettings.video_cover_image_url",
        required_settings={
            "content_preview_confirmed": True,
            "express_consent_given": True,
        },
        settings_body_key="tiktokSettings",
        known_gaps=(
            "exact allowed privacy_level values are account/creator-specific — "
            "documented enum may not match every creator's actual allowed set; "
            "docs state this must be fetched from a creator-info endpoint, which "
            "this contract does not yet call",
            "timezone behavior for scheduledFor not documented for TikTok specifically",
        ),
    ),
    "instagram": PlatformContract(
        platform="instagram",
        verified=True,
        duration_seconds_range=(3.0, 90.0),  # Reels
        max_size_bytes=300 * 1024 * 1024,
        aspect_ratio="9:16",
        caption_field="content",
        caption_max_chars=2200,
        title_supported=False,
        title_field=None,
        privacy_required=False,
        privacy_field=None,
        privacy_allowed_values=None,
        scheduling_supported=True,
        thumbnail_supported=True,
        thumbnail_field="platformSpecificData.instagramThumbnail",
        required_settings={},
        settings_body_key=None,
        known_gaps=(
            "exact scheduledFor field usage not shown in a worked example for "
            "Instagram specifically (only confirmed via the platform's own "
            "'Scheduling: Yes' quick-reference row)",
            "account must be Business or Creator — this contract does not verify "
            "account TYPE, only connection/active status via GET /v1/accounts",
        ),
    ),
    "facebook": PlatformContract(
        platform="facebook",
        verified=True,
        duration_seconds_range=(3.0, 60.0),  # Reels
        max_size_bytes=4 * 1024 * 1024 * 1024,
        aspect_ratio="9:16",
        caption_field="content",
        caption_max_chars=None,
        title_supported=True,
        title_field="platformSpecificData.title",
        privacy_required=False,
        privacy_field=None,
        privacy_allowed_values=None,
        scheduling_supported=True,
        thumbnail_supported=False,
        thumbnail_field=None,
        required_settings={"contentType": "reel"},
        settings_body_key=None,  # platformSpecificData is per-platforms[] entry, not root
        known_gaps=(
            "no custom thumbnail/cover mechanism is documented for Facebook Reels — "
            "Zernio/Facebook selects one automatically",
            "no general public/friends/only-me privacy enum is documented; only "
            "optional country-level geoRestriction exists",
            "exact scheduledFor worked example not shown for Facebook video/Reels "
            "specifically (only confirmed via the platform's own 'Scheduling: Yes' "
            "quick-reference row)",
        ),
    ),
}


def get_platform_contract(platform: str) -> Optional[PlatformContract]:
    return ZERNIO_PLATFORM_CONTRACTS.get(platform)
