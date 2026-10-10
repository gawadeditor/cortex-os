"""
hypogeum/publishing/zernio_publish_runtime.py — Runtime glue: completed
episode package -> ZernioMediaStagingProvider.stage() -> MediaReference ->
ZernioProvider.publish(), for the long episode and all of its associated
Shorts as one explicit publish package.

This is new, additive orchestration only. It does not modify, call, or
share any code path with hypogeum.agents.publishing_agent.py's STEP
6/8/9/10 (the existing direct-adapter YouTube/TikTok/Meta publishing flow),
which remains completely unchanged and unaffected by this module's
existence — publishing_agent.py is never imported here, and this module is
never imported there.

Never invents a Shorts schedule. Publishing/scheduling intent for every
deliverable in the package is a human editorial decision — this module
requires an explicit, pre-resolved SchedulePlan covering every deliverable
about to be published; a missing plan, or one that does not cover every
deliverable, blocks BEFORE any staging or publish call is attempted (no
partial staging of some deliverables followed by a block on others).
Per-datetime validity (future, timezone-aware, non-contradictory
publish_now/is_draft) is enforced by ZernioProvider.publish() itself — not
re-implemented here, per the "use existing provider abstractions" contract.

Mandatory Factory isolation invariant: PublishPackage.channel_id and
SchedulePlan.channel_id are both REQUIRED and are both checked against the
`channel_id` argument passed to publish_episode_package() BEFORE any
staging or publish call — a package assembled for one channel, or a
schedule built for one channel, can never be silently published under a
different channel_id (ChannelScopeMismatchError, reported via
PublishPackageResult.blocked/.blocked_reason like every other pre-flight
gate in this module). Provider-binding isolation itself is enforced
downstream, unchanged, by hypogeum.publishing.provider_repository.
resolve_active_binding()'s existing channel_id-scoped lookup (ADR-013 —
not re-implemented here).

Public API:
    DeliverableSchedule, SchedulePlan
    MissingSchedulePlanError, InvalidSchedulePlanError, ChannelScopeMismatchError
    DeliverableFile, PublishPackage
    DeliverablePublishOutcome, PublishPackageResult
    publish_episode_package(package, *, platform, channel_id, db_path=None,
                             schedule_plan=None, media_staging_provider=None,
                             publishing_provider=None, logical_alias="default")
        -> PublishPackageResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.publishing.media_staging import (
    MediaStagingProvider,
    SourceMedia,
    compute_sha256,
    staged_result_to_media_reference,
)
from hypogeum.publishing.provider import PublishingProvider
from hypogeum.publishing.zernio import ZernioProvider
from hypogeum.publishing.zernio_media_staging import ZernioMediaStagingProvider


# ---------------------------------------------------------------------------
# Schedule plan — caller-supplied only, never invented here
# ---------------------------------------------------------------------------

class ZernioPublishRuntimeError(RuntimeError):
    """Base class for this module's own failures."""


class MissingSchedulePlanError(ZernioPublishRuntimeError):
    """No schedule_plan was supplied at all."""


class InvalidSchedulePlanError(ZernioPublishRuntimeError):
    """A schedule_plan was supplied but does not cover every deliverable in
    the package, or is not a SchedulePlan instance."""


class ChannelScopeMismatchError(ZernioPublishRuntimeError):
    """
    PublishPackage.channel_id or SchedulePlan.channel_id does not match the
    channel_id this publish_episode_package() call is asserting — the
    mandatory Factory isolation invariant this module enforces before any
    staging or publish call. A package or schedule built for one channel
    can never be silently used to publish under a different channel_id.
    """


