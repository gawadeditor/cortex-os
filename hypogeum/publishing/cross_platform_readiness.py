"""
hypogeum/publishing/cross_platform_readiness.py — Per-(deliverable,
platform) fail-closed readiness for Zernio submissions beyond the single
platform hypogeum.publishing.readiness.check_publish_readiness() already
evaluates as a whole-episode gate.

Why a separate module rather than extending check_publish_readiness()
itself: that function's contract is "is this ONE episode ready to submit
on the requested platform(s)" — a single pass/fail per capability, shared
across every deliverable. Cross-platform distribution is fundamentally
per-(deliverable_id, platform): the main episode and a Short can be
simultaneously ready on YouTube and NOT ready on TikTok, and one Short
can be ready while another is not (missing caption). Reusing
check_publish_readiness()'s shape would force either a false global
pass/fail or a parallel re-implementation of its channel-scope/metadata
logic — this module instead REUSES readiness.py's own resolvers
(resolve_canonical_metadata, resolve_canonical_captions) so the two can
never diverge, and adds the platform-dimension entirely additively.

Routing source of truth: ChannelOperationsProfile.platform_deliverable_policy
(hypogeum.channel_config) — a channel with no policy configured fails
closed for EVERY platform/deliverable combination (never assumes YouTube-
only, or any other default). A (platform, deliverable_class) combination
absent from the policy is reported as `skipped_not_enabled` (deliberately
not attempted), never silently passed as ready.

Platform contract source of truth: hypogeum.publishing.
zernio_platform_contracts.ZERNIO_PLATFORM_CONTRACTS. A platform with no
verified contract (or none at all) fails EVERY deliverable on that
platform closed — "Do not guess undocumented fields."

Idempotency identity: platform is part of the lookup key on every
publication_intents check this module performs (WHERE ... AND platform=?)
— a succeeded YouTube intent can never suppress or be confused with a
distinct TikTok/Instagram/Facebook intent for the same deliverable, and
vice versa.

Public API:
    PlatformDeliverableCheckResult, CrossPlatformReadinessResult
    check_cross_platform_readiness(episode_id, *, channel_id, assets_root,
        db_path=None, provider_id="zernio", logical_alias="default",
        metadata_override=None) -> CrossPlatformReadinessResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from hypogeum.artifact_compatibility import _probe_duration_seconds
from hypogeum.publishing.deliverable_ids import (
    classify_deliverable_class,
    resolve_required_deliverable_ids,
    resolve_short_deliverable_id,
)
from hypogeum.publishing.provider_repository import resolve_active_binding
from hypogeum.publishing.readiness import (
    CaptionResolutionError,
    MetadataOverride,
    MetadataResolutionError,
    resolve_canonical_captions,
    resolve_canonical_metadata,
)
from hypogeum.publishing.zernio_media_staging import (
    _MAX_SIZE_BYTES as _ZERNIO_MAX_SIZE_BYTES,
    _SUPPORTED_CONTENT_TYPES as _ZERNIO_SUPPORTED_CONTENT_TYPES,
)
from hypogeum.publishing.zernio_platform_contracts import get_platform_contract


@dataclass
class PlatformDeliverableCheckResult:
    deliverable_id: str
    platform: str
    status: str  # "pass" | "fail" | "skipped_not_enabled"
    reason: str
    evidence: dict = field(default_factory=dict)


@dataclass
class CrossPlatformReadinessResult:
    episode_id: str
    channel_id: str
    checks: List[PlatformDeliverableCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when EVERY evaluated combination is pass or
        deliberately skipped-not-enabled — a single platform-specific
        failure is never masked as global success."""
        return bool(self.checks) and all(c.status != "fail" for c in self.checks)

    def failures(self) -> List[PlatformDeliverableCheckResult]:
        return [c for c in self.checks if c.status == "fail"]

    def for_platform(self, platform: str) -> List[PlatformDeliverableCheckResult]:
        return [c for c in self.checks if c.platform == platform]


