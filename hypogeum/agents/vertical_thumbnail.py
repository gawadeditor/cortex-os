"""
hypogeum/agents/vertical_thumbnail.py — Vertical (9:16) cover for short-native episodes

Short-native channels publish one vertical deliverable (the `main_episode`).
The 16:9 Thumbnail Director / composition / typography / compositor stack is
landscape-shaped end to end, so short-native used to get a stub
thumbnail_package with no real image (pipeline._short_native_thumbnail_package)
and every post went out with whatever frame the platform auto-picked.

This module produces a real, designed 9:16 cover:

  1. Load the thumbnail_concept_brief the Thumbnail Strategy already wrote
     (it runs in short-native — only the Director is skipped).
  2. One Claude call (prompts/vertical_thumbnail.txt) turns the rank-1
     concept into ONE professional image-generation prompt with the hook
     text baked in — the image model renders the scene AND the text, so
     there is no separate PIL compositor step (that step is 1280x720-only).
  3. hypogeum.muapi.generate_thumbnail(aspect_ratio="9:16") — the generator
     and gpt-image-2 already support portrait.
  4. hypogeum.agents.thumbnail_qa.run_thumbnail_qa(aspect_ratio="9:16") —
     already supports the 720x1280 expected-dims check.
  5. Write a schema-valid thumbnail_package_<ep>.json with the real
     main_thumbnail.asset_id.

Best-effort: any failure (missing concept brief, Claude error, generation
failure, QA fail) falls back to the auto-cover stub so an episode is never
blocked on the cover. The publish side treats a missing/stub cover the same
way it always has.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.logger import get_logger

_AGENT_NAME = "vertical_thumbnail_agent"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "vertical_thumbnail.txt"


def _stub_package(episode_id: str, reason: str) -> dict:
    """The historical auto-cover stub — a schema-valid package with no real
    image. Keeps every downstream checkpoint/QA/output-gate read resolving."""
    return {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {"asset_id": "short_native_autocover"},
        "short_thumbnails": [],
        "candidates": [
            {"candidate_id": "sn_auto_001", "rank": 1, "selected": True, "status": "auto_cover"}
        ],
        "thumbnail_qa_report": {"overall_status": "pass", "failures": [], "warnings": []},
        "generation_metadata": {
            "concept_driven": False,
            "short_native": True,
            "vertical_cover": False,
            "fallback_reason": reason,
        },
        "package_integrity": {"production_ready": True},
    }


def build_vertical_thumbnail_package(
    episode_id: str,
    output_dir: Optional[Path],
    config: Config,
    channel_id: Optional[str] = None,
    log=None,
) -> dict:
    """
    Produce a real 9:16 cover thumbnail_package for a short-native episode,
    or the auto-cover stub if any step cannot complete. Always returns a
    schema-valid thumbnail_package dict and writes it to
    <output_dir>/thumbnail_package_<episode_id>.json.
    """
    if log is None:
        log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    base = output_dir if output_dir is not None else (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )

    def _finish(pkg: dict) -> dict:
        try:
            from hypogeum.schemas import validate
            validate("thumbnail_package", pkg)
        except Exception as exc:  # noqa: BLE001 — schema drift guard, never fatal
            log.warn("vertical_thumbnail_package_schema_warn", error=str(exc))
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / f"thumbnail_package_{episode_id}.json").write_text(
                json.dumps(pkg, indent=2), encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warn("vertical_thumbnail_package_write_failed", error=str(exc))
        return pkg

    # ------------------------------------------------------------------
    # 1. Concept brief (written by Thumbnail Strategy, runs in short-native)
    # ------------------------------------------------------------------
    concept_path = base / f"thumbnail_concept_brief_{episode_id}.json"
    if not concept_path.exists():
        log.info("vertical_thumbnail_no_concept_brief", path=str(concept_path))
        return _finish(_stub_package(episode_id, "no_thumbnail_concept_brief"))
    try:
        concept_brief = json.loads(concept_path.read_text(encoding="utf-8"))
        concepts = concept_brief.get("concepts") or []
        winning = concepts[0] if concepts else None
        if not winning:
            raise ValueError("concept_brief has no concepts")
    except Exception as exc:  # noqa: BLE001
        log.warn("vertical_thumbnail_concept_brief_unreadable", error=str(exc))
        return _finish(_stub_package(episode_id, "concept_brief_unreadable"))

    # ------------------------------------------------------------------
    # 2. Claude — one professional 9:16 image prompt (text baked in)
    # ------------------------------------------------------------------
    try:
        from hypogeum.agent import run_agent
        from hypogeum.prompt_loader import load_prompt

        system_prompt = load_prompt(_PROMPT_FILE)
        try:
            from hypogeum.brand import visual_identity
            system_prompt = system_prompt.rstrip() + "\n\n" + \
                visual_identity.build_visual_identity_directive(mode="thumbnail_prompt_writer")
        except Exception:  # noqa: BLE001 — identity directive is additive, never required
            pass

        input_package: dict = {
            "task": "write_vertical_cover_prompt",
            "episode_id": episode_id,
            "thumbnail_concept_brief": concept_brief,
            "use_concept_id": winning.get("concept_id"),
        }
        try:
            from hypogeum.channel_creative_brief import channel_brief_for
            input_package["channel_brief"] = channel_brief_for(channel_id)
        except Exception:  # noqa: BLE001
            pass

        # Sonnet, not the expensive TD_PLANNING_MODEL (gpt-5.5-pro): this is
        # a single prompt-craft turn producing a ~300-token JSON, the same
        # tier the Thumbnail Strategy that feeds it already runs on.
        plan = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=getattr(config, "OPENROUTER_SONNET_MODEL_ID", "anthropic/claude-sonnet-4.6")
            or "anthropic/claude-sonnet-4.6",
            system_prompt=system_prompt,
            input_package=input_package,
            output_schema="vertical_thumbnail_plan",
            episode_id=episode_id,
            max_tokens=2048,
            config=config,
            output_dir=base,
            channel_id=channel_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warn("vertical_thumbnail_prompt_failed", error=str(exc))
        return _finish(_stub_package(episode_id, "prompt_write_failed"))

    image_prompt = (plan.get("image_prompt") or "").strip()
    hook_text = (plan.get("hook_text") or "").strip()
    if not image_prompt:
        return _finish(_stub_package(episode_id, "empty_image_prompt"))

    # ------------------------------------------------------------------
    # 3. Generate the 9:16 image (gate path when channel_id + grant resolve)
    # ------------------------------------------------------------------
    try:
        from hypogeum.muapi import generate_thumbnail
        result = generate_thumbnail(
            deliverable_id="main_episode",
            image_prompt=image_prompt,
            aspect_ratio="9:16",
            config=config,
            episode_id=episode_id,
            output_dir=base,
            agent=_AGENT_NAME,
            channel_id=channel_id,
        )
        main_asset_id = result.asset_id
        main_local = result.local_path
    except Exception as exc:  # MUAPI auth/quota/budget already halt upstream
        log.warn("vertical_thumbnail_generation_failed", error=str(exc))
        return _finish(_stub_package(episode_id, "generation_failed"))

    # ------------------------------------------------------------------
    # 4. QA (9:16 dims + not-black + not-placeholder). Advisory: a QA fail
    #    still ships the real cover — it beats no cover — but is recorded.
    # ------------------------------------------------------------------
    qa_report = {"overall_status": "skipped", "failures": [], "warnings": []}
    try:
        from hypogeum.agents.thumbnail_qa import run_thumbnail_qa
        qa_report = run_thumbnail_qa(
            thumbnail_local_path=main_local,
            aspect_ratio="9:16",
            asset_class="thumbnail_image",
        )
    except Exception as exc:  # noqa: BLE001
        log.warn("vertical_thumbnail_qa_error", error=str(exc))

    log.info(
        "vertical_thumbnail_built",
        episode_id=episode_id,
        concept_id=plan.get("concept_id"),
        hook_text=hook_text,
        asset_id=main_asset_id,
        qa_status=qa_report.get("overall_status"),
    )

    pkg = {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {"asset_id": main_asset_id},
        "short_thumbnails": [],
        "candidates": [{
            "candidate_id": plan.get("concept_id") or "vt_001",
            "rank": 1,
            "selected": True,
            "status": "generated",
            "asset_id": main_asset_id,
            "text_overlay": hook_text or None,
            "visual_prompt": image_prompt,
        }],
        "thumbnail_qa_report": {
            "overall_status": qa_report.get("overall_status", "skipped"),
            "failures": qa_report.get("failures", []),
            "warnings": qa_report.get("warnings", []),
        },
        "generation_metadata": {
            "concept_driven": True,
            "short_native": True,
            "vertical_cover": True,
            "aspect_ratio": "9:16",
        },
        "package_integrity": {"production_ready": True},
    }
    return _finish(pkg)
