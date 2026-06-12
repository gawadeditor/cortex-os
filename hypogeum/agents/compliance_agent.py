"""
hypogeum/agents/compliance_agent.py — Rights & Compliance Agent (R48)

Pre-publish compliance gate. Validates AI disclosure, source attribution,
music/license disclosure, and platform metadata requirements. Emits a
compliance_report with an APPROVED, WARNING, or REJECTED verdict.
The Publishing Agent must not run if verdict is REJECTED.

No Claude calls. Pure deterministic evaluation.

Inputs consumed:
  final_render_package   — Assembly Agent output (required); provides
                           metadata_source.ai_disclosure_text, episode_title,
                           section_titles, and music_log
  source_registry        — list of source entries from Research Director (optional)
  music_track_package    — Music Agent output (optional); triggers music license
                           disclosure checks when present
  output_dir             — override output directory (optional)

Checks (6):
  c_ai_disclosure_present  — BLOCKING — ai_disclosure_text non-empty
  c_ai_disclosure_content  — WARN     — text references "AI" or "artificial intelligence"
  c_source_attribution     — WARN     — source_registry non-empty when provided
  c_music_license          — WARN/FAIL — if music_track_package: sourcing_status not "failed"
  c_music_ai_tracks        — WARN     — if music_log has ai_generated entries, disclose
  c_platform_metadata      — BLOCKING — episode_title non-empty for platform compliance

Verdict rules:
  REJECTED — compliant=False; one or more blocking failures
  WARNING  — compliant=True; no failures, one or more warnings
  APPROVED — compliant=True; no failures, no warnings

Public API:
    run_compliance_agent(
        final_render_package, episode_id,
        source_registry=None,
        music_track_package=None,
        output_dir=None,
    ) → dict  (compliance_report, schema-validated, written to disk)

Logging events emitted:
  compliance_agent_started   — info  — inputs received
  compliance_agent_check     — debug — per-check result
  compliance_agent_verdict   — info  — final verdict (APPROVED/WARNING)
  compliance_agent_rejected  — error — verdict=REJECTED with failure list
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "compliance_agent"
_AGENT_VERSION = "1.0"

# Keywords that must appear in the AI disclosure text (case-insensitive).
# At least one must be present for the content check to pass.
_AI_DISCLOSURE_KEYWORDS = ("ai", "artificial intelligence", "generated", "automated")

# Terms banned from titles and descriptions (copyright/brand risk and sensationalism).
# Any match → c_banned_terms FAIL.
_BANNED_TERMS = (
    "conspiracy",
    "cover-up",
    "they don't want you to know",
    "secret society",
    "illuminati",
    "reptilian",
    "shocking",
    "unbelievable",
    "mindblowing",
    "mind-blowing",
    "you won't believe",
    "insane",
    "scientists confirm",
    "proof that",
    "definitive evidence",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_compliance_agent(
    final_render_package: dict,
    episode_id: str,
    source_registry: Optional[list] = None,
    music_track_package: Optional[dict] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Evaluate AI disclosure, source attribution, music licensing, and platform
    metadata requirements. Return a schema-validated compliance_report.

    Args:
        final_render_package:  Final Render Package from Assembly Agent (required).
                               Must contain metadata_source.ai_disclosure_text,
                               episode_title, section_titles, and music_log.
        episode_id:            Episode identifier for file naming and log correlation.
        source_registry:       List of source entries from Research Director (optional).
                               Checked for non-emptiness; content is not validated.
        music_track_package:   Music Agent output (optional). When provided, triggers
                               music sourcing status and license disclosure checks.
        output_dir:            Override directory for the output file.
                               Defaults to .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        compliance_report dict (schema-validated, written to disk).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "compliance_agent_started",
        episode_id=episode_id,
        has_source_registry=source_registry is not None,
        source_registry_count=len(source_registry) if source_registry else 0,
        has_music_track_package=bool(music_track_package),
    )

    report_id = f"cr_{episode_id}_{uuid.uuid4().hex[:8]}"
    evaluated_at = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    metadata_source = final_render_package.get("metadata_source", {})
    ai_disclosure_text = metadata_source.get("ai_disclosure_text", "") or ""
    episode_title = metadata_source.get("episode_title", "") or ""
    music_log = final_render_package.get("music_log", []) or []

    # ------------------------------------------------------------------
    # Check 1 — AI disclosure present (BLOCKING)
    # ------------------------------------------------------------------
    if not ai_disclosure_text.strip():
        _add_check(checks, "c_ai_disclosure_present", "fail",
                   "metadata_source.ai_disclosure_text is empty — AI disclosure required for all platforms")
        failures.append(
            "ai_disclosure_present: ai_disclosure_text is empty — required by platform policies"
        )
    else:
        _add_check(checks, "c_ai_disclosure_present", "pass",
                   f"ai_disclosure_text present ({len(ai_disclosure_text)} chars)")

    # ------------------------------------------------------------------
    # Check 2 — AI disclosure content (WARN)
    # ------------------------------------------------------------------
    disclosure_lower = ai_disclosure_text.lower()
    has_ai_keyword = any(kw in disclosure_lower for kw in _AI_DISCLOSURE_KEYWORDS)

    if ai_disclosure_text.strip() and not has_ai_keyword:
        _add_check(checks, "c_ai_disclosure_content", "warn",
                   "ai_disclosure_text does not reference 'AI', 'artificial intelligence', "
                   "'generated', or 'automated' — disclosure may be insufficient")
        warnings.append(
            "ai_disclosure_content: disclosure text lacks required AI-referencing keywords"
        )
    elif not ai_disclosure_text.strip():
        # Already failed in check 1; record as fail here too for completeness
        _add_check(checks, "c_ai_disclosure_content", "fail",
                   "ai_disclosure_text is empty — content check skipped")
    else:
        _add_check(checks, "c_ai_disclosure_content", "pass",
                   "ai_disclosure_text references AI-related keywords")

    # ------------------------------------------------------------------
    # Check 3 — Source attribution (WARN)
    # ------------------------------------------------------------------
    if source_registry is None:
        _add_check(checks, "c_source_attribution", "warn",
                   "source_registry not provided — source attribution not verified")
        warnings.append(
            "source_attribution: source_registry not provided — cannot verify research sources"
        )
    elif len(source_registry) == 0:
        _add_check(checks, "c_source_attribution", "warn",
                   "source_registry is empty — no sources attributed for this episode")
        warnings.append(
            "source_attribution: source_registry is empty — episode has no attributed sources"
        )
    else:
        _add_check(checks, "c_source_attribution", "pass",
                   f"source_registry has {len(source_registry)} attributed source(s)")

    # ------------------------------------------------------------------
    # Check 4 — Music license disclosure (WARN/FAIL)
    # ------------------------------------------------------------------
    if music_track_package is not None:
        sourcing_status = music_track_package.get("sourcing_status", "failed")
        sections_without_music = music_track_package.get("sections_without_music", [])

        if sourcing_status == "failed":
            _add_check(checks, "c_music_license", "warn",
                       f"music_track_package.sourcing_status=failed — "
                       f"music sourcing failed; unlicensed fallback may be in use")
            warnings.append(
                "music_license: sourcing_status=failed — music license provenance unverified"
            )
        elif sourcing_status == "partial":
            detail = (
                f"music_track_package.sourcing_status=partial"
                + (f"; sections_without_music={sections_without_music}" if sections_without_music else "")
            )
            _add_check(checks, "c_music_license", "warn", detail)
            warnings.append(
                f"music_license: sourcing_status=partial — "
                f"{len(sections_without_music)} section(s) may lack music license coverage"
            )
        else:
            _add_check(checks, "c_music_license", "pass",
                       f"music_track_package.sourcing_status=complete — all tracks licensed")
    else:
        _add_check(checks, "c_music_license", "warn",
                   "no music_track_package provided — music license status not verified")
        warnings.append(
            "music_license: music_track_package not provided — license provenance unverified"
        )

    # ------------------------------------------------------------------
    # Check 5 — AI-generated music tracks disclosed (WARN)
    # ------------------------------------------------------------------
    ai_generated_tracks = [
        entry for entry in music_log
        if entry.get("source") == "ai_generated"
    ]

    if ai_generated_tracks and not has_ai_keyword:
        _add_check(checks, "c_music_ai_tracks", "warn",
                   f"{len(ai_generated_tracks)} AI-generated music track(s) in music_log "
                   f"but ai_disclosure_text does not reference AI — disclosure may be incomplete")
        warnings.append(
            f"music_ai_tracks: {len(ai_generated_tracks)} ai_generated track(s) present "
            f"but disclosure text lacks AI keywords"
        )
    elif ai_generated_tracks:
        _add_check(checks, "c_music_ai_tracks", "pass",
                   f"{len(ai_generated_tracks)} AI-generated music track(s) present; "
                   f"AI disclosure text covers this")
    else:
        _add_check(checks, "c_music_ai_tracks", "pass",
                   "no AI-generated music tracks in music_log")

    # ------------------------------------------------------------------
    # Check 6 — Platform metadata completeness (BLOCKING)
    # ------------------------------------------------------------------
    if not episode_title.strip():
        _add_check(checks, "c_platform_metadata", "fail",
                   "metadata_source.episode_title is empty — "
                   "episode title required for platform compliance upload")
        failures.append(
            "platform_metadata: episode_title is empty — cannot submit to platforms without title"
        )
    else:
        _add_check(checks, "c_platform_metadata", "pass",
                   f"platform metadata complete — title={episode_title!r}")

    # ------------------------------------------------------------------
    # Check 7 — Banned terms in title and description (BLOCKING)
    # ------------------------------------------------------------------
    description = metadata_source.get("description", "") or ""
    combined_text = f"{episode_title} {description}".lower()
    found_terms = [term for term in _BANNED_TERMS if term in combined_text]

    if found_terms:
        terms_str = ", ".join(f"'{t}'" for t in found_terms[:5])
        _add_check(checks, "c_banned_terms", "fail",
                   f"banned or copyright-risk terms found in title/description: {terms_str}")
        failures.append(
            f"banned_terms: title or description contains banned terms — remove before publishing: {terms_str}"
        )
    else:
        _add_check(checks, "c_banned_terms", "pass",
                   "no banned or copyright-risk terms found in title/description")

    # ------------------------------------------------------------------
    # Determine final verdict
    # ------------------------------------------------------------------
    compliant = len(failures) == 0

    if not compliant:
        verdict = "REJECTED"
    elif warnings:
        verdict = "WARNING"
    else:
        verdict = "APPROVED"

    if verdict == "REJECTED":
        log.error(
            "compliance_agent_rejected",
            episode_id=episode_id,
            report_id=report_id,
            failures=failures,
        )
    else:
        log.info(
            "compliance_agent_verdict",
            episode_id=episode_id,
            report_id=report_id,
            verdict=verdict,
            compliant=compliant,
            warning_count=len(warnings),
        )

    report = {
        "report_id": report_id,
        "episode_id": episode_id,
        "evaluated_at": evaluated_at,
        "verdict": verdict,
        "compliant": compliant,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }

    validate("compliance_report", report)
    _write_report(report, episode_id, output_dir)

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_check(
    checks: list,
    check_id: str,
    status: str,
    detail: str,
) -> None:
    checks.append({
        "check_id": check_id,
        "status": status,
        "detail": detail,
    })


def _write_report(
    report: dict,
    episode_id: str,
    output_dir: Optional[Path],
) -> None:
    """Write compliance_report_{episode_id}.json to disk."""
    if output_dir is None:
        output_dir = (
            Path(__file__).resolve().parent.parent.parent
            / ".hypogeum-assets" / "episodes" / episode_id
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"compliance_report_{episode_id}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
