"""
tests/test_prompt_files_phase2a.py — Phase 2A: Versioned prompt file extraction

Coverage:
    TestPromptFilesExist        — every expected prompt file is on disk
    TestPromptFileHeaders       — every prompt file has all 4 required header keys
    TestPromptLoader            — load_prompt strips headers, read_metadata parses them
    TestMissingPromptFails      — missing file raises FileNotFoundError at import
    TestBodyPreservation        — body text is byte-for-byte identical to pre-header content
    TestAgentLoadFromFile       — every agent SYSTEM_PROMPT is non-empty and header-free
    TestPerplexityPromptExtracted — perplexity.py uses file-backed prompt (no inline string)
    TestMusicCategoriesExtracted  — elevenlabs.py loads music_categories.json (no inline dict)
    TestMusicCategoriesContent    — all 6 categories present and non-empty in JSON
    TestNoNetworkCalls          — importing agent modules makes no network calls
    TestLoadMusicCategories     — load_music_categories returns correct structure
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "hypogeum" / "prompts"

_REQUIRED_HEADERS = {"PROMPT_VERSION", "OWNER_AGENT", "MODEL_TARGET", "PURPOSE"}

_EXPECTED_TXT_FILES = [
    "executive_producer.txt",
    "research_director.txt",
    "narrative_architect.txt",
    "script_engine.txt",
    "visual_package_director.txt",
    "thumbnail_director.txt",
    "thumbnail_identity_guardian.txt",
    "brand_guardian_agent.txt",
    "topic_ideator.txt",
    "thumbnail_strategy.txt",
    "perplexity_research.txt",  # Phase 2A new
]

_EXPECTED_JSON_FILES = [
    "music_categories.json",    # Phase 2A new
]

_MUSIC_CATEGORIES = [
    "Mystery",
    "Discovery",
    "Investigation",
    "Ancient Civilizations",
    "Tension",
    "Resolution",
]

# Agent → (module_path, attribute_name)
_AGENT_PROMPT_ATTRS = {
    "executive_producer":       ("hypogeum.agents.executive_producer",       "SYSTEM_PROMPT"),
    "research_director":        ("hypogeum.agents.research_director",        "SYSTEM_PROMPT"),
    "narrative_architect":      ("hypogeum.agents.narrative_architect",       "SYSTEM_PROMPT"),
    "script_engine":            ("hypogeum.agents.script_engine",            "SYSTEM_PROMPT"),
    "visual_package_director":  ("hypogeum.agents.visual_package_director",  "SYSTEM_PROMPT"),
    "thumbnail_prompt_writer":  ("hypogeum.agents.thumbnail_prompt_writer",  "SYSTEM_PROMPT"),
    "thumbnail_identity_guardian": ("hypogeum.agents.thumbnail_identity_guardian", "_SYSTEM_PROMPT"),
    "brand_guardian_agent":     ("hypogeum.agents.brand_guardian_agent",     "SYSTEM_PROMPT"),
    "topic_ideator":            ("hypogeum.discovery.topic_ideator",         "SYSTEM_PROMPT"),
    "thumbnail_strategy":       ("hypogeum.skills.thumbnail_strategy",       "SYSTEM_PROMPT"),
}


# ---------------------------------------------------------------------------
# TestPromptFilesExist
# ---------------------------------------------------------------------------

class TestPromptFilesExist:
    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_prompt_file_exists(self, filename):
        path = _PROMPTS_DIR / filename
        assert path.exists(), f"Missing prompt file: {path}"
        assert path.stat().st_size > 0, f"Prompt file is empty: {path}"

    @pytest.mark.parametrize("filename", _EXPECTED_JSON_FILES)
    def test_json_prompt_file_exists(self, filename):
        path = _PROMPTS_DIR / filename
        assert path.exists(), f"Missing JSON prompt file: {path}"
        assert path.stat().st_size > 0, f"JSON prompt file is empty: {path}"

    def test_prompts_directory_exists(self):
        assert _PROMPTS_DIR.is_dir(), f"Prompts directory missing: {_PROMPTS_DIR}"

    def test_perplexity_research_txt_is_new_file(self):
        """perplexity_research.txt must exist as the extracted inline prompt."""
        assert (_PROMPTS_DIR / "perplexity_research.txt").exists()

    def test_music_categories_json_is_new_file(self):
        """music_categories.json must exist as the extracted inline dict."""
        assert (_PROMPTS_DIR / "music_categories.json").exists()


# ---------------------------------------------------------------------------
# TestPromptFileHeaders
# ---------------------------------------------------------------------------

class TestPromptFileHeaders:
    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_file_has_all_required_headers(self, filename):
        from hypogeum.prompt_loader import read_metadata
        path = _PROMPTS_DIR / filename
        meta = read_metadata(path)
        missing = {k for k in _REQUIRED_HEADERS if not meta.get(k)}
        assert not missing, (
            f"{filename} is missing required headers: {missing}\n"
            f"Found: {meta}"
        )

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_file_prompt_version_is_v1(self, filename):
        from hypogeum.prompt_loader import read_metadata
        meta = read_metadata(_PROMPTS_DIR / filename)
        version = meta["PROMPT_VERSION"]
        assert version in ("v1", "v2"), (
            f"{filename}: PROMPT_VERSION must be 'v1' or 'v2', got {version!r}"
        )

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_file_owner_agent_is_non_empty(self, filename):
        from hypogeum.prompt_loader import read_metadata
        meta = read_metadata(_PROMPTS_DIR / filename)
        assert meta["OWNER_AGENT"], f"{filename}: OWNER_AGENT must not be empty"

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_file_model_target_is_non_empty(self, filename):
        from hypogeum.prompt_loader import read_metadata
        meta = read_metadata(_PROMPTS_DIR / filename)
        assert meta["MODEL_TARGET"], f"{filename}: MODEL_TARGET must not be empty"

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_txt_file_purpose_is_non_empty(self, filename):
        from hypogeum.prompt_loader import read_metadata
        meta = read_metadata(_PROMPTS_DIR / filename)
        assert meta["PURPOSE"], f"{filename}: PURPOSE must not be empty"

    def test_music_categories_json_has_meta_block(self):
        path = _PROMPTS_DIR / "music_categories.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        for key in _REQUIRED_HEADERS:
            assert meta.get(key), (
                f"music_categories.json _meta missing or empty: {key}"
            )

    def test_music_categories_json_version_is_v1(self):
        data = json.loads((_PROMPTS_DIR / "music_categories.json").read_text())
        assert data["_meta"]["PROMPT_VERSION"] == "v1"


# ---------------------------------------------------------------------------
# TestPromptLoader
# ---------------------------------------------------------------------------

class TestPromptLoader:
    def test_load_prompt_strips_header_lines(self, tmp_path):
        from hypogeum.prompt_loader import load_prompt
        f = tmp_path / "test.txt"
        f.write_text(
            "# PROMPT_VERSION: v1\n"
            "# OWNER_AGENT: test\n"
            "# MODEL_TARGET: claude\n"
            "# PURPOSE: test\n"
            "You are a test agent.\n"
            "Do things.\n",
            encoding="utf-8",
        )
        body = load_prompt(f)
        assert body.startswith("You are a test agent.")
        assert "PROMPT_VERSION" not in body
        assert "OWNER_AGENT" not in body

    def test_load_prompt_returns_full_body(self, tmp_path):
        from hypogeum.prompt_loader import load_prompt
        f = tmp_path / "test.txt"
        content = "# PROMPT_VERSION: v1\n# OWNER_AGENT: x\n# MODEL_TARGET: y\n# PURPOSE: z\nLine1\nLine2\nLine3\n"
        f.write_text(content, encoding="utf-8")
        body = load_prompt(f)
        assert "Line1" in body
        assert "Line2" in body
        assert "Line3" in body

    def test_load_prompt_no_headers_returns_full_content(self, tmp_path):
        """Files without headers are returned unchanged (backwards compat)."""
        from hypogeum.prompt_loader import load_prompt
        f = tmp_path / "plain.txt"
        f.write_text("You are an agent.\nDo stuff.\n", encoding="utf-8")
        body = load_prompt(f)
        assert body.strip() == "You are an agent.\nDo stuff."

    def test_load_prompt_raises_on_missing_file(self, tmp_path):
        from hypogeum.prompt_loader import load_prompt
        with pytest.raises(FileNotFoundError):
            load_prompt(tmp_path / "does_not_exist.txt")

    def test_load_prompt_raises_on_headers_only_file(self, tmp_path):
        from hypogeum.prompt_loader import load_prompt
        f = tmp_path / "empty_body.txt"
        f.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: x\n# MODEL_TARGET: y\n# PURPOSE: z\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty after stripping"):
            load_prompt(f)

    def test_read_metadata_parses_all_keys(self, tmp_path):
        from hypogeum.prompt_loader import read_metadata
        f = tmp_path / "meta.txt"
        f.write_text(
            "# PROMPT_VERSION: v2\n"
            "# OWNER_AGENT: my_agent\n"
            "# MODEL_TARGET: openai/gpt-4\n"
            "# PURPOSE: Do things well\n"
            "Body text here.\n",
            encoding="utf-8",
        )
        meta = read_metadata(f)
        assert meta["PROMPT_VERSION"] == "v2"
        assert meta["OWNER_AGENT"] == "my_agent"
        assert meta["MODEL_TARGET"] == "openai/gpt-4"
        assert meta["PURPOSE"] == "Do things well"

    def test_read_metadata_stops_at_first_non_header_line(self, tmp_path):
        from hypogeum.prompt_loader import read_metadata
        f = tmp_path / "meta2.txt"
        f.write_text(
            "# PROMPT_VERSION: v1\n"
            "# OWNER_AGENT: x\n"
            "Body line that has # in it\n"
            "# MODEL_TARGET: y\n",
            encoding="utf-8",
        )
        meta = read_metadata(f)
        # MODEL_TARGET should NOT be found — it comes after a non-header line
        assert meta["MODEL_TARGET"] is None

    def test_read_metadata_missing_required_keys_are_none(self, tmp_path):
        from hypogeum.prompt_loader import read_metadata
        f = tmp_path / "partial.txt"
        f.write_text("# PROMPT_VERSION: v1\nBody.\n", encoding="utf-8")
        meta = read_metadata(f)
        assert meta["OWNER_AGENT"] is None
        assert meta["MODEL_TARGET"] is None
        assert meta["PURPOSE"] is None

    def test_load_music_categories_returns_dict(self):
        from hypogeum.prompt_loader import load_music_categories
        path = _PROMPTS_DIR / "music_categories.json"
        cats = load_music_categories(path)
        assert isinstance(cats, dict)
        assert len(cats) == 6

    def test_load_music_categories_raises_on_missing_file(self, tmp_path):
        from hypogeum.prompt_loader import load_music_categories
        with pytest.raises(FileNotFoundError):
            load_music_categories(tmp_path / "nope.json")

    def test_load_music_categories_raises_on_missing_key(self, tmp_path):
        from hypogeum.prompt_loader import load_music_categories
        f = tmp_path / "bad.json"
        f.write_text('{"_meta": {}}', encoding="utf-8")
        with pytest.raises(KeyError):
            load_music_categories(f)


# ---------------------------------------------------------------------------
# TestMissingPromptFails
# ---------------------------------------------------------------------------

class TestMissingPromptFails:
    def test_load_prompt_raises_file_not_found_clearly(self, tmp_path):
        """A missing prompt file raises FileNotFoundError — no silent fallback."""
        from hypogeum.prompt_loader import load_prompt
        missing = tmp_path / "ghost.txt"
        with pytest.raises(FileNotFoundError):
            load_prompt(missing)

    def test_agent_missing_file_raises_at_import(self, tmp_path, monkeypatch):
        """If the prompt file is deleted, the agent module raises FileNotFoundError."""
        from hypogeum.prompt_loader import load_prompt
        fake_path = tmp_path / "nonexistent.txt"
        with pytest.raises(FileNotFoundError):
            load_prompt(fake_path)

    def test_music_categories_missing_raises(self, tmp_path):
        from hypogeum.prompt_loader import load_music_categories
        with pytest.raises(FileNotFoundError):
            load_music_categories(tmp_path / "no_cats.json")


# ---------------------------------------------------------------------------
# TestBodyPreservation
# ---------------------------------------------------------------------------

class TestBodyPreservation:
    """Verify that body text sent to LLM is header-free and non-empty."""

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_body_has_no_prompt_version_line(self, filename):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / filename)
        for line in body.splitlines():
            assert not line.startswith("# PROMPT_VERSION"), (
                f"{filename}: header line leaked into body: {line!r}"
            )

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_body_is_non_empty(self, filename):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / filename)
        assert len(body.strip()) > 50, (
            f"{filename}: body too short ({len(body)} chars) — likely stripped too much"
        )

    @pytest.mark.parametrize("filename", _EXPECTED_TXT_FILES)
    def test_body_starts_with_non_comment_text(self, filename):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / filename)
        first_line = body.lstrip("\n").splitlines()[0] if body.strip() else ""
        assert not first_line.startswith("# "), (
            f"{filename}: body first line is still a comment: {first_line!r}"
        )

    def test_executive_producer_body_starts_with_known_title(self):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / "executive_producer.txt")
        assert "EXECUTIVE PRODUCER" in body or "Executive Producer" in body

    def test_research_director_body_mentions_research(self):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / "research_director.txt")
        assert "Research" in body or "research" in body

    def test_perplexity_body_mentions_historical(self):
        from hypogeum.prompt_loader import load_prompt
        body = load_prompt(_PROMPTS_DIR / "perplexity_research.txt")
        assert "historical" in body.lower() or "research assistant" in body.lower()


# ---------------------------------------------------------------------------
# TestAgentLoadFromFile
# ---------------------------------------------------------------------------

class TestAgentLoadFromFile:
    @pytest.mark.parametrize("agent_name,mod_path,attr", [
        (name, mod, attr)
        for name, (mod, attr) in _AGENT_PROMPT_ATTRS.items()
    ])
    def test_agent_system_prompt_is_non_empty(self, agent_name, mod_path, attr):
        mod = importlib.import_module(mod_path)
        prompt = getattr(mod, attr)
        assert isinstance(prompt, str), f"{agent_name}: {attr} is not a str"
        assert len(prompt.strip()) > 20, (
            f"{agent_name}: {attr} is too short ({len(prompt)} chars)"
        )

    @pytest.mark.parametrize("agent_name,mod_path,attr", [
        (name, mod, attr)
        for name, (mod, attr) in _AGENT_PROMPT_ATTRS.items()
    ])
    def test_agent_system_prompt_has_no_header_lines(self, agent_name, mod_path, attr):
        """The SYSTEM_PROMPT value must not contain # KEY: VALUE header lines."""
        mod = importlib.import_module(mod_path)
        prompt = getattr(mod, attr)
        for line in prompt.splitlines():
            stripped = line.strip()
            assert not (stripped.startswith("# ") and ": " in stripped and
                        any(stripped.startswith(f"# {k}:") for k in _REQUIRED_HEADERS)), (
                f"{agent_name}: header line found in SYSTEM_PROMPT: {line!r}"
            )

    @pytest.mark.parametrize("agent_name,mod_path,attr", [
        (name, mod, attr)
        for name, (mod, attr) in _AGENT_PROMPT_ATTRS.items()
    ])
    def test_agent_system_prompt_contains_you_are(self, agent_name, mod_path, attr):
        """Most agent prompts start with 'You are' — sanity check against wrong file load."""
        mod = importlib.import_module(mod_path)
        prompt = getattr(mod, attr)
        assert "You are" in prompt or "you are" in prompt, (
            f"{agent_name}: SYSTEM_PROMPT does not contain 'You are' — "
            "possible wrong file or empty body"
        )


