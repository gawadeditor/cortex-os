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
from hypogeum.publishing.deliverable_ids import MAIN_EPISODE_DELIVERABLE_ID, resolve_short_deliverable_id
from hypogeum.publishing.media_staging import MediaStagingProvider
from hypogeum.publishing.provider import PublishingProvider
from hypogeum.publishing.readiness import MetadataOverride, MetadataResolutionError, resolve_canonical_metadata
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
    validated. Best-effort only: this function never blocks or raises on
    an unresolvable title/description (a genuinely missing/ambiguous one
    is readiness's job to catch, always run first by
    publish_channel_release()) — on MetadataResolutionError, every
    deliverable simply carries title=None/description=None and Zernio's
    own documented server-side default applies (never a fabricated
    value).
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

    resolved_title: Optional[str] = None
    resolved_description: Optional[str] = None
    resolved_shorts_titles: dict = {}
    try:
        resolved = resolve_canonical_metadata(episode_id, episode_dir, db_path, metadata_override, final_render_package)
        resolved_title, resolved_description, resolved_shorts_titles = resolved.title, resolved.description, resolved.shorts_titles
    except MetadataResolutionError:
        pass

    main_path = episode_dir / f"main_{episode_id}.mp4"
    return PublishPackage(
        channel_id=channel_id, episode_id=episode_id, bundle_id=bundle_id,
        main_episode=DeliverableFile(
            deliverable_id=MAIN_EPISODE_DELIVERABLE_ID, local_path=main_path, mime_type="video/mp4",
            media_type="video", filename=main_path.name,
            title=resolved_title, description=resolved_description,
        ),
        shorts=[
            DeliverableFile(
                deliverable_id=resolve_short_deliverable_id(shorts_renders[i], i),
                local_path=episode_dir / f"short_{i}_{episode_id}.mp4",
                mime_type="video/mp4", media_type="video",
                filename=f"short_{i}_{episode_id}.mp4",
                title=resolved_shorts_titles.get(resolve_short_deliverable_id(shorts_renders[i], i)),
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

    results: List[ReadyBundle] = []
    for item in demand_items:
        if item.already_published:
            continue
        package = build_publish_package_from_episode_dir(
            channel_id, item.item_id, bundle_definition.bundle_id, assets_root,
            db_path=db_path, metadata_override=metadata_override_by_item.get(item.item_id),
        )
        for platform in resolved_platforms:
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
            )
            results.append(ReadyBundle(item_id=item.item_id, platform=platform, result=result))
    return results
