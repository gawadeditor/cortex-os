"""
hypogeum/channel_package_scaffold.py — give a new channel its Channel Package.

Every channel needs a ``channel_packages/<channel_id>/`` directory (workflow
definition + policies + a manifest) before it can resolve a workflow
binding, compute its package identity, or be granted a Channel Operating
Grant. The first channel (hypogeum) has one committed to the repo; a
channel created from the dashboard does not.

Only the genuinely shared, topic-agnostic artifacts are copied from the
template (the workflow definition and the deliverable contract, with this
channel's channel_id). The platform-distribution policy is NOT copied: it
is written from the new channel's own runtime policy (what the dashboard
wizard configured), so it can never start life as another channel's file.
Fixed 2026-10-03 — copying the whole template directory left
twinkle_mini_tales with hypogeum's policy file (channel_id 'hypogeum',
different platforms), which the sanctioned file->DB sync would have
applied over twinkle's real settings.

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
        _copy_shared_artifacts(src, dst, channel_id, db_path=db_path, packages_root=root)
    except Exception as exc:  # noqa: BLE001
        # roll back a half-made copy
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "created": False, "error": f"copy failed: {exc}"}

    return _register_from_manifest(channel_id, db_path, created=True, packages_root=root)


_SHARED_ROLES = ("workflow_definition", "deliverable_contract")


def _copy_shared_artifacts(src: Path, dst: Path, channel_id: str, *, db_path, packages_root: Path) -> None:
    import hashlib
    template = json.loads((src / "channel_package_manifest.json").read_text(encoding="utf-8"))
    manifest = {k: v for k, v in template.items() if k != "artifacts"}
    manifest["channel_id"] = channel_id
    manifest["artifacts"] = []
    dst.mkdir(parents=True)
    for art in template.get("artifacts", []):
        if art.get("role") not in _SHARED_ROLES:
            continue  # channel-specific (e.g. platform_distribution_policy): never copied
        target = dst / art["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (src / art["path"]).read_bytes()
        entry = dict(art)
        if art["role"] == "deliverable_contract":
            # shared shape, but the file names its channel: make it this one's
            doc = json.loads(raw)
            if "channel_id" in doc:
                doc["channel_id"] = channel_id
                raw = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
        # workflow_definition is byte-identical: keep its sha256/workflow_id —
        # run_pipeline binds by the template's workflow_id.
        target.write_bytes(raw)
        manifest["artifacts"].append(entry)
    (dst / "channel_package_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    from hypogeum.channel_config import (
        get_channel_operations_profile, write_platform_distribution_policy_from_runtime,
    )
    if get_channel_operations_profile(channel_id, db_path=db_path) is not None:
        write_platform_distribution_policy_from_runtime(
            channel_id, db_path=db_path, packages_root=packages_root,
            reason="scaffolded from this channel's own runtime policy",
        )
    # No runtime profile yet: no policy file. load_platform_distribution_policy
    # fails closed until one is written — never a borrowed one.


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