def _load_render_package(episode_dir: Path, episode_id: str) -> dict:
    render_path = episode_dir / f"final_render_package_{episode_id}.json"
    if not render_path.exists():
        return {}
    try:
        return json.loads(render_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _deliverable_file_path(
    episode_dir: Path, episode_id: str, deliverable_id: str, deliverable_ids_in_order: List[str],
    final_render_package: Optional[dict] = None,
) -> Path:
    """
    2026-08-23 fix: resolve a Short's video from its own canonical
    video_asset_id (final_render_package['shorts_renders'][i].
    video_asset_id) when present -- the same assets_root-relative,
    extension-less convention hypogeum.agents.production_output_gate's
    own equivalent fix already uses for caption_asset_id/video_asset_id.
    Previously this function only ever derived the generation-time index
    filename, so a repaired render promoted under its own immutable
    versioned asset id (the original short_{index}_{episode_id}.mp4
    preserved untouched alongside it) was invisible here. Falls back to
    the index convention when video_asset_id is empty/absent or
    final_render_package is not supplied — every unrepaired historical
    package's behavior is unchanged.
    """
    if deliverable_id == "main_episode":
        return episode_dir / f"main_{episode_id}.mp4"
    index = deliverable_ids_in_order.index(deliverable_id) - 1  # -1 for main_episode offset
    shorts_renders = (final_render_package or {}).get("shorts_renders") or []
    if 0 <= index < len(shorts_renders):
        video_asset_id = shorts_renders[index].get("video_asset_id") or ""
        if video_asset_id:
            return episode_dir.parent.parent / f"{video_asset_id}.mp4"
    return episode_dir / f"short_{index}_{episode_id}.mp4"


def _existing_intent_status(db_path, channel_id: str, episode_id: str, deliverable_id: str, platform: str) -> Optional[str]:
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT status FROM publication_intents
            WHERE channel_id = ? AND episode_id = ? AND deliverable_id = ? AND platform = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (channel_id, episode_id, deliverable_id, platform),
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _check_canonical_platform_metadata(
    *, final_render_package: dict, deliverable_id: str, platform: str,
    deliverable_class: str, class_entry: dict, final_duration_seconds: Optional[float],
    episode_dir: Path, episode_id: str, contract,
) -> List[str]:
    """
    Thin wrapper delegating to hypogeum.publishing.canonical_metadata --
    the generic, permanent, per-(deliverable_id, platform) contract that
    replaced this module's incident-only 2026-08-23 YouTube-only check.
    Applies to EVERY (deliverable_id, platform) pair the channel enables,
    Shorts included -- never exempted. See canonical_metadata.py's own
    docstring for the full field/validation contract, and
    docs/operations/audits/ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md
    for why the earlier main-episode-only design was insufficient.
    """
    from hypogeum.publishing.canonical_metadata import (
        resolve_canonical_platform_metadata,
        validate_canonical_platform_metadata,
    )

    metadata = resolve_canonical_platform_metadata(final_render_package, deliverable_id, platform)
    return validate_canonical_platform_metadata(
        metadata, deliverable_class=deliverable_class, platform=platform,
        class_entry=class_entry, final_duration_seconds=final_duration_seconds,
        episode_dir=episode_dir, episode_id=episode_id, contract=contract,
    )


def check_cross_platform_readiness(
    episode_id: str,
    *,
    channel_id: str,
    assets_root: Path,
    db_path=None,
    provider_id: str = "zernio",
    logical_alias: str = "default",
    metadata_override: Optional[MetadataOverride] = None,
) -> CrossPlatformReadinessResult:
    """
    Read-only. Never mutates any file or database row, never makes a
    provider/network call. Evaluates every (deliverable_id, platform)
    combination the channel's own platform_deliverable_policy declares
    enabled for this episode's actual deliverable set.
    """
    from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415

    result = CrossPlatformReadinessResult(episode_id=episode_id, channel_id=channel_id)

    try:
        profile = get_channel_operations_profile(channel_id, db_path=db_path)
    except RuntimeError as exc:
        result.checks.append(PlatformDeliverableCheckResult(
            deliverable_id="*", platform="*", status="fail",
            reason=f"could not read channel_operations_profile: {exc}",
        ))
        return result

    if profile is None:
        result.checks.append(PlatformDeliverableCheckResult(
            deliverable_id="*", platform="*", status="fail",
            reason=f"no channel_operations_profile configured for channel_id={channel_id!r}",
        ))
        return result

    policy = profile.platform_deliverable_policy or {}
    if not policy:
        result.checks.append(PlatformDeliverableCheckResult(
            deliverable_id="*", platform="*", status="fail",
            reason=(
                "no platform_deliverable_policy configured for this channel — cross-platform "
                "routing requires an explicit Channel Package policy, never a default matrix"
            ),
        ))
        return result

    episode_dir = Path(assets_root) / "episodes" / episode_id
    final_render_package = _load_render_package(episode_dir, episode_id)
    deliverable_ids = resolve_required_deliverable_ids(episode_id, assets_root)

    try:
        resolved_metadata = resolve_canonical_metadata(episode_id, episode_dir, db_path, metadata_override, final_render_package)
        metadata_error: Optional[MetadataResolutionError] = None
    except MetadataResolutionError as exc:
        resolved_metadata = None
        metadata_error = exc

    for deliverable_id in deliverable_ids:
        deliverable_class = classify_deliverable_class(deliverable_id)
        file_path = _deliverable_file_path(
            episode_dir, episode_id, deliverable_id, deliverable_ids, final_render_package,
        )

        for platform, platform_policy in policy.items():
            class_entry = (platform_policy or {}).get(deliverable_class, {})
            if not bool((class_entry or {}).get("enabled")):
                result.checks.append(PlatformDeliverableCheckResult(
                    deliverable_id=deliverable_id, platform=platform, status="skipped_not_enabled",
                    reason=(
                        f"platform_deliverable_policy does not enable platform={platform!r} "
                        f"for deliverable_class={deliverable_class!r}"
                    ),
                ))
                continue

            failures: List[str] = []
            evidence: dict = {}

            if metadata_error is not None:
                failures.append(f"canonical metadata unresolved: {metadata_error.reason}")

            binding = resolve_active_binding(
                db_path, channel_id=channel_id, capability="publishing",
                platform=platform, logical_alias=logical_alias,
            )
            if binding is None or binding.get("provider_id") != provider_id:
                failures.append(
                    f"no enabled channel_provider_bindings row for provider_id={provider_id!r}, platform={platform!r}"
                )

            if not file_path.exists():
                failures.append(f"deliverable file not found on disk: {file_path.name}")
            elif file_path.stat().st_size == 0:
                failures.append(f"deliverable file is empty (0 bytes): {file_path.name}")
            else:
                size_bytes = file_path.stat().st_size
                evidence["size_bytes"] = size_bytes
                if size_bytes > _ZERNIO_MAX_SIZE_BYTES:
                    failures.append(f"{size_bytes} bytes exceeds Zernio's documented media size limit")
                if "video/mp4" not in _ZERNIO_SUPPORTED_CONTENT_TYPES:
                    failures.append("video/mp4 is not in Zernio's documented supported content types")

            # contract is resolved for EVERY platform now (YouTube has its
            # own verified ZERNIO_PLATFORM_CONTRACTS entry, added
            # 2026-08-23) -- needed below by the generic canonical-
            # metadata check regardless of platform.
            contract = get_platform_contract(platform)
            if platform != "youtube":
                if contract is None or not contract.verified:
                    failures.append(f"no verified Zernio platform contract for platform={platform!r} — fail closed")
                else:
                    if contract.max_size_bytes is not None and file_path.exists():
                        size_bytes = file_path.stat().st_size
                        if size_bytes > contract.max_size_bytes:
                            failures.append(
                                f"{size_bytes} bytes exceeds {platform}'s documented max size "
                                f"({contract.max_size_bytes} bytes)"
                            )

                    if contract.duration_seconds_range is not None and file_path.exists():
                        duration = _probe_duration_seconds(file_path)
                        evidence["duration_seconds"] = duration
                        min_dur, max_dur = contract.duration_seconds_range
                        if duration <= 0:
                            failures.append(
                                f"could not measure a real duration for {file_path.name} (ffprobe returned "
                                f"0 or failed) — refusing to assume it satisfies {platform}'s "
                                f"{min_dur}-{max_dur}s requirement without real evidence"
                            )
                        elif not (min_dur <= duration <= max_dur):
                            failures.append(
                                f"{file_path.name} is {duration:.1f}s, outside {platform}'s documented "
                                f"{min_dur}-{max_dur}s duration range"
                            )

                    try:
                        captions = resolve_canonical_captions(final_render_package, [deliverable_id])
                        caption = captions[deliverable_id]
                        evidence["caption_length"] = len(caption)
                        if contract.caption_max_chars is not None and len(caption) > contract.caption_max_chars:
                            failures.append(
                                f"caption is {len(caption)} chars, exceeding {platform}'s documented "
                                f"{contract.caption_max_chars}-char limit — truncation is only permitted "
                                f"when explicitly defined by policy, which this channel has not configured"
                            )
                    except CaptionResolutionError as exc:
                        failures.append(f"caption unresolved: {exc.reason}")

                    if contract.privacy_required:
                        privacy_value = (class_entry or {}).get("privacy")
                        if not privacy_value:
                            failures.append(
                                f"platform {platform!r} requires {contract.privacy_field} to be set but "
                                f"platform_deliverable_policy has no 'privacy' value configured for "
                                f"({platform!r}, {deliverable_class!r})"
                            )
                        elif contract.privacy_allowed_values and privacy_value not in contract.privacy_allowed_values:
                            failures.append(
                                f"configured privacy value {privacy_value!r} is not one of "
                                f"{contract.privacy_allowed_values} for platform={platform!r}"
                            )
                        else:
                            evidence["privacy_value"] = privacy_value

                    if contract.required_policy_fields:
                        missing_policy_fields = [
                            f for f in contract.required_policy_fields if f not in (class_entry or {})
                        ]
                        if missing_policy_fields:
                            failures.append(
                                f"platform {platform!r} requires policy field(s) {missing_policy_fields} to be "
                                f"set but platform_deliverable_policy has no value configured for them on "
                                f"({platform!r}, {deliverable_class!r})"
                            )
                        else:
                            evidence["required_policy_fields"] = {
                                f: class_entry[f] for f in contract.required_policy_fields
                            }

            # Generic canonical-metadata contract, applied to EVERY
            # enabled (deliverable_id, platform) pair -- Shorts included,
            # never exempted (see canonical_metadata.py's own docstring).
            final_duration = _probe_duration_seconds(file_path) if file_path.exists() else None
            failures.extend(_check_canonical_platform_metadata(
                final_render_package=final_render_package, deliverable_id=deliverable_id,
                platform=platform, deliverable_class=deliverable_class, class_entry=class_entry,
                final_duration_seconds=final_duration if (final_duration or 0) > 0 else None,
                episode_dir=episode_dir, episode_id=episode_id, contract=contract,
            ))

            existing_status = _existing_intent_status(db_path, channel_id, episode_id, deliverable_id, platform)
            if existing_status in ("succeeded", "open"):
                failures.append(
                    f"an existing {existing_status!r} publication_intent already exists for this exact "
                    f"(episode_id={episode_id!r}, deliverable_id={deliverable_id!r}, platform={platform!r})"
                )
                evidence["existing_intent_status"] = existing_status

            result.checks.append(PlatformDeliverableCheckResult(
                deliverable_id=deliverable_id, platform=platform,
                status="fail" if failures else "pass",
                reason="; ".join(failures) if failures else "all cross-platform readiness checks passed",
                evidence=evidence,
            ))

    return result
