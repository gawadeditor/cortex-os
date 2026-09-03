"""Tests for hypogeum.channel_keyword_research — the per-channel
competitor / trending keyword intelligence used by the metadata agents."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum import channel_keyword_research as ckr


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        conn.execute(
            "INSERT INTO channels (channel_id, name, status) VALUES (?, ?, ?)",
            ("twinkle", "Twinkle Mini Tales", "active"),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return p


def _seed(db_path, channel_id="twinkle", *, researched_at=None, research=None):
    research = research or {
        "high_volume_search_terms": ["bedtime story", "bedtime stories for kids"],
        "competitor_hashtags": ["#bedtimestories", "#kidsvideo"],
        "trending_terms": ["calm bedtime story"],
        "audience_questions": ["what is a good bedtime story for a 3 year old"],
        "notable_competitors": ["Some Channel"],
    }
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO channel_keyword_research "
            "(channel_id, research_json, researched_at, source_query_count, total_cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, json.dumps(research),
             researched_at or datetime.now(timezone.utc).isoformat(), 3, 0.03),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_returns_none_when_absent(db_path):
    assert ckr.get_keyword_research("twinkle", db_path=db_path) is None


def test_get_returns_seeded_row(db_path):
    _seed(db_path)
    row = ckr.get_keyword_research("twinkle", db_path=db_path)
    assert row["competitor_hashtags"] == ["#bedtimestories", "#kidsvideo"]
    assert row["source_query_count"] == 3


def test_is_stale():
    assert ckr.is_stale(None) is True
    fresh = {"researched_at": datetime.now(timezone.utc).isoformat()}
    assert ckr.is_stale(fresh) is False
    old = {"researched_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
    assert ckr.is_stale(old) is True


def test_keyword_context_for_metadata(db_path):
    assert ckr.keyword_context_for_metadata("twinkle", db_path=db_path) is None
    _seed(db_path)
    ctx = ckr.keyword_context_for_metadata("twinkle", db_path=db_path)
    assert ctx["high_volume_search_terms"][0] == "bedtime story"
    assert "researched_at" in ctx


def test_metadata_channel_context_merges_brief_and_research(db_path, monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_creative_brief.channel_brief_for",
        lambda cid: {"genre": "Bedtime Stories", "format": "short", "audience": "kids 2-6"},
    )
    # no research yet -> still returns the brief-derived context
    ctx = ckr.metadata_channel_context("twinkle", db_path=db_path)
    assert ctx["genre"] == "Bedtime Stories"
    assert ctx["brand_is_unknown"] is True
    assert "competitor_hashtags" not in ctx

    _seed(db_path)
    ctx = ckr.metadata_channel_context("twinkle", db_path=db_path)
    assert ctx["competitor_hashtags"] == ["#bedtimestories", "#kidsvideo"]
    assert ctx["trending_terms"] == ["calm bedtime story"]


def test_metadata_channel_context_none_for_no_channel(db_path):
    assert ckr.metadata_channel_context(None, db_path=db_path) is None


def test_refresh_persists_structured_research(db_path, monkeypatch):
    monkeypatch.setattr(
        "hypogeum.channel_creative_brief.build_creative_brief",
        lambda cid, db_path=None: {
            "genre": "Bedtime Stories", "format": "one-minute short",
            "audience": "children 2-6",
        },
    )

    class _Res:
        def __init__(self, content):
            self.content = content
            self.usage = {"total_tokens": 500}

    calls = []

    def fake_pq(q):
        calls.append(q)
        return _Res(f"raw answer for: {q[:30]}")

    def fake_cc(system_prompt, payload):
        assert "competitor_research" in payload
        return {
            "high_volume_search_terms": ["Bedtime Story", "bedtime story", "bedtime stories for kids"],
            "competitor_hashtags": ["bedtimestories", "#kidsvideo", "#kidsvideo"],
            "trending_terms": ["viral bedtime story"],
            "audience_questions": ["how long should a bedtime story be?"],
            "notable_competitors": ["Channel X"],
        }

    out = ckr.refresh_keyword_research(
        "twinkle", config=object(), db_path=db_path,
        perplexity_query=fake_pq, claude_call=fake_cc,
    )
    assert len(calls) == 3
    # dedup + normalisation
    assert out["competitor_hashtags"] == ["#bedtimestories", "#kidsvideo"]
    assert "bedtime story" in out["high_volume_search_terms"]
    assert out["audience_questions"] == ["how long should a bedtime story be"]

    # persisted and now considered fresh
    row = ckr.get_keyword_research("twinkle", db_path=db_path)
    assert row is not None
    assert ckr.is_stale(row) is False


def test_refresh_skips_when_fresh(db_path, monkeypatch):
    _seed(db_path)

    def boom(*a, **k):  # noqa: ANN001
        raise AssertionError("should not run research when a fresh row exists")

    out = ckr.refresh_keyword_research(
        "twinkle", config=object(), db_path=db_path,
        perplexity_query=boom, claude_call=boom,
    )
    assert out["competitor_hashtags"] == ["#bedtimestories", "#kidsvideo"]


def test_refresh_force_overrides_freshness(db_path, monkeypatch):
    _seed(db_path)
    monkeypatch.setattr(
        "hypogeum.channel_creative_brief.build_creative_brief",
        lambda cid, db_path=None: {"genre": "g", "format": "f", "audience": "a"},
    )

    class _Res:
        content = "x"
        usage = {"total_tokens": 10}

    out = ckr.refresh_keyword_research(
        "twinkle", config=object(), db_path=db_path, force=True,
        perplexity_query=lambda q: _Res(),
        claude_call=lambda s, p: {
            "high_volume_search_terms": ["new term"],
            "competitor_hashtags": ["#new"],
            "trending_terms": [], "audience_questions": [], "notable_competitors": [],
        },
    )
    assert out["high_volume_search_terms"] == ["new term"]