@dataclass(frozen=True)
class DeliverableSchedule:
    """One deliverable's resolved publish timing — passed straight through
    to ZernioProvider.publish()/publish_multi_platform()'s own scheduling
    kwargs, unmodified.

    platform_scheduled_for_by_platform (2026-08-23): optional {platform:
    datetime} — when a platform is present here, its OWN value governs
    that platform's `platforms[].scheduledFor` entry in a multi-platform
    post, independently of every other platform in the same post and of
    this deliverable's own top-level scheduled_for/platform_scheduled_for.
    A platform absent from this dict falls back to platform_scheduled_for
    (if set) then scheduled_for, exactly as before this field existed —
    empty dict (default) is a complete no-op, byte-identical to prior
    behavior. This is what lets bundle cohesion (every deliverable's own
    base scheduled_for, preserved for release-order purposes) coexist
    with genuinely independent per-platform timestamps within one
    aggregated post, rather than collapsing every platform in a Short's
    post onto one generic timestamp.
    """
    scheduled_for: Optional[datetime] = None
    platform_scheduled_for: Optional[datetime] = None
    timezone_name: Optional[str] = None
    publish_now: bool = False
    is_draft: bool = False
    platform_scheduled_for_by_platform: Dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulePlan:
    """
    Maps deliverable_id -> DeliverableSchedule for every deliverable in a
    PublishPackage. Must be built by the caller from a resolved editorial
    calendar (a human decision) — this module never computes an offset,
    per-platform lead time, or default timing on the caller's behalf.

    channel_id is required: a Channel Package's own publishing schedule
    and timezone are declared per-channel (see module docstring's Factory
    isolation invariant); a SchedulePlan is only ever valid for the one
    channel it was built for, checked by publish_episode_package() before
    any staging or publish call.
    """
    channel_id: str
    entries: Dict[str, DeliverableSchedule] = field(default_factory=dict)

    def get(self, deliverable_id: str) -> Optional[DeliverableSchedule]:
        return self.entries.get(deliverable_id)


def _validate_schedule_plan_covers(schedule_plan, expected_deliverable_ids: List[str]) -> None:
    if schedule_plan is None:
        raise MissingSchedulePlanError(
            "schedule_plan is required — Shorts/episode publish timing is a human editorial "
            "decision and is never invented by this runtime"
        )
    if not isinstance(schedule_plan, SchedulePlan):
        raise InvalidSchedulePlanError(
            f"schedule_plan must be a SchedulePlan instance, got {type(schedule_plan).__name__}"
        )
    missing = [d for d in expected_deliverable_ids if d not in schedule_plan.entries]
    if missing:
        raise InvalidSchedulePlanError(
            f"schedule_plan is missing an entry for deliverable_id(s): {missing} — every "
            f"deliverable in the package must have a resolved schedule before any staging "
            f"or publish call is attempted"
        )


def _validate_channel_scope(package: "PublishPackage", schedule_plan: "SchedulePlan", channel_id: str) -> None:
    """
    Mandatory Factory isolation invariant, checked before any staging or
    publish call. Raises ChannelScopeMismatchError — never silently
    substitutes the asserted channel_id, never proceeds under a mismatch.
    """
    if package.channel_id != channel_id:
        raise ChannelScopeMismatchError(
            f"PublishPackage.channel_id={package.channel_id!r} does not match the asserted "
            f"channel_id={channel_id!r} for this publish_episode_package() call — refusing to "
            f"publish another channel's package under a different channel"
        )
    if schedule_plan.channel_id != channel_id:
        raise ChannelScopeMismatchError(
            f"SchedulePlan.channel_id={schedule_plan.channel_id!r} does not match the asserted "
            f"channel_id={channel_id!r} for this publish_episode_package() call — refusing to "
            f"apply another channel's schedule/timezone"
        )


