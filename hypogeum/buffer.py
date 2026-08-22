"""
hypogeum/buffer.py — Channel-scoped, deliverable/bundle-generic ready-ahead
buffer gate.

Factory Core is channel-agnostic (see CLAUDE.md's Strategic Reorientation:
"NOT a YouTube channel — Multi-Channel Autonomous Media Factory Platform").
This module has NO concept of "episode", "Shorts", "weekly", or "one
episode ahead" — those are Hypogeum's own Channel Package configuration,
not Factory constants. A different channel may declare a completely
different deliverable/bundle shape and buffer target (e.g. six
independently-publishable Shorts with no long-form episode at all, buffer
target 2) and this module behaves identically for it — every decision is
driven by the DeliverableBundleDefinition passed in, never by a hardcoded
Hypogeum assumption.

Two layers, deliberately separated:
    compute_buffer_state(definition, items)   — pure, deterministic, no
        I/O. Scoped to exactly one (channel_id, bundle_id,
        target_buffer_count). Enforces isolation STRUCTURALLY: raises
        ForeignDemandItemError if any ChannelDemandItem does not belong to
        that exact (channel_id, bundle_id) pair — a ready item belonging
        to one channel or one bundle can never silently count toward a
        different channel's or bundle's buffer.
    gather_channel_demand_items(...)          — the I/O layer that builds
        ChannelDemandItem objects for one (channel_id, bundle_id) from
        real state. See its own docstring for the schema gap it fails
        closed on rather than guessing at.

SCHEMA GAP (documented here, NOT invented or redesigned in this slice):
the Channel Package (channel_operations_profiles.production_cadence_json /
publishing_schedule_json) has no generic, formalized, cross-channel
contract for:
  (a) which deliverable/bundle definitions a channel declares, their
      bundle membership rules (e.g. "these 3 Shorts belong to this one
      long-form's bundle" vs. "each Short is its own independent bundle
      instance"), or their target ready-ahead buffer count — see
      DeliverableBundleDefinition's own docstring;
  (b) a generic way to resolve "which ready item already occupies the
      next/current publishing slot" (this module's cadence-agnostic stand-
      in for "current week") for an arbitrary channel's arbitrary cadence.
Compiling a real Channel Package into this module's inputs is therefore
NOT implemented here. Every DeliverableBundleDefinition, every item_id set,
and every current-cycle assignment must be supplied explicitly by the
caller — today, run_worker.py's own narrow, documented, per-channel stand-
in (see its _resolve_bundle_definitions_for_channel()), never inferred or
guessed by this module. A channel with no compiled definition is simply
never gated by this module at all (fail closed at the call site, not here).

Gate contract — generic, target-based (not "0/1/2+", which was Hypogeum's
own now-removed buffer target of 1):
    buffer_count <  target_buffer_count  -> "enqueue"
    buffer_count == target_buffer_count  -> "hold"
    buffer_count >  target_buffer_count  -> "excess"

Public API:
    DeliverableBundleDefinition, ChannelDemandItem, BufferState
    ForeignDemandItemError
    compute_buffer_state(definition, items)                       -> BufferState
    resolve_complete_item_ids_for_channel(channel_id, db_path=None) -> list[str]
    gather_channel_demand_items(definition, *, item_ids,
                                 current_cycle_item_ids, db_path=None,
                                 assets_root, platforms=(), provider_id="zernio")
        -> list[ChannelDemandItem]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, List, Optional, Sequence


class ForeignDemandItemError(ValueError):
    """
    Raised by compute_buffer_state() when a ChannelDemandItem does not
    belong to the exact (channel_id, bundle_id) of the
    DeliverableBundleDefinition it is being evaluated against. This is the
    structural enforcement of "a ready item belonging to one channel must
    never satisfy another channel's buffer" — never a silent filter, so a
    caller that accidentally mixes demand from two channels/bundles finds
    out immediately rather than getting a quietly-wrong buffer count.
    """


@dataclass(frozen=True)
class DeliverableBundleDefinition:
    """
    One channel-declared deliverable or bundle definition. Entirely
    channel-declared; Factory Core assigns no universal meaning to
    bundle_id — it is an opaque string chosen by whatever compiled this
    definition (a future Channel-Package compiler; today, an explicit,
    documented call-site stand-in — see this module's SCHEMA GAP note).

    Illustrative examples (never hardcoded inside this module):
        Channel A: bundle_id='main_episode_bundle' — one long-form episode
                   plus its associated Shorts, published together as one
                   unit. target_buffer_count=1.
        Channel B: bundle_id='short' — each Short is independently
                   publishable, no long-form episode exists at all.
                   target_buffer_count=2 (illustrative; channel-declared).
    """
    channel_id: str
    bundle_id: str
    target_buffer_count: int


@dataclass(frozen=True)
class ChannelDemandItem:
    """
    One compiled unit of ready supply toward exactly one
    (channel_id, bundle_id)'s buffer — e.g. one completed episode+Shorts
    bundle instance for Channel A, or one completed independent Short for
    Channel B. item_id is opaque to this module (never assumed to be an
    "episode_id" in any Factory-Core sense).
    """
    channel_id: str
    bundle_id: str
    item_id: str
    publish_ready: bool
    already_published: bool
    is_current_cycle: bool


@dataclass(frozen=True)
class BufferState:
    channel_id: str
    bundle_id: str
    action: str  # "enqueue" | "hold" | "excess"
    buffer_count: int
    target_buffer_count: int
    counted_item_ids: List[str] = field(default_factory=list)
    excluded_item_ids: List[str] = field(default_factory=list)
    reason: str = ""


def compute_buffer_state(
    definition: DeliverableBundleDefinition,
    items: Sequence[ChannelDemandItem],
) -> BufferState:
    """
    Pure — no I/O, no side effects, deterministic for a given input.

    Scoped strictly to definition.channel_id + definition.bundle_id. Raises
    ForeignDemandItemError (see its own docstring) if any item does not
    match both — this is the load-bearing isolation guarantee: this
    function can never be tricked into letting one channel's or one
    bundle's ready item count toward a different channel's or bundle's
    buffer, regardless of what its caller passes in.
    """
    foreign = [
        i for i in items
        if i.channel_id != definition.channel_id or i.bundle_id != definition.bundle_id
    ]
    if foreign:
        raise ForeignDemandItemError(
            f"compute_buffer_state() for channel_id={definition.channel_id!r} "
            f"bundle_id={definition.bundle_id!r} received {len(foreign)} item(s) belonging to a "
            f"different channel/bundle: "
            f"{[(i.channel_id, i.bundle_id, i.item_id) for i in foreign]}"
        )

    counted = [
        i.item_id for i in items
        if i.publish_ready and not i.already_published and not i.is_current_cycle
    ]
    excluded = [i.item_id for i in items if i.item_id not in counted]
    n = len(counted)
    target = definition.target_buffer_count

    if n < target:
        action = "enqueue"
        reason = (
            f"{n}/{target} publish-ready buffer item(s) for channel_id={definition.channel_id!r} "
            f"bundle_id={definition.bundle_id!r} — refill may enqueue"
        )
    elif n == target:
        action = "hold"
        reason = (
            f"{n}/{target} publish-ready buffer item(s) for channel_id={definition.channel_id!r} "
            f"bundle_id={definition.bundle_id!r} — target met, do not enqueue"
        )
    else:
        action = "excess"
        reason = (
            f"{n}/{target} publish-ready buffer item(s) for channel_id={definition.channel_id!r} "
            f"bundle_id={definition.bundle_id!r} — excess, do not enqueue"
        )

    return BufferState(
        channel_id=definition.channel_id, bundle_id=definition.bundle_id, action=action,
        buffer_count=n, target_buffer_count=target,
        counted_item_ids=counted, excluded_item_ids=excluded, reason=reason,
    )


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------

def resolve_complete_item_ids_for_channel(
    channel_id: str, db_path: Optional[Path] = None,
) -> List[str]:
    """
    Generic, channel-scoped item resolution — the only channel-scoping join
    the current schema actually supports (episodes has no channel_id column
    of its own; every episode is attributed to a channel only via its
    episode_topics row(s), same join hypogeum.run_registry.
    _sync_episode_row() and the prior slice's gatherer both already use).

    Deliberately does NOT know about bundle membership — it returns every
    'complete' item_id attributed to channel_id, full stop. A caller with
    more than one bundle_id per channel must partition this list itself
    (or resolve item_ids some other way); this module has no schema-backed
    way to do that partitioning today (see the module's SCHEMA GAP note).
    """
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT e.episode_id
            FROM episodes e
            JOIN episode_topics t ON t.episode_id = e.episode_id
            WHERE t.channel_id = ? AND e.status = 'complete'
            """,
            (channel_id,),
        ).fetchall()
        return [r["episode_id"] for r in rows]
    finally:
        conn.close()


def gather_channel_demand_items(
    definition: DeliverableBundleDefinition,
    *,
    item_ids: Sequence[str],
    current_cycle_item_ids: FrozenSet[str],
    db_path: Optional[Path] = None,
    assets_root: Path,
    platforms: Sequence[str] = (),
    provider_id: str = "zernio",
) -> List[ChannelDemandItem]:
    """
    Real-state gathering for compute_buffer_state()'s input, scoped to
    exactly one (channel_id, bundle_id) via `definition`.

    item_ids and current_cycle_item_ids are REQUIRED, caller-supplied — see
    this module's own SCHEMA GAP note: Factory Core has no generic way to
    compile "which item_ids belong to this bundle" or "which of them
    already occupies the next publishing slot" from the current Channel
    Package schema, so this function never guesses either. A caller with
    no real slot-resolution available must pass
    current_cycle_item_ids=frozenset() explicitly and document why at the
    call site (see run_worker.py) — treating nothing as confirmed
    current-cycle is the fail-closed default here: it can only ever
    UNDER-count the buffer (never over-count it), so the gate errs toward
    "enqueue" rather than silently starving a channel of a refill it
    genuinely still needs.

    Only publish_ready (via
    hypogeum.publishing.readiness.check_publish_readiness(), itself
    already generic/deliverable-agnostic — see that module) and
    already_published are computed here — both are real signals the
    current schema DOES express generically. Bundle membership itself is
    never computed here; item_ids is exactly the set the caller says
    belongs to this bundle.

    already_published checks BOTH the legacy direct-adapter tables
    (publishing_outcomes.publishing_status='success',
    publish_calendar.publish_status='live' — unchanged, any single row is
    still treated as sufficient there, matching that legacy path's own
    long-established, loose per-deliverable semantics, which this module
    does not redefine) AND the ADR-013 publication_intents.status=
    'succeeded' signal that hypogeum.publishing.submission_ledger.
    record_success() writes on a confirmed Zernio publish — scoped by
    BOTH episode_id AND channel_id (publication_intents has its own
    channel_id column; this is a WHERE-clause channel-isolation
    guarantee, not a post-hoc filter). Without the Zernio check, a real
    Zernio-confirmed publish would never be recognized here at all
    (Zernio writes to publication_intents/publishing_receipts/
    provider_content_mappings, never to the legacy tables) — the buffer
    would keep counting an already-published item as spare buffer
    forever, and refill would never correctly trigger after a real
    advance.

    The Zernio signal requires EVERY required deliverable_id for the item
    (resolved via hypogeum.publishing.deliverable_ids.
    resolve_required_deliverable_ids() — the SAME resolver
    build_publish_package_from_episode_dir() uses to build what actually
    gets staged, so the two can never disagree) to have its own confirmed
    'succeeded' publication_intents row — a single succeeded deliverable
    out of a four-deliverable bundle (main + 3 Shorts) does NOT mark the
    whole item published; it remains correctly counted as not-yet-fully-
    published buffer supply. Duplicate intent rows for the same
    deliverable_id never inflate completeness (SELECT DISTINCT).
    """
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415
    from hypogeum.publishing.deliverable_ids import resolve_required_deliverable_ids  # noqa: PLC0415
    from hypogeum.publishing.readiness import check_publish_readiness  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        result: List[ChannelDemandItem] = []
        for item_id in item_ids:
            published_row = conn.execute(
                "SELECT 1 FROM publishing_outcomes WHERE episode_id = ? AND publishing_status = 'success' LIMIT 1",
                (item_id,),
            ).fetchone()
            live_calendar_row = conn.execute(
                "SELECT 1 FROM publish_calendar WHERE episode_id = ? AND publish_status = 'live' LIMIT 1",
                (item_id,),
            ).fetchone()

            required_deliverable_ids = set(resolve_required_deliverable_ids(item_id, assets_root))
            if platforms:
                # Platform is part of the idempotency identity (ADR-013):
                # a succeeded YouTube intent must never be read as "this
                # item is fully done" for a DIFFERENT platform this call
                # is also requesting (e.g. TikTok/Instagram/Facebook) --
                # so zernio_fully_published is computed PER REQUESTED
                # PLATFORM and the item counts as fully published only
                # when EVERY platform in `platforms` independently has
                # every required deliverable succeeded on THAT platform.
                # A call that requests only platforms=("youtube",) is
                # therefore unaffected by whether any other platform has
                # ever been attempted at all.
                zernio_fully_published = bool(required_deliverable_ids)
                for platform in platforms:
                    succeeded_rows = conn.execute(
                        """
                        SELECT DISTINCT deliverable_id FROM publication_intents
                        WHERE episode_id = ? AND channel_id = ? AND platform = ? AND status = 'succeeded'
                        """,
                        (item_id, definition.channel_id, platform),
                    ).fetchall()
                    succeeded_deliverable_ids = {r["deliverable_id"] for r in succeeded_rows}
                    if not required_deliverable_ids.issubset(succeeded_deliverable_ids):
                        zernio_fully_published = False
                        break
            else:
                # No specific platform requested (legacy/platform-agnostic
                # callers) -- preserve the original behavior: any
                # succeeded intent for a deliverable counts, regardless of
                # which platform it succeeded on.
                succeeded_rows = conn.execute(
                    """
                    SELECT DISTINCT deliverable_id FROM publication_intents
                    WHERE episode_id = ? AND channel_id = ? AND status = 'succeeded'
                    """,
                    (item_id, definition.channel_id),
                ).fetchall()
                succeeded_deliverable_ids = {r["deliverable_id"] for r in succeeded_rows}
                zernio_fully_published = bool(required_deliverable_ids) and required_deliverable_ids.issubset(succeeded_deliverable_ids)

            already_published = bool(published_row or live_calendar_row or zernio_fully_published)

            readiness = check_publish_readiness(
                item_id, assets_root=assets_root, db_path=db_path,
                channel_id=definition.channel_id, platforms=platforms,
                provider_id=provider_id, metadata_override=None,
            )
            result.append(ChannelDemandItem(
                channel_id=definition.channel_id, bundle_id=definition.bundle_id, item_id=item_id,
                publish_ready=readiness.passed, already_published=already_published,
                is_current_cycle=item_id in current_cycle_item_ids,
            ))
        return result
    finally:
        conn.close()
