"""Old OpenRouter key spent first, then the main key takes over on its own."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hypogeum import openrouter_drain as od


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-main")
    monkeypatch.setenv("OPENROUTER_DRAIN_KEY", "sk-old")


def test_config_uses_the_drain_key_until_exhausted(keys):
    from hypogeum.config import Config
    c = Config()
    assert c.OPENROUTER_API_KEY == "sk-old" and c.OPENROUTER_MAIN_KEY == "sk-main" and c.OPENROUTER_DRAIN_ACTIVE
    od.mark_exhausted("test")
    c = Config()
    assert c.OPENROUTER_API_KEY == "sk-main" and not c.OPENROUTER_DRAIN_ACTIVE


def test_low_drain_balance_switches_without_an_alert(keys):
    from hypogeum.config import Config
    from hypogeum.operator_alerts import check_openrouter_credits
    c = Config()
    answers = iter([{"total_credits": 470, "total_usage": 469},     # drain key: $1 left
                    {"total_credits": 25, "total_usage": 2}])        # main key: plenty
    out = check_openrouter_credits(c, http_get=lambda: next(answers))
    assert od.is_exhausted() and c.OPENROUTER_API_KEY == "sk-main"
    assert out == {}                                                   # no "credits low" alert


def test_402_on_the_drain_key_retries_the_same_call_on_the_main_key(keys, monkeypatch):
    from hypogeum import openrouter
    from hypogeum.config import Config
    seen = []

    def fake_post(url, headers, json, timeout):
        seen.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer sk-old":
            return SimpleNamespace(status_code=402, json=lambda: {}, text="")
        raise RuntimeError("stop-after-main")          # proves the retry used the main key
    monkeypatch.setattr(openrouter.requests, "post", fake_post)
    c = Config()
    with pytest.raises(RuntimeError, match="stop-after-main"):
        openrouter._call_openrouter("m", [{"role": "user", "content": "x"}], 10, c,
                                    openrouter.get_logger(agent="t"), 0)
    assert seen == ["Bearer sk-old", "Bearer sk-main"] and od.is_exhausted()
