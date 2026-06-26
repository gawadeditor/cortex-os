"""
tests/test_prompts_dashboard.py — Targeted tests for GET /prompts dashboard page.

Covers:
  TestPromptsDashboardPage — HTML page structure and content
"""

import pytest
from hypogeum.prompt_registry import list_prompts


@pytest.fixture()
def client():
    import run_dashboard
    run_dashboard.app.config["TESTING"] = True
    with run_dashboard.app.test_client() as c:
        yield c


class TestPromptsDashboardPage:

    def test_prompts_page_returns_200(self, client):
        resp = client.get("/prompts")
        assert resp.status_code == 200

    def test_prompts_page_is_html(self, client):
        resp = client.get("/prompts")
        assert b"<!DOCTYPE html>" in resp.data

    def test_prompts_page_shows_heading(self, client):
        resp = client.get("/prompts")
        assert b"Prompt Registry" in resp.data

    def test_prompts_page_shows_all_prompt_names(self, client):
        records = list_prompts()
        resp = client.get("/prompts")
        for r in records:
            assert r.name.encode() in resp.data, (
                f"Prompt name {r.name!r} not found in /prompts page"
            )

    def test_prompts_page_shows_owner_agents(self, client):
        records = list_prompts()
        resp = client.get("/prompts")
        owners = {r.owner_agent for r in records if r.owner_agent}
        for owner in owners:
            assert owner.encode() in resp.data, (
                f"Owner agent {owner!r} not found in /prompts page"
            )

    def test_prompts_page_shows_model_targets(self, client):
        records = list_prompts()
        resp = client.get("/prompts")
        models = {r.model_target for r in records if r.model_target}
        for model in models:
            assert model.encode() in resp.data, (
                f"Model target {model!r} not found in /prompts page"
            )

    def test_prompts_page_shows_versions(self, client):
        records = list_prompts()
        resp = client.get("/prompts")
        versions = {r.prompt_version for r in records if r.prompt_version}
        for version in versions:
            assert version.encode() in resp.data, (
                f"Version {version!r} not found in /prompts page"
            )

    def test_prompts_page_shows_body_text(self, client):
        resp = client.get("/prompts")
        assert b"<pre>" in resp.data

    def test_prompts_page_shows_char_count(self, client):
        resp = client.get("/prompts")
        assert b"chars" in resp.data

    def test_prompts_page_shows_prompt_count(self, client):
        records = list_prompts()
        resp = client.get("/prompts")
        count_str = f"{len(records)} prompt".encode()
        assert count_str in resp.data

    def test_prompts_page_nav_contains_link(self, client):
        resp = client.get("/prompts")
        assert b'href="/prompts"' in resp.data
