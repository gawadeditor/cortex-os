"""
tests/test_publishing_postiz.py — hypogeum.publishing.postiz.PostizProvider.

Isolated tmp_path DB + injected FakePostizTransport; no network, no real
Postiz call, no production DB. Response shapes mirror the Postiz source on
our server (POST /posts -> [{postId, integration}], POST /upload-from-url
-> {id, path}, GET /posts -> {posts: [...]}).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import initialize_schema
from hypogeum.publishing.postiz import (
    PostizAccountNeedsReconnectionError,
    PostizAmbiguousRetryBlockedError,
    PostizBindingMismatchError,
    PostizBindingNotFoundError,
    PostizCredentialNotFoundError,
    PostizMediaError,
    PostizProvider,
    PostizPublishRejectedError,
    PostizScheduleValidationError,
    PostizSettingsError,
    PostizTransportError,
    PostizTransportResponse,
    _to_postiz_html,
    build_platform_settings,
    options_from_runtime_kwargs,
)
from hypogeum.publishing.provider import MediaReference, UnsupportedPlatformError
from hypogeum.publishing.provider_registry import build_default_registry
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    get_provider_account,
    upsert_provider_account,
    upsert_provider_profile,
)

BASE = "https://postiz.test/api/public/v1"
INTEGRATION = "integ_yt_1"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "postiz_test.db"
    initialize_schema(p)
    return p


class FakePostizTransport:
    """Routes by URL suffix; records every call."""

    def __init__(self, *, create=None, error=None, upload_status=201):
        self.calls = []
        self._create = create
        self._error = error
        self._upload_status = upload_status
        self._uploads = 0

    def post(self, *, url, headers, json_body, timeout):
        self.calls.append(("POST", url, headers, json_body))
        if url.endswith("/upload-from-url"):
            self._uploads += 1
            if self._upload_status != 201:
                return PostizTransportResponse(self._upload_status, {"msg": "cannot fetch"})
            return PostizTransportResponse(201, {"id": f"m{self._uploads}",
                                                  "path": f"https://postiz.test/uploads/m{self._uploads}.mp4"})
        if self._error is not None:
            raise self._error
        if self._create is not None:
            return self._create
        integ = json_body["posts"][0]["integration"]["id"]
        return PostizTransportResponse(201, [{"postId": "pz_post_1", "integration": integ}])

    def get(self, *, url, headers, timeout):
        self.calls.append(("GET", url, headers, None))
        return PostizTransportResponse(200, {"posts": [
            {"id": "pz_post_1", "state": "PUBLISHED", "releaseURL": "https://youtu.be/abc",
             "releaseId": "abc", "publishDate": "2026-10-10T14:00:00.000Z",
             "integration": {"id": INTEGRATION, "providerIdentifier": "youtube"}},
        ]})

    def delete(self, *, url, headers, timeout):
        self.calls.append(("DELETE", url, headers, None))
        return PostizTransportResponse(200, {})

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/posts")]


def _setup(db_path, *, platform="youtube", provider_on_binding="postiz", account_status="active",
           needs_reconnection=False, external_account_id=INTEGRATION, credential=True):
    profile = upsert_provider_profile(db_path, provider_id="postiz", external_profile_id="org_1")
    account = upsert_provider_account(
        db_path, provider_id="postiz", platform=platform, external_account_id=external_account_id,
        provider_profile_id=profile["provider_profile_id"], status=account_status,
        needs_reconnection=needs_reconnection,
    )
    bind_channel_provider(
        db_path, channel_id="test_channel", capability="publishing", platform=platform,
        provider_id=provider_on_binding, provider_profile_id=profile["provider_profile_id"],
        provider_account_id=account["provider_account_id"], logical_alias="default",
    )
    if credential:
        create_secret_profile(provider="postiz", label="test postiz key", credential_type="api_key",
                              credential_data="pz_test_fake_key", scope="factory", db_path=db_path)
    return account


def _future(hours=24):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _kwargs(db_path, **over):
    kw = dict(
        channel_id="test_channel", deliverable_id="short_001", db_path=db_path,
        media=[MediaReference(url="https://cdn.example.com/v.mp4", media_type="video")],
        scheduled_for=_future(), title="Juno's Blanket Fort",
        content="Line one & more\n\nLine three",
        platform_options={"privacy": "public", "made_for_kids": True, "tags": ["bedtime"]},
    )
    kw.update(over)
    return kw


def _provider(transport):
    return PostizProvider(transport=transport, base_url=BASE)


# -- pure helpers -----------------------------------------------------------

def test_html_conversion_escapes_and_keeps_blank_lines():
    assert _to_postiz_html("a & b\n\nc <d>") == "<p>a &amp; b</p><p></p><p>c &lt;d&gt;</p>"
    assert _to_postiz_html(None) == ""


def test_youtube_made_for_kids_is_never_defaulted():
    with pytest.raises(PostizSettingsError, match="made_for_kids"):
        build_platform_settings("youtube", title="A title", options={})


def test_youtube_title_length_enforced():
    with pytest.raises(PostizSettingsError):
        build_platform_settings("youtube", title="x" * 101, options={"made_for_kids": False})


def test_tiktok_privacy_required_and_defaults_safe():
    with pytest.raises(PostizSettingsError, match="privacy_level"):
        build_platform_settings("tiktok", title="t", options={})
    s = build_platform_settings("tiktok", title="t", options={"privacy_level": "SELF_ONLY"})
    assert s["__type"] == "tiktok" and s["content_posting_method"] == "DIRECT_POST"
    assert s["video_made_with_ai"] is True and s["duet"] is False


def test_instagram_and_facebook_settings_shape():
    assert build_platform_settings("instagram", title=None, options={}) == {
        "__type": "instagram", "post_type": "post", "collaborators": []}
    assert build_platform_settings("facebook", title=None, options={}) == {"__type": "facebook"}


# -- registry -----------------------------------------------------------------

def test_registered_but_not_default_routed():
    from hypogeum.publishing.provider_registry import DEFAULT_PROVIDER_ROUTING
    registry = build_default_registry()
    assert isinstance(registry.resolve("postiz"), PostizProvider)
    assert "postiz" not in DEFAULT_PROVIDER_ROUTING.values()


# -- publish ------------------------------------------------------------------

def test_publish_success_records_receipt_and_sends_expected_body(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    result = _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                                  **_kwargs(db_path))
    assert result["status"] == "published"
    assert result["provider_content_id"] == "pz_post_1"
    (create,) = t.creates()
    body = create[3]
    assert create[2]["Authorization"] == "pz_test_fake_key"
    assert body["type"] == "schedule" and body["shortLink"] is False
    post = body["posts"][0]
    assert post["integration"] == {"id": INTEGRATION}
    assert post["value"][0]["content"] == "<p>Line one &amp; more</p><p></p><p>Line three</p>"
    assert post["value"][0]["image"] == [{"id": "m1", "path": "https://postiz.test/uploads/m1.mp4"}]
    assert post["settings"]["selfDeclaredMadeForKids"] == "yes"
    assert post["settings"]["tags"] == [{"value": "bedtime", "label": "bedtime"}]


def test_retry_after_success_reuses_attempt_without_new_post(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    p = _provider(t)
    p.publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    again = p.publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    assert again["reused_existing_attempt"] is True
    assert again["provider_content_id"] == "pz_post_1"
    assert len(t.creates()) == 1


def test_youtube_thumbnail_is_imported_and_attached(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    media = [MediaReference(url="https://cdn.example.com/v.mp4", media_type="video",
                            thumbnail_url="https://cdn.example.com/cover.jpg")]
    _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                         **_kwargs(db_path, media=media))
    settings = t.creates()[0][3]["posts"][0]["settings"]
    assert settings["thumbnail"] == {"id": "m2", "path": "https://postiz.test/uploads/m2.mp4"}


def test_transport_error_is_ambiguous_and_blocks_blind_retry(db_path):
    _setup(db_path)
    t = FakePostizTransport(error=PostizTransportError("timeout"))
    p = _provider(t)
    r = p.publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    assert r["status"] == "ambiguous"
    with pytest.raises(PostizAmbiguousRetryBlockedError):
        p.publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))


def test_validation_400_is_definitive_and_raises(db_path):
    _setup(db_path)
    t = FakePostizTransport(create=PostizTransportResponse(400, {"msg": "Please fix your settings"}))
    with pytest.raises(PostizPublishRejectedError, match="fix your settings"):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))


def test_401_marks_account_needs_reconnection(db_path):
    account = _setup(db_path)
    t = FakePostizTransport(create=PostizTransportResponse(401, {"msg": "Unauthorized"}))
    with pytest.raises(PostizPublishRejectedError):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))
    assert get_provider_account(db_path, provider_account_id=account["provider_account_id"])[
        "needs_reconnection"]


def test_server_error_is_ambiguous(db_path):
    _setup(db_path)
    t = FakePostizTransport(create=PostizTransportResponse(503, None))
    r = _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))
    assert r["status"] == "ambiguous"


def test_response_without_matching_integration_is_ambiguous(db_path):
    _setup(db_path)
    t = FakePostizTransport(create=PostizTransportResponse(201, [{"postId": "x", "integration": "other"}]))
    r = _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))
    assert r["status"] == "ambiguous"


def test_media_import_failure_is_definitive_and_retryable(db_path):
    _setup(db_path)
    with pytest.raises(PostizMediaError):
        _provider(FakePostizTransport(upload_status=400)).publish(
            platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    ok = _provider(FakePostizTransport()).publish(
        platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    assert ok["status"] == "published"


# -- fail-closed preconditions ---------------------------------------------------

def test_no_binding_fails_closed(db_path):
    t = FakePostizTransport()
    with pytest.raises(PostizBindingNotFoundError):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))
    assert t.calls == []


def test_binding_on_other_provider_is_refused(db_path):
    _setup(db_path, provider_on_binding="zernio")
    with pytest.raises(PostizBindingMismatchError):
        _provider(FakePostizTransport()).publish(platform="youtube", deliverable_type="short",
                                                 episode_id="ep_1", **_kwargs(db_path))


def test_disconnected_account_refused(db_path):
    _setup(db_path, needs_reconnection=True)
    with pytest.raises(PostizAccountNeedsReconnectionError):
        _provider(FakePostizTransport()).publish(platform="youtube", deliverable_type="short",
                                                 episode_id="ep_1", **_kwargs(db_path))


def test_missing_credential_refused_before_any_call(db_path):
    _setup(db_path, credential=False)
    t = FakePostizTransport()
    with pytest.raises(PostizCredentialNotFoundError):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path))
    assert t.calls == []


@pytest.mark.parametrize("over,err", [
    ({"media": []}, PostizMediaError),
    ({"media": [MediaReference(url="/local/file.mp4", media_type="video")]}, PostizMediaError),
    ({"scheduled_for": datetime.now(timezone.utc) - timedelta(hours=1)}, PostizScheduleValidationError),
    ({"scheduled_for": datetime(2030, 1, 1)}, PostizScheduleValidationError),
    ({"platform_options": {"privacy": "public"}}, PostizSettingsError),
])
def test_bad_input_rejected_before_any_db_or_network(db_path, over, err):
    t = FakePostizTransport()
    with pytest.raises(err):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                             **_kwargs(db_path, **over))
    assert t.calls == []


def test_unsupported_platform(db_path):
    with pytest.raises(UnsupportedPlatformError):
        _provider(FakePostizTransport()).publish(platform="x", deliverable_type="short",
                                                 episode_id="ep_1", **_kwargs(db_path))


# -- multi-platform -----------------------------------------------------------------

def test_multi_platform_one_post_per_platform_with_own_time(db_path):
    _setup(db_path, platform="youtube", external_account_id="integ_yt")
    _setup(db_path, platform="facebook", external_account_id="integ_fb", credential=False)
    t = FakePostizTransport()
    yt_time, fb_time = _future(24), _future(48)
    out = _provider(t).publish_multi_platform(
        platforms=["youtube", "facebook", "youtube"], deliverable_type="short", episode_id="ep_1",
        **{k: v for k, v in _kwargs(db_path).items() if k != "platform_options"},
        platform_scheduled_for_by_platform={"youtube": yt_time, "facebook": fb_time},
        platform_options={"youtube": {"made_for_kids": True}},
    )
    assert set(out) == {"youtube", "facebook"}
    assert all(r["status"] == "published" for r in out.values())
    creates = t.creates()
    assert len(creates) == 2
    by_integ = {c[3]["posts"][0]["integration"]["id"]: c[3] for c in creates}
    assert by_integ["integ_yt"]["date"].startswith(yt_time.strftime("%Y-%m-%dT%H:%M"))
    assert by_integ["integ_fb"]["date"].startswith(fb_time.strftime("%Y-%m-%dT%H:%M"))


def test_multi_platform_one_rejection_does_not_stop_others(db_path):
    _setup(db_path, platform="youtube", external_account_id="integ_yt")
    _setup(db_path, platform="facebook", external_account_id="integ_fb", credential=False)

    class Mixed(FakePostizTransport):
        def post(self, *, url, headers, json_body, timeout):
            if url.endswith("/posts") and json_body["posts"][0]["integration"]["id"] == "integ_yt":
                self.calls.append(("POST", url, headers, json_body))
                return PostizTransportResponse(400, {"msg": "bad youtube"})
            return super().post(url=url, headers=headers, json_body=json_body, timeout=timeout)

    out = _provider(Mixed()).publish_multi_platform(
        platforms=["youtube", "facebook"], deliverable_type="short", episode_id="ep_1",
        **{k: v for k, v in _kwargs(db_path).items() if k != "platform_options"},
        platform_options={"youtube": {"made_for_kids": False}},
    )
    assert out["youtube"]["status"] == "failed" and "bad youtube" in out["youtube"]["error"]
    assert out["facebook"]["status"] == "published"


# -- read side -------------------------------------------------------------------------

def test_get_post_finds_by_id_in_window(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    post = _provider(t).get_post("pz_post_1", around=datetime(2026, 10, 10, 14, tzinfo=timezone.utc),
                                 db_path=db_path)
    assert post["state"] == "PUBLISHED" and post["release_id"] == "abc"
    assert "startDate=2026-10-08" in t.calls[0][1] and "endDate=2026-10-12" in t.calls[0][1]
    assert _provider(t).get_post("missing", around=datetime(2026, 10, 10, tzinfo=timezone.utc),
                                 db_path=db_path) is None


# -- runtime (Zernio-shaped) inputs ---------------------------------------------------

def test_runtime_kwargs_translate_without_inventing_values():
    yt = options_from_runtime_kwargs(
        "youtube", platform_specific_data={"title": "T", "categoryId": "27", "madeForKids": True,
                                           "visibility": "unlisted"},
        root_fields={"tags": ["a", "b"], "hashtags": ["#x"]})
    assert yt == {"privacy": "unlisted", "made_for_kids": True, "tags": ["a", "b"]}
    assert "made_for_kids" not in options_from_runtime_kwargs("youtube", platform_specific_data={"title": "T"})
    # channel policy arrives via platform_settings (how the release trigger builds it)
    assert options_from_runtime_kwargs("youtube", platform_settings={"categoryId": "27", "madeForKids": True}) == {
        "made_for_kids": True}
    # per-episode metadata wins over policy
    assert options_from_runtime_kwargs("youtube", platform_settings={"madeForKids": True},
                                       platform_specific_data={"madeForKids": False})["made_for_kids"] is False
    tt = options_from_runtime_kwargs("tiktok", platform_settings={
        "privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True,
        "allow_stitch": False, "video_cover_image_url": "https://x/c.jpg"})
    assert tt == {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comments": True,
                  "allow_duet": True, "allow_stitch": False}


def test_publish_accepts_runtime_shaped_kwargs(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    kw = _kwargs(db_path)
    del kw["platform_options"]
    kw.update(platform_specific_data={"title": "Per-platform YouTube title", "madeForKids": False},
              root_fields={"tags": ["t1"]}, timezone_name="UTC")
    r = _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **kw)
    assert r["status"] == "published"
    settings = t.creates()[0][3]["posts"][0]["settings"]
    assert settings["title"] == "Per-platform YouTube title"
    assert settings["selfDeclaredMadeForKids"] == "no"
    assert settings["type"] == "public"


def test_youtube_without_made_for_kids_in_policy_fails_closed(db_path):
    """A channel policy that never set madeForKids must not publish to YouTube
    via Postiz (Postiz requires the declaration; Zernio silently omitted it)."""
    _setup(db_path)
    t = FakePostizTransport()
    kw = _kwargs(db_path)
    del kw["platform_options"]
    kw["platform_specific_data"] = {"title": "A valid title"}
    with pytest.raises(PostizSettingsError, match="made_for_kids"):
        _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **kw)
    assert t.calls == []


def test_draft_sends_draft_type(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                         **_kwargs(db_path, scheduled_for=None, is_draft=True))
    assert t.creates()[0][3]["type"] == "draft"


class TestYoutubeTagsFitTheFiveHundredCharacterLimit:
    """2026-10-10: Postiz rejected a YouTube post with 'The maximum allowed is 500 characters in total for all tags'."""

    def test_tags_are_trimmed_from_the_tail_to_fit(self):
        from hypogeum.publishing.postiz import _fit_youtube_tags, _YOUTUBE_TAGS_MAX_CHARS
        tags = [f"bedtime story for kids number {i}" for i in range(40)]
        kept = _fit_youtube_tags(tags)
        assert kept == tags[:len(kept)] and 0 < len(kept) < len(tags)
        assert sum(len(t) + 1 + 2 for t in kept) <= _YOUTUBE_TAGS_MAX_CHARS

    def test_a_short_tag_list_is_untouched(self):
        from hypogeum.publishing.postiz import _fit_youtube_tags
        assert _fit_youtube_tags(["history", "mystery"]) == ["history", "mystery"]