# ---------------------------------------------------------------------------
# Publish package — one long episode + its Shorts, as local deliverable files
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliverableFile:
    deliverable_id: str  # e.g. 'main_episode', 'short_0'
    local_path: Path
    mime_type: str
    media_type: str  # 'image' | 'video' | 'gif' | 'document'
    filename: str
    thumbnail_url: Optional[str] = None
    title: Optional[str] = None
    alt_text: Optional[str] = None
    # This deliverable's own publication description/caption text — Zernio's
    # POST /v1/posts top-level `content` field, which for platform=youtube
    # becomes the video's description (verified via docs.zernio.com/posts/
    # create-post and docs.zernio.com/platforms/youtube). `title` above is
    # sent as the POST's top-level `title` (-> the YouTube video title, max
    # 100 chars) in ADDITION to its existing use as this media item's own
    # `mediaItems[].title`. Both were previously constructed but never
    # actually threaded into the request body at all -- see
    # ZernioProvider.publish()/_build_publish_body()'s own docstrings.
    description: Optional[str] = None
    # Local path to this deliverable's OWN canonical thumbnail image, not
    # yet staged (unlike thumbnail_url above, which is already a public
    # URL). publish_episode_package() stages this file separately from
    # the main media (its own SourceMedia/staged deliverable identity,
    # deliverable_id + '_thumbnail') and passes the resulting public URL
    # as thumbnail_url when building the MediaReference actually sent to
    # the provider. None means no thumbnail requirement for this
    # deliverable (e.g. every Short, by default) or one could not be
    # resolved -- readiness (hypogeum.publishing.readiness.
    # check_publish_readiness()'s F_THUMBNAIL check) is the actual gate
    # that blocks BEFORE this ever reaches staging; this field is
    # populated best-effort by the package builder, never a second,
    # independent enforcement point.
    thumbnail_local_path: Optional[Path] = None
    # Real production wiring for hypogeum.publishing.canonical_metadata
    # (2026-08-23, see docs/operations/audits/
    # ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md item 5): per-
    # platform platforms[].platformSpecificData dicts (keyed by
    # platform, e.g. {"youtube": {"categoryId": "27", ...}}), built by
    # build_publish_package_from_episode_dir() from each platform's own
    # resolved CanonicalPlatformMetadata via
    # canonical_metadata.build_zernio_platform_specific_data(). Empty
    # dict (default) when no canonical metadata was resolved for any
    # platform -- never invented here.
    platform_specific_data: Dict[str, dict] = field(default_factory=dict)
    # Root-level, non-platform-specific Zernio request fields (tags/
    # hashtags -- see canonical_metadata.build_zernio_root_fields()).
    root_fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PublishPackage:
    """
    The long episode and all of its associated Shorts, published as one
    explicit unit — never partially assembled by this module.

    channel_id is required: provenance and the Factory isolation invariant
    (checked against publish_episode_package()'s own channel_id argument
    before any staging/publish call — see ChannelScopeMismatchError).
    bundle_id is optional provenance identifying which channel-declared
    bundle/production-unit (hypogeum.buffer.DeliverableBundleDefinition —
    e.g. 'main_episode_bundle', 'short') this package instance fulfills;
    carried for traceability only, never interpreted by this module.
    """
    channel_id: str
    episode_id: str
    main_episode: DeliverableFile
    shorts: List[DeliverableFile] = field(default_factory=list)
    bundle_id: Optional[str] = None

    def all_deliverables(self) -> List[DeliverableFile]:
        return [self.main_episode, *self.shorts]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DeliverablePublishOutcome:
    deliverable_id: str
    status: str  # "published" | "partial" | "ambiguous" | "blocked"
    stage_result: Optional[dict] = None
    # 2026-08-22 (Zernio aggregation model): one deliverable is staged and
    # submitted to ALL its requested platforms via ONE provider call
    # (ZernioProvider.publish_multi_platform() when the injected provider
    # supports it), never one call per platform. platform_results carries
    # that call's own per-platform result dict (the exact same shape
    # publish_result used to hold for a single platform), keyed by
    # platform -- e.g. {"youtube": {...}, "tiktok": {...}, ...}. Every
    # platform-specific local publication_intent/publishing_attempt/
    # publishing_receipt this produces still references the SAME shared
    # provider_publication_id -- see ZernioProvider.publish_multi_platform()'s
    # own docstring. publish_result (singular) is kept for a provider that
    # does NOT support publish_multi_platform (the single-platform
    # fallback path) -- exactly one platform, exactly one result, unchanged
    # from before this field existed.
    platform_results: Dict[str, dict] = field(default_factory=dict)
    publish_result: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class PublishPackageResult:
    episode_id: str
    platforms: List[str]
    blocked: bool
    blocked_reason: Optional[str] = None
    deliverables: List[DeliverablePublishOutcome] = field(default_factory=list)

    @property
    def all_published(self) -> bool:
        if not self.deliverables:
            return False
        for d in self.deliverables:
            if d.platform_results:
                if not all(r.get("status") == "published" for r in d.platform_results.values()):
                    return False
            elif d.status != "published":
                return False
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _route_platforms_by_binding(
    platforms: List[str], *, channel_id: str, logical_alias: str, db_path: Optional[Path],
) -> List[tuple]:
    """
    [(provider, [platforms]), ...] — each platform goes to the provider its
    own channel_provider_bindings row names, so a channel can move to Postiz
    one platform at a time. Anything not bound to 'postiz' (including no
    binding at all) stays on ZernioProvider, which keeps its own fail-closed
    binding checks — i.e. exactly the behaviour before Postiz existed.
    Order of first appearance is preserved for deterministic calls.
    """
    from hypogeum.publishing.provider_repository import resolve_active_binding

    groups: Dict[str, List[str]] = {}
    for platform in platforms:
        binding = resolve_active_binding(
            db_path, channel_id=channel_id, capability="publishing",
            platform=platform, logical_alias=logical_alias,
        )
        provider_id = "postiz" if binding and binding.get("provider_id") == "postiz" else "zernio"
        groups.setdefault(provider_id, []).append(platform)
    routed = []
    for provider_id, group in groups.items():
        if provider_id == "postiz":
            from hypogeum.publishing.postiz import PostizProvider
            routed.append((PostizProvider(), group))
        else:
            routed.append((ZernioProvider(), group))
    return routed


