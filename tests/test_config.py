"""
tests/test_config.py — Unit verification for hypogeum/config.py

Exercises:
  - Fail-fast on missing required var (ConfigError)
  - Optional vars resolve to defaults when absent
  - Confirmed model slugs are the defaults
  - Type coercion: int, float, None fallbacks
  - ConfigTypeError on bad type values
  - validate_for_milestone() raises MilestoneConfigError with full list of missing vars
  - validate_for_milestone() passes when all required vars are set
  - tiktok_available property is True only when all four vars are set
  - __repr__ never exposes secret values
  - .env file is parsed and does not override real env vars
  - Unknown milestone raises ValueError

Run with:  python -m pytest tests/test_config.py -v
"""

import os
import sys
import tempfile
import textwrap
import pytest

# Ensure the project root is on sys.path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import (
    Config,
    ConfigError,
    ConfigTypeError,
    MilestoneConfigError,
    _load_dotenv,
)
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(env: dict) -> Config:
    """Instantiate Config with a controlled environment."""
    original = os.environ.copy()
    # Remove all known pipeline vars so prior state doesn't bleed in
    pipeline_vars = [
        "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
        "OPENROUTER_APP_NAME", "OPENROUTER_FALLBACK_MODEL",
        "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
        "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL",
        "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
        "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST",
        "ELEVENLABS_MUSIC_MODEL_ID", "ELEVENLABS_MUSIC_DEFAULT_DURATION",
        "OPENAI_API_KEY", "WHISPER_MODEL",
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
        "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
        "META_INSTAGRAM_ACCOUNT_ID", "META_FACEBOOK_PAGE_ID",
        "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
    ]
    # Set all pipeline vars to "" before applying env so that _load_dotenv
    # (which skips keys already present in os.environ) cannot reload values
    # from a real .env file on disk and contaminate tests that expect missing vars.
    for var in pipeline_vars:
        os.environ[var] = ""
    os.environ.update(env)
    try:
        return Config()
    finally:
        # Restore original environment
        for var in pipeline_vars:
            os.environ.pop(var, None)
        os.environ.update({k: v for k, v in original.items() if k in pipeline_vars})


MINIMUM_ENV = {"OPENROUTER_API_KEY": "sk-or-test-key"}


# ---------------------------------------------------------------------------
# Fail-fast: missing required variable
# ---------------------------------------------------------------------------

