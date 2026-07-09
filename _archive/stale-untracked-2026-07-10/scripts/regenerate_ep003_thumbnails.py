"""
scripts/regenerate_ep003_thumbnails.py

Full Thumbnail Director run for ep_003 using the concept-driven path.

Phases executed:
  1. Phase 1  — Claude planning from thumbnail_concept_brief_ep_003.json
  2. Validator — distinctness gate on the three image_prompts
  3. Phase 2  — MUAPI generation: candidate_1, candidate_2, candidate_3
  4. Wordmark  — PIL compositing via _apply_wordmark_overlay (inside generate_thumbnail)
  5. QA        — thumbnail_qa checks (dimensions, asset_class, text_overlay, candidate count)
  6. TIG       — Claude vision gate on main.jpg
  7. Sync      — ep_003 pushed to SQLite for dashboard
  8. Dashboard — stop → start (graceful restart)

Usage:
    python3 scripts/regenerate_ep003_thumbnails.py

Requirements:
    OPENROUTER_API_KEY and MUAPI_API_KEY in .env (loaded automatically via Config)

Prints:
    model used per candidate, asset_id, text_overlay, TIG verdict, QA count, URL
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config
from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.agents.thumbnail_prompt_validator import validate_prompt_distinctness
from hypogeum.disk_sync import sync_disk_episode_to_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPISODE_ID  = "ep_003"
EPISODE_DIR = _ROOT / ".hypogeum-assets" / "episodes" / EPISODE_ID
DASHBOARD_URL = "http://127.0.0.1:5001"
STOP_SCRIPT  = _ROOT / "scripts" / "stop_dashboard_background.sh"
START_SCRIPT = _ROOT / "scripts" / "start_dashboard_background.sh"

_REQUIRED_FILES = [
    EPISODE_DIR / "thumbnails" / "candidate_1.jpg",
    EPISODE_DIR / "thumbnails" / "candidate_2.jpg",
    EPISODE_DIR / "thumbnails" / "candidate_3.jpg",
    EPISODE_DIR / "thumbnails" / "main.jpg",
]


def _sep(char: str = "─", n: int = 70) -> None:
    print(char * n)


def _h(title: str) -> None:
    _sep("═")
    print(f"  {title}")
    _sep("═")


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        print(f"\nERROR: {label} not found at:\n  {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_files() -> bool:
    print()
    _sep()
    print("FILE VERIFICATION")
    _sep()
    all_ok = True
    for p in _REQUIRED_FILES:
        size = p.stat().st_size if p.exists() else 0
        status = f"OK  ({size:,} bytes)" if p.exists() and size > 0 else "MISSING ✗"
        print(f"  {'✓' if p.exists() and size > 0 else '✗'}  {p.name:<20}  {status}")
        if not p.exists() or size == 0:
            all_ok = False
    return all_ok


def _sync_episode() -> None:
    print()
    _sep()
    print("SYNCING ep_003 TO DASHBOARD DB")
    _sep()
    try:
        result = sync_disk_episode_to_db(EPISODE_ID)
        ok     = result.get("ok", False)
        action = result.get("action", "?")
        status = result.get("status", "?")
        if ok:
            print(f"  OK  action={action}  status={status}")
        else:
            print(f"  WARN: sync returned ok=False — {result.get('error', 'unknown error')}")
    except Exception as exc:
        print(f"  WARN: sync raised {type(exc).__name__}: {exc}")


def _restart_dashboard() -> None:
    print()
    _sep()
    print("RESTARTING DASHBOARD")
    _sep()

    # Stop
    try:
        r = subprocess.run(
            ["bash", str(STOP_SCRIPT)], capture_output=True, text=True, timeout=15
        )
        for line in r.stdout.strip().splitlines():
            print(f"  [stop]  {line}")
    except Exception as exc:
        print(f"  [stop]  WARN: {exc}")

    time.sleep(1)

    # Start
    try:
        r = subprocess.run(
            ["bash", str(START_SCRIPT)], capture_output=True, text=True, timeout=15
        )
        for line in r.stdout.strip().splitlines():
            print(f"  [start] {line}")
    except Exception as exc:
        print(f"  [start] WARN: {exc}")


def _print_generation_plan_summary(plan: dict) -> None:
    """Print the three prompts that will be submitted to MUAPI."""
    print()
    _sep()
    print("PHASE 1 — GENERATION PLAN (concept-driven)")
    _sep()
    candidates = plan.get("main_episode", {}).get("ranked_candidates", [])
    for c in candidates:
        overlay = c.get("text_overlay") or {}
        hook    = overlay.get("text", "—") if isinstance(overlay, dict) else str(overlay)
        print(f"\n  Rank {c.get('rank')}  [{c.get('candidate_id')}]")
        print(f"    Hook:   {hook}")
        print(f"    Prompt: {c.get('image_prompt', '')[:120]}...")

    # Distinctness check on the plan
    result = validate_prompt_distinctness(candidates)
    print()
    print(f"  Distinctness validator: {'PASSED ✓' if result['passed'] else 'FAILED ✗'}")
    for f in result["failures"]:
        print(f"    FAIL: {f}", file=sys.stderr)
    for w in result["warnings"]:
        print(f"    WARN: {w}")
    if not result["passed"]:
        print("\nABORTED: Prompts are too similar. Re-run Phase 1 or adjust concept brief.",
              file=sys.stderr)
        sys.exit(1)


def _print_package_summary(pkg: dict, meta_model: str) -> None:
    """Print per-candidate results after MUAPI generation."""
    print()
    _h("THUMBNAIL PACKAGE SUMMARY")

    gen_meta = pkg.get("generation_metadata", {})
    primary_model  = gen_meta.get("primary_model",  meta_model or "unknown")
    fallback_model = gen_meta.get("fallback_model", "none")

    print(f"\n  Provider:       muapi")
    print(f"  Primary model:  {primary_model}")
    print(f"  Fallback model: {fallback_model}")
    print(f"  Resolution:     {gen_meta.get('resolution', '?')}")
    print(f"  Quality:        {gen_meta.get('quality', '?')}")

    print()
    _sep()
    print("PER-CANDIDATE RESULTS")
    _sep()
    candidates = pkg.get("candidates", [])
    for c in candidates:
        status  = c.get("status", "?")
        ok_mark = "✓" if status == "generated" else "✗"
        print(f"\n  {ok_mark} Candidate {c.get('rank')}  [{c.get('candidate_id')}]")
        print(f"    Status:      {status}")
        print(f"    Asset ID:    {c.get('asset_id') or '—'}")
        print(f"    File:        {c.get('file_path') or '—'}")
        print(f"    Hook text:   {c.get('text_overlay') or '—'}")
        print(f"    Placement:   {c.get('placement') or '—'}")
        # Model: same primary unless this candidate is known to have used fallback
        # (TD doesn't track per-candidate model; fallback_model recorded at package level)
        model_used = primary_model if status == "generated" else f"{primary_model} → {fallback_model}"
        print(f"    Model used:  {model_used}")

    print()
    _sep()
    print("TIG VERDICT")
    _sep()
    tig = pkg.get("thumbnail_identity_report") or {}
    verdict      = tig.get("verdict", "not run")
    blocking     = tig.get("blocking_count", 0)
    warning_cnt  = tig.get("warning_count", 0)
    violations   = tig.get("violations", [])
    print(f"\n  Verdict:   {verdict}")
    print(f"  Blocking:  {blocking}")
    print(f"  Warnings:  {warning_cnt}")
    for v in violations:
        sev  = v.get("severity", "?")
        desc = v.get("description", "")
        print(f"    [{sev}] {v.get('rule_id')} — {desc}")

    print()
    _sep()
    print("THUMBNAIL QA")
    _sep()
    qa = pkg.get("thumbnail_qa_report") or {}
    print(f"\n  Overall status: {qa.get('overall_status', 'not run')}")
    for check in qa.get("checks", []):
        mark = "✓" if check["status"] == "pass" else ("⚠" if check["status"] == "warn" else "✗")
        print(f"    {mark} {check['check_id']:<35} {check['detail'][:60]}")
    for f in qa.get("failures", []):
        print(f"    FAIL: {f}", file=sys.stderr)

    print()
    _sep()
    print("MAIN THUMBNAIL")
    _sep()
    main_thumb = pkg.get("main_thumbnail", {})
    production_ready = pkg.get("package_integrity", {}).get("production_ready", False)
    print(f"\n  Asset ID:         {main_thumb.get('asset_id', '—')}")
    print(f"  Candidates gen:   {pkg.get('candidates_generated', 0)}/{pkg.get('candidates_required', 3)}")
    print(f"  Production ready: {'YES ✓' if production_ready else 'NO ✗'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _h(f"THUMBNAIL REGENERATION — {EPISODE_ID} (concept-driven)")
    print(f"\n  Episode dir:    {EPISODE_DIR}")

    # ── Load inputs ────────────────────────────────────────────────────────
    handoff_brief = _load_json(
        EPISODE_DIR / f"thumbnail_handoff_brief_{EPISODE_ID}.json",
        "thumbnail_handoff_brief",
    )
    concept_brief_path = EPISODE_DIR / f"thumbnail_concept_brief_{EPISODE_ID}.json"
    if not concept_brief_path.exists():
        print(f"\nERROR: concept brief not found. Run first:\n"
              f"  python3 scripts/generate_thumbnail_strategy.py --episode {EPISODE_ID}",
              file=sys.stderr)
        sys.exit(1)

    concept_brief = json.loads(concept_brief_path.read_text(encoding="utf-8"))
    print(f"\n  Concept brief:  {concept_brief_path.name}")
    print(f"  Concepts:")
    for c in concept_brief.get("concepts", []):
        print(f"    [{c['emotional_trigger']:>13}]  {c['concept_name']}  →  \"{c['suggested_hook_text']}\"")

    # ── Load config ────────────────────────────────────────────────────────
    print("\n  Loading config (reads .env)...")
    try:
        config = Config()
    except Exception as exc:
        print(f"\nERROR: Config failed: {exc}", file=sys.stderr)
        sys.exit(1)

    muapi_model     = getattr(config, "THUMBNAIL_IMAGE_MODEL",  "?")
    td_model        = getattr(config, "TD_PLANNING_MODEL",       "?")
    print(f"  TD planning:    {td_model}")
    print(f"  MUAPI model:    {muapi_model}")

    # ── Run Thumbnail Director (Phases 1–3) ───────────────────────────────
    print()
    _sep()
    print("RUNNING THUMBNAIL DIRECTOR (full pipeline)...")
    _sep()

    thumbnail_package: dict | None = None
    try:
        thumbnail_package = run_thumbnail_director(
            handoff_brief=handoff_brief,
            config=config,
            episode_id=EPISODE_ID,
            output_dir=EPISODE_DIR,
        )
    except Exception as exc:
        print(f"\nERROR during run_thumbnail_director: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        # Still try to read any package that was written
        pkg_path = EPISODE_DIR / f"thumbnail_package_{EPISODE_ID}.json"
        if pkg_path.exists():
            print(f"\nPartial package found on disk — printing what was written:")
            thumbnail_package = json.loads(pkg_path.read_text(encoding="utf-8"))
        else:
            sys.exit(1)

    # ── Print generation plan summary (written to disk by run_agent) ───────
    plan_path = EPISODE_DIR / f"thumbnail_generation_plan_{EPISODE_ID}.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        _print_generation_plan_summary(plan)

    # ── Print package summary ──────────────────────────────────────────────
    _print_package_summary(thumbnail_package, muapi_model)

    # ── Verify files on disk ───────────────────────────────────────────────
    files_ok = _verify_files()
    if not files_ok:
        print("\nWARN: some thumbnail files are missing — check MUAPI logs above",
              file=sys.stderr)

    # ── Sync to dashboard DB ───────────────────────────────────────────────
    _sync_episode()

    # ── Restart dashboard ──────────────────────────────────────────────────
    _restart_dashboard()

    # ── Final summary ──────────────────────────────────────────────────────
    print()
    _h("DONE")
    tig = (thumbnail_package or {}).get("thumbnail_identity_report") or {}
    pr  = (thumbnail_package or {}).get("package_integrity", {}).get("production_ready", False)
    print(f"\n  TIG verdict:      {tig.get('verdict', 'not run')}")
    print(f"  Production ready: {'YES ✓' if pr else 'NO ✗'}")
    print(f"  Dashboard:        {DASHBOARD_URL}/episode/{EPISODE_ID}")
    print(f"  Thumbnail tab:    {DASHBOARD_URL}/episode/{EPISODE_ID}?tab=thumbnails")


if __name__ == "__main__":
    main()