def publish_episode_package(
    package: PublishPackage,
    *,
    platforms: List[str],
    channel_id: str,
    db_path: Optional[Path] = None,
    schedule_plan: Optional[SchedulePlan] = None,
    media_staging_provider: Optional[MediaStagingProvider] = None,
    publishing_provider: Optional[PublishingProvider] = None,
    logical_alias: str = "default",
    deliverable_ids: Optional[List[str]] = None,
    platform_settings: Optional[Dict[str, dict]] = None,
) -> PublishPackageResult:
    """
    Stage and publish every deliverable in `package` (main episode + every
    Short) to every platform in `platforms`, through the existing generic
    provider abstractions — no local orchestration bypasses
    hypogeum.publishing.media_staging's guard_and_prepare_staging_attempt()
    write-ahead boundary or hypogeum.publishing.submission_ledger's
    equivalent inside ZernioProvider.publish()/publish_multi_platform();
    this module only sequences calls into them.

    2026-08-22 (Zernio aggregation model): each deliverable is staged
    ONCE (not once per platform) and submitted to ALL of `platforms` via
    ONE provider call — publisher.publish_multi_platform(platforms=...)
    when the injected provider exposes it (ZernioProvider does), so one
    Short targeting 4 platforms produces exactly one provider POST/PUT,
    never 4. A provider that does NOT expose publish_multi_platform (any
    non-Zernio PublishingProvider implementation) falls back to the
    original one-call-per-platform loop via .publish() — this preserves
    the generic PublishingProvider contract exactly for every other
    provider; nothing here assumes every provider supports aggregation.

    Defaults to real ZernioMediaStagingProvider()/ZernioProvider() — both
    construct without making any network call (matching their own
    contracts) and only make a real HTTP call when actually invoked. Every
    test in this slice injects fakes via media_staging_provider/
    publishing_provider instead.

    Blocks BEFORE any staging or publish call — for the whole package, not
    per-deliverable — when schedule_plan is missing or does not cover every
    deliverable_id in the package (MissingSchedulePlanError /
    InvalidSchedulePlanError), OR when package.channel_id or
    schedule_plan.channel_id does not match the `channel_id` argument
    (ChannelScopeMismatchError — the mandatory Factory isolation
    invariant). All three are caught here and reported via
    PublishPackageResult.blocked/.blocked_reason rather than raised, so a
    caller can inspect the result uniformly.

    Per-deliverable failures (staging or publish) do not stop the loop —
    every deliverable in the package is always attempted once the
    package-level schedule-plan gate passes; a partial-failure package
    reports each deliverable's own outcome for the caller to reconcile.

    deliverable_ids: optional filter. None (default) preserves the
    original, still-supported behavior — every deliverable in `package`
    is attempted. When given, ONLY deliverables whose deliverable_id is
    in this list are staged/published for this call; every other
    deliverable in `package` is silently excluded from THIS call (not
    reported as blocked/failed — it was never requested in the first
    place). This is what lets a caller (hypogeum.publishing.
    zernio_channel_release_trigger.process_channel_ready_bundles()) route
    the main episode to one platform-set and Shorts to a different
    platform-set from the SAME PublishPackage, without submitting
    deliverables a Channel Package's platform_deliverable_policy never
    enabled for that combination. schedule_plan coverage is validated
    against this SAME filtered set, not the full package.

    platform_settings: {platform: settings_dict}, e.g. {"tiktok": {...}}
    -- matches publish_multi_platform()'s own shape. Ignored by the
    single-platform .publish() fallback path (that provider contract
    never had a per-platform settings concept).

    Provider routing (2026-10-03, Postiz migration): when no
    publishing_provider is injected, each platform is routed by its own
    binding (_route_platforms_by_binding) -- Postiz-bound platforms go to
    PostizProvider, everything else to ZernioProvider as before. With a
    single provider group the behaviour is exactly the previous one; with
    two groups, one group raising marks only ITS platforms failed.
    """
    if not platforms:
        return PublishPackageResult(
            episode_id=package.episode_id, platforms=[],
            blocked=True, blocked_reason="publish_episode_package() requires at least one platform",
            deliverables=[],
        )

    all_deliverables = package.all_deliverables()
    deliverables = (
        all_deliverables if deliverable_ids is None
        else [d for d in all_deliverables if d.deliverable_id in deliverable_ids]
    )
    expected_ids = [d.deliverable_id for d in deliverables]

    try:
        _validate_schedule_plan_covers(schedule_plan, expected_ids)
        _validate_channel_scope(package, schedule_plan, channel_id)
    except ZernioPublishRuntimeError as exc:
        return PublishPackageResult(
            episode_id=package.episode_id, platforms=platforms,
            blocked=True, blocked_reason=str(exc), deliverables=[],
        )

    if publishing_provider is not None:
        routed = [(publishing_provider, list(platforms))]
    else:
        routed = _route_platforms_by_binding(
            platforms, channel_id=channel_id, logical_alias=logical_alias, db_path=db_path,
        )
    if media_staging_provider is not None:
        staging = media_staging_provider
    elif publishing_provider is None and all(type(p).__name__ == "PostizProvider" for p, _ in routed):
        # Every platform publishes through Postiz: stage straight into its own
        # media library, no Zernio presigned upload (and no Zernio dependency).
        from hypogeum.publishing.postiz_media_staging import PostizMediaStagingProvider
        staging = PostizMediaStagingProvider()
    else:
        staging = ZernioMediaStagingProvider()
    publisher = routed[0][0]
    supports_aggregation = all(hasattr(p, "publish_multi_platform") for p, _ in routed)
    if not supports_aggregation and len(routed) > 1:
        raise ZernioPublishRuntimeError(
            "mixed provider routing requires every provider to support publish_multi_platform"
        )

    outcomes: List[DeliverablePublishOutcome] = []
    for deliverable in deliverables:
        sched = schedule_plan.get(deliverable.deliverable_id)

        try:
            source = SourceMedia(
                local_path=deliverable.local_path,
                sha256=compute_sha256(deliverable.local_path),
                size_bytes=deliverable.local_path.stat().st_size,
                mime_type=deliverable.mime_type,
                media_type=deliverable.media_type,
                filename=deliverable.filename,
                channel_id=channel_id,
                episode_id=package.episode_id,
                deliverable_id=deliverable.deliverable_id,
            )
            staged = staging.stage(source, db_path=db_path)
        except Exception as exc:
            outcomes.append(DeliverablePublishOutcome(
                deliverable_id=deliverable.deliverable_id, status="blocked", error=str(exc),
            ))
            continue

        # A canonical thumbnail resolved by the package builder (best-
        # effort; readiness's F_THUMBNAIL check is the actual gate that
        # blocked BEFORE this call if one was required and missing) is
        # staged as its OWN distinct deliverable identity
        # (f"{deliverable_id}_thumbnail") -- a separate media upload from
        # the video itself, never a re-upload of the video, and never
        # skipped silently if staging fails: a thumbnail staging failure
        # blocks this deliverable exactly like a main-media staging
        # failure would.
        resolved_thumbnail_url = deliverable.thumbnail_url
        if deliverable.thumbnail_local_path is not None:
            try:
                thumb_source = SourceMedia(
                    local_path=deliverable.thumbnail_local_path,
                    sha256=compute_sha256(deliverable.thumbnail_local_path),
                    size_bytes=deliverable.thumbnail_local_path.stat().st_size,
                    mime_type="image/jpeg", media_type="image",
                    filename=deliverable.thumbnail_local_path.name,
                    channel_id=channel_id, episode_id=package.episode_id,
                    deliverable_id=f"{deliverable.deliverable_id}_thumbnail",
                )
                staged_thumbnail = staging.stage(thumb_source, db_path=db_path)
                resolved_thumbnail_url = staged_thumbnail.public_url
            except Exception as exc:
                outcomes.append(DeliverablePublishOutcome(
                    deliverable_id=deliverable.deliverable_id, status="blocked",
                    stage_result=_staged_to_dict(staged), error=f"thumbnail staging failed: {exc}",
                ))
                continue

        media_ref = staged_result_to_media_reference(
            staged, media_type=deliverable.media_type,
            thumbnail_url=resolved_thumbnail_url, title=deliverable.title,
            alt_text=deliverable.alt_text,
        )
        deliverable_type = "main_episode" if deliverable is package.main_episode else "short"

        # Per-platform cover wiring. media_ref.thumbnail_url already lands on
        # mediaItems[].thumbnail (YouTube's documented cover field). TikTok
        # and Instagram Reels take the cover from their OWN documented
        # fields instead (see hypogeum.publishing.zernio_platform_contracts):
        #   tiktok    -> tiktokSettings.video_cover_image_url  (root settings)
        #   instagram -> platformSpecificData.instagramThumbnail  (per entry)
        # Facebook Reels has no documented custom-cover field. Copies are
        # made per deliverable so the shared caller dicts are never mutated.
        _eff_platform_settings = dict(platform_settings or {})
        _eff_platform_specific = dict(deliverable.platform_specific_data or {})
        if resolved_thumbnail_url:
            if "tiktok" in platforms:
                _tk = dict(_eff_platform_settings.get("tiktok") or {})
                _tk.setdefault("video_cover_image_url", resolved_thumbnail_url)
                _eff_platform_settings["tiktok"] = _tk
            if "instagram" in platforms:
                _ig = dict(_eff_platform_specific.get("instagram") or {})
                _ig.setdefault("instagramThumbnail", resolved_thumbnail_url)
                _eff_platform_specific["instagram"] = _ig

        if supports_aggregation:
            platform_results = {}
            group_error = None
            for group_publisher, group_platforms in routed:
                # Postiz cannot change a YouTube video's privacy after upload, so a
                # vertical (Short) cover is held by uploading private early; the adapter
                # records the hold and the cover-hold sweep flips it public later.
                _extra = {}
                if type(group_publisher).__name__ == "PostizProvider" and "youtube" in group_platforms \
                        and _has_vertical_cover(deliverable):
                    _extra["platform_options"] = {"youtube": {"cover_hold": True}}
                try:
                    platform_results.update(group_publisher.publish_multi_platform(
                        platforms=group_platforms,
                        deliverable_type=deliverable_type,
                        episode_id=package.episode_id,
                        channel_id=channel_id,
                        deliverable_id=deliverable.deliverable_id,
                        db_path=db_path,
                        logical_alias=logical_alias,
                        media=[media_ref],
                        title=deliverable.title,
                        content=deliverable.description,
                        scheduled_for=sched.scheduled_for,
                        platform_scheduled_for=sched.platform_scheduled_for,
                        platform_scheduled_for_by_platform=sched.platform_scheduled_for_by_platform,
                        timezone_name=sched.timezone_name,
                        publish_now=sched.publish_now,
                        is_draft=sched.is_draft,
                        platform_settings=_eff_platform_settings,
                        platform_specific_data=_eff_platform_specific,
                        root_fields=deliverable.root_fields,
                        **_extra,
                    ))
                except Exception as exc:
                    if len(routed) == 1:
                        group_error = exc
                        break
                    for gp in group_platforms:
                        platform_results[gp] = {"platform": gp, "deliverable_type": deliverable_type,
                                                "status": "failed", "error": str(exc)}
            if group_error is not None:
                outcomes.append(DeliverablePublishOutcome(
                    deliverable_id=deliverable.deliverable_id, status="blocked",
                    stage_result=_staged_to_dict(staged), error=str(group_error),
                ))
                continue

            statuses = {r.get("status", "ambiguous") for r in platform_results.values()}
            overall = "published" if statuses == {"published"} else (
                "partial" if "published" in statuses else "ambiguous"
            )
            outcomes.append(DeliverablePublishOutcome(
                deliverable_id=deliverable.deliverable_id,
                status=overall,
                stage_result=_staged_to_dict(staged),
                platform_results=platform_results,
            ))
            continue

        # Fallback: provider does not support aggregation -- one .publish()
        # call per platform, exactly the pre-2026-08-22 behavior, just
        # scoped to this one deliverable's single staged media instead of
        # re-staging per platform.
        per_platform_results: Dict[str, dict] = {}
        stage_failed = False
        for platform in platforms:
            try:
                per_platform_results[platform] = publisher.publish(
                    platform=platform,
                    deliverable_type=deliverable_type,
                    episode_id=package.episode_id,
                    channel_id=channel_id,
                    deliverable_id=deliverable.deliverable_id,
                    db_path=db_path,
                    logical_alias=logical_alias,
                    media=[media_ref],
                    title=deliverable.title,
                    content=deliverable.description,
                    scheduled_for=sched.scheduled_for,
                    platform_scheduled_for=(
                        sched.platform_scheduled_for_by_platform.get(platform)
                        or sched.platform_scheduled_for
                    ),
                    timezone_name=sched.timezone_name,
                    publish_now=sched.publish_now,
                    is_draft=sched.is_draft,
                    platform_settings=(platform_settings or {}).get(platform),
                )
            except Exception as exc:
                outcomes.append(DeliverablePublishOutcome(
                    deliverable_id=deliverable.deliverable_id, status="blocked",
                    stage_result=_staged_to_dict(staged), error=str(exc),
                ))
                stage_failed = True
                break
        if stage_failed:
            continue

        statuses = {r.get("status", "ambiguous") for r in per_platform_results.values()}
        overall = "published" if statuses == {"published"} else (
            "partial" if "published" in statuses else "ambiguous"
        )
        outcomes.append(DeliverablePublishOutcome(
            deliverable_id=deliverable.deliverable_id,
            status=overall,
            stage_result=_staged_to_dict(staged),
            platform_results=per_platform_results,
        ))

    return PublishPackageResult(
        episode_id=package.episode_id, platforms=platforms,
        blocked=False, deliverables=outcomes,
    )


def _has_vertical_cover(deliverable) -> bool:
    """Does this deliverable have a vertical (Short) cover, i.e. must its YouTube upload be held private
    until the operator sets the cover in Studio? The package's own thumbnail path is authoritative, but a
    salvaged / re-assembled episode can lose that reference (2026-10-10: ep_014876fc went public with no
    hold), so fall back to the designed cover that sits next to the episode's video:
    <episode dir>/thumbnails/<deliverable_id>.jpg."""
    if _is_vertical_image(getattr(deliverable, "thumbnail_local_path", None)):
        return True
    try:
        from pathlib import Path  # noqa: PLC0415
        video = getattr(deliverable, "local_path", None)
        if video is not None:
            return _is_vertical_image(Path(video).parent / "thumbnails" / f"{deliverable.deliverable_id}.jpg")
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_vertical_image(path) -> bool:
    """True when the cover image is portrait (a Short's cover). False on any doubt."""
    if path is None:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.height > im.width
    except Exception:  # noqa: BLE001
        return False


def _staged_to_dict(staged) -> dict:
    return {
        "media_staging_provider_id": staged.media_staging_provider_id,
        "public_url": staged.public_url,
        "status": staged.status,
        "remote_object_id": staged.remote_object_id,
    }
