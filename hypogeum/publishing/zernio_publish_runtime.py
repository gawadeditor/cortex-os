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
    to ZernioProvider.publish()'s own scheduling kwargs, unmodified."""
    scheduled_for: Optional[datetime] = None
    platform_scheduled_for: Optional[datetime] = None
    timezone_name: Optional[str] = None
    publish_now: bool = False
    is_draft: bool = False


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
    status: str  # "published" | "ambiguous" | "blocked"
    stage_result: Optional[dict] = None
    publish_result: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class PublishPackageResult:
    episode_id: str
    platform: str
    blocked: bool
    blocked_reason: Optional[str] = None
    deliverables: List[DeliverablePublishOutcome] = field(default_factory=list)

    @property
    def all_published(self) -> bool:
        return bool(self.deliverables) and all(d.status == "published" for d in self.deliverables)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def publish_episode_package(
    package: PublishPackage,
    *,
    platform: str,
    channel_id: str,
    db_path: Optional[Path] = None,
    schedule_plan: Optional[SchedulePlan] = None,
    media_staging_provider: Optional[MediaStagingProvider] = None,
    publishing_provider: Optional[PublishingProvider] = None,
    logical_alias: str = "default",
) -> PublishPackageResult:
    """
    Stage and publish every deliverable in `package` (main episode + every
    Short) to `platform`, through the existing generic provider
    abstractions — no local orchestration bypasses
    hypogeum.publishing.media_staging's guard_and_prepare_staging_attempt()
    write-ahead boundary or hypogeum.publishing.submission_ledger's
    equivalent inside ZernioProvider.publish(); this module only sequences
    calls into them.

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
    """
    deliverables = package.all_deliverables()
    expected_ids = [d.deliverable_id for d in deliverables]

    try:
        _validate_schedule_plan_covers(schedule_plan, expected_ids)
        _validate_channel_scope(package, schedule_plan, channel_id)
    except ZernioPublishRuntimeError as exc:
        return PublishPackageResult(
            episode_id=package.episode_id, platform=platform,
            blocked=True, blocked_reason=str(exc), deliverables=[],
        )

    staging = media_staging_provider if media_staging_provider is not None else ZernioMediaStagingProvider()
    publisher = publishing_provider if publishing_provider is not None else ZernioProvider()

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

        media_ref = staged_result_to_media_reference(
            staged, media_type=deliverable.media_type,
            thumbnail_url=deliverable.thumbnail_url, title=deliverable.title,
            alt_text=deliverable.alt_text,
        )

        try:
            publish_result = publisher.publish(
                platform=platform,
                deliverable_type=("main_episode" if deliverable is package.main_episode else "short"),
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
                timezone_name=sched.timezone_name,
                publish_now=sched.publish_now,
                is_draft=sched.is_draft,
            )
        except Exception as exc:
            outcomes.append(DeliverablePublishOutcome(
                deliverable_id=deliverable.deliverable_id, status="blocked",
                stage_result=_staged_to_dict(staged), error=str(exc),
            ))
            continue

        outcomes.append(DeliverablePublishOutcome(
            deliverable_id=deliverable.deliverable_id,
            status=publish_result.get("status", "ambiguous"),
            stage_result=_staged_to_dict(staged),
            publish_result=publish_result,
        ))

    return PublishPackageResult(
        episode_id=package.episode_id, platform=platform,
        blocked=False, deliverables=outcomes,
    )


def _staged_to_dict(staged) -> dict:
    return {
        "media_staging_provider_id": staged.media_staging_provider_id,
        "public_url": staged.public_url,
        "status": staged.status,
        "remote_object_id": staged.remote_object_id,
    }
