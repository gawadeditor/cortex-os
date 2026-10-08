"""OAuth code exchange lives in the Service layer, not the route handler."""

from __future__ import annotations

import pytest

from hypogeum import oauth_service


class _Resp:
    def __init__(self, ok, data):
        self.ok = ok
        self._data = data

    def json(self):
        return self._data


def test_no_code_is_rejected():
    r = oauth_service.exchange_youtube_code("", "http://x/cb")
    assert not r.ok and r.error == "no_code" and r.http_status == 400


def test_successful_exchange_persists_refresh_token(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(oauth_service.requests, "post",
                        lambda *a, **k: _Resp(True, {"refresh_token": "RT-123"}))
    persisted = {}
    monkeypatch.setattr(oauth_service, "_persist_refresh_token",
                        lambda prov, label, tok, db_path: persisted.update(
                            provider=prov, label=label, token=tok))

    r = oauth_service.exchange_youtube_code("code-abc", "http://x/cb", db_path=db)
    assert r.ok
    assert persisted == {"provider": "youtube", "label": "youtube-oauth-refresh",
                         "token": "RT-123"}


def test_provider_error_is_surfaced(monkeypatch):
    monkeypatch.setattr(oauth_service.requests, "post",
                        lambda *a, **k: _Resp(False, {"error": "invalid_grant",
                                                     "error_description": "bad code"}))
    r = oauth_service.exchange_youtube_code("code", "http://x/cb")
    assert not r.ok and r.error == "invalid_grant" and r.http_status == 400


def test_missing_refresh_token_is_an_error(monkeypatch):
    monkeypatch.setattr(oauth_service.requests, "post",
                        lambda *a, **k: _Resp(True, {"access_token": "AT-only"}))
    r = oauth_service.exchange_youtube_code("code", "http://x/cb")
    assert not r.ok and r.error == "no_refresh_token"


def test_network_error_is_502(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(oauth_service.requests, "post", _boom)
    r = oauth_service.exchange_youtube_code("code", "http://x/cb")
    assert not r.ok and r.http_status == 502


def test_route_handler_is_thin():
    # the callback route must delegate, not do the exchange inline
    import ast
    import inspect
    import run_dashboard
    src = inspect.getsource(run_dashboard.youtube_oauth_callback)
    assert "exchange_youtube_code" in src
    assert "oauth2.googleapis.com" not in src   # no token URL in the handler
    assert "grant_type" not in src              # no token payload in the handler


def test_youtube_oauth_label_is_per_channel_only_when_asked():
    from hypogeum.oauth_service import youtube_oauth_label
    assert youtube_oauth_label() == "youtube-oauth-refresh"
    assert youtube_oauth_label("twinkle_mini_tales") == "youtube-oauth-refresh:twinkle_mini_tales"
