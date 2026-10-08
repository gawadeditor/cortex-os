"""
hypogeum/publishing/zernio_release_flow.py — Operator-facing orchestration:
ties hypogeum.publishing.readiness, hypogeum.publishing.schedule_compiler,
and hypogeum.publishing.zernio_publish_runtime together into the one
callable operation "publish this channel's one completed release bundle
through Zernio, scheduled into its next real configured slot."

This is the missing glue identified as the smallest remaining gap in the
already-approved slice: readiness, schedule compilation, and the stage/
publish runtime each already existed as separate, independently-tested,
single-purpose modules, but nothing tied them into one sequenced
operation. This module still owns none of their logic — it only
sequences three existing calls, each gating the next.

NOT wired into run_worker.py, hypogeum.agents.publishing_agent, or any
automatic trigger — this is a deliberate, explicit, single-call entry
point an operator (or a future, separately-authorized automatic trigger)
invokes for exactly one (channel_id, episode_id, platform) at a time.
Deciding WHEN in the pipeline lifecycle Zernio submission should fire
automatically is a distinct operational decision this module does not
make.

Sequencing — never partially skips a check, never proceeds past a
blocked stage:
    1. check_publish_readiness()                     — fails closed;
       blocks before any staging/publish call.
    2. compile_schedule_plan_from_channel_package()   — fails closed on
       missing/invalid channel schedule config; never invents a time.
    3. publish_episode_package()                      — stages + publishes
       via the existing generic provider abstractions. Defaults to real
       ZernioMediaStagingProvider()/ZernioProvider() (matching that
       module's own contract — construction alone makes no call); every
       test in this slice injects fakes instead.

Public API:
    ReleasePublishResult
    publish_channel_release(channel_id, episode_id, platform, package, *,
                             assets_root, db_path=None, metadata_override=None,
                             publish_now=False, logical_alias="default",
                             provider_id="zernio",
                             media_staging_provider=None, publishing_provider=None)
        -> ReleasePublishResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.publishing.media_staging import MediaStagingProvider
from hypogeum.publishing.provider import PublishingProvider
from hypogeum.publishing.readiness import MetadataOverride, ReadinessCheckResult, check_publish_readiness
from hypogeum.publishing.schedule_compiler import ScheduleCompilationResult, compile_schedule_plan_from_channel_package
from hypogeum.publishing.zernio_publish_runtime import PublishPackage, PublishPackageResult, publish_episode_package


@dataclass
class ReleasePublishResult:
    channel_id: str
    episode_id: str
    platforms: List[str]
    status: str  # "blocked_readiness" | "blocked_schedule" | "blocked_publish" | "published" | "partial"
    readiness_by_platform: Dict[str, ReadinessCheckResult] = field(default_factory=dict)
    schedule: Optional[ScheduleCompilationResult] = None
    publish: Optional[PublishPackageResult] = None
    blocked_reason: Optional[str] = None
    blocked_platforms: List[str] = field(default_factory=list)


def publish_channel_release(
    channel_id: str,
    episode_id: str,
    platforms: List[str],
    package: PublishPackage,
    *,
    assets_root: Path,
    db_path: Optional[Path] = None,
    metadata_override: Optional[MetadataOverride] = None,
    publish_now: bool = False,
    logical_alias: str = "default",
    provider_id: str = "zernio",
    require_explicit_schedule_policy: bool = False,
    media_staging_provider: Optional[MediaStagingProvider] = None,
    publishing_provider: Optional[PublishingProvider] = None,
    now: Optional[datetime] = None,
    deliverable_ids: Optional[List[str]] = None,
    platform_settings: Optional[Dict[str, dict]] = None,
) -> ReleasePublishResult:
    """
    Read-only through stages 1–2; stage 3 is the only stage that can ever
    write anything (staging attempts, publishing attempts/receipts — all
    via the pre-existing write-ahead boundaries those modules already
    own), and only after both prior gates pass.

    2026-08-22 (Zernio aggregation model): `platforms` (plural) — one
    deliverable-group release now targets every platform in this list via
    ONE staging pass and ONE provider call (see publish_episode_package()'s
    own docstring), never one call per platform.

    check_publish_readiness() is still called ONCE PER PLATFORM in
    `platforms`, never once with the whole list combined — its
    F_THUMBNAIL check ("main-episode thumbnail required whenever
    'youtube' is among the requested platforms") is scoped to the
    EPISODE, not to which deliverables this specific call is actually
    submitting, so combining e.g. youtube+tiktok+instagram+facebook into
    one readiness call would incorrectly let an unapproved YouTube
    main-episode thumbnail block a Facebook Shorts-only submission that
    has nothing to do with YouTube. Each platform's own readiness result
    is preserved in readiness_by_platform; a platform that fails
    readiness is excluded from the aggregated publish call entirely
    (never partially staged) and reported in blocked_platforms — it does
    NOT block the platforms that passed.

    channel_id is required and flows unchanged into every stage — this
    function performs no channel-scope resolution of its own beyond what
    each stage already enforces (CHANNEL_SCOPE in readiness,
    ChannelScopeMismatchError in the publish runtime).

    require_explicit_schedule_policy=False (default) preserves the
    schedule compiler's own backward-compatible fallback-to-single-slot
    behavior — the right default for a manual/legacy caller. Autonomous
    callers (hypogeum.publishing.zernio_channel_release_trigger) pass
    True, disabling that fallback entirely — see
    compile_schedule_plan_from_channel_package()'s own docstring.

    now is forwarded unchanged to compile_schedule_plan_from_channel_package()
    (None means "real wall clock", the default for every real caller).
    Exposed here only so a test/replay caller can deterministically prove
    that a schedule recomputed with a LATER now (simulating time passing
    across a worker restart) never moves an already-persisted attempt —
    the pre-existing hypogeum.publishing.submission_ledger write-ahead
    guard (invoked inside stage 3's publisher.publish()/publish_multi_platform())
    refuses a second external call once an attempt already exists for this
    (channel_id, episode_id, deliverable_id, platform, provider_id), so a
    freshly recomputed (and possibly different) schedule_result.slot is
    never actually submitted a second time.

    deliverable_ids: optional filter forwarded unchanged to
    publish_episode_package() and used (instead of every deliverable in
    `package`) to determine which deliverables' slots this call needs
    from the schedule compiler — None (default) preserves the original
    behavior of covering the whole package. This is the mechanism that
    lets a caller route the main episode to one platform-set and Shorts
    to a different platform-set from the same PublishPackage (see
    hypogeum.publishing.zernio_channel_release_trigger.
    process_channel_ready_bundles()).
    """
    if not platforms:
        return ReleasePublishResult(
            channel_id=channel_id, episode_id=episode_id, platforms=[],
            status="blocked_readiness", blocked_reason="no platforms requested",
        )

    readiness_by_platform: Dict[str, ReadinessCheckResult] = {}
    passed_platforms: List[str] = []
    blocked_platforms: List[str] = []
    for platform in platforms:
        readiness = check_publish_readiness(
            episode_id, channel_id=channel_id, assets_root=assets_root, db_path=db_path,
            platforms=(platform,), logical_alias=logical_alias, provider_id=provider_id,
            metadata_override=metadata_override,
        )
        readiness_by_platform[platform] = readiness
        if readiness.passed:
            passed_platforms.append(platform)
        else:
            blocked_platforms.append(platform)

    if not passed_platforms:
        failed = {p: [c.capability for c in r.checks if c.status == "fail"] for p, r in readiness_by_platform.items()}
        return ReleasePublishResult(
            channel_id=channel_id, episode_id=episode_id, platforms=platforms,
            status="blocked_readiness", readiness_by_platform=readiness_by_platform,
            blocked_reason=f"readiness check failed for every requested platform: {failed}",
            blocked_platforms=blocked_platforms,
        )

    target_deliverable_ids = (
        deliverable_ids if deliverable_ids is not None
        else [d.deliverable_id for d in package.all_deliverables()]
    )
    # Produce-ahead buffer: if the caller didn't pin `now` and this is a
    # scheduled (not publish-now) release, advance the reference time past
    # any slot already handed to another finished-but-unpublished episode of
    # this channel, so back-to-back releases land on consecutive days
    # instead of colliding on the same "today/tomorrow at time_of_day" slot.
    _compile_now = now
    if _compile_now is None and not publish_now:
        try:
            from hypogeum.publishing.publish_slot_ledger import next_free_now
            _compile_now = next_free_now(channel_id, datetime.now(timezone.utc), db_path=db_path)
        except Exception:  # noqa: BLE001
            _compile_now = now

    schedule_result = compile_schedule_plan_from_channel_package(
        channel_id, target_deliverable_ids, db_path=db_path, publish_now=publish_now,
        require_explicit_policy=require_explicit_schedule_policy, now=_compile_now,
    )
    if schedule_result.blocked:
        return ReleasePublishResult(
            channel_id=channel_id, episode_id=episode_id, platforms=platforms,
            status="blocked_schedule", readiness_by_platform=readiness_by_platform, schedule=schedule_result,
            blocked_reason=schedule_result.blocked_reason, blocked_platforms=blocked_platforms,
        )
    # Roll forward over any day that's already taken (belt-and-suspenders for
    # next_free_now's gap scan: the today→tomorrow edge, or a slot recorded
    # between that scan and here). Without this a compiled slot landing on a
    # taken day just blocks the release and the episode never publishes.
    if not publish_now and schedule_result.slot is not None:
        try:
            from hypogeum.publishing.publish_slot_ledger import day_is_taken as _day_taken
            _cursor = schedule_result.slot
            for _ in range(14):
                if _day_taken(channel_id, _cursor, episode_id, db_path=db_path) is None:
                    break
                _next_day = (_cursor.astimezone(timezone.utc) + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
                _rolled = compile_schedule_plan_from_channel_package(
                    channel_id, target_deliverable_ids, db_path=db_path, publish_now=False,
                    require_explicit_policy=require_explicit_schedule_policy, now=_next_day,
                )
                if _rolled.blocked or _rolled.slot is None:
                    break
                schedule_result, _cursor = _rolled, _rolled.slot
        except Exception:  # noqa: BLE001
            pass
    # One-episode-per-day-per-channel — enforced for a scheduled release
    # (schedule_result.slot) AND for a publish_now release (its "slot" is
    # today). A publish_now that bypassed this check is exactly how a
    # channel ended up with two posts on 2026-09-05 (a forced republish
    # landing on a day that already had a scheduled/published episode);
    # publish_now is a release for TODAY and must respect the daily cap
    # the same as any other.
    _guard_slot = schedule_result.slot
    if publish_now and _guard_slot is None:
        _guard_slot = (now or datetime.now(timezone.utc))
    if _guard_slot is not None:
        try:
            from hypogeum.publishing.publish_slot_ledger import day_is_taken, record_slot
            clash = day_is_taken(channel_id, _guard_slot, episode_id, db_path=db_path)
            if clash:
                return ReleasePublishResult(
                    channel_id=channel_id, episode_id=episode_id, platforms=platforms,
                    status="blocked_schedule", readiness_by_platform=readiness_by_platform,
                    schedule=schedule_result, blocked_platforms=blocked_platforms,
                    blocked_reason=(
                        f"channel '{channel_id}' already has episode '{clash}' publishing on "
                        f"{str(_guard_slot)[:10]} — refusing a second release for the same day "
                        f"(one episode per day per channel; applies to publish_now too)"
                    ),
                )
            record_slot(channel_id, _guard_slot, episode_id, db_path=db_path)
        except Exception:  # noqa: BLE001
            pass

    # A per-platform override slot (evidence-based, e.g. YouTube "Saturday
    # 15:00") must not stack two episodes on one platform on one day — the
    # ledger above only spaces out the base slot (2026-10-08).
    if not publish_now:
        from hypogeum.publishing.platform_slot_guard import resolve_platform_collisions
        schedule_result = resolve_platform_collisions(channel_id, episode_id, schedule_result, db_path=db_path)

    outcome = publish_episode_package(
        package, platforms=passed_platforms, channel_id=channel_id, db_path=db_path,
        schedule_plan=schedule_result.schedule_plan, logical_alias=logical_alias,
        media_staging_provider=media_staging_provider, publishing_provider=publishing_provider,
        deliverable_ids=deliverable_ids, platform_settings=platform_settings,
    )
    if outcome.blocked:
        status = "blocked_publish"
    elif outcome.all_published:
        status = "published" if not blocked_platforms else "partial"
    else:
        status = "partial"

    return ReleasePublishResult(
        channel_id=channel_id, episode_id=episode_id, platforms=platforms,
        status=status, readiness_by_platform=readiness_by_platform, schedule=schedule_result, publish=outcome,
        blocked_reason=outcome.blocked_reason if outcome.blocked else None,
        blocked_platforms=blocked_platforms,
    )
