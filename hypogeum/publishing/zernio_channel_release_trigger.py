"""
hypogeum/publishing/zernio_channel_release_trigger.py — Connects
channel-scoped demand discovery (hypogeum.buffer) to the Zernio
release-flow orchestrator (hypogeum.publishing.zernio_release_flow), so a
channel's newly-completed, publish-ready bundles actually get submitted —
without ever re-submitting a bundle that already succeeded, across any
number of repeated polls or a worker restart.

Wired into run_worker.py's automatic loop as a new, explicitly-gated step
(run_worker.py::_process_channel_publishing(), called from
run_iteration() only when Config.AUTO_ZERNIO_PUBLISHING_ENABLED is set —
defaults off) — see that function's own docstring for the exact seam and
its channel-level fail-closed/exception-isolation contract. This module
itself still makes no automatic-trigger-cadence decision beyond "the
caller decides when to call me"; run_worker.py owns the cadence.

Idempotency, by construction (no new state of its own):
    1. Only items with hypogeum.buffer.ChannelDemandItem.already_published
       == False are ever attempted — and that flag is now correctly
       computed from BOTH the legacy tables and the ADR-013
       publication_intents.status='succeeded' signal (see hypogeum/buffer.py).
       A bundle that already succeeded through Zernio is therefore
       excluded from every subsequent call automatically.
    2. Even if demand discovery ever raced or double-counted an item,
       hypogeum.publishing.zernio_publish_runtime.publish_episode_package()
       (via ZernioMediaStagingProvider.stage()'s
       guard_and_prepare_staging_attempt() and ZernioProvider.publish()'s
       guard_and_prepare_attempt(), both pre-existing, both unmodified)
       durably commits a write-ahead record BEFORE any external call and
       refuses a second concurrent/duplicate attempt for the same
       (channel_id, episode_id, deliverable_id, provider). Repeated
       polling can therefore never produce two live Zernio submissions for
       the same deliverable.

Target platforms are resolved from the Channel Package itself
(ChannelOperationsProfile.enabled_platforms) unless the caller explicitly
overrides them — "Cortex reads each Channel Package's... platform
destinations" applied literally; a channel with no enabled platforms
configured is correctly treated as "nothing to publish", not an error.

Deliverable file paths are resolved by the same on-disk naming convention
hypogeum.publishing.readiness and hypogeum.agents.production_output_gate
already use (main_{episode_id}.mp4, short_{i}_{episode_id}.mp4) — this
convention is the shared Assembly Agent's own output shape, not
Hypogeum-specific. Deliverable_id (the ledger identity — staging attempt,
publication_intent, receipt) is a SEPARATE concern from file naming:
'main_episode' for the long-form, 'short_<short_id>' for each Short —
matching hypogeum.agents.publishing_agent._deliverable_id_for()'s own,
already-established, channel-agnostic convention exactly (never a new
scheme invented by this module) — see _resolve_short_deliverable_id()'s
own docstring for the one narrow, generic fallback it applies when a
render package's own short_id is missing or still a generation-time
placeholder.

Public API:
    ReadyBundle
    build_publish_package_from_episode_dir(channel_id, episode_id, bundle_id,
        assets_root, *, db_path=None, metadata_override=None) -> PublishPackage
    process_channel_ready_bundles(channel_id, bundle_definition, *,
        db_path=None, assets_root, platforms=None, provider_id="zernio",
        logical_alias="default", publish_now=False,
        metadata_override_by_item=None, current_cycle_item_ids=frozenset(),
        media_staging_provider=None, publishing_provider=None)
        -> list[ReadyBundle]
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence

from hypogeum.buffer import DeliverableBundleDefinition, gather_channel_demand_items, resolve_complete_item_ids_for_channel
from hypogeum.publishing.deliverable_ids import MAIN_EPISODE_DELIVERABLE_ID, classify_deliverable_class, resolve_short_deliverable_id
from hypogeum.publishing.media_staging import MediaStagingProvider
from hypogeum.publishing.provider import PublishingProvider
from hypogeum.publishing.readiness import (
    CaptionResolutionError,
    MetadataOverride,
    MetadataResolutionError,
    ThumbnailResolutionError,
    resolve_canonical_captions,
    resolve_canonical_main_thumbnail,
    resolve_canonical_metadata,
)
from hypogeum.publishing.cross_platform_readiness import check_cross_platform_readiness
from hypogeum.publishing.zernio_platform_contracts import build_platform_settings
from hypogeum.publishing.zernio_publish_runtime import DeliverableFile, PublishPackage
from hypogeum.publishing.zernio_release_flow import ReleasePublishResult, publish_channel_release


@dataclass
class ReadyBundle:
    item_id: str
    platform: str
    result: ReleasePublishResult


def build_publish_package_from_episode_dir(
    channel_id: str,
    episode_id: str,
    bundle_id: Optional[str],
    assets_root: Path,
    *,
    db_path: Optional[Path] = None,
    metadata_override: Optional[MetadataOverride] = None,
) -> PublishPackage:
    """
    Read-only. Constructs a PublishPackage from the standard on-disk
    deliverable-file convention — never invents a file that isn't there;
    hypogeum.publishing.readiness's own checks are what actually verify
    each file exists and is non-empty before any staging call is allowed
    to proceed. Physical file paths are always resolved by 0-indexed
    position (short_{i}_{episode_id}.mp4, matching Assembly Agent's own
    output convention); deliverable_id (the ledger identity, independent
    of file naming) is resolved by hypogeum.publishing.deliverable_ids —
    the SAME shared resolver hypogeum.buffer uses to determine bundle
    completeness, so the two can never disagree on identity.

    Each deliverable's title/description is populated via
    hypogeum.publishing.readiness.resolve_canonical_metadata() — the EXACT
    same resolution check_publish_readiness()'s A_METADATA gate already
    performs, reused here (never re-derived independently) so the real
    submitted title/description can never diverge from what readiness
    validated.

    Raises MetadataResolutionError (never fails open to title=None/
    description=None) when canonical release metadata cannot be resolved
    unambiguously — the autonomous production path must know explicitly
    that metadata is unresolved BEFORE any staging/provider call, not
    silently proceed with a Zernio-server-default title. Callers that
    process multiple items in a loop (e.g. process_channel_ready_bundles())
    catch this per item so one item's unresolved metadata never aborts
    another item's processing.
    """
    episode_dir = Path(assets_root) / "episodes" / episode_id
    render_path = episode_dir / f"final_render_package_{episode_id}.json"
    final_render_package: dict = {}
    if render_path.exists():
        try:
            final_render_package = json.loads(render_path.read_text(encoding="utf-8"))
        except Exception:
            final_render_package = {}
    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []

    resolved = resolve_canonical_metadata(episode_id, episode_dir, db_path, metadata_override, final_render_package)
    resolved_title, resolved_description, resolved_shorts_titles = resolved.title, resolved.description, resolved.shorts_titles

    # Best-effort, same discipline as title/description above: readiness's
    # own F_THUMBNAIL check (hypogeum.publishing.readiness.
    # check_publish_readiness()) is the actual gate that blocks BEFORE
    # this package is ever staged if the main episode requires a
    # thumbnail and none can be resolved -- this is not a second,
    # independent enforcement point, only best-effort population using
    # the EXACT SAME resolver readiness itself calls.
    resolved_thumbnail_path = None
    try:
        resolved_thumbnail_path = resolve_canonical_main_thumbnail(episode_id, episode_dir).local_path
    except ThumbnailResolutionError:
        pass

    # Best-effort caption resolution per Short, same discipline as
    # title/description/thumbnail above: cross_platform_readiness's own
    # per-(deliverable, platform) caption check is the actual gate that
    # blocks a Short from ever reaching staging on a non-YouTube platform
    # when its caption is unresolved. This is what makes the resolved
    # caption text actually REACH the real Zernio `content` field (via
    # DeliverableFile.description, the SAME field `_build_publish_body()`
    # already sends as `content`) instead of only existing inside a
    # separate, never-consulted readiness check.
    short_ids_in_order = [resolve_short_deliverable_id(shorts_renders[i], i) for i in range(len(shorts_renders))]
    resolved_captions: dict = {}
    for short_id in short_ids_in_order:
        try:
            resolved_captions.update(resolve_canonical_captions(final_render_package, [short_id]))
        except CaptionResolutionError:
            pass  # this specific Short's caption is unresolved -- readiness blocks it per-deliverable

    main_path = episode_dir / f"main_{episode_id}.mp4"
    return PublishPackage(
        channel_id=channel_id, episode_id=episode_id, bundle_id=bundle_id,
        main_episode=DeliverableFile(
            deliverable_id=MAIN_EPISODE_DELIVERABLE_ID, local_path=main_path, mime_type="video/mp4",
            media_type="video", filename=main_path.name,
            title=resolved_title, description=resolved_description,
            thumbnail_local_path=resolved_thumbnail_path,
        ),
        shorts=[
            DeliverableFile(
                deliverable_id=short_ids_in_order[i],
                local_path=episode_dir / f"short_{i}_{episode_id}.mp4",
                mime_type="video/mp4", media_type="video",
                filename=f"short_{i}_{episode_id}.mp4",
                title=resolved_shorts_titles.get(short_ids_in_order[i]),
                description=resolved_captions.get(short_ids_in_order[i]),
            )
            for i in range(len(shorts_renders))
        ],
    )


def _resolve_target_platforms(channel_id: str, db_path: Optional[Path]) -> List[str]:
    """Read-only. Empty list (never a global default) when no
    ChannelOperationsProfile exists or no platform is enabled."""
    from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415

    try:
        profile = get_channel_operations_profile(channel_id, db_path=db_path)
    except RuntimeError:
        return []
    if profile is None:
        return []
    if not profile.publishing_enabled:
        # Existing, generic ChannelOperationsProfile field -- the one
        # already-established per-channel "autonomous publishing wanted
        # right now" switch. Respecting it here means a channel can be
        # taken out of autonomous publishing without touching Factory Core
        # or run_worker.py at all.
        return []
    return sorted(p for p, enabled in (profile.enabled_platforms or {}).items() if enabled)


def _resolve_platform_deliverable_policy(channel_id: str, db_path: Optional[Path]) -> dict:
    """Read-only. Empty dict when no ChannelOperationsProfile exists or
    none is configured -- callers treat an empty policy as "no per-
    deliverable routing restriction configured" (see
    _allowed_deliverable_ids_for_platform()'s own docstring for the exact
    backward-compatible semantics this produces)."""
    from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415

    try:
        profile = get_channel_operations_profile(channel_id, db_path=db_path)
    except RuntimeError:
        return {}
    if profile is None:
        return {}
    return profile.platform_deliverable_policy or {}


def _allowed_deliverable_ids_for_platform(
    package: PublishPackage, platform: str, platform_deliverable_policy: dict,
) -> List[str]:
    """
    Which of `package`'s deliverable_ids are permitted on `platform`,
    per the channel's own platform_deliverable_policy
    (deliverable CLASS -- 'main_episode' | 'shorts', see
    hypogeum.publishing.deliverable_ids.classify_deliverable_class() --
    -> platform -> {"enabled": bool}).

    Backward compatible: an EMPTY platform_deliverable_policy (the
    default for a channel that has never configured one) means "no per-
    deliverable routing restriction" -- every deliverable in `package` is
    allowed on every requested platform, exactly as before this
    mechanism existed. A NON-empty policy is authoritative and fails
    closed on omission: a (deliverable_class, platform) combination
    genuinely absent from it is NOT allowed, never silently permitted.
    """
    all_ids = [d.deliverable_id for d in package.all_deliverables()]
    if not platform_deliverable_policy:
        return all_ids
    allowed = []
    for deliverable_id in all_ids:
        deliverable_class = classify_deliverable_class(deliverable_id)
        class_entry = (platform_deliverable_policy.get(platform) or {}).get(deliverable_class) or {}
        if bool(class_entry.get("enabled")):
            allowed.append(deliverable_id)
    return allowed


def _resolve_platform_settings_for_call(
    package: PublishPackage, platform: str, allowed_deliverable_ids: List[str], platform_deliverable_policy: dict,
) -> Optional[dict]:
    """
    Resolves the ONE platform-settings object (e.g. TikTok's
    {"privacy_level": ..., "content_preview_confirmed": True, ...})
    actually sent for this platform call — the exact same
    build_platform_settings() readiness's own privacy check validates
    against, so a readiness pass can never diverge from what the real
    request contains.

    One call submits one settings object shared by every allowed
    deliverable on this platform (not per-deliverable) — correct for
    every policy this codebase installs today, where every deliverable
    routed to a given non-YouTube platform shares one deliverable_class
    ('shorts'). Uses the FIRST allowed deliverable's own class/policy
    entry; a future Channel Package mixing deliverable classes with
    DIFFERENT settings on the SAME platform in one call is out of this
    function's scope (documented, not silently mishandled: it would
    apply the first deliverable's settings to all of them).
    """
    if not platform_deliverable_policy or not allowed_deliverable_ids:
        return None
    deliverable_class = classify_deliverable_class(allowed_deliverable_ids[0])
    policy_entry = (platform_deliverable_policy.get(platform) or {}).get(deliverable_class) or {}
    return build_platform_settings(platform, policy_entry)


def process_channel_ready_bundles(
    channel_id: str,
    bundle_definition: DeliverableBundleDefinition,
    *,
    db_path: Optional[Path] = None,
    assets_root: Path,
    platforms: Optional[Sequence[str]] = None,
    provider_id: str = "zernio",
    logical_alias: str = "default",
    publish_now: bool = False,
    metadata_override_by_item: Optional[Dict[str, MetadataOverride]] = None,
    current_cycle_item_ids: FrozenSet[str] = frozenset(),
    media_staging_provider: Optional[MediaStagingProvider] = None,
    publishing_provider: Optional[PublishingProvider] = None,
) -> List[ReadyBundle]:
    """
    Find this channel's not-yet-published, complete items for
    `bundle_definition` and attempt publish_channel_release() for each,
    on every enabled platform. Never re-attempts an item already
    recognized as published (see module docstring for the idempotency
    argument) — safe to call repeatedly, on every worker poll, without
    tracking any state of its own.

    platforms=None (default) resolves the channel's own enabled platforms
    from its Channel Package — never a global default, never guessed.
    An explicit platforms= override is accepted for tests/operator use.

    metadata_override_by_item: optional {item_id: MetadataOverride} — the
    one place a human-resolved title/description/shorts_titles decision
    for a SPECIFIC item is threaded through; this function never invents
    or chooses one on its own.
    """
    resolved_platforms = list(platforms) if platforms is not None else _resolve_target_platforms(channel_id, db_path)
    if not resolved_platforms:
        return []

    item_ids = resolve_complete_item_ids_for_channel(channel_id, db_path=db_path)
    demand_items = gather_channel_demand_items(
        bundle_definition, item_ids=item_ids, current_cycle_item_ids=current_cycle_item_ids,
        db_path=db_path, assets_root=assets_root, platforms=tuple(resolved_platforms), provider_id=provider_id,
    )
    metadata_override_by_item = metadata_override_by_item or {}
    platform_deliverable_policy = _resolve_platform_deliverable_policy(channel_id, db_path)

    results: List[ReadyBundle] = []
    for item in demand_items:
        if item.already_published:
            continue
        try:
            package = build_publish_package_from_episode_dir(
                channel_id, item.item_id, bundle_definition.bundle_id, assets_root,
                db_path=db_path, metadata_override=metadata_override_by_item.get(item.item_id),
            )
        except MetadataResolutionError as exc:
            # Fail closed, per item — never silently proceed with a
            # missing/ambiguous title (no fallback to a Zernio server
            # default), and never let one item's unresolved metadata
            # abort another item's processing in the same channel tick.
            for platform in resolved_platforms:
                results.append(ReadyBundle(
                    item_id=item.item_id, platform=platform,
                    result=ReleasePublishResult(
                        channel_id=channel_id, episode_id=item.item_id, platform=platform,
                        status="blocked_metadata", blocked_reason=exc.reason,
                    ),
                ))
            continue

        # Cross-platform readiness — called ONCE per item, BEFORE any
        # staging/intent/provider call for ANY platform below. A failed
        # (deliverable, platform) combination is removed from that
        # platform's allowed set entirely; it is never staged, never
        # uploaded, never given a publication_intent, never POSTed. This
        # is the actual write-path wiring for
        # hypogeum.publishing.cross_platform_readiness.
        # check_cross_platform_readiness() -- previously only reachable
        # as an independently-called checker, never consulted by this
        # real submission loop.
        cross_platform_passed_ids_by_platform: Dict[str, set] = {}
        if platform_deliverable_policy:
            cp_result = check_cross_platform_readiness(
                item.item_id, channel_id=channel_id, assets_root=assets_root, db_path=db_path,
                provider_id=provider_id, logical_alias=logical_alias,
                metadata_override=metadata_override_by_item.get(item.item_id),
            )
            for platform in resolved_platforms:
                cross_platform_passed_ids_by_platform[platform] = {
                    c.deliverable_id for c in cp_result.for_platform(platform) if c.status == "pass"
                }

        for platform in resolved_platforms:
            allowed_deliverable_ids = _allowed_deliverable_ids_for_platform(
                package, platform, platform_deliverable_policy,
            )
            if platform_deliverable_policy:
                allowed_deliverable_ids = [
                    d for d in allowed_deliverable_ids
                    if d in cross_platform_passed_ids_by_platform.get(platform, set())
                ]
            if not allowed_deliverable_ids:
                # No deliverable in this item is routed to this platform
                # by the Channel Package's own policy -- deliberately not
                # attempted (never a call, never an intent, never a
                # "blocked" result to reconcile).
                continue
            result = publish_channel_release(
                channel_id, item.item_id, platform, package,
                assets_root=assets_root, db_path=db_path,
                metadata_override=metadata_override_by_item.get(item.item_id),
                publish_now=publish_now, logical_alias=logical_alias, provider_id=provider_id,
                # Autonomous path: never silently fall back to the
                # channel's single legacy schedule slot -- every required
                # deliverable must have its own explicit
                # deliverable_schedule_policy entry, or this blocks closed
                # before any staging/provider call. See
                # compile_schedule_plan_from_channel_package()'s own
                # docstring for the manual-caller-only fallback this
                # deliberately disables here.
                require_explicit_schedule_policy=True,
                media_staging_provider=media_staging_provider, publishing_provider=publishing_provider,
                # Route ONLY the deliverables this platform is actually
                # enabled for, per platform_deliverable_policy -- see
                # _allowed_deliverable_ids_for_platform()'s own docstring.
                # An empty (unconfigured) policy allows every deliverable,
                # preserving the original whole-package-per-platform
                # behavior exactly.
                deliverable_ids=allowed_deliverable_ids if platform_deliverable_policy else None,
                # The exact settings object (e.g. TikTok's privacy_level)
                # readiness's own check validates against -- resolved from
                # the SAME platform_deliverable_policy, never re-derived
                # independently.
                platform_settings=_resolve_platform_settings_for_call(
                    package, platform, allowed_deliverable_ids, platform_deliverable_policy,
                ),
            )
            results.append(ReadyBundle(item_id=item.item_id, platform=platform, result=result))
    return results
