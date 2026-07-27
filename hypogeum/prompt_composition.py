"""
hypogeum/prompt_composition.py — Conditional skill selection for Prompt Writer agents.

Pure, deterministic skill selection. No file I/O beyond the Path objects the
caller already constructed, no network access, no global mutable state, and
no import-time mode resolution — every call to select_skills() takes the
mode as an explicit argument, decided by the caller from that call's own
input.

This module knows nothing about any specific agent, prompt content, or
skill content. Each Prompt Writer agent owns its own list of SkillSpec
objects and passes the mode it has already determined for that call.

Public API:
    KNOWN_MODES           — the mode strings this module recognises
    SkillSpec             — (path, modes) — a candidate skill and the modes it applies to
    SkillSelection         — (paths, reason) — the result of a selection call
    select_skills(specs, mode) -> SkillSelection
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# The only mode strings this module currently recognises. A caller passing
# None or any other string triggers the safe fallback in select_skills().
# "legacy"/"concept_driven" — Thumbnail Prompt Writer (existing).
# "veo_only"/"seedance_only"/"mixed_tier"/"no_tier" — Visual Package
# Director Stage B (Phase B1 Checkpoint 1, 2026-07-18): which provider
# family(s) are present in a scene batch, resolved from that batch's actual
# configured tier->model routing, never a hardcoded tier assumption.
KNOWN_MODES: frozenset[str] = frozenset({
    "legacy", "concept_driven",
    "veo_only", "seedance_only", "mixed_tier", "no_tier",
})


@dataclass(frozen=True)
class SkillSpec:
    """A candidate skill file and the set of modes in which it applies."""

    path: Path
    modes: frozenset[str]


@dataclass(frozen=True)
class SkillSelection:
    """Result of a select_skills() call."""

    paths: tuple[Path, ...]
    reason: str


def select_skills(specs: list[SkillSpec], mode: Optional[str]) -> SkillSelection:
    """
    Select the skill paths applicable to `mode`, preserving the order of
    `specs` and removing duplicate paths.

    Safe fallback: if `mode` is None, or any string not in KNOWN_MODES, every
    spec in `specs` is selected (the full stack) and `reason` records that a
    fallback occurred. An unrecognised or missing mode signal must never
    silently narrow the skill stack — narrowing only happens for a mode this
    module explicitly recognises.
    """
    if mode not in KNOWN_MODES:
        selected = _dedupe(spec.path for spec in specs)
        return SkillSelection(
            paths=selected,
            reason=f"fallback_full_stack:unrecognised_mode={mode!r}",
        )

    selected = _dedupe(spec.path for spec in specs if mode in spec.modes)
    return SkillSelection(
        paths=selected,
        reason=f"mode={mode}:selected={len(selected)}_of_{len(specs)}",
    )


def _dedupe(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)
