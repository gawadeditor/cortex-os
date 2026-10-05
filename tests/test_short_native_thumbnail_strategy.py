"""Short-native runs the Thumbnail Strategy (2026-10-05): its concept brief is
what the vertical 9:16 cover is built from — skipping it left ep_1139264e
scheduled with no designed cover. Best-effort: a failure never stops the run."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hypogeum import pipeline


def _cfg(short_native):
    return SimpleNamespace(SHORT_NATIVE_MODE=short_native)


def test_short_native_generates_the_concept_brief(tmp_path):
    brief = {"thumbnail_concept_brief_id": "tcb_ep", "episode_id": "ep", "concepts": [{"id": 1}]}
    with patch.object(pipeline, "run_thumbnail_strategy", return_value=brief) as run:
        out = pipeline._run_thumbnail_strategy({}, {}, _cfg(True), "ep", tmp_path)
    assert run.called and out == brief


def test_short_native_strategy_failure_does_not_stop_the_episode(tmp_path):
    with patch.object(pipeline, "run_thumbnail_strategy", side_effect=RuntimeError("llm down")):
        out = pipeline._run_thumbnail_strategy({}, {}, _cfg(True), "ep", tmp_path)
    assert out["concepts"] == [] and out["short_native"] is True


def test_long_form_strategy_failure_still_raises(tmp_path):
    with patch.object(pipeline, "run_thumbnail_strategy", side_effect=RuntimeError("llm down")):
        try:
            pipeline._run_thumbnail_strategy({}, {}, _cfg(False), "ep", tmp_path)
        except RuntimeError:
            return
    raise AssertionError("long-form failure was swallowed")
