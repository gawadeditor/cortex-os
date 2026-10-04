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
import re
from dataclasses import dataclass, replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence

from hypogeum.agents.production_output_gate import run_production_output_gate
from hypogeum.agents.shorts_metadata import ShortsMetadataError, generate_shorts_captions, generate_shorts_titles
from hypogeum.buffer import DeliverableBundleDefinition, gather_channel_demand_items, resolve_complete_item_ids_for_channel
from hypogeum.config import Config
from hypogeum.logger import get_logger
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


def _backfill_shorts_metadata(
    episode_id: str,
    episode_dir: Path,
    final_render_package: dict,
    *,
    config: Config,
    channel_id: Optional[str] = None,
    titles_llm_call=None,
    captions_llm_call=None,
) -> dict:
    """
    2026-08-22 fix (mandatory written metadata): before
    resolve_canonical_metadata()/resolve_canonical_captions() are ever
    consulted, generate and persist real, script-grounded shorts_titles/
    shorts_captions into the canonical final_render_package_{episode_id}.json
    for any Short still missing one -- this is what let ep_f3619022's 3
    Shorts stay indefinitely blocked with content="" (0 chars, "No
    content" in the Zernio UI): titles/captions were only ever generated
    at Assembly-render time, so an episode rendered before
    generate_shorts_captions() existed (or whose generation failed then)
    had no path back to a resolved value short of a full re-render.

    Read-only if nothing is missing (the common case for a healthy
    episode) -- never re-generates a title/caption that already resolves.
    When generation IS needed, uses hypogeum.agents.shorts_metadata.
    generate_shorts_titles()/generate_shorts_captions() (the SAME
    functions Assembly Agent itself calls, never a second implementation)
    against script_package_{episode_id}.json's shorts_scripts[], matched
    POSITIONALLY to shorts_renders (Script Engine's own numbering, e.g.
    'short_001', is independent of the Visual Package's per-scene
    short_id/deliverable_id scheme -- see build_publish_package_from_episode_dir()'s
    own docstring for the same convention) and re-keyed to each Short's
    real canonical deliverable_id before being written back.

    Never fails open: a ShortsMetadataError from either generator is
    caught per-field (title vs. caption) and simply leaves that field
    unresolved in the written package -- resolve_canonical_metadata()/
    resolve_canonical_captions() are the actual gates that block on a
    still-missing value; this function only ever tries to fill a gap, it
    never invents a placeholder and never blocks by itself. Persists a
    timestamped backup of the previous final_render_package before
    writing, matching this episode directory's own established backup
    convention.

    titles_llm_call/captions_llm_call: injectable, forwarded unchanged to
    the two generators -- tests supply a fake (no real OpenRouter/Claude
    call, no network, no cost); production callers omit them and accept
    each generator's own real default.

    Returns the (possibly updated) final_render_package dict. Callers
    that already loaded it from disk must use THIS return value, not
    their own stale copy, for any subsequent resolution.
    """
    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []
    if not shorts_renders:
        return final_render_package

    expected_short_ids = [resolve_short_deliverable_id(shorts_renders[i], i) for i in range(len(shorts_renders))]
    metadata_source = dict((final_render_package or {}).get("metadata_source") or {})

    def _is_placeholder(deliverable_id: str, value: str) -> bool:
        return value.strip().lower() == deliverable_id.strip().lower()

    def _needs_generation(field_name: str, value_key: str) -> bool:
        entries = metadata_source.get(field_name) or []
        by_id = {
            (e.get("short_id") or "").strip(): (e.get(value_key) or "").strip()
            for e in entries if isinstance(e, dict)
        }
        for sid in expected_short_ids:
            val = by_id.get(sid, "")
            if not val or _is_placeholder(sid, val):
                return True
        return False

    needs_titles = _needs_generation("shorts_titles", "title")
    needs_captions = _needs_generation("shorts_captions", "caption")
    if not needs_titles and not needs_captions:
        return final_render_package

    script_package = _load_json_if_exists(episode_dir / f"script_package_{episode_id}.json")
    shorts_scripts = (script_package or {}).get("shorts_scripts") or []
    if len(shorts_scripts) != len(expected_short_ids):
        # Positional matching is only safe when the counts agree -- never
        # guess an alignment; leave both fields exactly as they were and
        # let the downstream resolver report the real blocker.
        return final_render_package
    requested_shorts_scripts = [
        {**shorts_scripts[i], "short_id": expected_short_ids[i]} for i in range(len(expected_short_ids))
    ]

    changed = False
    if needs_titles:
        try:
            generated = generate_shorts_titles(
                requested_shorts_scripts, config=config, episode_id=episode_id,
                channel_id=channel_id, llm_call=titles_llm_call,
            )
            metadata_source["shorts_titles"] = [
                {"short_id": e["short_id"], "title": e["title"]} for e in generated
            ]
            changed = True
        except ShortsMetadataError:
            pass  # left unresolved -- resolve_canonical_metadata() is the real gate

    if needs_captions:
        try:
            generated = generate_shorts_captions(
                requested_shorts_scripts, config=config, episode_id=episode_id,
                channel_id=channel_id, llm_call=captions_llm_call,
            )
            metadata_source["shorts_captions"] = [
                {
                    "short_id": e["short_id"], "caption": e["caption"],
                    "source": "generate_shorts_captions_v1",
                    "version": datetime.now(timezone.utc).isoformat(),
                }
                for e in generated
            ]
            changed = True
        except ShortsMetadataError:
            pass  # left unresolved -- resolve_canonical_captions() is the real gate

    if not changed:
        return final_render_package

    updated_package = {**final_render_package, "metadata_source": metadata_source}
    _write_final_render_package_with_backup(episode_id, episode_dir, final_render_package, updated_package)
    return updated_package


