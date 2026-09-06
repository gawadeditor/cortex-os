"""Tests for hypogeum.agents.content_analysis_agent + its wiring into
client_content_sync (P3)."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema
from hypogeum import client_manager as cm
from hypogeum.agents import content_analysis_agent as caa


_GOOD = {
    "hook": {"promise": "A faster morning routine", "strength_1_10": 7, "type": "how_to"},
    "caption": {"length": "short", "has_cta": True, "hashtag_strategy": "few_niche"},
    "thumbnail": {"present": False, "text_legibility": "n/a", "contrast": "n/a",
                  "subject": "n/a", "clutter": "n/a", "score_1_10": 0},
    "format_tags": ["talking_head", "b_roll"],
    "topic_tags": ["morning routine", "productivity"],
    "notes": "Opens on a direct promise to the viewer.",
}


@pytest.fixture()
def client(tmp_path):
    p = tmp_path / "adv.db"
    initialize_schema(p)
    c = cm.create_client("Acme", db_path=p)
    cid = cm.upsert_content_item(c["client_id"], "youtube", platform_post_id="V1",
                                 title="How I cut my morning to 10 minutes",
                                 caption="Full routine below 👇 #productivity",
                                 source="manual_url", db_path=p)
    return p, c, cid


def test_analyze_stores_profile(client):
    p, c, cid = client
    item = cm.get_content_item(cid, db_path=p)
    res = caa.analyze_content_item(
        item, client=c,
        analyze_fn=lambda payload, image: (json.dumps(_GOOD), 0.004),
        db_path=p)
    assert res["ok"] and res["cost_usd"] == pytest.approx(0.004)
    prof = cm.get_content_profile(cid, db_path=p)
    assert prof["analysis"]["hook"]["strength_1_10"] == 7
    assert prof["model_slug"] and prof["cost_usd"] == pytest.approx(0.004)


def test_malformed_model_output_is_coerced(client):
    p, c, cid = client
    item = cm.get_content_item(cid, db_path=p)
    res = caa.analyze_content_item(
        item, client=c,
        analyze_fn=lambda payload, image: json.dumps({"hook": {"strength_1_10": 99}}),
        db_path=p)
    assert res["ok"]
    prof = cm.get_content_profile(cid, db_path=p)["analysis"]
    assert prof["hook"]["strength_1_10"] == 10          # clamped
    assert prof["format_tags"] == ["b_roll"]            # defaulted
    assert prof["thumbnail"]["present"] is False


def test_full_video_depth_not_implemented(client):
    p, c, cid = client
    c2 = dict(c, analysis_depth="full_video")
    with pytest.raises(NotImplementedError):
        caa.analyze_content_item(cm.get_content_item(cid, db_path=p), client=c2,
                                 analyze_fn=lambda *a: "{}", db_path=p)


def test_sync_profiles_content_cost_gated(tmp_path):
    p = tmp_path / "adv.db"
    initialize_schema(p)
    c = cm.create_client("Acme", monthly_analysis_budget_usd=0.01, db_path=p)
    cid = c["client_id"]
    cm.link_managed_account(cid, "youtube", provider_account_id="zacc",
                            provider_profile_id="zprof", db_path=p)

    from tests.test_client_content_sync import _FakeProvider, _post
    prov = _FakeProvider([_post("V1", "youtube"), _post("V2", "youtube")])

    calls = []

    def _fn(payload, image):
        calls.append(payload["_content_id"])
        return (json.dumps(_GOOD), 0.02)  # one call blows the $0.01 budget

    from hypogeum.publishing import client_content_sync as ccs
    out = ccs.sync_client_content(cid, provider=prov, analyze=True, analyze_fn=_fn,
                                  db_path=p)
    assert out["profiled"] == 1               # stopped after the budget was spent
    assert len(calls) == 1
