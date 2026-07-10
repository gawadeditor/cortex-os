"""
hypogeum/humanization_checkpoint.py — Isolated Humanization Stage checkpoint
persistence and reuse (0.1.0)

Persistence/reuse utility only. This module does not decide whether a stage
should run, does not call run_humanization_stage(), does not write pipeline
state, does not touch the database or run registry, and is not imported by
hypogeum/pipeline.py or any production agent.

Path convention: mirrors hypogeum/agent.py::_resolve_output_path() and
hypogeum/episode_artifacts.py's existing episode-directory layout —
{assets_root}/episodes/{episode_id}/humanized_script_package_{episode_id}.json
— rather than inventing a parallel structure. Every function takes
assets_root as a required, explicit parameter (never defaulted to the real
project's .hypogeum-assets/), so tests can point it at tmp_path and never
touch production data.

Path safety: hypogeum/episode_artifacts.py::safe_episode_asset_path() guards
traversal for a *filename* once already inside a known episode directory,
but does not independently validate episode_id itself before using it to
build that directory path — an episode_id containing "/" or ".." segments
would already have escaped by the time that guard runs. This module adds an
explicit episode_id character check first, then applies the same
resolve()/relative_to() traversal-guard technique as a second, independent
check on the fully-constructed path — defense in depth, not a replacement
for the existing helper's own guarantees.

Atomic write: matches hypogeum/elevenlabs.py::_write_music_library()'s
existing temp-file-then-rename convention (`path.with_suffix(".tmp")` then
an atomic replace), extended with an explicit flush()/fsync() before the
replace, since this module's correctness contract is stricter than that
helper's (a checkpoint must never be observed in a partially-written state).

Load strictness — a deliberate divergence from hypogeum/pipeline.py's
_load_package(): that function silently returns None on any read/parse
failure (malformed JSON is treated identically to "no checkpoint"). This
module raises HumanizationCheckpointError instead, so corruption is never
mistaken for a cache miss — the explicit requirement for this checkpoint
layer.

Logging: intentionally omitted. hypogeum.logger.get_logger() defaults to
appending to .hypogeum-assets/pipeline.log, which would itself be a file
write this isolated module does not perform for any other purpose;
_load_package() (the closest existing "checkpoint utility" precedent) does
not log either. Wiring observability here is a pipeline-integration
decision for a later, separate task.

KNOWN INCOMPATIBILITY (documented per explicit instruction, not silently
worked around): build_humanization_fingerprint() implements the fingerprint
formula required by this module's own specification — a SHA-256 of
{episode_id, script_package, mode, stage_version, wrapper_version,
skill_commit_sha, model_id} in canonical JSON. hypogeum.agents.
humanization_stage.run_humanization_stage() currently computes
humanization.input_hash differently: SHA-256 of only the ordered list of
original narration beat texts, via a non-canonical json.dumps() call (no
sort_keys, no fixed separators). The two hashes are neither the same value
nor derived from the same inputs for the same package. Per this task's
explicit scope boundary, hypogeum/agents/humanization_stage.py is NOT
modified here — load_reusable_humanization_checkpoint() compares the
persisted humanization.input_hash field against a freshly computed
build_humanization_fingerprint() exactly as specified, which means any
checkpoint saved by today's run_humanization_stage() will be reported stale
(reuse returns None) until a future task reconciles the two formulas. This
is expected, safe behavior given the constraint against expanding scope —
never a silent inconsistency.

Public API:
    HumanizationCheckpointError
    build_humanization_fingerprint(...) -> str
    humanization_checkpoint_path(...) -> Path
    save_humanization_checkpoint(...) -> Path
    load_humanization_checkpoint(...) -> Optional[dict]
    load_reusable_humanization_checkpoint(...) -> Optional[dict]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

from hypogeum.schemas import SchemaError, validate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHECKPOINT_FILENAME_TEMPLATE = "humanized_script_package_{episode_id}.json"
_DEFAULT_STAGE_VERSION = "0.1.0"
_DEFAULT_WRAPPER_VERSION = "0.1.0"
_DEFAULT_SKILL_COMMIT_SHA = "1b48564898e999219882660237fde01bf4843a0f"

_UNSAFE_EPISODE_ID_CHARS = re.compile(r"[/\\\x00]")


class HumanizationCheckpointError(Exception):
    """
    Raised for any checkpoint-layer failure: unsafe episode_id, malformed
    JSON, a non-dict top-level value, schema validation failure, or an I/O
    error during read/write. Corruption is never silently treated as a
    cache miss — a truthful None return means "no checkpoint exists",
    nothing else.
    """


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def build_humanization_fingerprint(
    *,
    episode_id: str,
    script_package: dict,
    mode: str,
    stage_version: str = _DEFAULT_STAGE_VERSION,
    wrapper_version: str = _DEFAULT_WRAPPER_VERSION,
    skill_commit_sha: str = _DEFAULT_SKILL_COMMIT_SHA,
    model_id: Optional[str] = None,
) -> str:
    """
    Return a stable SHA-256 hex digest over the complete set of inputs that
    determine whether a previously-computed Humanization result may be
    reused: the full script_package content, the episode identity, the
    requested mode, and every version/identity field that, if changed,
    would mean a prior result no longer describes the current
    configuration.

    Deterministic: canonical JSON (sort_keys=True, fixed separators,
    ensure_ascii=False) means dict key insertion order never affects the
    result, and identical logical input always produces the same digest.
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


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _validate_episode_id_safe(episode_id: str) -> None:
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise HumanizationCheckpointError("episode_id must be a non-empty string")
    if _UNSAFE_EPISODE_ID_CHARS.search(episode_id):
        raise HumanizationCheckpointError(
            f"unsafe episode_id: {episode_id!r} (contains a path separator or null byte)"
        )
    if episode_id in (".", ".."):
        raise HumanizationCheckpointError(f"unsafe episode_id: {episode_id!r}")


