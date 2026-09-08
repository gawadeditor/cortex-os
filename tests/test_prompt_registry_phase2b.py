"""
tests/test_prompt_registry_phase2b.py — Phase 2B: Prompt Registry API

Coverage:
    TestPromptRegistryListAll      — list_prompts() returns all .txt files
    TestPromptRegistryMetadata     — metadata fields parsed correctly per prompt
    TestPromptRegistryBodyExcludes — get_prompt() body excludes header lines
    TestPromptRegistryMissing      — get_prompt/get_prompt_metadata return None for unknown
    TestPromptRegistryToDict       — to_dict() shape for list vs. detail mode
    TestPromptApiList              — GET /api/prompts returns correct JSON
    TestPromptApiDetail            — GET /api/prompts/<name> returns full record
    TestPromptApi404               — GET /api/prompts/<missing> returns 404 JSON
    TestPromptRegistryNoNetwork    — importing registry never makes network calls
    TestMusicJsonExcluded          — music_categories.json is not in list_prompts()
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Prompt registry unit tests (no Flask)
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "hypogeum" / "prompts"

_EXPECTED_NAMES = {
    "brand_guardian_agent",
    "cinematic_director",
    "content_analysis",
    "executive_producer",
    "narrative_architect",
    "perplexity_research",
    "research_director",
    "script_engine",
    "thumbnail_director",
    "thumbnail_identity_guardian",
    "thumbnail_strategy",
    "source_verifier_query",
    "topic_ideator",
    "visual_package_director",
    # VPD sub-stage prompt files (loaded directly by visual_package_director.py)
    "vpd_skeleton",
    "vpd_scene_prompts",
    "vpd_skeleton_meta",
    "vpd_skeleton_scene_batch",
    # Visual Intelligence V1–V4 prompt files
    "vpd_scene_presentation_planner",
    "vpd_visual_intent_qa",
    "shot_diversity_planner",
    "editorial_qa",
}

_REQUIRED_META_KEYS = {"PROMPT_VERSION", "OWNER_AGENT", "MODEL_TARGET", "PURPOSE"}


class TestPromptRegistryListAll:
    def test_list_prompts_returns_all_txt_files(self):
        from hypogeum.prompt_registry import list_prompts
        records = list_prompts()
        names = {r.name for r in records}
        assert names == _EXPECTED_NAMES, (
            f"Expected {_EXPECTED_NAMES}, got {names}"
        )

    def test_list_prompts_count_matches_txt_files(self):
        from hypogeum.prompt_registry import list_prompts
        txt_count = len(list(_PROMPTS_DIR.glob("*.txt")))
        assert len(list_prompts()) == txt_count

    def test_list_prompts_sorted_alphabetically(self):
        from hypogeum.prompt_registry import list_prompts
        records = list_prompts()
        names = [r.name for r in records]
        assert names == sorted(names)

    def test_list_prompts_body_is_none(self):
        from hypogeum.prompt_registry import list_prompts
        for r in list_prompts():
            assert r.body is None, f"{r.name}: body should be None in list mode"

    def test_list_prompts_excludes_json(self):
        """music_categories.json must not appear in list_prompts()."""
        from hypogeum.prompt_registry import list_prompts
        names = {r.name for r in list_prompts()}
        assert "music_categories" not in names

    def test_list_prompts_each_has_file_path(self):
        from hypogeum.prompt_registry import list_prompts
        for r in list_prompts():
            assert r.file_path.endswith(".txt"), f"{r.name}: file_path must end in .txt"
            assert "prompts" in r.file_path

    def test_list_prompts_each_has_positive_char_count(self):
        from hypogeum.prompt_registry import list_prompts
        for r in list_prompts():
            assert r.char_count > 50, (
                f"{r.name}: char_count={r.char_count} suspiciously small"
            )

    def test_list_prompts_returns_prompt_record_instances(self):
        from hypogeum.prompt_registry import list_prompts, PromptRecord
        for r in list_prompts():
            assert isinstance(r, PromptRecord)


class TestPromptRegistryMetadata:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_prompt_version_is_v1(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        assert r.prompt_version in ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"), (
            f"{name}: prompt_version={r.prompt_version!r} (must be v1..v8)"
        )

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_owner_agent_is_non_empty(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        assert r.owner_agent, f"{name}: owner_agent is empty"

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_model_target_is_non_empty(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        assert r.model_target, f"{name}: model_target is empty"

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_purpose_is_non_empty(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        assert r.purpose, f"{name}: purpose is empty"

    def test_executive_producer_model_is_sonnet(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("executive_producer")
        assert r is not None
        assert "sonnet" in r.model_target.lower()

    def test_research_director_model_is_opus(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("research_director")
        assert r is not None
        assert "opus" in r.model_target.lower()

    def test_thumbnail_director_model_is_gpt(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("thumbnail_director")
        assert r is not None
        assert "gpt" in r.model_target.lower() or "openai" in r.model_target.lower()

    def test_perplexity_research_model_is_sonar(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("perplexity_research")
        assert r is not None
        assert "sonar" in r.model_target.lower()

    def test_get_prompt_metadata_returns_dict(self):
        from hypogeum.prompt_registry import get_prompt_metadata
        meta = get_prompt_metadata("executive_producer")
        assert isinstance(meta, dict)
        for key in _REQUIRED_META_KEYS:
            assert key in meta

    def test_get_prompt_metadata_values_match_get_prompt(self):
        from hypogeum.prompt_registry import get_prompt, get_prompt_metadata
        record = get_prompt("script_engine")
        meta = get_prompt_metadata("script_engine")
        assert record.prompt_version == meta["PROMPT_VERSION"]
        assert record.owner_agent    == meta["OWNER_AGENT"]
        assert record.model_target   == meta["MODEL_TARGET"]
        assert record.purpose        == meta["PURPOSE"]

    def test_get_prompt_metadata_missing_returns_none(self):
        from hypogeum.prompt_registry import get_prompt_metadata
        assert get_prompt_metadata("does_not_exist") is None

    def test_list_prompts_metadata_consistent_with_get_prompt(self):
        """Metadata in list_prompts() matches metadata in get_prompt() for each name."""
        from hypogeum.prompt_registry import list_prompts, get_prompt
        for listed in list_prompts():
            full = get_prompt(listed.name)
            assert full is not None
            assert listed.prompt_version == full.prompt_version
            assert listed.owner_agent    == full.owner_agent
            assert listed.model_target   == full.model_target
            assert listed.purpose        == full.purpose
            assert listed.char_count     == full.char_count


class TestPromptRegistryBodyExcludes:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_body_has_no_prompt_version_line(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        for line in r.body.splitlines():
            assert not line.startswith("# PROMPT_VERSION"), (
                f"{name}: body still contains PROMPT_VERSION header: {line!r}"
            )

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_body_has_no_owner_agent_line(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        for line in r.body.splitlines():
            assert not line.startswith("# OWNER_AGENT"), (
                f"{name}: body still contains OWNER_AGENT header"
            )

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_body_is_non_empty_string(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        assert r is not None
        assert isinstance(r.body, str)
        assert len(r.body.strip()) > 20

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_body_starts_with_non_comment_text(self, name):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt(name)
        first_line = r.body.lstrip("\n").splitlines()[0]
        assert not first_line.startswith("# "), (
            f"{name}: body first line is a comment: {first_line!r}"
        )

    def test_executive_producer_body_mentions_you_are(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("executive_producer")
        assert "You are" in r.body

    def test_char_count_matches_body_length(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("research_director")
        assert r.char_count == len(r.body)

    def test_char_count_in_list_mode_matches_full_mode(self):
        """list_prompts() char_count must equal get_prompt() char_count."""
        from hypogeum.prompt_registry import list_prompts, get_prompt
        for listed in list_prompts():
            full = get_prompt(listed.name)
            assert listed.char_count == full.char_count, (
                f"{listed.name}: list char_count={listed.char_count} "
                f"!= get char_count={full.char_count}"
            )


class TestPromptRegistryMissing:
    def test_get_prompt_missing_returns_none(self):
        from hypogeum.prompt_registry import get_prompt
        assert get_prompt("does_not_exist") is None

    def test_get_prompt_empty_string_returns_none(self):
        from hypogeum.prompt_registry import get_prompt
        assert get_prompt("") is None

    def test_get_prompt_metadata_missing_returns_none(self):
        from hypogeum.prompt_registry import get_prompt_metadata
        assert get_prompt_metadata("ghost_prompt") is None

    def test_get_prompt_path_traversal_is_safe(self):
        """Names with path separators return None (no file found)."""
        from hypogeum.prompt_registry import get_prompt
        assert get_prompt("../config") is None
        assert get_prompt("../../.env") is None


class TestPromptRegistryToDict:
    def test_to_dict_without_body_excludes_body_key(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("script_engine")
        d = r.to_dict(include_body=False)
        assert "body" not in d

    def test_to_dict_with_body_includes_body_key(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("script_engine")
        d = r.to_dict(include_body=True)
        assert "body" in d
        assert isinstance(d["body"], str)

    def test_to_dict_has_required_keys(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("executive_producer")
        d = r.to_dict(include_body=True)
        for key in ("name", "file_path", "prompt_version", "owner_agent",
                    "model_target", "purpose", "char_count", "body"):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_is_json_serialisable(self):
        from hypogeum.prompt_registry import get_prompt
        r = get_prompt("brand_guardian_agent")
        d = r.to_dict(include_body=True)
        # Must not raise
        serialised = json.dumps(d)
        assert len(serialised) > 100

    def test_to_dict_list_mode_is_json_serialisable(self):
        from hypogeum.prompt_registry import list_prompts
        records = list_prompts()
        payload = [r.to_dict(include_body=False) for r in records]
        serialised = json.dumps(payload)
        assert len(serialised) > 100


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """Flask test client with testing mode enabled."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from run_dashboard import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestPromptApiList:
    def test_api_prompts_returns_200(self, client):
        resp = client.get("/api/prompts")
        assert resp.status_code == 200

    def test_api_prompts_content_type_is_json(self, client):
        resp = client.get("/api/prompts")
        assert "application/json" in resp.content_type

    def test_api_prompts_has_prompts_key(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        assert "prompts" in data

    def test_api_prompts_count_matches_registry(self, client):
        from hypogeum.prompt_registry import list_prompts
        resp = client.get("/api/prompts")
        data = resp.get_json()
        assert len(data["prompts"]) == len(list_prompts())

    def test_api_prompts_all_expected_names_present(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        names = {p["name"] for p in data["prompts"]}
        assert names == _EXPECTED_NAMES

    def test_api_prompts_each_record_has_required_fields(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        required = {"name", "file_path", "prompt_version", "owner_agent",
                    "model_target", "purpose", "char_count"}
        for record in data["prompts"]:
            missing = required - set(record.keys())
            assert not missing, f"Record {record.get('name')!r} missing: {missing}"

    def test_api_prompts_list_has_no_body_field(self, client):
        """List endpoint must not include body text — callers use /api/prompts/<name>."""
        resp = client.get("/api/prompts")
        data = resp.get_json()
        for record in data["prompts"]:
            assert "body" not in record, (
                f"Record {record.get('name')!r} unexpectedly contains 'body'"
            )

    def test_api_prompts_music_json_excluded(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        names = {p["name"] for p in data["prompts"]}
        assert "music_categories" not in names

    def test_api_prompts_version_is_v1_for_all(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        for record in data["prompts"]:
            assert record["prompt_version"] in ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"), (
                f"{record['name']}: prompt_version={record['prompt_version']!r} (must be v1..v8)"
            )

    def test_api_prompts_char_count_is_positive_int(self, client):
        resp = client.get("/api/prompts")
        data = resp.get_json()
        for record in data["prompts"]:
            assert isinstance(record["char_count"], int)
            assert record["char_count"] > 0, f"{record['name']}: char_count=0"


class TestPromptApiDetail:
    def test_api_prompt_detail_returns_200(self, client):
        resp = client.get("/api/prompts/executive_producer")
        assert resp.status_code == 200

    def test_api_prompt_detail_content_type_json(self, client):
        resp = client.get("/api/prompts/executive_producer")
        assert "application/json" in resp.content_type

    def test_api_prompt_detail_has_prompt_key(self, client):
        resp = client.get("/api/prompts/executive_producer")
        data = resp.get_json()
        assert "prompt" in data

    def test_api_prompt_detail_has_body(self, client):
        resp = client.get("/api/prompts/executive_producer")
        data = resp.get_json()
        prompt = data["prompt"]
        assert "body" in prompt
        assert isinstance(prompt["body"], str)
        assert len(prompt["body"]) > 50

    def test_api_prompt_detail_body_excludes_headers(self, client):
        resp = client.get("/api/prompts/executive_producer")
        data = resp.get_json()
        body = data["prompt"]["body"]
        for line in body.splitlines():
            assert not line.startswith("# PROMPT_VERSION"), (
                f"Header leaked into API body: {line!r}"
            )

    def test_api_prompt_detail_metadata_correct(self, client):
        resp = client.get("/api/prompts/research_director")
        data = resp.get_json()
        p = data["prompt"]
        assert p["prompt_version"] == "v2"
        assert p["owner_agent"] == "research_director"
        assert "opus" in p["model_target"].lower()

    def test_api_prompt_detail_has_all_fields(self, client):
        resp = client.get("/api/prompts/script_engine")
        data = resp.get_json()
        p = data["prompt"]
        for field in ("name", "file_path", "prompt_version", "owner_agent",
                      "model_target", "purpose", "char_count", "body"):
            assert field in p, f"Missing field: {field}"

    def test_api_prompt_detail_name_matches_request(self, client):
        resp = client.get("/api/prompts/thumbnail_strategy")
        data = resp.get_json()
        assert data["prompt"]["name"] == "thumbnail_strategy"

    def test_api_prompt_detail_char_count_matches_body(self, client):
        resp = client.get("/api/prompts/brand_guardian_agent")
        data = resp.get_json()
        p = data["prompt"]
        assert p["char_count"] == len(p["body"])

    @pytest.mark.parametrize("name", sorted(_EXPECTED_NAMES))
    def test_api_prompt_detail_all_prompts_accessible(self, client, name):
        resp = client.get(f"/api/prompts/{name}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "prompt" in data
        assert data["prompt"]["name"] == name

    def test_api_prompt_detail_perplexity_research(self, client):
        resp = client.get("/api/prompts/perplexity_research")
        assert resp.status_code == 200
        data = resp.get_json()
        body = data["prompt"]["body"]
        assert "research assistant" in body.lower()
        assert "PROMPT_VERSION" not in body


class TestPromptApi404:
    def test_api_prompt_missing_returns_404(self, client):
        resp = client.get("/api/prompts/does_not_exist")
        assert resp.status_code == 404

    def test_api_prompt_missing_returns_json(self, client):
        resp = client.get("/api/prompts/does_not_exist")
        assert "application/json" in resp.content_type

    def test_api_prompt_missing_error_key(self, client):
        resp = client.get("/api/prompts/does_not_exist")
        data = resp.get_json()
        assert "error" in data

    def test_api_prompt_missing_error_mentions_name(self, client):
        resp = client.get("/api/prompts/ghost_prompt")
        data = resp.get_json()
        assert "ghost_prompt" in data["error"]

    def test_api_prompt_missing_no_prompt_key(self, client):
        resp = client.get("/api/prompts/does_not_exist")
        data = resp.get_json()
        assert "prompt" not in data

    def test_api_prompt_path_traversal_returns_404(self, client):
        """Path traversal attempts must not expose files outside prompts/."""
        resp = client.get("/api/prompts/../config")
        assert resp.status_code in (404, 405, 400)

    def test_api_prompt_music_categories_returns_404(self, client):
        """music_categories is a .json file, not accessible via .txt path."""
        resp = client.get("/api/prompts/music_categories")
        assert resp.status_code == 404


class TestPromptRegistryNoNetwork:
    """Importing and using the registry must never make network calls."""

    def test_list_prompts_makes_no_network_calls(self):
        with patch("requests.Session.send") as mock_send, \
             patch("requests.get") as mock_get, \
             patch("requests.post") as mock_post:
            from hypogeum.prompt_registry import list_prompts
            list_prompts()
            mock_send.assert_not_called()
            mock_get.assert_not_called()
            mock_post.assert_not_called()

    def test_get_prompt_makes_no_network_calls(self):
        with patch("requests.Session.send") as mock_send:
            from hypogeum.prompt_registry import get_prompt
            get_prompt("executive_producer")
            mock_send.assert_not_called()

    def test_get_prompt_metadata_makes_no_network_calls(self):
        with patch("requests.Session.send") as mock_send:
            from hypogeum.prompt_registry import get_prompt_metadata
            get_prompt_metadata("research_director")
            mock_send.assert_not_called()

    def test_import_prompt_registry_makes_no_network_calls(self):
        with patch("requests.Session.send") as mock_send:
            import importlib
            import hypogeum.prompt_registry
            importlib.reload(hypogeum.prompt_registry)
            mock_send.assert_not_called()


class TestMusicJsonExcluded:
    def test_music_categories_json_not_in_list(self):
        from hypogeum.prompt_registry import list_prompts
        names = {r.name for r in list_prompts()}
        assert "music_categories" not in names

    def test_get_prompt_music_categories_returns_none(self):
        from hypogeum.prompt_registry import get_prompt
        assert get_prompt("music_categories") is None

    def test_get_prompt_metadata_music_categories_returns_none(self):
        from hypogeum.prompt_registry import get_prompt_metadata
        assert get_prompt_metadata("music_categories") is None

    def test_only_txt_files_in_registry(self):
        from hypogeum.prompt_registry import list_prompts
        for r in list_prompts():
            assert r.file_path.endswith(".txt"), (
                f"{r.name}: non-.txt file in registry: {r.file_path}"
            )