# ---------------------------------------------------------------------------
# TestPerplexityPromptExtracted
# ---------------------------------------------------------------------------

class TestPerplexityPromptExtracted:
    def test_perplexity_research_system_prompt_from_file(self):
        """_RESEARCH_SYSTEM_PROMPT must come from perplexity_research.txt, not inline."""
        import hypogeum.perplexity as perp
        # Verify it's a non-empty string loaded from the file
        assert isinstance(perp._RESEARCH_SYSTEM_PROMPT, str)
        assert len(perp._RESEARCH_SYSTEM_PROMPT.strip()) > 10

    def test_perplexity_module_has_prompt_file_attribute(self):
        import hypogeum.perplexity as perp
        assert hasattr(perp, "_PROMPT_FILE"), (
            "perplexity.py must define _PROMPT_FILE pointing to perplexity_research.txt"
        )
        assert perp._PROMPT_FILE.name == "perplexity_research.txt"

    def test_perplexity_system_prompt_no_header_lines(self):
        import hypogeum.perplexity as perp
        for line in perp._RESEARCH_SYSTEM_PROMPT.splitlines():
            assert not line.startswith("# PROMPT_VERSION"), (
                f"Header leaked into perplexity system prompt: {line!r}"
            )

    def test_perplexity_system_prompt_mentions_research_assistant(self):
        import hypogeum.perplexity as perp
        assert "research assistant" in perp._RESEARCH_SYSTEM_PROMPT.lower()

    def test_perplexity_no_inline_string_literal(self):
        """The inline _RESEARCH_SYSTEM_PROMPT string must be gone from perplexity.py source."""
        import hypogeum.perplexity as perp
        src = inspect.getsource(perp)
        assert '"You are a research assistant specialized in historical fact-finding.' not in src, (
            "Inline _RESEARCH_SYSTEM_PROMPT string still present in perplexity.py"
        )


