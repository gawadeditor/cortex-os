"""A persistent MUAPI 402 pauses production with the real cause, and the
preflight refuses to start an episode while that pause stands."""
import pytest

from hypogeum.db import get_worker_control, initialize_schema, set_worker_control
from hypogeum.muapi_spend_breaker import clear_breaker, trip_provider_billing
from hypogeum.production_preflight import (
    ProviderBillingBlockedError, assert_provider_billing_ok,
)
from hypogeum.operator_actions import _plain_error


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    return p


def test_trip_pauses_with_real_cause(db):
    assert trip_provider_billing("MUAPI", "HTTP 402", db_path=db) is True
    assert get_worker_control("queue_paused", "0", db_path=db) == "1"
    assert "MUAPI" in get_worker_control("queue_paused_reason", "", db_path=db)


def test_trip_never_overwrites_existing_pause(db):
    set_worker_control("queue_paused", "1", db_path=db)
    set_worker_control("queue_paused_reason", "muapi_spend_breaker: runaway", db_path=db)
    assert trip_provider_billing("MUAPI", "HTTP 402", db_path=db) is False
    assert get_worker_control("queue_paused_reason", "", db_path=db) == "muapi_spend_breaker: runaway"


def test_preflight_blocks_then_resume_clears(db):
    assert_provider_billing_ok(db_path=db)            # clean: passes
    trip_provider_billing("MUAPI", "HTTP 402", db_path=db)
    with pytest.raises(ProviderBillingBlockedError):
        assert_provider_billing_ok(db_path=db)
    clear_breaker(db_path=db)
    assert_provider_billing_ok(db_path=db)            # resumed: passes


def test_preflight_ignores_other_pause_reasons(db):
    set_worker_control("queue_paused", "1", db_path=db)
    set_worker_control("queue_paused_reason", "muapi_spend_breaker: runaway", db_path=db)
    assert_provider_billing_ok(db_path=db)


def test_muapi_402_is_not_reported_as_openrouter():
    assert _plain_error("MUAPI quota exhausted (HTTP 402) after every bounded retry") == "رصيد MUAPI خلص"
    assert _plain_error("OpenRouter 402 credits exhausted") == "رصيد OpenRouter خلص"
