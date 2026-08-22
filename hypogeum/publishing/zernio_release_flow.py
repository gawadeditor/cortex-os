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

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hypogeum.publishing.media_staging import MediaStagingProvider
from hypogeum.publishing.provider import PublishingProvider
from hypogeum.publishing.readiness import MetadataOverride, ReadinessCheckResult, check_publish_readiness
from hypogeum.publishing.schedule_compiler import ScheduleCompilationResult, compile_schedule_plan_from_channel_package
from hypogeum.publishing.zernio_publish_runtime import PublishPackage, PublishPackageResult, publish_episode_package


@dataclass
class ReleasePublishResult:
    channel_id: str
    episode_id: str
    platform: str
    status: str  # "blocked_readiness" | "blocked_schedule" | "blocked_publish" | "published" | "partial"
    readiness: Optional[ReadinessCheckResult] = None
    schedule: Optional[ScheduleCompilationResult] = None
    publish: Optional[PublishPackageResult] = None
    blocked_reason: Optional[str] = None


def publish_channel_release(
    channel_id: str,
    episode_id: str,
    platform: str,
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
) -> ReleasePublishResult:
    """
    Read-only through stages 1–2; stage 3 is the only stage that can ever
    write anything (staging attempts, publishing attempts/receipts — all
    via the pre-existing write-ahead boundaries those modules already
    own), and only after both prior gates pass.

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
    """
    readiness = check_publish_readiness(
        episode_id, channel_id=channel_id, assets_root=assets_root, db_path=db_path,
        platforms=(platform,), logical_alias=logical_alias, provider_id=provider_id,
        metadata_override=metadata_override,
    )
    if not readiness.passed:
        failed = [c.capability for c in readiness.checks if c.status == "fail"]
        return ReleasePublishResult(
            channel_id=channel_id, episode_id=episode_id, platform=platform,
            status="blocked_readiness", readiness=readiness,
            blocked_reason=f"readiness check failed: {failed}",
        )

    deliverable_ids = [d.deliverable_id for d in package.all_deliverables()]
    schedule_result = compile_schedule_plan_from_channel_package(
        channel_id, deliverable_ids, db_path=db_path, publish_now=publish_now,
        require_explicit_policy=require_explicit_schedule_policy,
    )
    if schedule_result.blocked:
        return ReleasePublishResult(
            channel_id=channel_id, episode_id=episode_id, platform=platform,
            status="blocked_schedule", readiness=readiness, schedule=schedule_result,
            blocked_reason=schedule_result.blocked_reason,
        )

    outcome = publish_episode_package(
        package, platform=platform, channel_id=channel_id, db_path=db_path,
        schedule_plan=schedule_result.schedule_plan, logical_alias=logical_alias,
        media_staging_provider=media_staging_provider, publishing_provider=publishing_provider,
    )
    if outcome.blocked:
        status = "blocked_publish"
    elif outcome.all_published:
        status = "published"
    else:
        status = "partial"

    return ReleasePublishResult(
        channel_id=channel_id, episode_id=episode_id, platform=platform,
        status=status, readiness=readiness, schedule=schedule_result, publish=outcome,
        blocked_reason=outcome.blocked_reason if outcome.blocked else None,
    )