def _load_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_final_render_package_with_backup(
    episode_id: str, episode_dir: Path, previous: dict, updated: dict,
) -> None:
    """Backs up the previous final_render_package before overwriting it —
    matching this episode directory's own established
    `.backup_<UTC-timestamp>.json` convention (used throughout this
    episode's production history for every other canonical-file mutation)
    — never overwrites the canonical file without a recoverable prior
    copy on disk."""
    package_path = episode_dir / f"final_render_package_{episode_id}.json"
    if package_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = episode_dir / f"final_render_package_{episode_id}.backup_{ts}.json"
        backup_path.write_text(json.dumps(previous, indent=2, ensure_ascii=False), encoding="utf-8")
    package_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")


def _backfill_canonical_platform_metadata(
    episode_id: str,
    episode_dir: Path,
    final_render_package: dict,
    *,
    config: Config,
    channel_id: Optional[str],
    target_platforms: List[str],
    platform_deliverable_policy: dict,
    llm_call=None,
) -> dict:
    """
    Permanent workflow fix (2026-08-23, see docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md item 1): generates
    and persists a real canonical_platform_metadata entry for every
    ENABLED (deliverable_id, platform) pair still missing one -- this is
    what makes "no human writes a repair artifact by hand" true for every
    future episode, not just this one. Read-only if nothing is missing
    (the common case once an episode's metadata has already been
    generated once). Mirrors _backfill_shorts_metadata()'s own structure
    and fail-closed-per-item discipline exactly.

    "Enabled" is read from platform_deliverable_policy, the SAME policy
    dict _allowed_deliverable_ids_for_platform() already uses -- a
    (platform, deliverable_class) combination the channel has not
    enabled is never generated for for (no wasted generation, no
    canonical entry for a platform this deliverable will never target).

    Never fails open: a CanonicalMetadataGenerationError for one
    deliverable is caught and simply leaves that deliverable's still-
    missing platform(s) unresolved -- hypogeum.publishing.
    cross_platform_readiness is the real gate that fails a still-missing
    entry closed; this function only ever tries to fill a gap.
    """
    from hypogeum.agents.canonical_metadata_agent import (
        CanonicalMetadataGenerationError,
        generate_canonical_platform_metadata_for_deliverable,
    )
    from hypogeum.publishing.canonical_metadata import resolve_canonical_platform_metadata

    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []
    short_ids = [resolve_short_deliverable_id(shorts_renders[i], i) for i in range(len(shorts_renders))]
    all_deliverable_ids = [MAIN_EPISODE_DELIVERABLE_ID, *short_ids]

    metadata_source = dict((final_render_package or {}).get("metadata_source") or {})
    existing_entries = list(metadata_source.get("canonical_platform_metadata") or [])
    changed = False

    for deliverable_id in all_deliverable_ids:
        deliverable_class = classify_deliverable_class(deliverable_id)
        missing_platforms = [
            p for p in target_platforms
            if bool((((platform_deliverable_policy or {}).get(p) or {}).get(deliverable_class) or {}).get("enabled"))
            and resolve_canonical_platform_metadata(final_render_package, deliverable_id, p) is None
        ]
        if not missing_platforms:
            continue
        try:
            new_entries = generate_canonical_platform_metadata_for_deliverable(
                episode_id, episode_dir, deliverable_id, deliverable_class, missing_platforms,
                final_render_package, config=config, channel_id=channel_id,
                platform_deliverable_policy=platform_deliverable_policy, llm_call=llm_call,
            )
        except CanonicalMetadataGenerationError:
            continue  # left unresolved -- cross_platform_readiness is the real gate

        # The generator deliberately leaves thumbnail_ref unset for a
        # main_episode entry (it is not itself in the business of
        # resolving thumbnails) -- filled in here from the SAME resolver
        # hypogeum.publishing.readiness.check_publish_readiness()'s own
        # F_THUMBNAIL gate uses, so the canonical package always points
        # at the real, already-selected thumbnail, never a second,
        # independently-guessed one. Best-effort: a Short has no
        # thumbnail requirement by default (see canonical_metadata.py's
        # own validation) and is left as None.
        if deliverable_id == MAIN_EPISODE_DELIVERABLE_ID:
            try:
                thumb = resolve_canonical_main_thumbnail(episode_id, episode_dir)
                for entry in new_entries:
                    entry["thumbnail_ref"] = thumb.asset_id
            except ThumbnailResolutionError:
                pass  # left unresolved -- readiness's own F_THUMBNAIL check is the real gate

        existing_entries.extend(new_entries)
        changed = True

    # Backfill thumbnail_ref onto ANY existing main_episode entry that is
    # missing it — not only entries generated in this call. An entry
    # written by an earlier run (before the openrouter POST_PLAN
    # plan-binding fix, or before resolve_canonical_main_thumbnail
    # tolerated a stale-report warn) is present but carries
    # thumbnail_ref=None, so `missing_platforms` above is empty and the
    # per-creation backfill never reaches it — leaving cross_platform_
    # readiness to fail youtube/main_episode forever on "thumbnail_ref is
    # absent".
    _main_needs_thumb = [
        e for e in existing_entries
        if e.get("deliverable_id") == MAIN_EPISODE_DELIVERABLE_ID and not e.get("thumbnail_ref")
    ]
    if _main_needs_thumb:
        try:
            _thumb = resolve_canonical_main_thumbnail(episode_id, episode_dir)
            for e in _main_needs_thumb:
                e["thumbnail_ref"] = _thumb.asset_id
            changed = True
        except ThumbnailResolutionError:
            pass  # readiness's own F_THUMBNAIL check is the real gate

    if not changed:
        return final_render_package

    metadata_source["canonical_platform_metadata"] = existing_entries
    updated_package = {**final_render_package, "metadata_source": metadata_source}
    _write_final_render_package_with_backup(episode_id, episode_dir, final_render_package, updated_package)
    return updated_package