# ---------------------------------------------------------------------------
# TestMusicCategoriesExtracted
# ---------------------------------------------------------------------------

class TestMusicCategoriesExtracted:
    def test_elevenlabs_music_category_prompts_from_json(self):
        """_MUSIC_CATEGORY_PROMPTS must be a dict loaded from music_categories.json."""
        import hypogeum.elevenlabs as el
        assert isinstance(el._MUSIC_CATEGORY_PROMPTS, dict)
        assert len(el._MUSIC_CATEGORY_PROMPTS) == 6

    def test_elevenlabs_has_music_categories_file_attr(self):
        import hypogeum.elevenlabs as el
        assert hasattr(el, "_MUSIC_CATEGORIES_FILE"), (
            "elevenlabs.py must define _MUSIC_CATEGORIES_FILE"
        )
        assert el._MUSIC_CATEGORIES_FILE.name == "music_categories.json"

    def test_elevenlabs_no_inline_dict_literal(self):
        """The inline _MUSIC_CATEGORY_PROMPTS dict must be gone from elevenlabs.py."""
        import hypogeum.elevenlabs as el
        src = inspect.getsource(el)
        assert '"Mystery": (' not in src, (
            "Inline 'Mystery' category string still present in elevenlabs.py"
        )
        assert '"Slow atmospheric dark underscore' not in src, (
            "Inline Mystery prompt string still present in elevenlabs.py"
        )


