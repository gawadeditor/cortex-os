"""Passive Provider Learning — evidence rows, summary, leaderboard, bake-off hook."""

from __future__ import annotations

from hypogeum.db import initialize_schema
from hypogeum.provider_evidence import leaderboard, model_summary, record


def test_record_and_summary(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    for ok, lat, cost, q in [(True, 60, 0.06, 100), (True, 80, 0.06, 100),
                             (False, None, None, None)]:
        record("muapi", "seedance-pro-t2v-fast", source="bakeoff", ok=ok,
               latency_s=lat, cost_usd=cost, quality_proxy=q, db_path=db)
    s = model_summary("seedance-pro-t2v-fast", db_path=db)
    assert s["n"] == 3
    assert s["success_rate"] == 0.667
    assert s["avg_latency_s"] == 70.0
    assert s["avg_cost_usd"] == 0.06


def test_leaderboard_orders_by_success_then_cost(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    for _ in range(3):
        record("muapi", "good", source="bakeoff", ok=True, cost_usd=0.5, db_path=db)
    record("muapi", "cheap-flaky", source="bakeoff", ok=True, cost_usd=0.1, db_path=db)
    record("muapi", "cheap-flaky", source="bakeoff", ok=False, db_path=db)
    record("muapi", "cheap-flaky", source="bakeoff", ok=True, cost_usd=0.1, db_path=db)
    lb = leaderboard(min_n=3, db_path=db)
    assert [x["model_id"] for x in lb] == ["good", "cheap-flaky"]
    assert lb[0]["success_rate"] == 1.0


def test_record_never_raises_on_bad_db(tmp_path):
    # no exception even if the path is nonsense
    record("muapi", "m", source="bakeoff", ok=True, db_path=tmp_path / "nope" / "x.db")


def test_bakeoff_writes_evidence(tmp_path, monkeypatch):
    from hypogeum import video_bakeoff as bo
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bo")
    monkeypatch.setattr(bo, "list_video_models", lambda *a, **k: [
        {"id": "m-a", "price_usd": 0.1, "family": "x", "notes": "", "dynamic_pricing": False}])
    # evidence goes to the default DB path; point it at tmp
    import hypogeum.provider_evidence as pe
    seen = []
    monkeypatch.setattr(pe, "record", lambda *a, **k: seen.append((a, k)))

    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True
    bo.run_bakeoff("kids", "Scene: x.", ["m-a"], config=cfg)
    assert seen and seen[0][1]["source"] == "bakeoff" and seen[0][1]["ok"] is True
