"""
hypogeum/deterministic_allocation.py — Deterministic, plan-derived
output-slot identity for immutable generation envelopes (Factory Core,
video domain).

Fixes a proven defect: `hypogeum.agents.clip_repair_agent.
plan_confirmed_shot_allocations()` derives a shot's `extra_scene_id` by
checking CURRENT FILESYSTEM STATE (does the candidate output path
already exist?) and advancing to the next free slot if so. That
function is correct and still required for its own real purpose --
finding a live, safe slot for a generation attempt that is *actually
about to run* -- but it must never be the source of an *already-
compiled, immutable envelope's* identity: adding, deleting, or merely
discovering an unrelated file on disk changed what a completely
different beat's shot compiled to (`s02__extra2` -> `s02__extra3`),
which changed that shot's `expected_output_identity`,
`deterministic_output_path`, `operation_payload_sha256`, and therefore
its `envelope_id` -- entirely without a compiler, prompt, plan, or
Channel Package version ever changing. See docs/architecture/
PROTECTED_GENERATION_LIFECYCLE_RECONCILIATION.md and docs/architecture/
AUTHORIZED_GENERATION_ENVELOPE.md Sec 4.4 ("no process may update a stale
envelope's checksum field in place").

`resolve_canonical_output_slot()` below is the replacement identity
source for envelope compilation: it is a pure function of the
cinematic coverage plan's own already-stable `shot_order` field and
never touches the filesystem, so an already-compiled envelope's
identity can never change because of what does or does not exist on
disk. Physical output occupancy remains a real, necessary check -- it
is moved to `check_output_collision()`, called only at the actual
pre-execution boundary (never during compilation), where an occupied
canonical path is a hard denial, never a silent reassignment to a
different slot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

__all__ = [
    "OutputSlotIdentity",
    "resolve_canonical_output_slot",
    "check_output_collision",
]


class OutputSlotIdentity:
    """Plain, immutable holder for a canonically-derived output slot --
    no behavior, no filesystem access, no domain-specific validation
    beyond what its own fields already assert."""

    __slots__ = ("extra_scene_id", "output_path", "expected_output_identity")

    def __init__(self, *, extra_scene_id: str, output_path: Path, expected_output_identity: str):
        self.extra_scene_id = extra_scene_id
        self.output_path = output_path
        self.expected_output_identity = expected_output_identity

    def __repr__(self) -> str:
        return f"OutputSlotIdentity(extra_scene_id={self.extra_scene_id!r}, output_path={self.output_path!r})"


def resolve_canonical_output_slot(
    *, episode_id: str, scene_id: str, shot_order: int, short_id: Optional[str] = None,
    assets_root: Path,
) -> OutputSlotIdentity:
    """
    Derives a shot's output-slot identity purely from canonical,
    already-stable plan data: `extra_scene_id = f"{scene_id}__extra
    {shot_order}"`. `shot_order` is the cinematic coverage plan's own
    field for this shot's position within its beat -- set once when the
    plan is authored, never recomputed from runtime state.

    Never reads or checks the filesystem in any way. Two calls with the
    same (scene_id, shot_order, short_id) always produce the identical
    OutputSlotIdentity, regardless of what files exist, were created, or
    were deleted between the two calls -- this is the entire point:
    compiled envelope identity must be reproducible from plan data
    alone.

    Reuses hypogeum.muapi's own canonical path/asset-id conventions
    (scene_file_path()'s "{output_dir}/video/[sh_{short_id}/]sc_
    {scene_id}.mp4" and the matching "episodes/{episode_id}/video/
    [sh_{short_id}/]sc_{scene_id}" asset-id shape) rather than
    reimplementing them -- there is exactly one place in the repository
    that owns this convention.
    """
    if shot_order is None or shot_order < 1:
        raise ValueError(f"shot_order must be a positive integer, got {shot_order!r}")

    from hypogeum.muapi import scene_file_path  # local import: avoid a hard module-load-time dependency

    extra_scene_id = f"{scene_id}__extra{shot_order}"
    output_dir = Path(assets_root) / "episodes" / episode_id
    output_path = scene_file_path(output_dir, extra_scene_id, short_id)
    if short_id:
        expected_output_identity = f"episodes/{episode_id}/video/sh_{short_id}/sc_{extra_scene_id}"
    else:
        expected_output_identity = f"episodes/{episode_id}/video/sc_{extra_scene_id}"

    return OutputSlotIdentity(
        extra_scene_id=extra_scene_id, output_path=output_path, expected_output_identity=expected_output_identity,
    )


def check_output_collision(output_path: Path) -> bool:
    """
    The ONE place physical output occupancy is checked -- deliberately
    separate from `resolve_canonical_output_slot()` above and called
    only at the actual pre-execution boundary (e.g. by
    hypogeum.submission_gate.submit(), never during envelope
    compilation). Returns True if the canonical intended output path is
    already occupied. Callers must treat True as a hard denial (deny
    execution, record an output-collision result, require
    reconciliation or a new envelope) -- never as a signal to silently
    select a different slot.
    """
    return Path(output_path).exists()
