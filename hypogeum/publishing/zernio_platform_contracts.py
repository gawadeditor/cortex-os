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
    # Platform-specific settings this contract requires to be present,
    # with a value FIXED at this (provider) level regardless of any
    # channel's own policy -- e.g. a field with only one valid value for
    # every caller. Keys not listed here are optional per the
    # documentation. Rarely used now that policy-driven fields below
    # exist -- prefer required_policy_fields for anything that is a real
    # per-channel choice (even a "must be true" legal consent flag is a
    # channel/operator decision, not a provider constant).
    required_settings: dict = field(default_factory=dict)
    # Documented settings-object field names this contract knows about
    # that a Channel Package's platform_deliverable_policy MAY supply a
    # value for (beyond the single privacy_field/privacy_allowed_values
    # pair, which keeps its own dedicated enum-validated handling). When
    # present in policy_entry, passed through verbatim into the built
    # settings object under the SAME key name -- this module only
    # records that the key is a real, documented field for this
    # platform; it never invents or defaults a value itself. Never
    # channel-specific: this is provider (Zernio/TikTok/etc) knowledge,
    # not a Hypogeum editorial choice -- the ACTUAL values live in the
    # Channel Package's own platform_deliverable_policy, never here.
    policy_passthrough_fields: Tuple[str, ...] = ()
    # Subset of policy_passthrough_fields that MUST be present in
    # policy_entry (in addition to privacy_field, when privacy_required)
    # before this platform is considered ready for autonomous submission
    # -- readiness fails closed, per (deliverable, platform), when any
    # of these is absent from the channel's own configured policy.
    required_policy_fields: Tuple[str, ...] = ()
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
        # 2026-08-23: content_preview_confirmed/express_consent_given were
        # previously hardcoded True here regardless of any channel's own
        # policy -- moved to policy_passthrough_fields/required_policy_fields
        # below. Even a "must be true" legal consent flag is a real
        # per-channel operator decision (whose consent, for which content),
        # never a provider constant Factory Core should assert on a
        # channel's behalf.
        required_settings={},
        # Verified live via docs.zernio.com/platforms/tiktok (2026-08-23):
        # allow_comment/allow_duet/allow_stitch (boolean; duet/stitch
        # "required for videos") plus the two legal consent flags are all
        # real, documented tiktokSettings fields.
        policy_passthrough_fields=(
            "content_preview_confirmed", "express_consent_given",
            "allow_comment", "allow_duet", "allow_stitch",
        ),
        required_policy_fields=(
            "content_preview_confirmed", "express_consent_given",
            "allow_comment", "allow_duet", "allow_stitch",
        ),
        settings_body_key="tiktokSettings",
        known_gaps=(
            "exact allowed privacy_level values are account/creator-specific — "
            "documented enum may not match every creator's actual allowed set; "
            "docs state this must be fetched from a creator-info endpoint, which "
            "this contract does not yet call",
            "timezone behavior for scheduledFor not documented for TikTok specifically",
            "commercialContentType (branded/commercial-content declaration) is "
            "documented but deliberately never set by this contract or any "
            "policy value below -- only set explicitly, per-episode, when "
            "content genuinely qualifies as branded content; omission is the "
            "correct default, never invented.",
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


def build_platform_settings(platform: str, policy_entry: dict) -> Optional[dict]:
    """
    Translates a Channel Package's generic platform_deliverable_policy
    entry (e.g. {"enabled": true, "privacy": "PUBLIC_TO_EVERYONE"}) into
    the EXACT Zernio field names/shape this platform's verified contract
    requires -- the ONE place that mapping lives, so readiness's own
    validation (which checks contract.privacy_field/privacy_allowed_values
    against the SAME policy_entry) and the real request body can never
    silently diverge.

    Returns None when this platform has no settings object to build
    (Instagram/Facebook today — no required_settings, no privacy field,
    no policy_passthrough_fields).

    Never invents a policy VALUE that isn't in policy_entry:
    - contract.required_settings supplies only fixed, provider-constant
      values with no per-channel choice at all (rare -- prefer
      required_policy_fields for anything that is a real operator
      decision, even a "must be true" consent flag).
    - contract.privacy_field's value comes only from policy_entry["privacy"].
    - Every other name in contract.policy_passthrough_fields (which
      includes required_policy_fields -- required ones are also
      passthrough) is copied verbatim from policy_entry ONLY when the
      channel's own policy actually set it; a genuinely required field
      the channel never configured is simply absent here, never defaulted
      -- readiness (check_cross_platform_readiness) is what fails that
      combination closed, not this function inventing a value to paper
      over it.
    """
    contract = get_platform_contract(platform)
    if contract is None or not contract.verified:
        return None

    settings: dict = dict(contract.required_settings)
    if contract.privacy_field and policy_entry.get("privacy"):
        # contract.privacy_field is a dotted path like
        # 'tiktokSettings.privacy_level' -- only the leaf name is needed
        # here since `settings` IS the tiktokSettings/facebookSettings
        # object itself.
        leaf = contract.privacy_field.split(".")[-1]
        settings[leaf] = policy_entry["privacy"]
    for field_name in contract.policy_passthrough_fields:
        if field_name in policy_entry:
            settings[field_name] = policy_entry[field_name]

    return settings or None