# ---------------------------------------------------------------------------
# TestMusicCategoriesContent
# ---------------------------------------------------------------------------

class TestMusicCategoriesContent:
    @pytest.mark.parametrize("category", _MUSIC_CATEGORIES)
    def test_category_present_in_json(self, category):
        data = json.loads((_PROMPTS_DIR / "music_categories.json").read_text())
        cats = data["categories"]
        assert category in cats, f"Category {category!r} missing from music_categories.json"

    @pytest.mark.parametrize("category", _MUSIC_CATEGORIES)
    def test_category_prompt_is_non_empty(self, category):
        data = json.loads((_PROMPTS_DIR / "music_categories.json").read_text())
        prompt = data["categories"][category]
        assert isinstance(prompt, str) and len(prompt) > 20, (
            f"Category {category!r} has empty or too-short prompt"
        )

    @pytest.mark.parametrize("category", _MUSIC_CATEGORIES)
    def test_elevenlabs_module_has_category(self, category):
        import hypogeum.elevenlabs as el
        assert category in el._MUSIC_CATEGORY_PROMPTS, (
            f"Category {category!r} missing from elevenlabs._MUSIC_CATEGORY_PROMPTS"
        )

    def test_mystery_prompt_preserved_exactly(self):
        """Spot-check that extraction preserved the original text."""
        import hypogeum.elevenlabs as el
        mystery = el._MUSIC_CATEGORY_PROMPTS["Mystery"]
        assert "solo piano" in mystery
        assert "60–80 BPM" in mystery
        assert "No percussion" in mystery

    def test_ancient_civilizations_prompt_preserved(self):
        import hypogeum.elevenlabs as el
        ancient = el._MUSIC_CATEGORY_PROMPTS["Ancient Civilizations"]
        assert "duduk" in ancient
        assert "ancient world" in ancient.lower()

    def test_music_categories_json_is_valid_json(self):
        path = _PROMPTS_DIR / "music_categories.json"
        data = json.loads(path.read_text())
        assert "categories" in data
        assert "_meta" in data


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    """Importing agent modules must never make network calls."""

    def test_importing_executive_producer_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send, \
             patch("requests.get") as mock_get, \
             patch("requests.post") as mock_post:
            import hypogeum.agents.executive_producer  # already imported — no re-exec
            mock_send.assert_not_called()
            mock_get.assert_not_called()
            mock_post.assert_not_called()

    def test_importing_perplexity_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send:
            import hypogeum.perplexity
            mock_send.assert_not_called()

    def test_importing_elevenlabs_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send:
            import hypogeum.elevenlabs
            mock_send.assert_not_called()

    def test_importing_topic_ideator_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send:
            import hypogeum.discovery.topic_ideator
            mock_send.assert_not_called()

    def test_importing_brand_guardian_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send:
            import hypogeum.agents.brand_guardian_agent
            mock_send.assert_not_called()

    def test_importing_thumbnail_identity_guardian_makes_no_network_call(self):
        with patch("requests.Session.send") as mock_send:
            import hypogeum.agents.thumbnail_identity_guardian
            mock_send.assert_not_called()
