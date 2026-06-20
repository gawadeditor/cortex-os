"""
scripts/resume_narration_ep003.py

Audio-only resume for ep_003: generates missing narration beats b_004–b_035
using the existing b_001–b_003 as checkpoints, then runs Assembly + QA.

Run from the repo root:
    python3 scripts/resume_narration_ep003.py

Environment required:
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_ID   (pinned: j9jfwdrw7BRfcR43Qohk)
    OPENROUTER_API_KEY    (for Config validation only, not used here)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.agents.audio_agent import resume_narration_agent
from hypogeum.config import Config

EPISODE_ID = "ep_003"
ASSETS_ROOT = _ROOT / ".hypogeum-assets"
EPISODE_DIR = ASSETS_ROOT / "episodes" / EPISODE_ID


def _load(schema: str) -> dict:
    path = EPISODE_DIR / f"{schema}_{EPISODE_ID}.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")
    with open(path) as f:
        return json.load(f)


def _make_config() -> Config:
    required = {
        "OPENROUTER_API_KEY":   os.environ.get("OPENROUTER_API_KEY", "sk-placeholder"),
        "ELEVENLABS_API_KEY":   os.environ["ELEVENLABS_API_KEY"],
        "ELEVENLABS_VOICE_ID":  os.environ["ELEVENLABS_VOICE_ID"],
    }
    orig = {k: os.environ.get(k) for k in required}
    for k, v in required.items():
        os.environ[k] = v
    try:
        cfg = Config()
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cfg


def _count_present_beats(episode_id: str) -> list[str]:
    narration_dir = EPISODE_DIR / "narration"
    return sorted(
        p.stem for p in narration_dir.glob("b_*.mp3")
        if p.stat().st_size > 0
    )


def main() -> None:
    print(f"\n{'='*60}")
    print(f"  ep_003 Narration Resume")
    print(f"{'='*60}\n")

    # --- Preflight ---
    for var in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set. Export it and re-run.")
            sys.exit(1)

    # --- Load existing artifacts ---
    print("Loading existing ep_003 packages...")
    script_package            = _load("script_package")
    existing_narration_package = _load("narration_package")

    all_main_beats = [
        beat["beat_id"]
        for sec in script_package.get("sections", [])
        for beat in sec.get("narration_beats", [])
    ]
    present_before = _count_present_beats(EPISODE_ID)
    missing_before = [b for b in all_main_beats if b not in present_before]

    print(f"  Script beats (main): {len(all_main_beats)}")
    print(f"  Present on disk:     {len(present_before)} — {present_before}")
    print(f"  Missing:             {len(missing_before)} — {missing_before}")

    if not missing_before:
        print("\nAll beats already present. Skipping narration generation.")
        print("Running rebuild_ep003_assembly_qa.py directly...\n")
        _run_rebuild()
        return

    # --- Resume narration ---
    print(f"\n{'─'*60}")
    print(f"Generating {len(missing_before)} missing beats via ElevenLabs...")
    print(f"Voice ID: {os.environ['ELEVENLABS_VOICE_ID']}")

    config = _make_config()
    narration_package = resume_narration_agent(
        script_package=script_package,
        existing_narration_package=existing_narration_package,
        config=config,
        episode_id=EPISODE_ID,
        output_dir=EPISODE_DIR,
        skip_shorts=True,  # ep_003 was originally run with skip_shorts
    )

    # --- Report narration result ---
    segs = narration_package.get("narration_segments", [])
    generated = [s for s in segs if s.get("generation_status") == "generated"]
    failed    = [s for s in segs if s.get("generation_status") != "generated"]
    qa_report = narration_package.get("audio_qa_report", {})
    present_after = _count_present_beats(EPISODE_ID)

    print(f"\nNarration result:")
    print(f"  Beats generated (this run):  {len(generated)} / {len(segs)}")
    print(f"  Beats failed:                {len(failed)}")
    print(f"  Audio QA status:             {qa_report.get('overall_status')}")
    print(f"  Production ready:            {narration_package.get('package_integrity', {}).get('production_ready')}")
    print(f"  Present on disk after run:   {len(present_after)}")
    missing_after = [b for b in all_main_beats if b not in present_after]
    if missing_after:
        print(f"  Still missing:               {missing_after}")
    else:
        print(f"  Still missing:               none")

    main_narration = EPISODE_DIR / "narration" / "main_narration.mp3"
    if main_narration.exists():
        size_kb = main_narration.stat().st_size // 1024
        print(f"  main_narration.mp3:          {size_kb} KB")

    # --- Run Assembly + QA ---
    print(f"\n{'─'*60}")
    print("Running Assembly + QA rebuild...")
    _run_rebuild()


def _run_rebuild() -> None:
    rebuild_script = _ROOT / "scripts" / "rebuild_ep003_assembly_qa.py"
    result = subprocess.run(
        [sys.executable, str(rebuild_script)],
        cwd=str(_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
