"""
hypogeum/skill_registry.py — Centralized internal skill registry

Every skill file in hypogeum/skills/ is registered here with:
  - Its canonical full path (full skill document)
  - Its compact path (runtime-optimized version, where compiled)
  - Its default load mode (compact | full | na)
  - Its type (provider | cinematic | intelligence | governance)
  - The agents it is wired to, and which prompt stage
  - A rationale explaining why it is or is not injected
  - Approximate character counts for budget tracking

This module is the authoritative reference for which agent skills are active.
No skill should be silently unwired. Skills not injected into any agent must
carry an explicit status (GOVERNANCE, SUPERSEDED, PENDING) and a rationale
explaining why they are excluded.

Wiring is performed in each agent module by passing skill paths from this
registry to compose_system_prompt(). The registry itself does not load skills —
it only defines paths, metadata, and provides the diagnostic log helper.

Compact skill files live under hypogeum/skills/compiled/ and are runtime-
optimized versions of the full skill documents. They preserve all hard rules,
evidence level labels, §references, and REQUIRES VERIFICATION flags, while
removing audit prose, examples, version history, and large tables not needed
at runtime. Agents load compact by default; full versions are retained for
human audit and reference only.

Public API:
    SKILLS                               — dict[str, SkillRecord] — all registered skills
    get_skill_path(skill_id, mode)       — Path for a registered skill (mode: "compact"|"full"|"default")
    log_loaded_skills(agent, paths, log) — emit skill_manifest log event

Status constants:
    WIRED      — injected into at least one agent via compose_system_prompt()
    GOVERNANCE — meta/template document; intentionally not injected into agents
    SUPERSEDED — replaced by a newer version; not injected; kept for reference
    PENDING    — authored but not yet wired; must be resolved before production

Load mode constants:
    LOAD_COMPACT — agent uses compact version by default
    LOAD_FULL    — agent uses full version (no compact available)
    LOAD_NA      — skill is not injected into any agent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Skill type constants
# ---------------------------------------------------------------------------

PROVIDER     = "provider"      # Provider-specific prompt translation skill
CINEMATIC    = "cinematic"     # Cinematic/visual grammar rules
INTELLIGENCE = "intelligence"  # Pipeline intelligence and strategy
GOVERNANCE   = "governance"    # Meta/template document

# ---------------------------------------------------------------------------
# Wiring status constants
# ---------------------------------------------------------------------------

WIRED             = "wired"       # Injected into at least one agent
GOVERNANCE_STATUS = "governance"  # Intentionally not injected — meta-document
SUPERSEDED        = "superseded"  # Replaced by a newer skill
PENDING           = "pending"     # Authored but not yet wired

# ---------------------------------------------------------------------------
# Load mode constants
# ---------------------------------------------------------------------------

LOAD_COMPACT = "compact"  # Use compact version by default
LOAD_FULL    = "full"     # Use full version (no compact available)
LOAD_NA      = "na"       # Not injected into any agent


# ---------------------------------------------------------------------------
# Skill file roots
# ---------------------------------------------------------------------------

_SKILLS_ROOT   = Path(__file__).resolve().parent / "skills"
_COMPILED_DIR  = _SKILLS_ROOT / "compiled"
_VI_DIR        = _SKILLS_ROOT / "visual_intelligence"
_VPD_DIR       = _SKILLS_ROOT / "visual_package_director"


# ---------------------------------------------------------------------------
# SkillRecord
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """Metadata for one internal skill file."""
    skill_id:          str
    path:              Path           # Full skill document (canonical source)
    skill_type:        str            # PROVIDER | CINEMATIC | INTELLIGENCE | GOVERNANCE
    status:            str            # WIRED | GOVERNANCE_STATUS | SUPERSEDED | PENDING
    wired_to:          list[str]      # list of "agent_name:stage" strings; empty if not injected
    rationale:         str            # why this skill is or is not wired
    compact_path:      Optional[Path] = None   # Compact runtime version (None if not compiled)
    default_load_mode: str            = LOAD_FULL  # LOAD_COMPACT | LOAD_FULL | LOAD_NA
    full_chars:        int            = 0      # Approximate char count of full skill file
    compact_chars:     int            = 0      # Approximate char count of compact file (0 if none)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SKILLS: dict[str, SkillRecord] = {

    # -----------------------------------------------------------------------
    # Cinematic skills
    # -----------------------------------------------------------------------

    "cinematic_prompt_architecture": SkillRecord(
        skill_id="cinematic_prompt_architecture",
        path=_VPD_DIR / "cinematic_prompt_architecture.md",
        skill_type=CINEMATIC,
        status=WIRED,
        wired_to=[
            "visual_package_director:stage_a_legacy",
            "visual_package_director:stage_b_scene_prompts",
        ],
        rationale=(
            "9-layer cinematic prompt structure specification for VPD scene prompt writing. "
            "Wired to Stage A (legacy path) and Stage B (primary scene prompt generation). "
            "Already wired before B0 — no change. No compact version created in B1: "
            "CPA is a prescriptive spec document and cannot be safely condensed without "
            "risking loss of required layer rules."
        ),
        compact_path=None,
        default_load_mode=LOAD_FULL,
        full_chars=0,   # Large file — exact size not measured; contributes ~10K+ chars to Stage B
        compact_chars=0,
    ),

    "visual_language_engine": SkillRecord(
        skill_id="visual_language_engine",
        path=_VI_DIR / "visual_language_engine.md",
        skill_type=CINEMATIC,
        status=WIRED,
        wired_to=[
            "visual_package_director:stage_a4_presentation_planner",
            "shot_diversity_planner:planning",
        ],
        rationale=(
            "Defines presentation methods, shot scales, camera vocabulary, and composition "
            "rules used across cinematic planning. "
            "VPD Stage A4: already wired before B0. "
            "SDP: wired in B0 — SDP assigns shot_scale/camera_movement using this vocabulary "
            "but was operating without it (audit finding m-02). "
            "B1: compact version created. SDP total (prompt + VLE compact) ~11K chars — "
            "within acceptable budget."
        ),
        compact_path=_COMPILED_DIR / "visual_language_engine.compact.md",
        default_load_mode=LOAD_COMPACT,
        full_chars=11_017,
        compact_chars=5_100,
    ),

    # -----------------------------------------------------------------------
    # Provider translation skills
    # -----------------------------------------------------------------------

    "veo_translation_skill": SkillRecord(
        skill_id="veo_translation_skill",
        path=_SKILLS_ROOT / "veo_translation_skill.md",
        skill_type=PROVIDER,
        status=WIRED,
        wired_to=["visual_package_director:stage_b_scene_prompts"],
        rationale=(
            "Teaches how Veo3 interprets prompt language — vocabulary, ordering, and "
            "structural choices that produce reliable cinematic output. "
            "Injected into Stage B so VPD writes prompts in Veo-compatible vocabulary. "
            "Was silently unwired before B0 (audit finding M-02). "
            "B1: compact version created. Reduces from 64,914 to ~8,000 chars in Stage B."
        ),
        compact_path=_COMPILED_DIR / "veo_translation_skill.compact.md",
        default_load_mode=LOAD_COMPACT,
        full_chars=64_914,
        compact_chars=8_000,
    ),

    "seedance_translation_skill": SkillRecord(
        skill_id="seedance_translation_skill",
        path=_SKILLS_ROOT / "seedance_translation_skill.md",
        skill_type=PROVIDER,
        status=WIRED,
        wired_to=["visual_package_director:stage_b_scene_prompts"],
        rationale=(
            "Teaches how Seedance 2.0 interprets prompt language — behavioral differences "
            "from Veo, vocabulary, and translation rules for Seedance-routed scenes. "
            "Injected into Stage B so VPD can write for Seedance when the provider router "
            "assigns scenes to Seedance. Was silently unwired before B0 (audit finding M-02). "
            "B1: compact version created. Reduces from 56,463 to ~8,200 chars in Stage B."
        ),
        compact_path=_COMPILED_DIR / "seedance_translation_skill.compact.md",
        default_load_mode=LOAD_COMPACT,
        full_chars=56_463,
        compact_chars=8_200,
    ),

    # -----------------------------------------------------------------------
    # Intelligence skills
    # -----------------------------------------------------------------------

    "thumbnail_intelligence_pipeline": SkillRecord(
        skill_id="thumbnail_intelligence_pipeline",
        path=_VI_DIR / "thumbnail_intelligence_pipeline.md",
        skill_type=INTELLIGENCE,
        status=WIRED,
        wired_to=["thumbnail_prompt_writer:phase_1_planning"],
        rationale=(
            "Authoritative pipeline reference for the Thumbnail Director execution sequence. "
            "Injected into thumbnail_prompt_writer Phase 1 so the planning LLM understands "
            "the full stage sequence (CTR Strategy → Composition → Typography → TIG → QA → MUAPI). "
            "Was silently unwired before B0 (audit finding m-01). "
            "B1: compact version created. Source is already small (4,436 chars); compact "
            "removes version headers and prose to ~2,100 chars."
        ),
        compact_path=_COMPILED_DIR / "thumbnail_intelligence_pipeline.compact.md",
        default_load_mode=LOAD_COMPACT,
        full_chars=4_436,
        compact_chars=2_100,
    ),

    # -----------------------------------------------------------------------
    # Governance — intentionally not injected into agents
    # -----------------------------------------------------------------------

    "provider_skill_template": SkillRecord(
        skill_id="provider_skill_template",
        path=_SKILLS_ROOT / "provider_skill_template.md",
        skill_type=GOVERNANCE,
        status=GOVERNANCE_STATUS,
        wired_to=[],
        rationale=(
            "Meta-governance document. Defines the required structure, content, formats, "
            "and evidence taxonomy (L1–L5) for all provider translation skills. "
            "Used by humans authoring new provider skills. Not injected into agents — "
            "contains no provider-specific behavioral information."
        ),
        compact_path=None,
        default_load_mode=LOAD_NA,
        full_chars=0,
        compact_chars=0,
    ),

    # -----------------------------------------------------------------------
    # Superseded — retained for reference only
    # -----------------------------------------------------------------------

    "veo_prompt_skill": SkillRecord(
        skill_id="veo_prompt_skill",
        path=_SKILLS_ROOT / "veo_prompt_skill.md",
        skill_type=PROVIDER,
        status=SUPERSEDED,
        wired_to=[],
        rationale=(
            "Veo prompt translation skill v1.0. Superseded by veo_translation_skill v3.0, "
            "which is the active provider skill for Veo. Not injected into any agent. "
            "Retained in the repository for historical reference."
        ),
        compact_path=None,
        default_load_mode=LOAD_NA,
        full_chars=0,
        compact_chars=0,
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_skill_path(skill_id: str, mode: str = "default") -> Path:
    """
    Return the Path for a registered skill.

    Args:
        skill_id: A key from the SKILLS registry.
        mode:     Load mode selection:
                  "default"  — use the skill's default_load_mode
                  "compact"  — use compact_path; falls back to path if compact_path is None
                  "full"     — always use the full path

    Returns:
        Absolute Path to the skill file.

    Raises:
        KeyError: skill_id is not registered.
    """
    record = SKILLS[skill_id]

    if mode == "full":
        return record.path

    if mode == "compact":
        return record.compact_path if record.compact_path is not None else record.path

    # mode == "default"
    if record.default_load_mode == LOAD_COMPACT and record.compact_path is not None:
        return record.compact_path
    return record.path


def log_loaded_skills(agent_name: str, skill_paths: list[Path], log) -> None:
    """
    Emit a structured log event listing which skills are active for this agent.

    Emits:
        skill_manifest      — info — lists loaded and missing skill filenames
        skill_files_missing — warn — when any expected skill file is absent on disk

    Call this at the start of each agent run function, passing the skill_paths
    that were composed into the agent's system prompt at import time.

    Args:
        agent_name:  The agent identifier (e.g. "visual_package_director").
        skill_paths: The skill Path objects that were passed to compose_system_prompt().
                     Pass an empty list for agents that load no skills.
        log:         A structured logger instance from get_logger().
    """
    loaded: list[str] = []
    missing: list[str] = []

    for path in skill_paths:
        if path.exists():
            loaded.append(path.name)
        else:
            missing.append(path.name)

    log.info(
        "skill_manifest",
        agent=agent_name,
        loaded=loaded,
        missing=missing,
        loaded_count=len(loaded),
        missing_count=len(missing),
    )

    if missing:
        log.warn(
            "skill_files_missing",
            agent=agent_name,
            missing=missing,
        )
