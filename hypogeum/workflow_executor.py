"""
hypogeum/workflow_executor.py — Generic workflow-artifact loader,
resolver, and drift verifier (Factory Core).

Zero channel-specific content: this module knows only the generic
Channel Package directory convention (channel_packages/{channel_id}/
channel_package_manifest.json + the artifact files it lists) and the
generic hypogeum.workflow_registry index. It has no knowledge of
Hypogeum, of any stage name, or of any handler.

The versioned artifact FILE is authoritative, never a cached database
row: verify_and_load_workflow() re-reads the live file from disk on
every call, recomputes its checksum, and fails closed
(WorkflowArtifactUnreadableError / WorkflowDriftError) if the file is
missing, unreadable, unparseable, or its checksum no longer matches
what hypogeum.workflow_registry has on record for the bound
(channel_id, workflow_id, workflow_version). The registry is only ever
consulted for the *expected* checksum to verify against -- it is never
treated as a substitute for reading the file.

StageOrderVerifier is the runtime, per-execution proof that the actual
stage sequence a caller executes matches the verified live definition's
own stage_order, one stage at a time, in real time -- not merely a
one-time check at the top of a run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from hypogeum import workflow_registry as _workflow_registry

__all__ = [
    "WorkflowArtifactUnreadableError",
    "WorkflowDriftError",
    "StageOrderVerifier",
    "channel_package_dir",
    "load_channel_package_manifest",
    "load_workflow_artifact_bytes",
    "compute_channel_package_directory_checksum",
    "verify_and_load_workflow",
]

_PACKAGES_ROOT = Path(__file__).resolve().parent.parent / "channel_packages"


class WorkflowArtifactUnreadableError(Exception):
    """The Channel Package's workflow artifact could not be located,
    read, or parsed -- or no matching entry for (channel_id,
    workflow_id, workflow_version) exists in the registry index yet.
    Always fails closed; never silently falls back to a cached copy."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorkflowDriftError(Exception):
    """The live artifact file's checksum (Channel Package drift) or the
    actual runtime execution order (StageOrderVerifier) no longer
    matches the registered definition. Raised before further execution
    proceeds -- never a warning, never a best-effort continuation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def channel_package_dir(channel_id: str, *, packages_root: Optional[Path] = None) -> Path:
    root = Path(packages_root) if packages_root is not None else _PACKAGES_ROOT
    return root / channel_id


def load_channel_package_manifest(channel_id: str, *, packages_root: Optional[Path] = None) -> dict:
    manifest_path = channel_package_dir(channel_id, packages_root=packages_root) / "channel_package_manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise WorkflowArtifactUnreadableError(f"channel_package_manifest.json unreadable for {channel_id!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowArtifactUnreadableError(f"channel_package_manifest.json is not valid JSON for {channel_id!r}: {exc}") from exc


def _find_workflow_artifact_entry(manifest: dict, workflow_id: str) -> dict:
    for artifact in manifest.get("artifacts", []):
        if artifact.get("role") == "workflow_definition" and artifact.get("workflow_id") == workflow_id:
            return artifact
    raise WorkflowArtifactUnreadableError(
        f"no workflow_definition artifact entry for workflow_id={workflow_id!r} in channel_package_manifest.json"
    )


def load_workflow_artifact_bytes(channel_id: str, workflow_id: str, *, packages_root: Optional[Path] = None) -> bytes:
    """Reads the live workflow artifact file fresh from disk every call
    -- never cached, never trusted from a prior read."""
    manifest = load_channel_package_manifest(channel_id, packages_root=packages_root)
    entry = _find_workflow_artifact_entry(manifest, workflow_id)
    artifact_path = channel_package_dir(channel_id, packages_root=packages_root) / entry["path"]
    try:
        return artifact_path.read_bytes()
    except OSError as exc:
        raise WorkflowArtifactUnreadableError(f"workflow artifact file unreadable at {artifact_path}: {exc}") from exc


def compute_channel_package_directory_checksum(channel_id: str, *, packages_root: Optional[Path] = None) -> str:
    """sha256 over the manifest bytes plus every listed artifact file's
    bytes, in the manifest's own declared order -- used by
    hypogeum.channel_config.compute_channel_package_identity() to fold
    the versioned package directory into the overall Channel Package
    checksum, per the approved plan ("include it in the Channel Package
    manifest and package checksum")."""
    manifest_path = channel_package_dir(channel_id, packages_root=packages_root) / "channel_package_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    hasher = hashlib.sha256()
    hasher.update(manifest_bytes)
    for artifact in manifest.get("artifacts", []):
        artifact_path = channel_package_dir(channel_id, packages_root=packages_root) / artifact["path"]
        hasher.update(artifact_path.read_bytes())
    return hasher.hexdigest()


def verify_and_load_workflow(
    channel_id: str,
    workflow_id: str,
    workflow_version: str,
    *,
    packages_root: Optional[Path] = None,
    registry_db_path: Optional[Path] = None,
) -> dict:
    """
    The one function a real executor calls before dispatching stage 1.

    1. Reads the live artifact file fresh from disk (never cached).
    2. Recomputes its checksum.
    3. Resolves the expected checksum from hypogeum.workflow_registry
       for the exact (channel_id, workflow_id, workflow_version) key --
       fails closed (WorkflowArtifactUnreadableError) if no such
       registration exists.
    4. Compares live checksum against the registered one -- fails closed
       (WorkflowDriftError) on any mismatch.
    5. Returns the parsed LIVE definition (from the file just read, not
       from the registry's own cached copy) so callers always act on
       what is actually on disk right now.
    """
    live_bytes = load_workflow_artifact_bytes(channel_id, workflow_id, packages_root=packages_root)
    live_checksum = _workflow_registry.compute_definition_checksum(live_bytes)

    registered = _workflow_registry.resolve_workflow_version(
        channel_id, workflow_id, workflow_version, db_path=registry_db_path,
    )
    if registered is None:
        raise WorkflowArtifactUnreadableError(
            f"no registered workflow found for {channel_id!r}/{workflow_id!r}@{workflow_version!r} "
            "-- register it first via hypogeum.workflow_registry.register_workflow()"
        )
    if registered.workflow_checksum != live_checksum:
        raise WorkflowDriftError(
            f"live artifact checksum {live_checksum!r} for {channel_id!r}/{workflow_id!r}@{workflow_version!r} "
            f"no longer matches registered checksum {registered.workflow_checksum!r} -- the Channel Package "
            "file changed without a new version being registered"
        )

    return _workflow_registry.parse_workflow_definition(live_bytes)


class StageOrderVerifier:
    """
    Real-time proof that the actual execution order a caller follows
    matches a verified, registered stage_order -- checked at every
    single stage transition, not only once at the top of a run.

    Construct with the stage_order list from a verify_and_load_workflow()
    result; call advance(stage_name) immediately before (or in place of)
    each stage's own start-of-stage bookkeeping. Any deviation --
    unexpected stage, out-of-order stage, extra stage beyond the
    registered list, or the run ending before every registered stage
    ran -- is a WorkflowDriftError, never silently tolerated.
    """

    def __init__(self, stage_order: "list[str]"):
        self._expected = list(stage_order)
        self._index = 0

    def advance(self, stage_name: str) -> None:
        if self._index >= len(self._expected):
            raise WorkflowDriftError(
                f"execution order drift: stage {stage_name!r} started after all "
                f"{len(self._expected)} registered stages already completed"
            )
        expected = self._expected[self._index]
        if stage_name != expected:
            raise WorkflowDriftError(
                f"execution order drift: expected stage {expected!r} at position "
                f"{self._index}, but {stage_name!r} started instead"
            )
        self._index += 1

    def assert_complete(self) -> None:
        """Call after the run finishes (success or handled failure) to
        prove every registered stage actually started -- a run that
        exits early without reaching the end of the registered sequence
        is itself worth being able to detect, though callers may choose
        not to enforce this on a legitimate early-halt path (e.g. an
        Executive Producer REJECT)."""
        if self._index != len(self._expected):
            raise WorkflowDriftError(
                f"execution order drift: only {self._index} of {len(self._expected)} "
                "registered stages started before the run ended"
            )