def _pick_root_title_description(
    final_render_package: dict, deliverable_id: str,
    fallback_title: Optional[str], fallback_description: Optional[str],
):
    """
    Permanent workflow fix (2026-08-23, item 3 of docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md: "the production
    Zernio path must consume the canonical package directly"): when a
    real canonical_platform_metadata entry exists for this deliverable,
    its own title/description become the shared, root-level Zernio
    title/content -- never the older resolve_canonical_metadata()/
    resolve_canonical_captions() value once a real canonical package
    exists. Falls back to the caller-supplied (legacy-resolved) values
    only when no canonical entry exists at all for this deliverable, on
    any platform -- preserving exact prior behavior for an episode that
    genuinely has no canonical package.

    Zernio's aggregation model (one deliverable -> one post covering
    every target platform) means the root title/content are inherently
    SHARED across all of that deliverable's platforms -- true per-
    platform caption variance lives in platforms[].platformSpecificData
    (see hypogeum.publishing.canonical_metadata.build_zernio_platform_specific_data()),
    a smaller, structured field set, not a second full alternate
    description. 'youtube' is preferred as the root source when present
    (matches every real Hypogeum topology: main_episode targets YouTube
    only, and a Short's YouTube variant is generally its most complete/
    representative one); otherwise the first available platform's entry
    is used -- never fabricated, never re-derived independently of what
    was actually generated/approved.
    """
    from hypogeum.publishing.canonical_metadata import (
        materialize_youtube_main_episode_description,
        resolve_canonical_platform_metadata,
    )

    deliverable_class = classify_deliverable_class(deliverable_id)
    for platform in ("youtube", "tiktok", "instagram", "facebook"):
        metadata = resolve_canonical_platform_metadata(final_render_package, deliverable_id, platform)
        if metadata is not None:
            # The real, complete, viewer-facing description -- base +
            # deterministic chapters + sources -- for a YouTube
            # main_episode, using the SAME function readiness validates
            # against, so what gets sent can never diverge from what was
            # approved as ready. Every other (deliverable_class,
            # platform) combination is unaffected: this returns
            # metadata.description unchanged.
            description = metadata.description
            if deliverable_class == "main_episode" and platform == "youtube":
                description = materialize_youtube_main_episode_description(metadata)
            return metadata.title or fallback_title, description
    return fallback_title, fallback_description


