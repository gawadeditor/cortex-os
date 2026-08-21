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
    A_METADATA            — title (conflict-checked across
                             metadata_source.episode_title / topic_brief /
                             episodes.topic, unless overridden), description,
                             shorts_titles.
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
    check_publish_readiness(episode_id, *, assets_root, db_path=None,
                             channel_id=None, platforms=(), logical_alias="default",
                             provider_id="zernio", metadata_override=None)
        -> ReadinessCheckResult
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


def _check_metadata(
    episode_id: str,
    episode_dir: Path,
    db_path: Optional[Path],
    metadata_override: Optional[MetadataOverride],
    final_render_package: dict,
) -> GateCheckResult:
    metadata_source = (final_render_package or {}).get("metadata_source", {}) or {}

    # -- title: conflict-checked across every known candidate source unless
    # an operator override is explicitly supplied. Never auto-picks one.
    if metadata_override is not None and metadata_override.title:
        title = metadata_override.title
        title_evidence = {"resolved_by": "metadata_override"}
    else:
        candidates: dict = {}
        ms_title = (metadata_source.get("episode_title") or "").strip()
        if ms_title:
            candidates["metadata_source.episode_title"] = ms_title
        topic_brief = _load_json(episode_dir / f"topic_brief_{episode_id}.json")
        tb_title = (topic_brief.get("episode_title") or "").strip()
        if tb_title:
            candidates["topic_brief.episode_title"] = tb_title
        db_topic = _lookup_episode_topic_from_db(db_path, episode_id)
        if db_topic and db_topic.strip():
            candidates["episodes.topic"] = db_topic.strip()

        distinct_values = sorted(set(candidates.values()))
        if not distinct_values:
            return GateCheckResult(
                capability="A_METADATA", status="fail",
                reason="no title candidate found in metadata_source.episode_title, topic_brief, or episodes.topic",
                evidence={"candidates": candidates},
                remediation="Supply a MetadataOverride(title=...) with an operator-confirmed title, or populate metadata_source.episode_title.",
            )
        if len(distinct_values) > 1:
            return GateCheckResult(
                capability="A_METADATA", status="fail",
                reason=f"{len(distinct_values)} conflicting title candidates found — this check never auto-selects one",
                evidence={"candidates": candidates},
                remediation="Supply a MetadataOverride(title=...) with the operator-confirmed correct title.",
            )
        title = distinct_values[0]
        title_evidence = {"resolved_by": "single_consistent_candidate", "candidates": candidates}

    # -- description
    if metadata_override is not None and metadata_override.description:
        description = metadata_override.description
        description_evidence = {"resolved_by": "metadata_override"}
    else:
        description = (metadata_source.get("description") or "").strip()
        if not description:
            return GateCheckResult(
                capability="A_METADATA", status="fail",
                reason="metadata_source.description is empty and no MetadataOverride.description supplied",
                evidence={"title": title, **title_evidence},
                remediation="Populate metadata_source.description, or supply a MetadataOverride(description=...).",
            )
        description_evidence = {"resolved_by": "metadata_source"}

    # -- shorts_titles (must cover every expected Short with a non-empty title)
    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []
    if metadata_override is not None and metadata_override.shorts_titles is not None:
        shorts_titles = metadata_override.shorts_titles
        shorts_titles_source = "metadata_override"
    else:
        shorts_titles = metadata_source.get("shorts_titles") or []
        shorts_titles_source = "metadata_source"

    if shorts_renders:
        if len(shorts_titles) != len(shorts_renders) or any(not (t or "").strip() for t in shorts_titles):
            return GateCheckResult(
                capability="A_METADATA", status="fail",
                reason=(
                    f"shorts_titles ({len(shorts_titles)} from {shorts_titles_source}) does not cover all "
                    f"{len(shorts_renders)} expected Short(s) with a non-empty title each"
                ),
                evidence={"title": title, "shorts_expected": len(shorts_renders), "shorts_titles": shorts_titles},
                remediation="Populate metadata_source.shorts_titles (one non-empty entry per Short), or supply a MetadataOverride(shorts_titles=...).",
            )

    return GateCheckResult(
        capability="A_METADATA", status="pass",
        reason="title, description, and shorts_titles all present and unambiguous",
        evidence={
            "title": title, **title_evidence,
            "description_length": len(description), "description_resolved_by": description_evidence["resolved_by"],
            "shorts_titles_count": len(shorts_titles), "shorts_titles_source": shorts_titles_source,
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
    assets_root: Path,
    db_path: Optional[Path] = None,
    channel_id: Optional[str] = None,
    platforms: Sequence[str] = (),
    logical_alias: str = "default",
    provider_id: str = "zernio",
    metadata_override: Optional[MetadataOverride] = None,
) -> ReadinessCheckResult:
    """
    Read-only, deterministic. Never mutates any file or database row.

    Loads final_render_package_{episode_id}.json (and, for the title check
    only, topic_brief_{episode_id}.json and episodes.topic) from disk/DB and
    runs all five checks above. A missing package file is not itself fatal
    to every check — each check evaluates what it can from an empty dict/
    missing file and reports its own explicit failure reason.
    """
    episode_dir = Path(assets_root) / "episodes" / episode_id
    final_render_package = _load_json(episode_dir / f"final_render_package_{episode_id}.json")

    checks = [
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
