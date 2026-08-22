"""
hypogeum/publishing/readiness.py — Publish-Readiness Check.

Read-only, deterministic check for one episode (long-form + associated
Shorts) answering "is this episode safe to hand to the Zernio stage->publish
runtime glue (hypogeum.publishing.zernio_publish_runtime)?" Never mutates
any file or database row, never uploads, never publishes.

Distinct from hypogeum.agents.production_output_gate, which certifies real
technical output QUALITY (resolution, freeze, burned captions, thumbnail
placeholder detection...). This module owns a separate, non-overlapping
concern: is the episode's PUBLISHING metadata / deliverable-file / provider-
binding / staging-eligibility state complete enough to submit through a
provider at all. Neither module re-implements the other; a caller that
wants both technical QA and publish readiness runs both.

Missing or conflicting metadata blocks publishing clearly — this module
never silently chooses or rewrites a title, description, or Shorts titles.
An explicit, caller-supplied MetadataOverride resolves an otherwise-blocking
metadata gap; this module never invents one on its own.

Checks (each recorded as a GateCheckResult — capability/status/reason/
evidence, never silent):
    CHANNEL_SCOPE          — channel_id is required (no implicit/global
                             fallback) and must be the ONE channel
                             episode_topics actually attributes episode_id
                             to. Missing, ambiguous (no attributing row, or
                             rows disagreeing on channel_id), or mismatched
                             scope fails closed and SHORT-CIRCUITS every
                             other check — no metadata, render, binding, or
                             staging evidence for this episode_id is ever
                             read or returned under the wrong channel_id, so
                             a caller asserting the wrong channel can never
                             observe another channel's episode data through
                             this function.
    A_METADATA             — title (canonical release-package sources only:
                             metadata_source.episode_title / topic_brief.
                             episode_title, conflict-checked against EACH
                             OTHER unless overridden; episodes.topic is a
                             discovery/topic label retained only as
                             provenance evidence, never a blocking
                             candidate), description (canonical sources only:
                             metadata_source.description / metadata_source.
                             logline / topic_brief.logline, conflict-checked
                             the same way), shorts_titles. None of these are
                             ever invented — a genuinely missing field (e.g.
                             no episode in production history has ever had a
                             real, non-placeholder shorts_titles entry) fails
                             closed and is reported as the exact missing
                             field rather than fabricated.
    B_LONG_RENDER          — main_{episode_id}.mp4 exists and is non-empty.
    C_SHORTS_RENDERS       — one real, non-empty short_{i}_{episode_id}.mp4
                             per final_render_package.shorts_renders[] entry.
    D_PUBLISHING_BINDING   — an enabled channel_provider_bindings row exists
                             for (channel_id, 'publishing', platform,
                             logical_alias) -> provider_id, for every
                             requested platform. Passes trivially (with an
                             explicit reason) when no platforms are
                             requested.
    E_MEDIA_STAGING_READY  — an active provider credential exists in
                             Credential Manager, and every deliverable file
                             is a valid, non-empty, size/mime-eligible
                             staging candidate (same enum
                             hypogeum.publishing.zernio_media_staging uses —
                             imported, never re-declared).

Public API:
    MetadataOverride, GateCheckResult, ReadinessCheckResult
    check_publish_readiness(episode_id, *, channel_id, assets_root,
                             db_path=None, platforms=(), logical_alias="default",
                             provider_id="zernio", metadata_override=None)
        -> ReadinessCheckResult
        channel_id is a required keyword argument — there is no default
        and no global fallback; every call must state which channel it is
        evaluating readiness for.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from hypogeum.credential_manager import list_secret_profiles
from hypogeum.publishing.provider_repository import resolve_active_binding
from hypogeum.publishing.zernio_media_staging import (
    _MAX_SIZE_BYTES as _ZERNIO_MAX_SIZE_BYTES,
    _SUPPORTED_CONTENT_TYPES as _ZERNIO_SUPPORTED_CONTENT_TYPES,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class GateCheckResult:
    capability: str
    status: str  # "pass" | "fail"
    reason: str
    evidence: dict = field(default_factory=dict)
    remediation: str = ""


@dataclass
class ReadinessCheckResult:
    passed: bool
    episode_id: str
    evaluated_at: str
    checks: List[GateCheckResult] = field(default_factory=list)
    metadata_override_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "episode_id": self.episode_id,
            "evaluated_at": self.evaluated_at,
            "metadata_override_applied": self.metadata_override_applied,
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass(frozen=True)
class MetadataOverride:
    """
    Explicit, operator-supplied resolution for an otherwise-blocking
    metadata gap. Every field is optional independently — supplying `title`
    resolves only the title conflict/absence check; description and
    shorts_titles are still evaluated from the real package unless also
    overridden. This check never constructs one on its own.
    """
    title: Optional[str] = None
    description: Optional[str] = None
    shorts_titles: Optional[List[str]] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CHANNEL SCOPE — mandatory Factory isolation invariant, evaluated first
# ---------------------------------------------------------------------------

def _check_channel_scope(episode_id: str, channel_id: Optional[str], db_path: Optional[Path]) -> GateCheckResult:
    """
    channel_id is a required argument with no default and no global
    fallback (see module docstring). This check additionally proves the
    caller's asserted channel_id actually matches the ONE channel
    episode_topics attributes episode_id to — episode_topics.channel_id is
    the only real, existing attribution of an episode to a channel
    (episodes itself has no channel_id column; hypogeum.run_registry.
    _sync_episode_row() uses the same join). Missing, ambiguous, or
    mismatched scope fails closed; never repaired with a default or a
    guess.
    """
    if not channel_id:
        return GateCheckResult(
            capability="CHANNEL_SCOPE", status="fail",
            reason="channel_id is required and was not supplied — channel scope cannot be proven",
            remediation="Supply the explicit channel_id this readiness check is being evaluated for.",
        )

    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT channel_id FROM episode_topics WHERE episode_id = ?", (episode_id,),
        ).fetchall()
    finally:
        conn.close()
    attributed = {r["channel_id"] for r in rows}

    if not attributed:
        return GateCheckResult(
            capability="CHANNEL_SCOPE", status="fail",
            reason=f"no episode_topics row attributes episode_id={episode_id!r} to any channel — channel scope cannot be proven",
            evidence={"asserted_channel_id": channel_id},
            remediation="Verify episode_id is correct and has at least one episode_topics row.",
        )
    if attributed != {channel_id}:
        return GateCheckResult(
            capability="CHANNEL_SCOPE", status="fail",
            reason=(
                f"episode_id={episode_id!r} is attributed to channel(s) {sorted(attributed)}, "
                f"not the asserted channel_id={channel_id!r}"
            ),
            evidence={"asserted_channel_id": channel_id, "attributed_channel_ids": sorted(attributed)},
            remediation="This episode does not belong to the asserted channel — do not proceed under this channel_id.",
        )
    return GateCheckResult(
        capability="CHANNEL_SCOPE", status="pass",
        reason=f"episode_id={episode_id!r} confirmed attributed to channel_id={channel_id!r}",
        evidence={"channel_id": channel_id},
    )


# ---------------------------------------------------------------------------
# A. METADATA
# ---------------------------------------------------------------------------

def _lookup_episode_topic_from_db(db_path: Optional[Path], episode_id: str) -> Optional[str]:
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT topic FROM episodes WHERE episode_id = ?", (episode_id,),
        ).fetchone()
        return row["topic"] if row and row["topic"] else None
    finally:
        conn.close()


class MetadataResolutionError(RuntimeError):
    """
    Raised by resolve_canonical_metadata() when title/description/any
    required Short title cannot be resolved unambiguously. Carries the
    same reason/evidence/remediation _check_metadata() reports as a
    failed GateCheckResult — factored out into one shared resolver so a
    real publish call (hypogeum.publishing.zernio_channel_release_trigger.
    build_publish_package_from_episode_dir()) uses the EXACT SAME
    resolution readiness already validated, rather than risking a second,
    independently-written implementation that could silently diverge
    (readiness saying "pass, title is X" while the real request sends a
    different or empty value).
    """
    def __init__(self, reason: str, evidence: dict, remediation: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence
        self.remediation = remediation


@dataclass(frozen=True)
class ResolvedMetadata:
    title: str
    description: str
    shorts_titles: dict  # {canonical deliverable_id: title}
    title_evidence: dict = field(default_factory=dict)
    description_evidence: dict = field(default_factory=dict)
    shorts_titles_source: str = "metadata_source"


def resolve_canonical_metadata(
    episode_id: str,
    episode_dir: Path,
    db_path: Optional[Path],
    metadata_override: Optional[MetadataOverride],
    final_render_package: dict,
) -> ResolvedMetadata:
    """
    The exact title/description/shorts_titles resolution _check_metadata()
    performs, minus the pass/fail packaging — see that function and
    MetadataResolutionError's own docstrings. Raises
    MetadataResolutionError (never returns a partial/guessed result) on
    any of the same conditions _check_metadata() would report as a
    failure.
    """
    metadata_source = (final_render_package or {}).get("metadata_source", {}) or {}

    # -- title: resolved from canonical completed-release-package sources
    # only (metadata_source.episode_title, topic_brief.episode_title) —
    # conflict-checked against EACH OTHER, unless an operator override is
    # explicitly supplied. episodes.topic is a discovery/topic label, not
    # canonical publication metadata — it is retained below purely as
    # provenance evidence and can NEVER conflict-block a real canonical
    # title, and never contributes a candidate value when the canonical
    # sources are silent (a discovery label is not a publication title).
    # This check never auto-picks between two conflicting CANONICAL
    # candidates; only a genuine metadata_source/topic_brief disagreement
    # blocks.
    if metadata_override is not None and metadata_override.title:
        title = metadata_override.title
        title_evidence = {"resolved_by": "metadata_override"}
    else:
        canonical_candidates: dict = {}
        ms_title = (metadata_source.get("episode_title") or "").strip()
        if ms_title:
            canonical_candidates["metadata_source.episode_title"] = ms_title
        topic_brief = _load_json(episode_dir / f"topic_brief_{episode_id}.json")
        tb_title = (topic_brief.get("episode_title") or "").strip()
        if tb_title:
            canonical_candidates["topic_brief.episode_title"] = tb_title

        db_topic = _lookup_episode_topic_from_db(db_path, episode_id)
        discovery_topic_provenance = db_topic.strip() if db_topic and db_topic.strip() else None

        distinct_canonical_values = sorted(set(canonical_candidates.values()))
        if not distinct_canonical_values:
            raise MetadataResolutionError(
                reason="no canonical publication title found in metadata_source.episode_title or topic_brief.episode_title",
                evidence={"canonical_candidates": canonical_candidates, "discovery_topic_provenance": discovery_topic_provenance},
                remediation="Supply a MetadataOverride(title=...) with an operator-confirmed title, or populate metadata_source.episode_title.",
            )
        if len(distinct_canonical_values) > 1:
            raise MetadataResolutionError(
                reason=f"{len(distinct_canonical_values)} conflicting CANONICAL title candidates found (metadata_source vs. topic_brief) — this check never auto-selects one",
                evidence={"canonical_candidates": canonical_candidates, "discovery_topic_provenance": discovery_topic_provenance},
                remediation="Supply a MetadataOverride(title=...) with the operator-confirmed correct title.",
            )
        title = distinct_canonical_values[0]
        title_evidence = {
            "resolved_by": "single_consistent_canonical_candidate",
            "canonical_candidates": canonical_candidates,
            "discovery_topic_provenance": discovery_topic_provenance,
        }

    # -- description: resolved from canonical completed-release-package
    # sources only (metadata_source.description, metadata_source.logline,
    # topic_brief.logline), conflict-checked against EACH OTHER exactly like
    # title above, unless an operator override is explicitly supplied.
    # metadata_source.logline and topic_brief.logline are real, existing,
    # already-generated package fields (the Executive Producer's logline is
    # copied into metadata_source.description/.logline by every real
    # production episode observed to date) — never a fabricated value. This
    # check never invents or regenerates copy; it only reuses what an
    # earlier agent already produced.
    if metadata_override is not None and metadata_override.description:
        description = metadata_override.description
        description_evidence = {"resolved_by": "metadata_override"}
    else:
        description_candidates: dict = {}
        ms_description = (metadata_source.get("description") or "").strip()
        if ms_description:
            description_candidates["metadata_source.description"] = ms_description
        ms_logline = (metadata_source.get("logline") or "").strip()
        if ms_logline:
            description_candidates["metadata_source.logline"] = ms_logline
        topic_brief = _load_json(episode_dir / f"topic_brief_{episode_id}.json")
        tb_logline = (topic_brief.get("logline") or "").strip()
        if tb_logline:
            description_candidates["topic_brief.logline"] = tb_logline

        distinct_description_values = sorted(set(description_candidates.values()))
        if not distinct_description_values:
            raise MetadataResolutionError(
                reason=(
                    "no canonical description found in metadata_source.description, "
                    "metadata_source.logline, or topic_brief.logline"
                ),
                evidence={"title": title, **title_evidence, "description_candidates": description_candidates},
                remediation="Populate metadata_source.description (or topic_brief.logline), or supply a MetadataOverride(description=...).",
            )
        if len(distinct_description_values) > 1:
            raise MetadataResolutionError(
                reason=f"{len(distinct_description_values)} conflicting CANONICAL description candidates found — this check never auto-selects one",
                evidence={"title": title, **title_evidence, "description_candidates": description_candidates},
                remediation="Supply a MetadataOverride(description=...) with the operator-confirmed correct description.",
            )
        description = distinct_description_values[0]
        description_evidence = {
            "resolved_by": "single_consistent_canonical_candidate",
            "description_candidates": description_candidates,
        }

    # -- shorts_titles (must cover every expected Short, by canonical
    # deliverable_id, with a non-empty, non-placeholder title).
    #
    # Two accepted shapes, matched generically (never assumes which one a
    # caller uses):
    #   - a flat list of strings (legacy/override shape; matched
    #     POSITIONALLY against shorts_renders — the original, pre-existing
    #     behavior, unchanged for backward compatibility);
    #   - a list of {"short_id": <canonical deliverable_id>, "title": ...}
    #     dicts (the shape every real completed episode in production
    #     history that ever populated this field actually uses — matched
    #     by deliverable_id, robust to reordering).
    #
    # A title that is literally equal to its own deliverable_id/short_id
    # (case-insensitive) is treated as the historical placeholder pattern
    # (confirmed across every episode in production history that ever
    # populated shorts_titles: 'short_001' -> title 'short_001', etc.) —
    # never accepted as a real title.
    from hypogeum.publishing.deliverable_ids import resolve_short_deliverable_id  # noqa: PLC0415

    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []
    expected_short_ids = [resolve_short_deliverable_id(shorts_renders[i], i) for i in range(len(shorts_renders))]

    if metadata_override is not None and metadata_override.shorts_titles is not None:
        shorts_titles = metadata_override.shorts_titles
        shorts_titles_source = "metadata_override"
    else:
        shorts_titles = metadata_source.get("shorts_titles") or []
        shorts_titles_source = "metadata_source"

    def _is_placeholder(deliverable_id: str, candidate_title: str) -> bool:
        return candidate_title.strip().lower() == deliverable_id.strip().lower()

    if shorts_renders:
        if shorts_titles and isinstance(shorts_titles[0], dict):
            title_by_id = {
                (entry.get("short_id") or "").strip(): (entry.get("title") or "").strip()
                for entry in shorts_titles if isinstance(entry, dict)
            }
            missing = [sid for sid in expected_short_ids if not title_by_id.get(sid)]
            placeholder = [
                sid for sid in expected_short_ids
                if title_by_id.get(sid) and _is_placeholder(sid, title_by_id[sid])
            ]
        else:
            # Legacy positional shape: only count + non-empty is verifiable
            # (no deliverable_id carried by a bare string), matching the
            # original pre-existing behavior exactly.
            missing = expected_short_ids if (len(shorts_titles) != len(shorts_renders) or any(not (t or "").strip() for t in shorts_titles)) else []
            placeholder = [
                expected_short_ids[i] for i, t in enumerate(shorts_titles)
                if i < len(expected_short_ids) and _is_placeholder(expected_short_ids[i], (t or ""))
            ]

        if missing:
            raise MetadataResolutionError(
                reason=(
                    f"shorts_titles ({shorts_titles_source}) does not cover all {len(shorts_renders)} "
                    f"expected Short(s) with a non-empty title each — missing: {missing}"
                ),
                evidence={"title": title, "shorts_expected": expected_short_ids, "shorts_titles": shorts_titles, "missing_deliverable_ids": missing},
                remediation="Populate metadata_source.shorts_titles (one non-empty, non-placeholder entry per Short), or supply a MetadataOverride(shorts_titles=...).",
            )
        if placeholder:
            raise MetadataResolutionError(
                reason=(
                    f"shorts_titles ({shorts_titles_source}) contains placeholder title(s) equal to their "
                    f"own deliverable_id — never a real title: {placeholder}"
                ),
                evidence={"title": title, "shorts_expected": expected_short_ids, "shorts_titles": shorts_titles, "placeholder_deliverable_ids": placeholder},
                remediation="Replace the placeholder value(s) with real, accurate Short titles.",
            )

    # Build the canonical {deliverable_id: title} shorts_titles map,
    # regardless of which input shape was supplied — this is the shape a
    # real publish call actually needs (one title per deliverable_id), not
    # the raw list/dict-list a package file happens to store on disk.
    if not shorts_renders:
        resolved_shorts_titles: dict = {}
    elif shorts_titles and isinstance(shorts_titles[0], dict):
        resolved_shorts_titles = {sid: title_by_id[sid] for sid in expected_short_ids}
    else:
        resolved_shorts_titles = {
            expected_short_ids[i]: (shorts_titles[i] or "").strip() for i in range(len(expected_short_ids))
        }

    shorts_titles_source_final = "metadata_override" if (metadata_override is not None and metadata_override.shorts_titles is not None) else "metadata_source"
    return ResolvedMetadata(
        title=title, description=description, shorts_titles=resolved_shorts_titles,
        title_evidence=title_evidence, description_evidence=description_evidence,
        shorts_titles_source=shorts_titles_source_final,
    )


def _check_metadata(
    episode_id: str,
    episode_dir: Path,
    db_path: Optional[Path],
    metadata_override: Optional[MetadataOverride],
    final_render_package: dict,
) -> GateCheckResult:
    try:
        resolved = resolve_canonical_metadata(episode_id, episode_dir, db_path, metadata_override, final_render_package)
    except MetadataResolutionError as exc:
        return GateCheckResult(
            capability="A_METADATA", status="fail",
            reason=exc.reason, evidence=exc.evidence, remediation=exc.remediation,
        )
    return GateCheckResult(
        capability="A_METADATA", status="pass",
        reason="title, description, and shorts_titles all present and unambiguous",
        evidence={
            "title": resolved.title, **resolved.title_evidence,
            "description_length": len(resolved.description), "description_resolved_by": resolved.description_evidence["resolved_by"],
            "shorts_titles_count": len(resolved.shorts_titles), "shorts_titles_source": resolved.shorts_titles_source,
        },
    )


# ---------------------------------------------------------------------------
# B. LONG RENDER
# ---------------------------------------------------------------------------

def _check_long_render(episode_id: str, episode_dir: Path) -> GateCheckResult:
    main_mp4 = episode_dir / f"main_{episode_id}.mp4"
    if not main_mp4.exists():
        return GateCheckResult(
            capability="B_LONG_RENDER", status="fail",
            reason=f"main render file not found on disk: {main_mp4}",
            remediation="Regenerate the main episode render via Assembly Agent.",
        )
    if main_mp4.stat().st_size == 0:
        return GateCheckResult(
            capability="B_LONG_RENDER", status="fail",
            reason=f"main render file is empty (0 bytes): {main_mp4}",
            remediation="Regenerate the main episode render via Assembly Agent.",
        )
    return GateCheckResult(
        capability="B_LONG_RENDER", status="pass",
        reason="main render file present and non-empty",
        evidence={"path": str(main_mp4), "size_bytes": main_mp4.stat().st_size},
    )


# ---------------------------------------------------------------------------
# C. SHORTS RENDERS
# ---------------------------------------------------------------------------

def _check_shorts_renders(episode_id: str, episode_dir: Path, final_render_package: dict) -> GateCheckResult:
    shorts = (final_render_package or {}).get("shorts_renders", []) or []
    if not shorts:
        return GateCheckResult(
            capability="C_SHORTS_RENDERS", status="pass",
            reason="no Shorts expected for this episode", evidence={},
        )
    missing = []
    for i, sr in enumerate(shorts):
        p = episode_dir / f"short_{i}_{episode_id}.mp4"
        if not p.exists() or p.stat().st_size == 0:
            missing.append({"index": i, "short_id": sr.get("short_id"), "expected_path": str(p)})
    if missing:
        return GateCheckResult(
            capability="C_SHORTS_RENDERS", status="fail",
            reason=f"{len(missing)}/{len(shorts)} expected Short render file(s) missing or empty",
            evidence={"missing": missing},
            remediation="Regenerate the missing Short render(s) via Assembly Agent.",
        )
    return GateCheckResult(
        capability="C_SHORTS_RENDERS", status="pass",
        reason=f"all {len(shorts)} expected Short render file(s) present and non-empty",
        evidence={"count": len(shorts)},
    )


# ---------------------------------------------------------------------------
# D. PUBLISHING BINDING
# ---------------------------------------------------------------------------

def _check_publishing_binding(
    channel_id: Optional[str], platforms: Sequence[str], logical_alias: str,
    provider_id: str, db_path: Optional[Path],
) -> GateCheckResult:
    if not platforms:
        return GateCheckResult(
            capability="D_PUBLISHING_BINDING", status="pass",
            reason="no platforms requested for this check", evidence={},
        )
    if not channel_id:
        return GateCheckResult(
            capability="D_PUBLISHING_BINDING", status="fail",
            reason="channel_id is required to resolve publishing bindings but was not supplied",
        )
    missing = []
    for platform in platforms:
        binding = resolve_active_binding(
            db_path, channel_id=channel_id, capability="publishing",
            platform=platform, logical_alias=logical_alias,
        )
        if binding is None or binding.get("provider_id") != provider_id:
            missing.append(platform)
    if missing:
        return GateCheckResult(
            capability="D_PUBLISHING_BINDING", status="fail",
            reason=f"no enabled channel_provider_bindings row for provider_id={provider_id!r}, platform(s): {missing}",
            evidence={"channel_id": channel_id, "missing_platforms": missing},
            remediation="Create real channel_provider_bindings rows (hypogeum.publishing.provider_repository.bind_channel_provider) for these platforms before publishing through this provider.",
        )
    return GateCheckResult(
        capability="D_PUBLISHING_BINDING", status="pass",
        reason=f"enabled binding confirmed for provider_id={provider_id!r}, platform(s): {list(platforms)}",
        evidence={"channel_id": channel_id, "platforms": list(platforms)},
    )


# ---------------------------------------------------------------------------
# E. MEDIA STAGING READY
# ---------------------------------------------------------------------------

def _check_media_staging_ready(
    episode_id: str, episode_dir: Path, final_render_package: dict,
    provider_id: str, db_path: Optional[Path],
) -> GateCheckResult:
    profiles = list_secret_profiles(provider=provider_id, scope="factory", db_path=db_path)
    active = [p for p in profiles if p.status == "active"]
    if not active:
        return GateCheckResult(
            capability="E_MEDIA_STAGING_READY", status="fail",
            reason=f"no active provider={provider_id!r} secret profile found in Credential Manager",
            remediation="Create an active secret profile for this provider before any staging attempt.",
        )

    deliverable_paths = [episode_dir / f"main_{episode_id}.mp4"]
    for i, _sr in enumerate((final_render_package or {}).get("shorts_renders", []) or []):
        deliverable_paths.append(episode_dir / f"short_{i}_{episode_id}.mp4")

    problems: List[str] = []
    for p in deliverable_paths:
        if not p.exists():
            problems.append(f"{p.name}: file missing")
            continue
        size = p.stat().st_size
        if size == 0:
            problems.append(f"{p.name}: file is empty (0 bytes)")
            continue
        if size > _ZERNIO_MAX_SIZE_BYTES:
            problems.append(f"{p.name}: {size} bytes exceeds the provider's documented size limit")
        mime = "video/mp4"
        if mime not in _ZERNIO_SUPPORTED_CONTENT_TYPES:
            problems.append(f"{p.name}: mime_type {mime!r} is not in the provider's supported set")

    if problems:
        return GateCheckResult(
            capability="E_MEDIA_STAGING_READY", status="fail",
            reason=f"{len(problems)} deliverable(s) not eligible for staging",
            evidence={"problems": problems},
            remediation="Fix the offending deliverable file(s) before attempting to stage.",
        )
    return GateCheckResult(
        capability="E_MEDIA_STAGING_READY", status="pass",
        reason=f"active credential present; {len(deliverable_paths)} deliverable(s) staging-eligible",
        evidence={"deliverable_count": len(deliverable_paths)},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_publish_readiness(
    episode_id: str,
    *,
    channel_id: str,
    assets_root: Path,
    db_path: Optional[Path] = None,
    platforms: Sequence[str] = (),
    logical_alias: str = "default",
    provider_id: str = "zernio",
    metadata_override: Optional[MetadataOverride] = None,
) -> ReadinessCheckResult:
    """
    Read-only, deterministic. Never mutates any file or database row.

    channel_id is required — there is no default and no global fallback
    (see module docstring and mandatory Factory isolation invariant). The
    CHANNEL_SCOPE check runs first and, on any failure (missing,
    ambiguous, or mismatched scope), SHORT-CIRCUITS: no other check runs,
    and no metadata/render/binding/staging evidence for episode_id is ever
    read or included in the result — a caller asserting the wrong
    channel_id can never observe another channel's episode data through
    this function, not even in a failed check's evidence.

    Once channel scope is proven, loads final_render_package_{episode_id}.json
    (and, for the title check only, topic_brief_{episode_id}.json and
    episodes.topic) from disk/DB and runs the remaining five checks. A
    missing package file is not itself fatal to every check — each check
    evaluates what it can from an empty dict/missing file and reports its
    own explicit failure reason.
    """
    scope_check = _check_channel_scope(episode_id, channel_id, db_path)
    if scope_check.status != "pass":
        return ReadinessCheckResult(
            passed=False, episode_id=episode_id, evaluated_at=_now_iso(),
            checks=[scope_check], metadata_override_applied=metadata_override is not None,
        )

    episode_dir = Path(assets_root) / "episodes" / episode_id
    final_render_package = _load_json(episode_dir / f"final_render_package_{episode_id}.json")

    checks = [
        scope_check,
        _check_metadata(episode_id, episode_dir, db_path, metadata_override, final_render_package),
        _check_long_render(episode_id, episode_dir),
        _check_shorts_renders(episode_id, episode_dir, final_render_package),
        _check_publishing_binding(channel_id, platforms, logical_alias, provider_id, db_path),
        _check_media_staging_ready(episode_id, episode_dir, final_render_package, provider_id, db_path),
    ]

    return ReadinessCheckResult(
        passed=all(c.status == "pass" for c in checks),
        episode_id=episode_id,
        evaluated_at=_now_iso(),
        checks=checks,
        metadata_override_applied=metadata_override is not None,
    )
