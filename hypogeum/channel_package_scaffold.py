"""
hypogeum/channel_package_scaffold.py — give a new channel its Channel Package.

Every channel needs a ``channel_packages/<channel_id>/`` directory (workflow
definition + policies + a manifest) before it can resolve a workflow
binding, compute its package identity, or be granted a Channel Operating
Grant. The first channel (hypogeum) has one committed to the repo; a
channel created from the dashboard does not.

This copies the hypogeum package as the template — the workflow, the
platform-distribution policy and the deliverable contract are
topic-agnostic and shared — swapping only the manifest's ``channel_id``,
then registers the workflow definition under the new channel_id.

Idempotent: a no-op when the directory already exists.

Public API:
    channel_package_exists(channel_id) -> bool
    scaffold_channel_package(channel_id, *, db_path=None, template="hypogeum") -> dict
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGES = _ROOT / "channel_packages"


def channel_package_exists(channel_id: str, *, packages_root: Optional[Path] = None) -> bool:
    root = Path(packages_root) if packages_root is not None else _PACKAGES
    return (root / channel_id / "channel_package_manifest.json").is_file()


def channel_outro_path(
    channel_id: str, *, packages_root: Optional[Path] = None,
) -> Optional[Path]:
    """The channel's fixed branded outro clip (``channel_packages/<id>/outro.mp4``),
    or None when the channel has none. Assembly appends it verbatim to the end
    of every episode's main render — a per-channel sign-off (e.g. a gentle
    "follow for a story tomorrow" for a bedtime channel). Opt-in purely by the
    file's presence; no config, no code change per channel."""
    if not channel_id:
        return None
    root = Path(packages_root) if packages_root is not None else _PACKAGES
    p = root / channel_id / "outro.mp4"
    return p if p.is_file() else None


def channel_outro_policy(
    channel_id: str, *, packages_root: Optional[Path] = None,
) -> Optional[dict]:
    """The channel's outro policy (``channel_packages/<id>/outro_policy.json``),
    or None when the channel has no policy file.

    ``{"mode": "generated", "follow_line": "...", "tie_directive": "..."}``
    means the Script Engine appends a closing "outro" beat to every episode —
    a gentle one-sentence tie plus ``follow_line`` verbatim, narrated in the
    channel voice — and that beat gets its own generated shots through the
    normal pipeline (no static outro.mp4). Any other / missing policy leaves
    the static ``channel_outro_path`` behaviour unchanged."""
    if not channel_id:
        return None
    root = Path(packages_root) if packages_root is not None else _PACKAGES
    p = root / channel_id / "outro_policy.json"
    if not p.is_file():
        return None
    try:
        pol = json.loads(p.read_text(encoding="utf-8"))
        return pol if isinstance(pol, dict) else None
    except Exception:  # noqa: BLE001
        return None


def channel_i2v_disabled(
    channel_id: str, *, packages_root: Optional[Path] = None,
) -> bool:
    """
    Whether this channel's Visual Package Director must never request an
    image-to-video (I2V) scene, per ``channel_packages/<id>/i2v_policy.json``
    ``{"mode": "disabled"}``. Same opt-in-by-file pattern as
    channel_outro_policy — no config, no code change per channel, absence
    of the file means unchanged (I2V allowed).

    Added 2026-09-05: confirmed live (ep_5f72565c, "Pip the Hedgehog") that
    wan2.7-image-to-video's own reference-frame-leak defect has no reliable
    fix for every clip -- hypogeum.source_enhancement.
    check_and_trim_reference_frame_leak() cleanly fixes a fast hard-cut
    leak, but some clips are a slow, multi-second CROSSFADE from the
    conditioning image into real content with no stable cut point at all
    (measured live: the frame-vs-frame-0 diff never plateaus across a 3s
    scan — it is still climbing when the scan ends). No downstream trim
    can fix that shape; the only reliable fix is not requesting I2V in the
    first place. When set, Visual Package Director skips
    generate_entity_reference_images() entirely (no hosted reference URLs
    are produced), so every scene naturally resolves to the channel's T2V
    model instead -- the exact fallback the operator authorized.
    """
    if not channel_id:
        return False
    root = Path(packages_root) if packages_root is not None else _PACKAGES
    p = root / channel_id / "i2v_policy.json"
    if not p.is_file():
        return False
    try:
        pol = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(pol, dict) and str(pol.get("mode", "")).lower() == "disabled"
    except Exception:  # noqa: BLE001
        return False


def scaffold_channel_package(channel_id: str, *, db_path: Optional[Path] = None,
                             template: str = "hypogeum",
                             packages_root: Optional[Path] = None) -> dict:
    """
    Create channel_packages/<channel_id>/ from the template and register its
    workflow. Returns {ok, created: bool, workflows_registered: [...], error?}.
    Never raises.
    """
    if not channel_id or "/" in channel_id or channel_id.startswith("."):
        return {"ok": False, "created": False, "error": f"bad channel_id {channel_id!r}"}

    root = Path(packages_root) if packages_root is not None else _PACKAGES
    dst = root / channel_id
    if channel_package_exists(channel_id, packages_root=root):
        return _register_from_manifest(channel_id, db_path, created=False, packages_root=root)

    src = root / template
    if not (src / "channel_package_manifest.json").is_file():
        return {"ok": False, "created": False,
                "error": f"template package {template!r} not found"}

    try:
        shutil.copytree(src, dst)
        manifest_path = dst / "channel_package_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["channel_id"] = channel_id
        # keep artifact workflow_id / versions / sha256 — the workflow is a
        # shared, content-identical template; run_pipeline binds by the
        # template's workflow_id.
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # roll back a half-made copy
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "created": False, "error": f"copy failed: {exc}"}

    return _register_from_manifest(channel_id, db_path, created=True, packages_root=root)


def _register_from_manifest(channel_id: str, db_path, *, created: bool,
                            packages_root: Optional[Path] = None) -> dict:
    root = Path(packages_root) if packages_root is not None else _PACKAGES
    registered: list[str] = []
    try:
        from hypogeum.workflow_executor import load_channel_package_manifest
        from hypogeum.workflow_registry import register_workflow, WorkflowIntegrityError
        manifest = load_channel_package_manifest(channel_id, packages_root=root)
        for art in manifest.get("artifacts", []):
            if art.get("role") != "workflow_definition":
                continue
            path = root / channel_id / art["path"]
            try:
                register_workflow(
                    channel_id, art["workflow_id"], art["workflow_version"],
                    path.read_bytes(), db_path=db_path,
                )
                registered.append(f"{art['workflow_id']}@{art['workflow_version']}")
            except WorkflowIntegrityError as exc:
                return {"ok": False, "created": created,
                        "workflows_registered": registered,
                        "error": f"workflow integrity: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "created": created,
                "workflows_registered": registered, "error": str(exc)}
    return {"ok": True, "created": created, "workflows_registered": registered}