def humanization_checkpoint_path(*, assets_root: Path, episode_id: str) -> Path:
    """
    Return the checkpoint path for one episode, guarded against traversal.

    Raises HumanizationCheckpointError if episode_id is unsafe (contains a
    path separator, is empty, or is "." / "..") or if the fully-constructed
    path would somehow resolve outside the episode's own directory.
    """
    _validate_episode_id_safe(episode_id)

    ep_dir = Path(assets_root) / "episodes" / episode_id
    filename = _CHECKPOINT_FILENAME_TEMPLATE.format(episode_id=episode_id)
    candidate = (ep_dir / filename).resolve()

    try:
        candidate.relative_to(ep_dir.resolve())
    except ValueError as exc:
        raise HumanizationCheckpointError(
            f"unsafe episode_id: {episode_id!r} (checkpoint path would escape the episode directory)"
        ) from exc

    return candidate


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_humanization_checkpoint(*, assets_root: Path, episode_id: str, package: dict) -> Path:
    """
    Validate and atomically persist a humanized_script_package checkpoint.

    Never mutates `package`. Never leaves a partially-written final
    checkpoint — writes to a sibling temp file, flushes and fsyncs it, then
    atomically replaces the destination; the temp file is removed if any
    step before the replace fails.

    Raises HumanizationCheckpointError if `package` fails schema validation
    (nothing is written) or if the write itself fails.
    """
    try:
        validate("humanized_script_package", package)
    except SchemaError as exc:
        raise HumanizationCheckpointError(
            f"refusing to save an invalid humanized_script_package: {exc}"
        ) from exc

    path = humanization_checkpoint_path(assets_root=assets_root, episode_id=episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = json.dumps(package, ensure_ascii=False, indent=2)
    tmp_path = path.with_suffix(".tmp")

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise HumanizationCheckpointError(
            f"failed to write checkpoint at {path}: {exc}"
        ) from exc

    return path


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_humanization_checkpoint(*, assets_root: Path, episode_id: str) -> Optional[dict]:
    """
    Load and schema-validate a checkpoint if one exists.

    Returns None only when no checkpoint file exists at the expected path.
    Raises HumanizationCheckpointError for every other failure mode:
    unreadable file, malformed JSON, a non-dict top-level value, or a
    schema-validation failure. Corruption is never returned as None.
    """
    path = humanization_checkpoint_path(assets_root=assets_root, episode_id=episode_id)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HumanizationCheckpointError(f"failed to read checkpoint at {path}: {exc}") from exc

    try:
        package = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HumanizationCheckpointError(
            f"malformed JSON in checkpoint at {path}: {exc}"
        ) from exc

    if not isinstance(package, dict):
        raise HumanizationCheckpointError(
            f"checkpoint at {path} is not a JSON object (got {type(package).__name__})"
        )

    try:
        validate("humanized_script_package", package)
    except SchemaError as exc:
        raise HumanizationCheckpointError(
            f"checkpoint at {path} failed schema validation: {exc}"
        ) from exc

    return package


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------

def load_reusable_humanization_checkpoint(
    *,
    assets_root: Path,
    episode_id: str,
    script_package: dict,
    mode: str,
    stage_version: str = _DEFAULT_STAGE_VERSION,
    wrapper_version: str = _DEFAULT_WRAPPER_VERSION,
    skill_commit_sha: str = _DEFAULT_SKILL_COMMIT_SHA,
    model_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Load a checkpoint only if every reuse condition holds; otherwise None.

    Reuse conditions (all must match): checkpoint exists and is
    schema-valid (a malformed/invalid checkpoint raises inside
    load_humanization_checkpoint() above — it is never treated as "no
    match"), top-level episode_id matches, humanization.mode/
    stage_version/wrapper_version/skill_commit_sha/model_id all match the
    requested values, and the checkpoint's persisted humanization.input_hash
    equals a freshly computed build_humanization_fingerprint() for the
    current inputs. A stale checkpoint (any condition fails) is left on
    disk untouched — this function never deletes or overwrites it.
    """
    package = load_humanization_checkpoint(assets_root=assets_root, episode_id=episode_id)
    if package is None:
        return None

    if package.get("episode_id") != episode_id:
        return None

    h = package.get("humanization", {})
    if h.get("mode") != mode:
        return None
    if h.get("stage_version") != stage_version:
        return None
    if h.get("wrapper_version") != wrapper_version:
        return None
    if h.get("skill_commit_sha") != skill_commit_sha:
        return None
    if h.get("model_id") != model_id:
        return None

    fingerprint = build_humanization_fingerprint(
        episode_id=episode_id,
        script_package=script_package,
        mode=mode,
        stage_version=stage_version,
        wrapper_version=wrapper_version,
        skill_commit_sha=skill_commit_sha,
        model_id=model_id,
    )
    if h.get("input_hash") != fingerprint:
        return None

    return package
