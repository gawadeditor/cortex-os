"""PostizMediaStagingProvider: stage into Postiz's own library; the adapter
then reuses the uploaded {id, path} instead of importing it again."""
from __future__ import annotations

import hashlib

import pytest

from hypogeum.db import initialize_schema
from hypogeum.publishing import postiz_media_staging as pms
from hypogeum.publishing.media_staging import SourceMedia
from hypogeum.publishing.postiz import PostizMediaError, PostizTransportError, split_staged_media_url


class _Upload:
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.body, self.exc, self.calls = status, body, exc, []

    def upload(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.status, self.body


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    initialize_schema(db)
    monkeypatch.setattr(pms, "_resolve_postiz_credential", lambda db_path, channel_id=None: "key")
    f = tmp_path / "ep_aaaa1111_short.mp4"
    f.write_bytes(b"x" * 2048)
    src = SourceMedia(local_path=f, sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                      size_bytes=2048, mime_type="video/mp4", media_type="video", filename=f.name,
                      channel_id="ch", episode_id="ep_aaaa1111", deliverable_id="short_001")
    return db, src


def _prov(up):
    return pms.PostizMediaStagingProvider(upload_transport=up, base_url="http://pz/api")


def test_stage_returns_postiz_url_with_id_fragment(env):
    db, src = env
    up = _Upload(body={"id": "m1", "path": "https://pz.example/uploads/a.mp4"})
    res = _prov(up).stage(src, db_path=db)
    assert res.public_url == "https://pz.example/uploads/a.mp4#postiz-id=m1"
    assert up.calls[0]["headers"] == {"Authorization": "key"}
    assert up.calls[0]["url"] == "http://pz/api/upload"
    assert split_staged_media_url(res.public_url) == ("https://pz.example/uploads/a.mp4", "m1")


def test_second_stage_of_same_file_reuses_without_upload(env):
    db, src = env
    up = _Upload(body={"id": "m1", "path": "https://pz.example/uploads/a.mp4"})
    first = _prov(up).stage(src, db_path=db)
    second = _prov(up).stage(src, db_path=db)
    assert second.public_url == first.public_url and len(up.calls) == 1


def test_rejected_upload_fails_closed(env):
    db, src = env
    with pytest.raises(PostizMediaError):
        _prov(_Upload(status=413, body={})).stage(src, db_path=db)


def test_transport_error_is_ambiguous_and_blocks_blind_retry(env):
    db, src = env
    with pytest.raises(PostizMediaError):
        _prov(_Upload(exc=PostizTransportError("boom"))).stage(src, db_path=db)
    with pytest.raises(Exception):  # a prior ambiguous attempt must be reconciled, not re-uploaded
        _prov(_Upload(body={"id": "m1", "path": "https://x/y.mp4"})).stage(src, db_path=db)


def test_oversize_file_refused_before_any_call(env, monkeypatch):
    db, src = env
    monkeypatch.setattr(pms, "_MAX_SIZE_BYTES", 10)
    up = _Upload(body={})
    with pytest.raises(pms.PostizMediaTooLargeError):
        _prov(up).stage(src, db_path=db)
    assert up.calls == []


def test_plain_url_has_no_staged_id():
    assert split_staged_media_url("https://media.zernio.com/a.mp4") == ("https://media.zernio.com/a.mp4", None)