# Short-form caption cap. Well under Instagram's 2200 and TikTok's ~2200;
# a Short's caption is a hook, not a document. Sentence-boundary trim.
_SHORT_FORM_CAPTION_MAX_CHARS = 900
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _is_short_native_channel(channel_id: str, db_path: Optional[Path]) -> bool:
    """videos_per_week == 0 and shorts_per_week >= 1 — same derivation the
    rest of the factory uses. Fail-soft to False."""
    try:
        from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
        prof = get_channel_operations_profile(channel_id, db_path=db_path)
        cad = (getattr(prof, "production_cadence", None) or {}) if prof else {}
        return (
            cad.get("videos_per_week") == 0
            and isinstance(cad.get("shorts_per_week"), int)
            and cad.get("shorts_per_week") >= 1
        )
    except Exception:  # noqa: BLE001
        return False


def _short_form_caption(text: str) -> str:
    """Keep the leading whole sentences that fit _SHORT_FORM_CAPTION_MAX_CHARS
    (always at least the first sentence); hard word-trim if that alone is
    over cap. Strips any chapter/timestamp/source block a long-form
    description would carry."""
    text = (text or "").strip()
    if len(text) <= _SHORT_FORM_CAPTION_MAX_CHARS:
        return text
    kept: list = []
    for sentence in _SENTENCE_END.split(text):
        s = sentence.strip()
        if not s:
            continue
        candidate = (" ".join(kept + [s])).strip()
        if kept and len(candidate) > _SHORT_FORM_CAPTION_MAX_CHARS:
            break
        kept.append(s)
        if len(candidate) >= _SHORT_FORM_CAPTION_MAX_CHARS:
            break
    out = " ".join(kept).strip()
    if len(out) > _SHORT_FORM_CAPTION_MAX_CHARS:
        words = out.split()
        out = ""
        for w in words:
            if len(out) + len(w) + 1 > _SHORT_FORM_CAPTION_MAX_CHARS:
                break
            out = f"{out} {w}".strip()
    return out or text[:_SHORT_FORM_CAPTION_MAX_CHARS].rstrip()


