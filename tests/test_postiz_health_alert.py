"""Postiz outage alert: silent on one blip, loud after consecutive failures,
self-clearing, and not shadowed by another check's alert prefix."""
import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.operator_alerts import _sync, check_postiz_health, open_alert_keys


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    conn = get_connection(p)
    conn.execute(
        "INSERT INTO channel_provider_bindings (binding_id, channel_id, capability, platform, provider_id, "
        "logical_alias, enabled, created_at, updated_at) VALUES ('b1','c1','publishing','youtube','postiz',"
        "'default',1,'2026-10-01','2026-10-01')")
    conn.commit()
    conn.close()
    return p


def _down(_):
    raise RuntimeError("HTTP 502")


def _up(_):
    return None


def test_one_failure_is_silent_second_alerts(db):
    assert check_postiz_health(db, probe=_down) == {}
    out = check_postiz_health(db, probe=_down)
    assert "pzdown:postiz" in out and "502" in out["pzdown:postiz"][1]


def test_recovery_clears_and_resets_counter(db):
    check_postiz_health(db, probe=_down)
    check_postiz_health(db, probe=_down)
    assert check_postiz_health(db, probe=_up) == {}
    assert check_postiz_health(db, probe=_down) == {}      # counter restarted from zero


def test_local_dns_failure_is_not_blamed_on_the_server(db):
    def dns(_):
        raise OSError("NameResolutionError: nodename nor servname provided")
    check_postiz_health(db, probe=dns)
    body = check_postiz_health(db, probe=dns)["pzdown:postiz"][1]
    assert "النت عندك" in body


def test_no_postiz_binding_means_no_alert(tmp_path):
    p = tmp_path / "e.db"
    initialize_schema(p)
    assert check_postiz_health(p, probe=_down) == {}
    assert check_postiz_health(p, probe=_down) == {}


def test_alert_not_resolved_by_the_generic_posts_prefix(db):
    sent = []
    _sync("pzdown:", {"pzdown:postiz": ("t", "b")}, db, None, sent.append)
    assert "pzdown:postiz" in open_alert_keys("pzdown:", db)
    _sync("post", {}, db, None, sent.append)              # the unrelated 'posts' check running
    assert "pzdown:postiz" in open_alert_keys("pzdown:", db)
