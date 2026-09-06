"""Tests for hypogeum.audience_profile — turning measured demographics
into one sentence for the content agents, and its wiring into the brief."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.audience_profile import summarize_observed_audience


def _seed_audience(db_path, channel_id, platform, dimension, breakdown):
    import json
    import uuid
    from datetime import datetime, timezone
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO channel_audience (audience_id, channel_id, "
            "platform, dimension, snapshot_date, breakdown_json, source, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, channel_id, platform, dimension,
             datetime.now(timezone.utc).date().isoformat(),
             json.dumps(breakdown), "api",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "aud.db"
    initialize_schema(p)
    return p


def test_none_when_no_demographics(db_path):
    assert summarize_observed_audience("hypogeum", db_path=db_path) is None


def test_older_male_youtube_audience(db_path):
    _seed_audience(db_path, "hypogeum", "youtube", "age", [
        {"label": "35-44", "pct": 13.1}, {"label": "45-54", "pct": 20.7},
        {"label": "55-64", "pct": 26.7}, {"label": "65-", "pct": 39.4}])
    _seed_audience(db_path, "hypogeum", "youtube", "gender", [
        {"label": "male", "pct": 78.7}, {"label": "female", "pct": 21.3}])
    s = summarize_observed_audience("hypogeum", db_path=db_path)
    assert s is not None
    assert "older" in s and "45+" in s
    assert "predominantly male" in s
    assert "youtube" in s


def test_young_audience_phrasing(db_path):
    _seed_audience(db_path, "tw", "instagram", "age", [
        {"label": "18-24", "pct": 40.0}, {"label": "25-34", "pct": 30.0},
        {"label": "35-44", "pct": 10.0}])
    s = summarize_observed_audience("tw", db_path=db_path)
    assert "young" in s and "13–34" in s


def test_weak_gender_split_omitted(db_path):
    _seed_audience(db_path, "hypogeum", "youtube", "gender", [
        {"label": "male", "pct": 52.0}, {"label": "female", "pct": 48.0}])
    _seed_audience(db_path, "hypogeum", "youtube", "age", [
        {"label": "25-34", "pct": 55.0}])
    s = summarize_observed_audience("hypogeum", db_path=db_path)
    assert s is None or "predominantly" not in s


def test_wired_into_creative_brief(db_path, monkeypatch):
    _seed_audience(db_path, "hypogeum", "youtube", "age", [
        {"label": "55-64", "pct": 30.0}, {"label": "65-", "pct": 40.0}])
    from hypogeum import channel_creative_brief as ccb
    # build_creative_brief needs a channel + brand profile; just verify the
    # fallback path carries the key and the helper is invoked without error.
    brief = ccb.build_creative_brief("hypogeum", db_path=db_path)
    assert "observed_audience" in brief
