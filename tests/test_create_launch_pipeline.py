"""
tests/test_create_launch_pipeline.py — F14: /create/launch route integration tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "launch_route_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _post_launch(client, **kwargs):
    data = {
        "channel_id":   kwargs.get("channel_id",   "ch_test"),
        "topic":        kwargs.get("topic",         "my topic"),
        "video_type":   kwargs.get("video_type",    "long_video"),
        "quality_mode": kwargs.get("quality_mode",  "balanced"),
        "platforms":    kwargs.get("platforms",     ["youtube"]),
    }
    return client.post("/create/launch", data=data)


class TestCreateLaunchRoute:

    def test_redirects_to_episode_on_success(self, client):
        launched = {"status": "launched", "episode_id": "ep_test_001", "pid": 42}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched):
            resp = _post_launch(client)
        assert resp.status_code == 302
        assert "/episode/ep_test_001" in resp.headers["Location"]

    def test_redirects_to_create_with_error_on_blocked(self, client):
        blocked = {"status": "blocked", "missing": ["env:OPENROUTER_API_KEY"]}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=blocked):
            resp = _post_launch(client)
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "/create" in loc
        assert "error" in loc

    def test_error_redirect_not_episode(self, client):
        blocked = {"status": "blocked", "missing": ["ffmpeg not found on PATH"]}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=blocked):
            resp = _post_launch(client)
        assert "/episode/" not in resp.headers["Location"]

    def test_enqueue_topic_not_called_on_launch(self, client):
        launched = {"status": "launched", "episode_id": "ep_test_002", "pid": 99}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched), \
             patch("hypogeum.dashboard_control.enqueue_topic") as mock_enqueue:
            _post_launch(client)
        mock_enqueue.assert_not_called()

    def test_launch_uses_production_launcher(self, client):
        launched = {"status": "launched", "episode_id": "ep_xxx", "pid": 1}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched) as mock_launch:
            _post_launch(client)
        mock_launch.assert_called_once()

    def test_create_page_shows_error_from_query_param(self, client):
        resp = client.get("/create?error=env%3AOPENROUTER_API_KEY")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "OPENROUTER_API_KEY" in html or "error" in html.lower()

    def test_create_page_no_error_without_param(self, client):
        resp = client.get("/create")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Launch Error" not in html

    def test_launch_passes_channel_id(self, client):
        launched = {"status": "launched", "episode_id": "ep_ch_test", "pid": 1}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched) as mock_launch:
            _post_launch(client, channel_id="ch_special")
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("channel_id") == "ch_special" or \
               (call_kwargs.args and call_kwargs.args[0] == "ch_special")

    def test_launch_passes_topic(self, client):
        launched = {"status": "launched", "episode_id": "ep_topic_test", "pid": 1}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched) as mock_launch:
            _post_launch(client, topic="the silk road")
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("topic") == "the silk road" or \
               (call_kwargs.args and "the silk road" in call_kwargs.args)

    def test_no_mock_analytics_on_route_call(self, client):
        launched = {"status": "launched", "episode_id": "ep_no_mock", "pid": 1}
        with patch("hypogeum.production_launcher.launch_episode_from_dashboard",
                   return_value=launched), \
             patch("hypogeum.analytics_store.seed_mock_analytics") as mock_seed:
            _post_launch(client)
        mock_seed.assert_not_called()