class TestRequiredVars:
    def test_missing_openrouter_key_raises(self):
        with pytest.raises(ConfigError) as exc_info:
            make_config({})
        err = exc_info.value
        assert err.var_name == "OPENROUTER_API_KEY"
        assert "OPENROUTER_API_KEY" in str(err)
        assert ".env.example" in str(err)

    def test_whitespace_only_value_raises(self):
        with pytest.raises(ConfigError):
            make_config({"OPENROUTER_API_KEY": "   "})

    def test_minimum_required_env_succeeds(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.OPENROUTER_API_KEY == "sk-or-test-key"


# ---------------------------------------------------------------------------
# Model slug defaults
# ---------------------------------------------------------------------------

class TestModelSlugDefaults:
    def test_sonnet_slug_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.OPENROUTER_SONNET_MODEL_ID == "anthropic/claude-sonnet-4.6"

    def test_opus_slug_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.OPENROUTER_OPUS_MODEL_ID == "anthropic/claude-opus-4.6"

    def test_slug_override(self):
        cfg = make_config({
            **MINIMUM_ENV,
            "OPENROUTER_SONNET_MODEL_ID": "anthropic/claude-sonnet-4.5",
        })
        assert cfg.OPENROUTER_SONNET_MODEL_ID == "anthropic/claude-sonnet-4.5"


# ---------------------------------------------------------------------------
# Optional vars and defaults
# ---------------------------------------------------------------------------

class TestOptionalVars:
    def test_app_name_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.OPENROUTER_APP_NAME == "hypogeum-brain"

    def test_perplexity_model_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.PERPLEXITY_MODEL == "sonar-pro"

    def test_muapi_tier_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.MUAPI_DEFAULT_TIER == 2

    def test_muapi_timeout_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.MUAPI_TIMEOUT_SECONDS == 120

    def test_music_duration_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.ELEVENLABS_MUSIC_DEFAULT_DURATION == 120

    def test_whisper_model_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.WHISPER_MODEL == "whisper-1"

    def test_youtube_category_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.YOUTUBE_DEFAULT_CATEGORY_ID == 27

    def test_meta_graph_version_default(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.META_GRAPH_API_VERSION == "v19.0"

    def test_unset_optional_is_none(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.PERPLEXITY_API_KEY is None
        assert cfg.MUAPI_API_KEY is None
        assert cfg.ELEVENLABS_API_KEY is None
        assert cfg.ELEVENLABS_VOICE_ID is None
        assert cfg.YOUTUBE_CLIENT_ID is None
        assert cfg.META_APP_ID is None
        assert cfg.META_STAGING_BASE_URL is None


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

class TestTypeCoercion:
    def test_int_var_parsed(self):
        cfg = make_config({**MINIMUM_ENV, "MUAPI_DEFAULT_TIER": "3"})
        assert cfg.MUAPI_DEFAULT_TIER == 3
        assert isinstance(cfg.MUAPI_DEFAULT_TIER, int)

    def test_float_var_parsed(self):
        cfg = make_config({**MINIMUM_ENV, "ELEVENLABS_STABILITY": "0.75"})
        assert cfg.ELEVENLABS_STABILITY == pytest.approx(0.75)
        assert isinstance(cfg.ELEVENLABS_STABILITY, float)

    def test_float_var_absent_is_none(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.ELEVENLABS_STABILITY is None
        assert cfg.ELEVENLABS_SIMILARITY_BOOST is None

    def test_bad_int_raises_type_error(self):
        with pytest.raises(ConfigTypeError) as exc_info:
            make_config({**MINIMUM_ENV, "MUAPI_DEFAULT_TIER": "not-a-number"})
        assert "MUAPI_DEFAULT_TIER" in str(exc_info.value)

    def test_bad_float_raises_type_error(self):
        with pytest.raises(ConfigTypeError) as exc_info:
            make_config({**MINIMUM_ENV, "ELEVENLABS_STABILITY": "high"})
        assert "ELEVENLABS_STABILITY" in str(exc_info.value)

    def test_perplexity_max_tokens_int(self):
        cfg = make_config({**MINIMUM_ENV, "PERPLEXITY_MAX_TOKENS": "4096"})
        assert cfg.PERPLEXITY_MAX_TOKENS == 4096

    def test_perplexity_max_tokens_absent_is_none(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.PERPLEXITY_MAX_TOKENS is None


# ---------------------------------------------------------------------------
# TikTok availability property
# ---------------------------------------------------------------------------

class TestTikTokAvailable:
    def test_false_when_no_credentials(self):
        cfg = make_config(MINIMUM_ENV)
        assert cfg.tiktok_available is False

    def test_false_when_partial_credentials(self):
        cfg = make_config({
            **MINIMUM_ENV,
            "TIKTOK_CLIENT_KEY": "key",
            "TIKTOK_CLIENT_SECRET": "secret",
            # TIKTOK_ACCESS_TOKEN and TIKTOK_OPEN_ID absent
        })
        assert cfg.tiktok_available is False

    def test_true_when_all_credentials_present(self):
        cfg = make_config({
            **MINIMUM_ENV,
            "TIKTOK_CLIENT_KEY": "key",
            "TIKTOK_CLIENT_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "token",
            "TIKTOK_OPEN_ID": "openid",
        })
        assert cfg.tiktok_available is True


# ---------------------------------------------------------------------------
# validate_for_milestone()
# ---------------------------------------------------------------------------

class TestValidateForMilestone:
    def test_m0_passes_with_openrouter_key(self):
        cfg = make_config(MINIMUM_ENV)
        cfg.validate_for_milestone("M0")  # should not raise

    def test_m0_case_insensitive(self):
        cfg = make_config(MINIMUM_ENV)
        cfg.validate_for_milestone("m0")  # should not raise

    def test_m1_fails_without_perplexity(self):
        cfg = make_config(MINIMUM_ENV)
        with pytest.raises(MilestoneConfigError) as exc_info:
            cfg.validate_for_milestone("M1")
        err_str = str(exc_info.value)
        assert "PERPLEXITY_API_KEY" in err_str
        assert "M1" in err_str

    def test_m1_passes_with_all_required(self):
        cfg = make_config({**MINIMUM_ENV, "PERPLEXITY_API_KEY": "pplx-key"})
        cfg.validate_for_milestone("M1")  # should not raise

    def test_m5_lists_all_missing_vars(self):
        cfg = make_config(MINIMUM_ENV)
        with pytest.raises(MilestoneConfigError) as exc_info:
            cfg.validate_for_milestone("M5")
        err_str = str(exc_info.value)
        # All M5-specific vars should be named
        for name in [
            "PERPLEXITY_API_KEY", "MUAPI_API_KEY", "ELEVENLABS_API_KEY",
            "ELEVENLABS_VOICE_ID", "YOUTUBE_CLIENT_ID", "META_APP_ID",
            "META_STAGING_BASE_URL",
        ]:
            assert name in err_str, f"Expected {name} in error message"

    def test_unknown_milestone_raises_value_error(self):
        cfg = make_config(MINIMUM_ENV)
        with pytest.raises(ValueError) as exc_info:
            cfg.validate_for_milestone("M99")
        assert "M99" in str(exc_info.value)


# ---------------------------------------------------------------------------
# __repr__ safety (no secret leakage)
# ---------------------------------------------------------------------------

class TestReprSafety:
    def test_repr_does_not_contain_api_key_value(self):
        secret = "sk-or-super-secret-key-12345"
        cfg = make_config({"OPENROUTER_API_KEY": secret})
        r = repr(cfg)
        assert secret not in r
        assert "SET" in r

    def test_repr_shows_model_slugs(self):
        cfg = make_config(MINIMUM_ENV)
        r = repr(cfg)
        assert "anthropic/claude-sonnet-4.6" in r
        assert "anthropic/claude-opus-4.6" in r


# ---------------------------------------------------------------------------
# .env file parsing (_load_dotenv)
# ---------------------------------------------------------------------------

class TestDotenvParser:
    def _write_env(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        f.write(textwrap.dedent(content))
        f.close()
        return Path(f.name)

    def test_basic_key_value(self):
        path = self._write_env("FOO=bar\n")
        os.environ.pop("FOO", None)
        _load_dotenv(path)
        assert os.environ.get("FOO") == "bar"
        del os.environ["FOO"]

    def test_does_not_override_existing_env(self):
        path = self._write_env("FOO=from_file\n")
        os.environ["FOO"] = "from_env"
        _load_dotenv(path)
        assert os.environ["FOO"] == "from_env"
        del os.environ["FOO"]

    def test_comment_lines_ignored(self):
        path = self._write_env("# This is a comment\nBAR=baz\n")
        os.environ.pop("BAR", None)
        _load_dotenv(path)
        assert os.environ.get("BAR") == "baz"
        del os.environ["BAR"]

    def test_inline_comment_stripped(self):
        path = self._write_env("QUX=value # inline comment\n")
        os.environ.pop("QUX", None)
        _load_dotenv(path)
        assert os.environ.get("QUX") == "value"
        del os.environ["QUX"]

    def test_quoted_value_unquoted(self):
        path = self._write_env('QUOTED="hello world"\n')
        os.environ.pop("QUOTED", None)
        _load_dotenv(path)
        assert os.environ.get("QUOTED") == "hello world"
        del os.environ["QUOTED"]

    def test_blank_lines_ignored(self):
        path = self._write_env("\n\nKEY=val\n\n")
        os.environ.pop("KEY", None)
        _load_dotenv(path)
        assert os.environ.get("KEY") == "val"
        del os.environ["KEY"]

    def test_missing_file_is_noop(self):
        # Should not raise
        _load_dotenv(Path("/tmp/definitely_does_not_exist_hypogeum.env"))