def build_publish_package_from_episode_dir(
    channel_id: str,
    episode_id: str,
    bundle_id: Optional[str],
    assets_root: Path,
    *,
    db_path: Optional[Path] = None,
    metadata_override: Optional[MetadataOverride] = None,
    enable_metadata_backfill: bool = False,
    config: Optional[Config] = None,
    titles_llm_call=None,
    captions_llm_call=None,
    platform_deliverable_policy: Optional[dict] = None,
    target_platforms: Optional[List[str]] = None,
    canonical_metadata_llm_call=None,
) -> PublishPackage:
    """
    Read-only UNLESS enable_metadata_backfill=True. Constructs a
    PublishPackage from the standard on-disk deliverable-file convention —
    never invents a file that isn't there; hypogeum.publishing.readiness's
    own checks are what actually verify each file exists and is non-empty
    before any staging call is allowed to proceed. Physical file paths are
    always resolved by 0-indexed position (short_{i}_{episode_id}.mp4,
    matching Assembly Agent's own output convention); deliverable_id (the
    ledger identity, independent of file naming) is resolved by
    hypogeum.publishing.deliverable_ids — the SAME shared resolver
    hypogeum.buffer uses to determine bundle completeness, so the two can
    never disagree on identity.

    enable_metadata_backfill=False (default) preserves this function's
    original read-only contract exactly — the right default for a manual/
    read-only caller or a test that wants no filesystem side effects.
    process_channel_ready_bundles() (the real autonomous path) passes
    True, so a Short still missing its title/caption gets one generated
    and durably written to final_render_package_{episode_id}.json (see
    _backfill_shorts_metadata()'s own docstring) BEFORE resolution is
    attempted — this is what "No autonomous post may be created with
    empty visible content" actually requires: a real path back to a
    resolved value, not just a readiness check that blocks forever.
    config/titles_llm_call/captions_llm_call are forwarded unchanged to
    _backfill_shorts_metadata() (ignored when backfill is disabled).

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

    if enable_metadata_backfill:
        final_render_package = _backfill_shorts_metadata(
            episode_id, episode_dir, final_render_package,
            config=config or Config(), channel_id=channel_id,
            titles_llm_call=titles_llm_call, captions_llm_call=captions_llm_call,
        )
        # Permanent workflow fix (2026-08-23, see docs/operations/audits/
        # ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md): auto-generate
        # any missing canonical_platform_metadata entry for every enabled
        # (deliverable_id, platform) pair BEFORE this package is ever
        # staged -- must run AFTER _backfill_shorts_metadata() above, since
        # a Short's canonical generation grounds itself in the REAL,
        # already-resolved shorts_captions entry that call may have just
        # produced. Never runs when target_platforms/platform_deliverable_policy
        # were not supplied (a caller that doesn't pass them gets the
        # exact prior behavior -- this is additive, not a forced new
        # requirement on every caller of this function).
        if target_platforms:
            final_render_package = _backfill_canonical_platform_metadata(
                episode_id, episode_dir, final_render_package,
                config=config or Config(), channel_id=channel_id,
                target_platforms=target_platforms,
                platform_deliverable_policy=platform_deliverable_policy or {},
                llm_call=canonical_metadata_llm_call,
            )

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

    main_title, main_description = _pick_root_title_description(
        final_render_package, MAIN_EPISODE_DELIVERABLE_ID, resolved_title, resolved_description,
    )

    # Short-native: the main_episode is a vertical Short. A long-form
    # description (base + chapters + sources, ~2-3k chars) blows the
    # short-form platforms' caption limits (Instagram 2200) and fails the
    # whole multi-platform batch. Use a short caption: the leading whole
    # sentences that fit, hard-capped well under every platform's limit.
    try:
        if _is_short_native_channel(channel_id, db_path):
            main_description = _short_form_caption(main_description)
    except Exception:  # noqa: BLE001 — never fail package build on this
        pass

    main_path = episode_dir / f"main_{episode_id}.mp4"

    short_deliverables: List[DeliverableFile] = []
    for i in range(len(shorts_renders)):
        short_id = short_ids_in_order[i]
        short_title, short_description = _pick_root_title_description(
            final_render_package, short_id,
            resolved_shorts_titles.get(short_id), resolved_captions.get(short_id),
        )
        short_deliverables.append(DeliverableFile(
            deliverable_id=short_id,
            local_path=episode_dir / f"short_{i}_{episode_id}.mp4",
            mime_type="video/mp4", media_type="video",
            filename=f"short_{i}_{episode_id}.mp4",
            title=short_title, description=short_description,
            **_resolve_zernio_wiring(final_render_package, short_id),
        ))

    return PublishPackage(
        channel_id=channel_id, episode_id=episode_id, bundle_id=bundle_id,
        main_episode=DeliverableFile(
            deliverable_id=MAIN_EPISODE_DELIVERABLE_ID, local_path=main_path, mime_type="video/mp4",
            media_type="video", filename=main_path.name,
            title=main_title, description=main_description,
            thumbnail_local_path=resolved_thumbnail_path,
            **_resolve_zernio_wiring(final_render_package, MAIN_EPISODE_DELIVERABLE_ID),
        ),
        shorts=short_deliverables,
    )


# All platforms this Factory Core module knows how to resolve
# per-platform canonical metadata for -- NOT a channel-specific
# topology assumption (a channel's own platform_deliverable_policy is
# what actually decides which of these get targeted for a given
# deliverable; resolving all four here unconditionally is cheap local
# JSON lookups, never a provider call).
_ALL_KNOWN_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")


def _resolve_zernio_wiring(final_render_package: dict, deliverable_id: str) -> dict:
    """
    Real production wiring for hypogeum.publishing.canonical_metadata
    (2026-08-23, see docs/operations/audits/
    ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md item 5): resolves
    each platform's own CanonicalPlatformMetadata for this deliverable
    and builds the platform_specific_data/root_fields DeliverableFile
    kwargs the real publish_multi_platform() call site
    (hypogeum.publishing.zernio_publish_runtime.publish_episode_package())
    now actually consumes -- closing the gap where the provider accepted
    these fields but no real caller ever supplied them.

    Best-effort per platform: a platform with no resolved canonical
    metadata simply contributes nothing (never a fabricated default) --
    hypogeum.publishing.cross_platform_readiness's own canonical-
    metadata gate is what blocks a submission missing a REQUIRED field
    before this is ever reached; this function only wires through
    whatever real, already-approved metadata exists.
    """
    from hypogeum.publishing.canonical_metadata import (
        build_zernio_platform_specific_data,
        build_zernio_root_fields,
        resolve_canonical_platform_metadata,
    )

    platform_specific_data: dict = {}
    root_fields: dict = {}
    for platform in _ALL_KNOWN_PLATFORMS:
        metadata = resolve_canonical_platform_metadata(final_render_package, deliverable_id, platform)
        if metadata is None:
            continue
        data = build_zernio_platform_specific_data(metadata, platform)
        if data:
            platform_specific_data[platform] = data
        if platform == "youtube":
            root_fields = build_zernio_root_fields(metadata)
    return {"platform_specific_data": platform_specific_data, "root_fields": root_fields}


def _resolve_target_platforms(channel_id: str, db_path: Optional[Path]) -> List[str]:
    """
    Read-only. Empty list (never a global default) when no
    ChannelOperationsProfile exists, publishing is off, or no platform
    is targetable.

    2026-08-23 (provider-path readiness): a platform is targetable when
    EITHER the operator has explicitly enabled it
    (ChannelOperationsProfile.enabled_platforms), OR
    hypogeum.publishing.provider_repository.resolve_provider_ready_platforms()
    reports it as currently healthy through its actual bound provider.
    enabled_platforms can go stale -- it may still encode a PRIOR
    connector's failure for a platform that has since moved to a healthy
    provider path (e.g. a direct-integration outage recorded before the
    channel switched to publishing that platform through Zernio) -- a
    live, verified provider binding is authoritative evidence over a
    stale flag, never the reverse: a platform with NO healthy binding is
    never included just because enabled_platforms says so, since
    _resolve_approved_binding() (every real publish/repair call path)
    would fail closed on it anyway.
    """
    from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
    from hypogeum.publishing.provider_repository import resolve_provider_ready_platforms  # noqa: PLC0415

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
    operator_enabled = {p for p, enabled in (profile.enabled_platforms or {}).items() if enabled}
    provider_ready = set(resolve_provider_ready_platforms(db_path, channel_id=channel_id))
    return sorted(operator_enabled | provider_ready)


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


def _status_for_platform(result: ReleasePublishResult, platform: str) -> str:
    """
    2026-08-22 fix (Zernio aggregation model): result.status is computed
    across the WHOLE aggregated call (every platform in the group
    together) -- correct for "did the group's staging/schedule/publish
    call succeed at all", but NOT correct for "did THIS platform's own
    submission succeed", since a single aggregated post can legitimately
    carry a MIX of per-platform outcomes (see ZernioProvider.
    _handle_success_envelope()'s own 2026-08-22 fix for the
    PlatformTarget.errorMessage check this depends on). Derives the
    real per-platform status from
    result.publish.deliverables[].platform_results[platform] instead --
    "published" only when every deliverable in the group reports this
    platform as published; a platform that itself failed while its
    group-mates succeeded is never reported as anything but its own
    real outcome.
    """
    if result.status in ("blocked_readiness", "blocked_schedule", "blocked_metadata", "blocked_output_gate"):
        return result.status
    if result.publish is None or result.publish.blocked:
        return result.status
    per_platform_statuses = []
    for outcome in result.publish.deliverables:
        if outcome.platform_results:
            per_platform_statuses.append(outcome.platform_results.get(platform, {}).get("status", "ambiguous"))
        else:
            per_platform_statuses.append(outcome.status)
    if not per_platform_statuses:
        return result.status
    if all(s == "published" for s in per_platform_statuses):
        return "published"
    if any(s == "published" for s in per_platform_statuses):
        return "partial"
    return "blocked_publish"


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


def _item_has_future_publish_slot(
    channel_id: str, item_id: str, db_path: Optional[Path],
) -> bool:
    """True if this episode already occupies a publish slot whose time is
    still in the future (channel_publish_slots — the Zernio-schedule
    mirror). Used to skip re-compiling a slot for an episode that is
    already scheduled but not yet fully live. Best-effort: False on any
    error, so a query bug never silently blocks a legitimate release."""
    if not channel_id or not item_id:
        return False
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            row = conn.execute(
                "SELECT 1 FROM channel_publish_slots "
                "WHERE channel_id = ? AND episode_id = ? AND slot_utc > ? LIMIT 1",
                (channel_id, item_id, now_iso),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


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
    config: Optional[Config] = None,
    log=None,
    enable_production_output_gate: bool = True,
    titles_llm_call=None,
    captions_llm_call=None,
    canonical_metadata_llm_call=None,
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

    2026-08-22 fixes:
      - Mandatory written metadata: build_publish_package_from_episode_dir()
        is now called with enable_metadata_backfill=True — a Short still
        missing its title/caption gets one generated (via
        hypogeum.agents.shorts_metadata) and durably written before
        resolution is attempted, instead of staying indefinitely blocked.
      - production_output_gate wiring: hypogeum.agents.production_output_gate.
        run_production_output_gate() is now run ONCE per item, BEFORE any
        staging/intent/provider call for ANY platform/deliverable in that
        item (enable_production_output_gate=True, the default) — real
        burned-caption pixel verification, thumbnail verification, and
        every other real-output check now actually gates the Zernio write
        path, not just the legacy direct-YouTube path
        (hypogeum.agents.publishing_agent.py) it was previously wired into
        exclusively. A failed gate blocks the WHOLE item (every platform,
        every deliverable) — zero staging, zero POST, zero PUT — and is
        reported via ReadyBundle.result.status == "blocked_output_gate".
      - Zernio aggregation model: deliverables are grouped by their own
        identical resolved platform-set (main_episode -> {youtube} alone;
        every Short -> every enabled platform together) and
        publish_channel_release() is called ONCE per group with the
        group's full platforms list — never once per platform. One Short
        targeting 4 platforms therefore reaches ONE provider POST, not 4;
        see publish_channel_release()/publish_episode_package()/
        ZernioProvider.publish_multi_platform() for the rest of the chain.
        The external ReadyBundle contract is unchanged (still one entry
        per (item_id, platform), for run_worker.py's own per-platform
        logging) — internally, every ReadyBundle for platforms in the
        same group shares the exact same underlying ReleasePublishResult.
    """
    resolved_platforms = list(platforms) if platforms is not None else _resolve_target_platforms(channel_id, db_path)
    if not resolved_platforms:
        return []

    resolved_config = config or Config()
    resolved_log = log or get_logger(episode_id=None, agent="zernio_channel_release_trigger")

    # A shorts-only channel (cadence videos_per_week == 0, shorts_per_week >= 1)
    # produces short-native, portrait deliverables. production_output_gate must
    # know this so it requires 1080x1920 per clip — otherwise a landscape clip
    # that slipped through passes the gate and a portrait one is wrongly
    # rejected. The env default (SHORT_NATIVE_MODE=false) is right for the
    # generic case; here we derive it from the channel itself.
    if getattr(resolved_config, "SHORT_NATIVE_MODE", False) is not True:
        try:
            from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
            _ops = get_channel_operations_profile(channel_id, db_path=db_path)
            _cad = (getattr(_ops, "production_cadence", None) or {}) if _ops else {}
            if _cad.get("videos_per_week") == 0 and isinstance(_cad.get("shorts_per_week"), int) \
               and _cad.get("shorts_per_week") >= 1:
                resolved_config.SHORT_NATIVE_MODE = True
                resolved_log.info(
                    "short_native_mode_derived_from_cadence channel_id=%s", channel_id
                )
        except Exception:  # fail-soft — never block publishing on this
            pass

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
        # Already sitting in a FUTURE publish slot (channel_publish_slots,
        # the Zernio-schedule mirror) — a partial publish (YT succeeded,
        # TikTok still pending) keeps already_published False, and without
        # this guard every worker poll would compile a fresh slot and book
        # the same episode again (2026-09-07: one old episode ended up
        # scheduled on Zernio 4 times). Wait for the pending one to resolve.
        if _item_has_future_publish_slot(channel_id, item.item_id, db_path):
            resolved_log.info(
                "release_trigger_skip_already_scheduled_ahead",
                channel_id=channel_id, item_id=item.item_id,
            )
            continue
        try:
            package = build_publish_package_from_episode_dir(
                channel_id, item.item_id, bundle_definition.bundle_id, assets_root,
                db_path=db_path, metadata_override=metadata_override_by_item.get(item.item_id),
                enable_metadata_backfill=True, config=resolved_config,
                titles_llm_call=titles_llm_call, captions_llm_call=captions_llm_call,
                platform_deliverable_policy=platform_deliverable_policy,
                target_platforms=resolved_platforms,
                canonical_metadata_llm_call=canonical_metadata_llm_call,
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
                        channel_id=channel_id, episode_id=item.item_id, platforms=[platform],
                        status="blocked_metadata", blocked_reason=exc.reason,
                    ),
                ))
            continue

        # production_output_gate -- called ONCE per item, BEFORE any
        # staging/intent/provider call for ANY platform/deliverable below.
        # Real burned-caption pixel checks, real thumbnail QA/identity
        # re-checks, and every other real-output check this gate already
        # implements now actually block the Zernio write path -- exactly
        # the gap the 2026-08-22 forensic audit found (this gate was
        # previously wired ONLY into the legacy direct-YouTube
        # publishing_agent.py path). A failure blocks the ENTIRE item for
        # every platform -- zero staging, zero POST, zero PUT -- never a
        # partial submission of only the deliverables the gate didn't
        # check.
        if enable_production_output_gate:
            gate_result = run_production_output_gate(item.item_id, assets_root, resolved_config, resolved_log)
            if not gate_result.passed:
                failed_capabilities = [c.capability for c in gate_result.checks if c.status == "fail"]
                for platform in resolved_platforms:
                    results.append(ReadyBundle(
                        item_id=item.item_id, platform=platform,
                        result=ReleasePublishResult(
                            channel_id=channel_id, episode_id=item.item_id, platforms=[platform],
                            status="blocked_output_gate",
                            blocked_reason=f"production_output_gate failed: {failed_capabilities}",
                        ),
                    ))
                continue

        # Approval before publishing (multi-tenant Phase 4d,
        # hypogeum.publish_approvals): when the channel's workspace requires
        # it, nothing is staged or posted -- not even the early private
        # YouTube upload -- until someone approves (Telegram buttons or the
        # Workspaces page). Off by default; a no-op for channels without it.
        try:
            from hypogeum.publish_approvals import gate as _approval_gate
            _preview_d = package.shorts[0] if package.shorts else package.main_episode
            _decision = _approval_gate(channel_id, item.item_id, {
                "title": _preview_d.title, "description": _preview_d.description,
                "cover_path": _preview_d.thumbnail_local_path, "video_path": _preview_d.local_path,
            }, db_path=db_path)
        except Exception as exc:  # noqa: BLE001 — unknown approval state: hold, never publish blind
            resolved_log.error("publish_approval_gate_error", channel_id=channel_id,
                               item_id=item.item_id, error=str(exc)[:200])
            _decision = "hold"
        if _decision != "proceed":
            for platform in resolved_platforms:
                results.append(ReadyBundle(
                    item_id=item.item_id, platform=platform,
                    result=ReleasePublishResult(
                        channel_id=channel_id, episode_id=item.item_id, platforms=[platform],
                        status="awaiting_approval", blocked_reason="waiting for approval before publishing",
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
        #
        # 2026-08-22 partial-retry grouping fix: check_cross_platform_
        # readiness() reports status="fail" (evidence["existing_intent_
        # status"] set) for a (deliverable, platform) that already has a
        # succeeded/open publication_intent — correct for its own "is this
        # ready for a FRESH attempt" contract, but under the aggregation
        # model that "fail" must NOT remove the platform from this
        # deliverable's grouping set, or an already-succeeded platform
        # (e.g. youtube/tiktok from a prior poll) silently drops out of
        # every group once new platforms (e.g. instagram/facebook) are
        # enabled — producing a fresh {instagram,facebook}-only POST
        # instead of a GET+PUT retry against the existing post. Grouping
        # eligibility only needs "is this platform relevant to this
        # deliverable at all" — ZernioProvider.publish_multi_platform()'s
        # own per-platform guard_and_prepare_attempt() ledger check (not
        # this readiness check) is what actually decides use_existing
        # (PUT-add) vs to_submit (new attempt within the same post) once
        # the platform is included in the group.
        cross_platform_passed_ids_by_platform: Dict[str, set] = {}
        if platform_deliverable_policy:
            cp_result = check_cross_platform_readiness(
                item.item_id, channel_id=channel_id, assets_root=assets_root, db_path=db_path,
                provider_id=provider_id, logical_alias=logical_alias,
                metadata_override=metadata_override_by_item.get(item.item_id),
            )
            for platform in resolved_platforms:
                cross_platform_passed_ids_by_platform[platform] = {
                    c.deliverable_id for c in cp_result.for_platform(platform)
                    if c.status == "pass"
                    or c.evidence.get("existing_intent_status") in ("succeeded", "open")
                }

        allowed_deliverable_ids_by_platform: Dict[str, List[str]] = {}
        for platform in resolved_platforms:
            allowed_deliverable_ids = _allowed_deliverable_ids_for_platform(
                package, platform, platform_deliverable_policy,
            )
            if platform_deliverable_policy:
                allowed_deliverable_ids = [
                    d for d in allowed_deliverable_ids
                    if d in cross_platform_passed_ids_by_platform.get(platform, set())
                ]
            allowed_deliverable_ids_by_platform[platform] = allowed_deliverable_ids

        # Zernio aggregation model: group deliverables by their own
        # IDENTICAL resolved platform-set (e.g. main_episode -> {youtube};
        # every Short -> {youtube, tiktok, instagram, facebook}), then
        # call publish_channel_release() ONCE per group with that group's
        # full platforms list -- never once per platform. A deliverable
        # allowed on zero platforms is excluded entirely (never a call,
        # never an intent -- unchanged from before this grouping existed).
        platforms_by_deliverable: Dict[str, set] = {}
        for platform, ids in allowed_deliverable_ids_by_platform.items():
            for did in ids:
                platforms_by_deliverable.setdefault(did, set()).add(platform)

        groups: Dict[frozenset, List[str]] = {}
        for did, plats in platforms_by_deliverable.items():
            groups.setdefault(frozenset(plats), []).append(did)

        for platform_set, deliverable_ids_in_group in groups.items():
            group_platforms = sorted(platform_set)
            platform_settings_by_platform = {
                p: _resolve_platform_settings_for_call(
                    package, p, deliverable_ids_in_group, platform_deliverable_policy,
                )
                for p in group_platforms
            }
            platform_settings_by_platform = {p: s for p, s in platform_settings_by_platform.items() if s}

            result = publish_channel_release(
                channel_id, item.item_id, group_platforms, package,
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
                deliverable_ids=deliverable_ids_in_group,
                platform_settings=platform_settings_by_platform or None,
            )
            # One ReadyBundle per platform in the group -- preserves the
            # external (item_id, platform) contract run_worker.py's own
            # logging already relies on, while the underlying network
            # call was made exactly once for the whole group. Each
            # bundle's .result is a shallow copy of the SAME shared
            # ReleasePublishResult with .status recomputed to that one
            # platform's own real outcome (_status_for_platform()) --
            # every other field (readiness_by_platform, schedule,
            # publish, blocked_reason) still points at the identical
            # shared data, so a caller inspecting .publish.deliverables
            # still sees the whole group exactly as it was.
            for platform in group_platforms:
                per_platform_result = _dc_replace(result, status=_status_for_platform(result, platform))
                results.append(ReadyBundle(item_id=item.item_id, platform=platform, result=per_platform_result))
    return results
