"""
tests/test_vertical_thumbnail.py — 9:16 cover for short-native episodes

Covers hypogeum.agents.vertical_thumbnail.build_vertical_thumbnail_package:
  - no concept brief on disk        -> auto-cover stub, schema-valid
  - unreadable concept brief        -> stub
  - happy path                      -> real thumbnail_package with the
                                       generated asset_id, written to disk
  - Claude prompt-write failure     -> stub
  - image generation failure        -> stub
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.vertical_thumbnail import build_vertical_thumbnail_package
from hypogeum.schemas import validate


def _concept_brief(episode_id: str) -> dict:
    return {
        "thumbnail_concept_brief_id": f"tcb_{episode_id}",
        "episode_id": episode_id,
        "central_viewer_question": "What is hiding under the blanket?",
        "concepts": [
            {
                "concept_id": "A", "concept_name": "The Discovery",
                "viewer_promise": "You will see it too", "curiosity_gap": "what is it",
                "visual_thesis": "a kitten peers over a folded blanket fort, wide-eyed",
                "emotional_trigger": "discovery", "suggested_hook_text": "Juno Finds A Fort",
            },
            {
                "concept_id": "B", "concept_name": "The Scale",
                "viewer_promise": "It is bigger than you think", "curiosity_gap": "how big",
                "visual_thesis": "the fort towers over the tiny kitten",
                "emotional_trigger": "scale", "suggested_hook_text": "So Very Very Big",
            },
            {
                "concept_id": "C", "concept_name": "The Awe",
                "viewer_promise": "A quiet wonder", "curiosity_gap": "why glowing",
                "visual_thesis": "soft lamplight through the blanket weave",
                "emotional_trigger": "awe", "suggested_hook_text": "A Warm Glow Inside",
            },
        ],
    }


def _plan(episode_id: str) -> dict:
    return {
        "vertical_thumbnail_plan_id": f"vtp_{episode_id}",
        "episode_id": episode_id,
        "concept_id": "A",
        "hook_text": "JUNO'S FORT",
        "image_prompt": (
            "Vertical 9:16 photoreal cover: a tiny ginger kitten peers wide-eyed over a "
            "folded cream blanket fort, warm single-source lamplight from upper left, dark "
            "calm background, kitten fills the centre 70 percent of frame height, eyes in "
            "the upper-middle third. Bottom 28 percent kept calm for text. Hook text "
            "'JUNO'S FORT' rendered there, huge bold condensed all-caps cream fill with a "
            "thin dark outline, 'FORT' noticeably larger. No border, no watermark, no vignette."
        ),
        "rationale": "A wide-eyed animal at the moment of discovery stops a bedtime-audience scroll.",
    }


@pytest.fixture
def cfg():
    c = MagicMock()
    c.TD_PLANNING_MODEL = "anthropic/claude-sonnet-4.6"
    c.TD_PLANNING_MAX_TOKENS = 4096
    return c


def test_no_concept_brief_returns_schema_valid_stub(tmp_path, cfg):
    pkg = build_vertical_thumbnail_package(
        episode_id="ep_v1", output_dir=tmp_path, config=cfg, channel_id="twinkle", log=MagicMock(),
    )
    validate("thumbnail_package", pkg)
    assert pkg["main_thumbnail"]["asset_id"] == "short_native_autocover"
    assert pkg["generation_metadata"]["vertical_cover"] is False
    assert (tmp_path / "thumbnail_package_ep_v1.json").exists()


def test_unreadable_concept_brief_returns_stub(tmp_path, cfg):
    (tmp_path / "thumbnail_concept_brief_ep_v2.json").write_text("{ not json", encoding="utf-8")
    pkg = build_vertical_thumbnail_package(
        episode_id="ep_v2", output_dir=tmp_path, config=cfg, channel_id="twinkle", log=MagicMock(),
    )
    validate("thumbnail_package", pkg)
    assert pkg["generation_metadata"]["fallback_reason"] == "concept_brief_unreadable"


def test_happy_path_builds_real_vertical_cover(tmp_path, cfg):
    ep = "ep_v3"
    (tmp_path / f"thumbnail_concept_brief_{ep}.json").write_text(
        json.dumps(_concept_brief(ep)), encoding="utf-8",
    )
    thumb_file = tmp_path / "thumbnails" / "main.png"
    thumb_file.parent.mkdir(parents=True, exist_ok=True)
    thumb_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    gen_result = SimpleNamespace(
        asset_id=f"episodes/{ep}/thumbnails/main", local_path=thumb_file,
    )

    with patch("hypogeum.agent.run_agent", return_value=_plan(ep)) as mock_agent, \
         patch("hypogeum.muapi.generate_thumbnail", return_value=gen_result) as mock_gen, \
         patch("hypogeum.agents.thumbnail_qa.run_thumbnail_qa",
               return_value={"overall_status": "pass", "failures": [], "warnings": []}):
        pkg = build_vertical_thumbnail_package(
            episode_id=ep, output_dir=tmp_path, config=cfg, channel_id="twinkle", log=MagicMock(),
        )

    validate("thumbnail_package", pkg)
    assert pkg["main_thumbnail"]["asset_id"] == f"episodes/{ep}/thumbnails/main"
    assert pkg["generation_metadata"]["vertical_cover"] is True
    assert pkg["generation_metadata"]["aspect_ratio"] == "9:16"
    assert pkg["candidates"][0]["selected"] is True
    assert pkg["candidates"][0]["text_overlay"] == "JUNO'S FORT"
    # generate_thumbnail was asked for 9:16
    assert mock_gen.call_args.kwargs["aspect_ratio"] == "9:16"
    assert mock_agent.call_args.kwargs["output_schema"] == "vertical_thumbnail_plan"


def test_prompt_write_failure_falls_back_to_stub(tmp_path, cfg):
    ep = "ep_v4"
    (tmp_path / f"thumbnail_concept_brief_{ep}.json").write_text(
        json.dumps(_concept_brief(ep)), encoding="utf-8",
    )
    with patch("hypogeum.agent.run_agent", side_effect=RuntimeError("model down")):
        pkg = build_vertical_thumbnail_package(
            episode_id=ep, output_dir=tmp_path, config=cfg, channel_id="twinkle", log=MagicMock(),
        )
    validate("thumbnail_package", pkg)
    assert pkg["generation_metadata"]["fallback_reason"] == "prompt_write_failed"


def test_generation_failure_falls_back_to_stub(tmp_path, cfg):
    ep = "ep_v5"
    (tmp_path / f"thumbnail_concept_brief_{ep}.json").write_text(
        json.dumps(_concept_brief(ep)), encoding="utf-8",
    )
    with patch("hypogeum.agent.run_agent", return_value=_plan(ep)), \
         patch("hypogeum.muapi.generate_thumbnail", side_effect=RuntimeError("muapi 500")):
        pkg = build_vertical_thumbnail_package(
            episode_id=ep, output_dir=tmp_path, config=cfg, channel_id="twinkle", log=MagicMock(),
        )
    validate("thumbnail_package", pkg)
    assert pkg["generation_metadata"]["fallback_reason"] == "generation_failed"
