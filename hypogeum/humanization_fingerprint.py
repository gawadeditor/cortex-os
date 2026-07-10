"""
hypogeum/humanization_fingerprint.py — Canonical Humanization input
fingerprint (single source of truth)

Dependency-neutral by design: standard library only, no hypogeum imports at
all. This is what makes it safe for both hypogeum/agents/humanization_stage.py
and hypogeum/humanization_checkpoint.py to import it without risking a
circular dependency between those two modules — neither imports the other,
and both import only this one, one-directional dependency.

This module exists because hypogeum/agents/humanization_stage.py originally
computed its own humanization.input_hash using a narrower formula (SHA-256
of only the ordered narration texts, non-canonical JSON) than
hypogeum/humanization_checkpoint.py's build_humanization_fingerprint()
(SHA-256 of the full script_package plus episode/mode/version/skill/model
identity, canonical JSON) — meaning no checkpoint the stage produced could
ever pass the checkpoint layer's own reuse check. This module is the single
implementation both now share, resolving that incompatibility instead of
leaving two independent formulas to drift further apart.

Public API:
    STAGE_VERSION            — default stage version string
    WRAPPER_VERSION           — default wrapper version string
    PINNED_HUMANIZER_SHA      — default pinned upstream Humanizer commit SHA
    build_humanization_fingerprint(...) -> str
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

# ---------------------------------------------------------------------------
# Shared default constants — single source of truth for both the stage and
# the checkpoint layer, so a version bump only ever needs to happen here.
# ---------------------------------------------------------------------------

STAGE_VERSION = "0.1.0"
WRAPPER_VERSION = "0.1.0"
PINNED_HUMANIZER_SHA = "1b48564898e999219882660237fde01bf4843a0f"


def build_humanization_fingerprint(
    *,
    episode_id: str,
    script_package: dict,
    mode: str,
    stage_version: str = STAGE_VERSION,
    wrapper_version: str = WRAPPER_VERSION,
    skill_commit_sha: str = PINNED_HUMANIZER_SHA,
    model_id: Optional[str] = None,
) -> str:
    """
    Return a stable SHA-256 hex digest over the complete set of inputs that
    determine whether a previously-computed Humanization result may be
    reused: the full script_package content, the episode identity, the
    requested mode, and every version/identity field that, if changed,
    would mean a prior result no longer describes the current
    configuration.

    Deliberately excluded from the fingerprint: timestamps, generated
    output text, warnings, latency, and token usage — none of these
    describe the *input* configuration, and including any of them would
    make the fingerprint change on every run even when nothing that
    actually determines reusability changed.

    Deterministic: canonical JSON (sort_keys=True, fixed separators,
    ensure_ascii=False) means dict key insertion order never affects the
    result, non-ASCII content (e.g. Arabic narration) is hashed by its
    real characters rather than an escaped representation, and identical
    logical input always produces the same digest.
    """
    payload = {
        "episode_id": episode_id,
        "script_package": script_package,
        "mode": mode,
        "stage_version": stage_version,
        "wrapper_version": wrapper_version,
        "skill_commit_sha": skill_commit_sha,
        "model_id": model_id,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
