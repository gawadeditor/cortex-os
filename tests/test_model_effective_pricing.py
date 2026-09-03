"""Tests for hypogeum.model_effective_pricing — effective (billed) price
per model from real charge history."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum import model_effective_pricing as mep

_GC_COLS = ("reservation_id, grant_id, grant_version, provider, model, provider_operation, "
            "idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount, "
            "state, created_at, updated_at")


def _gc(conn, i, model, reserved, charged, day=1):
    conn.execute(
        f"INSERT INTO run_authorization_grant_consumption ({_GC_COLS}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"res_{model}_{i}", "g", 1, "muapi", model, "video.text_to_video",
         f"k_{model}_{i}", f"env{i}", f"auth{i}", reserved, charged, "committed",
         f"2026-09-{day:02d}T00:00:00+00:00", f"2026-09-{day:02d}T00:00:00+00:00"),
    )


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        from hypogeum.authorization_store import ensure_tables
        ensure_tables(conn)
        conn.execute("INSERT INTO episodes (episode_id, created_at) VALUES ('ep_x', '2026-09-01T00:00:00+00:00')")
        conn.execute("INSERT INTO runs (run_id, episode_id, started_at) VALUES ('r', 'ep_x', '2026-09-01T00:00:00+00:00')")
        for i in range(5):
            _gc(conn, i, "wan2.7-text-to-video", "0.10", "0.65", day=i + 1)
        for i in range(3):
            _gc(conn, i, "veo3.1-lite-text-to-video", "0.39", "0.39")
        for i in range(4):
            conn.execute(
                "INSERT INTO costs (cost_id, run_id, episode_id, provider, model, call_type, "
                "tokens_or_credits, cost_usd, called_at, metadata_version) "
                "VALUES (?, 'r', 'ep_x', 'openrouter', 'anthropic/claude-sonnet-4.6', 'text', "
                "1000, 0.02, '2026-09-02T00:00:00+00:00', 'v1')",
                (f"c{i}",),
            )
        conn.commit()
    finally:
        conn.close()
    return p


def test_effective_prices_flat_model(db_path):
    eff = mep.effective_prices(db_path=db_path)
    assert eff["wan2.7-text-to-video"].effective_price == 0.65
    assert eff["wan2.7-text-to-video"].n_calls == 5
    assert round(eff["wan2.7-text-to-video"].total_charged, 2) == 3.25
    assert eff["veo3.1-lite-text-to-video"].effective_price == 0.39
    assert eff["anthropic/claude-sonnet-4.6"].effective_price == 0.02
    assert "grant" in eff["wan2.7-text-to-video"].sources
    assert "costs" in eff["anthropic/claude-sonnet-4.6"].sources


def test_effective_price_single(db_path):
    assert mep.effective_price("wan2.7-text-to-video", db_path=db_path) == 0.65
    assert mep.effective_price("never-used-model", db_path=db_path) is None


def test_price_view_flags_drift(db_path):
    pv = mep.price_view("wan2.7-text-to-video", 0.10, db_path=db_path)
    assert pv["effective"] == 0.65
    assert pv["flag"] is True
    assert pv["drift_pct"] == 550.0
    assert pv["basis"] == "billed"
    assert pv["label"] == "$0.65"
    assert pv["n_calls"] == 5


def test_price_view_no_drift(db_path):
    pv = mep.price_view("veo3.1-lite-text-to-video", 0.39, db_path=db_path)
    assert pv["flag"] is False
    assert pv["drift"] == 0.0


def test_price_view_never_billed_falls_back_to_listed(db_path):
    pv = mep.price_view("some-new-model", 1.25, db_path=db_path)
    assert pv["basis"] == "listed"
    assert pv["label"] == "$1.25"
    assert pv["flag"] is False


def test_never_raises_on_bad_db(tmp_path):
    assert mep.effective_prices(db_path=tmp_path / "nonexistent.db") == {}
