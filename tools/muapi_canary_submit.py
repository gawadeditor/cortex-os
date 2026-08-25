#!/usr/bin/env python3
"""
tools/muapi_canary_submit.py — Isolated, single-shot MUAPI generation CLI.

Submits exactly one already-authorized, already-prompted planned shot to
MUAPI, using the existing production primitives (hypogeum.muapi.submit_scene,
hypogeum.muapi.MUAPIBudgetGuard, hypogeum.artifact_compatibility) rather than
duplicating provider submission or prompt-construction logic. This tool
never constructs a prompt itself and never reads the standalone Coverage
Plan for prompt text — see hypogeum.video_prompt_writer for that boundary.

Channel- and episode-agnostic: every episode/channel/shot identity is a CLI
argument; nothing about a specific episode, channel, or shot is hardcoded.

Authorization model
--------------------
A "canary manifest" is any JSON file shaped like
generation_authorization_manifest_{episode_id}.json's own `entries` list --
one or more dicts with target_shot_id/deliverable/scene fields and
prompt_provenance/primary_prompt_checksum attached (see
hypogeum.video_prompt_writer.sync_coverage_plan_shots_from_standalone_plan).
This tool requires --manifest-path to point at one, and requires that
exactly one entry in it match --shot-id -- whether that manifest is a full
89-slot batch authorization or a dedicated single-shot canary authorization
makes no difference to this tool; it never iterates past the one matched
entry (so a second entry in the same manifest can never leak into this
invocation), and it never mutates any entry other than the matched one.

Usage:
    # Preflight only -- zero provider calls, writes nothing.
    python3 tools/muapi_canary_submit.py \\
        --episode-id ep_f3619022 --channel-id hypogeum \\
        --shot-id b_047__shot2 --scene-id sh_short_003_s05 \\
        --extra-index 2 --tier 2 --max-charge-usd 0.39 \\
        --manifest-path .hypogeum-assets/episodes/ep_f3619022/canary_authorization_ep_f3619022.json \\
        --preflight-only

    # Actual submission -- exactly one paid provider call maximum.
    python3 tools/muapi_canary_submit.py \\
        --episode-id ep_f3619022 --channel-id hypogeum \\
        --shot-id b_047__shot2 --scene-id sh_short_003_s05 \\
        --extra-index 2 --tier 2 --max-charge-usd 0.39 \\
        --manifest-path .hypogeum-assets/episodes/ep_f3619022/canary_authorization_ep_f3619022.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.canary_preflight import (                                     # noqa: E402
    CanaryPreflightError,
    check_prompt_checksum,
    check_provenance,
    resolve_model_and_cost,
    sha256_text,
)
from hypogeum.config import Config                                          # noqa: E402
from hypogeum.muapi import (                                                # noqa: E402
    MODEL_CREDIT_COSTS,
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
    _tier_to_model,
    scene_file_path,
    submit_scene,
)

_CANARY_TOOL_VERSION = "muapi_canary_submit.py@1"


def _sha256(text: str) -> str:
    return sha256_text(text)


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise CanaryPreflightError("file_exists", f"required file not found: {path}")
    with open(path) as f:
        return json.load(f)


def _episode_dir(assets_root: Path, episode_id: str) -> Path:
    return assets_root / "episodes" / episode_id


def _resolve_authorization(manifest: dict, args: argparse.Namespace) -> dict:
    """Checks 1-3, 13: exactly one matching, active, field-consistent entry."""
    entries = manifest.get("entries", [])
    matches = [e for e in entries if e.get("target_shot_id") == args.shot_id]

    if len(matches) == 0:
        raise CanaryPreflightError(
            "exactly_one_authorization_entry",
            f"no manifest entry matches shot_id={args.shot_id!r}",
        )
    if len(matches) > 1:
        raise CanaryPreflightError(
            "exactly_one_authorization_entry",
            f"{len(matches)} manifest entries match shot_id={args.shot_id!r} "
            f"— refusing to disambiguate",
        )
    entry = matches[0]

    status = entry.get("canary_status", "active")
    if status != "active":
        raise CanaryPreflightError(
            "authorization_active",
            f"entry status is {status!r}, not 'active' — already completed or deauthorized",
        )

    field_checks = [
        ("shot_id", entry.get("target_shot_id"), args.shot_id),
        ("scene", entry.get("scene") or entry.get("scene_id"), args.scene_id),
        ("deliverable", entry.get("deliverable"), args.deliverable),
        ("aspect_ratio", entry.get("aspect_ratio"), args.aspect_ratio),
        ("extra_index", entry.get("extra_index"), args.extra_index),
    ]
    for field_name, manifest_val, cli_val in field_checks:
        if manifest_val is None:
            continue  # optional field on older-shaped entries; not a mismatch
        if str(manifest_val) != str(cli_val):
            raise CanaryPreflightError(
                "manifest_field_match",
                f"{field_name}: manifest has {manifest_val!r}, CLI args gave {cli_val!r}",
            )

    return entry


def _resolve_planned_shot(visual_package: dict, args: argparse.Namespace) -> dict:
    """Checks 4-7: shot exists exactly once, has a rendered prompt, its
    checksum matches the manifest, and provenance is attached."""
    scene = next(
        (s for s in visual_package.get("scenes", []) if s.get("scene_id") == args.scene_id),
        None,
    )
    if scene is None:
        raise CanaryPreflightError("planned_shot_exists", f"no scene {args.scene_id!r} in visual_package")

    plan = scene.get("cinematic_coverage_plan") or {}
    matches = [sh for sh in plan.get("shots", []) if sh.get("shot_id") == args.shot_id]
    if len(matches) == 0:
        raise CanaryPreflightError(
            "planned_shot_exists", f"shot {args.shot_id!r} not found in embedded Coverage Plan"
        )
    if len(matches) > 1:
        raise CanaryPreflightError(
            "planned_shot_exists",
            f"shot {args.shot_id!r} appears {len(matches)} times in embedded Coverage Plan",
        )
    shot = matches[0]

    prompt = (shot.get("primary_prompt") or "").strip()
    if not prompt:
        raise CanaryPreflightError("primary_prompt_present", f"shot {args.shot_id!r} has empty primary_prompt")

    return scene, shot


def _check_prompt_checksum(shot: dict, entry: dict) -> str:
    return check_prompt_checksum(shot["primary_prompt"], entry.get("primary_prompt_checksum"))


def _check_provenance(entry: dict) -> dict:
    return check_provenance(entry)


def _resolve_model(config: Config, args: argparse.Namespace) -> str:
    """Checks 8-9: tier resolves to the required model; cost within cap."""
    model, _cost = resolve_model_and_cost(config, args.tier, args.model, args.max_charge_usd)
    return model


def _extra_scene_id(scene_id: str, extra_index: int) -> str:
    return f"{scene_id}__extra{extra_index}"


def _check_output_collision(assets_root: Path, episode_id: str, scene_id: str,
                             short_id: Optional[str], extra_index: int) -> Path:
    """Check 10: deterministic output path must not already hold an
    unrelated file (or any file at all -- this shot has never been
    generated)."""
    output_dir = _episode_dir(assets_root, episode_id)
    extra_id = _extra_scene_id(scene_id, extra_index)
    path = scene_file_path(output_dir, extra_id, short_id)
    if path.exists():
        raise CanaryPreflightError(
            "no_output_collision",
            f"deterministic output path already exists: {path}",
        )
    return path


def _check_credentials(config: Config) -> None:
    """Check 11."""
    if not config.MUAPI_API_KEY:
        raise CanaryPreflightError("credentials_present", "MUAPI_API_KEY is not configured")


def _capture_rush_baseline(assets_root: Path, episode_id: str) -> dict:
    """Check 12: hash every existing physical clip under this episode's
    video/ directory before any submission, so Phase 7 can prove none of
    them changed."""
    video_dir = _episode_dir(assets_root, episode_id) / "video"
    baseline = {}
    if video_dir.exists():
        for path in sorted(video_dir.rglob("*.mp4")):
            h = hashlib.md5()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
            baseline[str(path.relative_to(video_dir))] = h.hexdigest()
    return baseline


def run_preflight(args: argparse.Namespace) -> dict:
    """Runs every fail-closed check. Zero provider calls, zero writes.
    Returns a machine-readable receipt dict. Raises CanaryPreflightError
    (with .check naming the failed gate) on the first failing check."""
    assets_root = Path(args.assets_root)
    episode_dir = _episode_dir(assets_root, args.episode_id)

    config = Config()
    _check_credentials(config)  # 11

    manifest = _load_json(Path(args.manifest_path))
    entry = _resolve_authorization(manifest, args)  # 1, 2, 3, 13

    vp_path = episode_dir / f"visual_package_{args.episode_id}.json"
    visual_package = _load_json(vp_path)
    scene, shot = _resolve_planned_shot(visual_package, args)  # 4, 5

    prompt_checksum = _check_prompt_checksum(shot, entry)  # 6
    provenance = _check_provenance(entry)  # 7

    model = _resolve_model(config, args)  # 8, 9

    output_path = _check_output_collision(  # 10
        assets_root, args.episode_id, args.scene_id, scene.get("short_id"), args.extra_index
    )

    rush_baseline = _capture_rush_baseline(assets_root, args.episode_id)  # 12

    receipt_dir = episode_dir / "canary_receipts"
    receipt_path = receipt_dir / f"{args.shot_id}_receipt.json"

    return {
        "tool_version": _CANARY_TOOL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode_id": args.episode_id,
        "channel_id": args.channel_id,
        "shot_id": args.shot_id,
        "scene_id": args.scene_id,
        "extra_index": args.extra_index,
        "deliverable": args.deliverable,
        "aspect_ratio": args.aspect_ratio,
        "extra_scene_id": _extra_scene_id(args.scene_id, args.extra_index),
        "provider_calls_made": 0,
        "expected_model": model,
        "expected_charge_usd": MODEL_CREDIT_COSTS[model],
        "max_charge_usd": args.max_charge_usd,
        "authorization_entry_matched": True,
        "planned_shot_resolved": True,
        "prompt_checksum": prompt_checksum,
        "prompt_provenance": provenance,
        "output_path": str(output_path),
        "output_collision": False,
        "receipt_path": str(receipt_path),
        "rush_baseline_count": len(rush_baseline),
        "rush_baseline": rush_baseline,
        "preflight_status": "PASS",
    }


def _submit_canary(args: argparse.Namespace, preflight: dict) -> dict:
    """PHASE 2 (paid): exactly one submit_scene() call, via the existing
    production primitive -- never a hand-rolled HTTP request."""
    assets_root = Path(args.assets_root)
    episode_dir = _episode_dir(assets_root, args.episode_id)
    config = Config()

    visual_package = _load_json(episode_dir / f"visual_package_{args.episode_id}.json")
    scene = next(s for s in visual_package["scenes"] if s["scene_id"] == args.scene_id)
    shot = next(
        sh for sh in scene["cinematic_coverage_plan"]["shots"] if sh["shot_id"] == args.shot_id
    )

    extra_scene_id = _extra_scene_id(args.scene_id, args.extra_index)
    scene_spec = {
        "scene_id": extra_scene_id,
        "short_id": scene.get("short_id"),
        "visual_spec": {
            "aspect_ratio": args.aspect_ratio,
            "duration_seconds": shot.get("estimated_usable_duration", 8.0),
        },
        "production_spec": {
            "primary_prompt": shot["primary_prompt"],
            "primary_tier": args.tier,
            "fallback_prompt": shot.get("fallback_prompt") or shot["primary_prompt"],
            "fallback_tier": args.tier,
            "reference_image_url": None,
        },
    }

    guard = MUAPIBudgetGuard(
        episode_budget_credits=args.max_charge_usd,
        scene_budget_credits=args.max_charge_usd,
        max_paid_attempts=1,
        log=_NullLog(),
    )

    result = submit_scene(
        scene_spec=scene_spec,
        config=config,
        episode_id=args.episode_id,
        output_dir=episode_dir,
        episode_id_for_asset=args.episode_id,
        agent="muapi_canary_submit",
        budget_guard=guard,
        # Phase 3 correction (2026-08-25): the canary is a live paid-video
        # path and MUST enter the same enforced boundary as production --
        # verified persisted plan, grant/plan/finance three-way agreement,
        # atomic CHANNEL_WEEKLY reservation, child authorization, submission
        # gate. There is no ungated paid fallback: without an authoritative
        # channel context the submission is denied before any network call.
        channel_id=args.channel_id,
    )
    return result


class _NullLog:
    """Minimal structured-log stand-in so this tool doesn't depend on the
    full episode logging stack for a single isolated invocation."""

    def _emit(self, level, event, **kw):
        print(json.dumps({"level": level, "event": event, **kw}), file=sys.stderr)

    def info(self, event, **kw):
        self._emit("info", event, **kw)

    def warn(self, event, **kw):
        self._emit("warn", event, **kw)

    def error(self, event, **kw):
        self._emit("error", event, **kw)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", required=True)
    p.add_argument("--shot-id", required=True)
    p.add_argument("--scene-id", required=True)
    p.add_argument("--extra-index", required=True, type=int)
    p.add_argument("--tier", required=True, type=int, choices=[1, 2, 3])
    p.add_argument("--max-charge-usd", required=True, type=float)
    p.add_argument("--deliverable", default=None, help="expected deliverable name, cross-checked against the manifest entry")
    p.add_argument("--aspect-ratio", default="9:16")
    p.add_argument("--model", default="veo3.1-lite-text-to-video", help="required resolved model; submission fails if the tier resolves to a different one")
    p.add_argument("--manifest-path", required=True, help="path to a JSON file shaped like generation_authorization_manifest_*.json")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    p.add_argument("--preflight-only", action="store_true")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # Phase 3 correction (2026-08-25): fail closed on a missing/blank
    # channel_id BEFORE preflight and before any provider/network call.
    # The canary is a paid path; without an authoritative channel context
    # it must not run at all (no ungated fallback exists).
    if not (args.channel_id or "").strip():
        print(json.dumps({
            "preflight_status": "FAIL",
            "failed_check": "channel_context",
            "reason": (
                "--channel-id is required: paid canary submission without an "
                "authoritative channel context is not permitted"
            ),
            "provider_calls_made": 0,
        }, indent=2))
        return 1

    try:
        preflight = run_preflight(args)
    except CanaryPreflightError as exc:
        print(json.dumps({
            "preflight_status": "FAIL",
            "failed_check": exc.check,
            "reason": exc.reason,
            "provider_calls_made": 0,
        }, indent=2))
        return 1

    if args.preflight_only:
        print(json.dumps(preflight, indent=2))
        return 0

    # ---- PHASE 2: exactly one paid submission ----
    submitted_at = datetime.now(timezone.utc).isoformat()
    try:
        result = _submit_canary(args, preflight)
        provider_calls_made = 1
        submission_error = None
    except (MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted) as exc:
        result = None
        provider_calls_made = 1  # the attempt itself counts even on failure
        submission_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — must still emit a receipt, never re-raise unrecorded
        result = None
        provider_calls_made = 1
        submission_error = f"{type(exc).__name__}: {exc}"

    completed_at = datetime.now(timezone.utc).isoformat()
    output_path = Path(preflight["output_path"])
    success = bool(result and result.get("generation_status") == "generated" and output_path.exists())

    receipt = {
        **{k: v for k, v in preflight.items() if k != "rush_baseline"},
        "submitted_at": submitted_at,
        "completed_at": completed_at,
        "provider_calls_made": provider_calls_made,
        "provider_result": result,
        "submission_error": submission_error,
        "success": success,
        "actual_output_exists": output_path.exists(),
        "actual_output_checksum": (
            hashlib.sha256(output_path.read_bytes()).hexdigest() if output_path.exists() else None
        ),
        "preflight_status": "PASS",
        "execution_status": "SUCCESS" if success else "FAILED_NO_RETRY",
    }

    receipt_path = Path(preflight["receipt_path"])
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = receipt_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2)
    tmp.replace(receipt_path)

    print(json.dumps(receipt, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
