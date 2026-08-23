"""
tests/test_zernio_short_repair.py — local repair of an existing,
already-scheduled Zernio post whose title/content/media/thumbnail is
wrong (the ep_f3619022 "No content" defect), covering:

- hypogeum.publishing.short_caption_repair (coherent cutoff selection,
  truncated .ass, ffmpeg command construction) -- pure, no ffmpeg
  process is ever started by these tests.
- hypogeum.publishing.zernio_repair_manifest (precondition gating,
  proposed PUT body construction) -- pure.
- hypogeum.publishing.zernio.ZernioProvider.repair_existing_post() --
  integration-level, through the real method, using a fake transport.
  No network call, no real Zernio credential, no production database.
"""

from __future__ import annotations

import json

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider import MediaReference
from hypogeum.publishing.provider_repository import bind_channel_provider, upsert_provider_account, upsert_provider_profile
from hypogeum.publishing.short_caption_repair import (
    NoCoherentCutoffError,
    build_repair_ffmpeg_command,
    build_trimmed_ass_text,
    select_coherent_cutoff,
)
from hypogeum.publishing.submission_ledger import guard_and_prepare_attempt, record_ambiguous, record_success
from hypogeum.publishing.zernio import (
    ZernioAmbiguousRetryBlockedError,
    ZernioProvider,
    ZernioTransportResponse,
)
from hypogeum.publishing.zernio_repair_manifest import (
    build_proposed_update_body,
    validate_repair_preconditions,
)

CHANNEL_ID = "hypogeum"
EPISODE_ID = "ep_f3619022"
ALL_PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"]


# ===========================================================================
# select_coherent_cutoff / build_trimmed_ass_text / build_repair_ffmpeg_command
# ===========================================================================

_SAMPLE_SRT = """1
00:00:00,000 --> 00:00:02,000
This is a complete sentence.

2
00:00:02,100 --> 00:00:04,000
This one trails off without

3
00:00:04,100 --> 00:00:06,000
punctuation and continues here.

4
00:00:06,100 --> 00:00:20,000
Far too late to ever be selected.
"""

_SAMPLE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,This is a complete sentence.
Dialogue: 0,0:00:02.10,0:00:04.00,Default,,0,0,0,,This one trails off without
Dialogue: 0,0:00:04.10,0:00:06.00,Default,,0,0,0,,punctuation and continues here.
Dialogue: 0,0:00:06.10,0:00:20.00,Default,,0,0,0,,Far too late to ever be selected.
"""


class TestSelectCoherentCutoff:
    def test_picks_last_sentence_complete_cue_within_limit(self):
        # cue1 ends at 2.0s and is sentence-complete; cue2 ends at 4.0s but
        # is a mid-sentence fragment ("...without") so it must be skipped
        # even though it fits within max_duration_s.
        plan = select_coherent_cutoff(_SAMPLE_SRT, max_duration_s=6.0, trailing_buffer_s=0.5)
        assert plan.cutoff_cue_index == 0
        assert plan.kept_cue_count == 1
        assert plan.last_kept_cue_end_s == 2.0
        assert plan.trim_seconds == 2.5

    def test_never_cuts_mid_sentence_even_when_a_later_fragment_fits(self):
        # cue3 ("...continues here.") completes cue2's sentence and ends at
        # 6.0s -- with a big enough limit, cue3 (not cue2) must be chosen.
        plan = select_coherent_cutoff(_SAMPLE_SRT, max_duration_s=10.0, trailing_buffer_s=0.5)
        assert plan.cutoff_cue_index == 2
        assert plan.last_kept_cue_end_s == 6.0

    def test_em_dash_continuation_is_never_treated_as_complete(self):
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\nThis is complete.\n\n"
            "2\n00:00:02,100 --> 00:00:04,000\nThis dangles --\n"
        )
        plan = select_coherent_cutoff(srt, max_duration_s=5.0, trailing_buffer_s=0.1)
        assert plan.cutoff_cue_index == 0
        assert plan.last_kept_cue_end_s == 2.0

    def test_raises_when_no_cue_completes_a_sentence_within_the_limit(self):
        srt = "1\n00:00:00,000 --> 00:00:02,000\nThis dangles without end\n"
        with pytest.raises(NoCoherentCutoffError):
            select_coherent_cutoff(srt, max_duration_s=10.0, trailing_buffer_s=0.5)

    def test_raises_on_empty_caption_track(self):
        with pytest.raises(NoCoherentCutoffError):
            select_coherent_cutoff("", max_duration_s=60.0)

    def test_never_selects_a_cue_that_would_exceed_max_duration_with_buffer(self):
        plan = select_coherent_cutoff(_SAMPLE_SRT, max_duration_s=100.0, trailing_buffer_s=0.5)
        assert plan.trim_seconds <= 100.0
        assert plan.last_kept_cue_end_s == 20.0  # the far-too-late cue is now reachable and complete


class TestBuildTrimmedAssText:
    def test_keeps_only_the_requested_cue_count(self):
        trimmed = build_trimmed_ass_text(_SAMPLE_ASS, keep_cue_count=1)
        assert trimmed.count("Dialogue:") == 1
        assert "This is a complete sentence." in trimmed
        assert "This one trails off" not in trimmed

    def test_preserves_the_header_and_style_sections_untouched(self):
        trimmed = build_trimmed_ass_text(_SAMPLE_ASS, keep_cue_count=2)
        assert "[Script Info]" in trimmed
        assert "[V4+ Styles]" in trimmed
        assert "Style: Default,Arial" in trimmed

    def test_raises_when_keep_cue_count_exceeds_available_cues(self):
        with pytest.raises(ValueError):
            build_trimmed_ass_text(_SAMPLE_ASS, keep_cue_count=99)

    def test_raises_on_zero_or_negative_keep_cue_count(self):
        with pytest.raises(ValueError):
            build_trimmed_ass_text(_SAMPLE_ASS, keep_cue_count=0)


class TestBuildRepairFfmpegCommand:
    def test_uses_the_given_binary_never_a_hardcoded_default(self, tmp_path):
        cmd = build_repair_ffmpeg_command(
            tmp_path / "in.mp4", tmp_path / "cap.ass", tmp_path / "out.mp4", 58.5,
            ffmpeg_binary="/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        )
        assert cmd[0] == "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"

    def test_includes_trim_duration_and_subtitles_filter(self, tmp_path):
        cmd = build_repair_ffmpeg_command(
            tmp_path / "in.mp4", tmp_path / "cap.ass", tmp_path / "out.mp4", 58.5, ffmpeg_binary="ffmpeg",
        )
        assert "-t" in cmd
        assert cmd[cmd.index("-t") + 1] == "58.500"
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("subtitles=filename=")
        assert str(tmp_path / "out.mp4") == cmd[-1]

    def test_never_starts_a_subprocess_itself(self, tmp_path, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("build_repair_ffmpeg_command() must never execute anything")
        monkeypatch.setattr("subprocess.run", _boom)
        build_repair_ffmpeg_command(tmp_path / "in.mp4", tmp_path / "cap.ass", tmp_path / "out.mp4", 10.0, ffmpeg_binary="ffmpeg")


# ===========================================================================
# validate_repair_preconditions / build_proposed_update_body
# ===========================================================================

def _ok_kwargs(**overrides):
    kwargs = dict(
        title="A Real Title", content="A real caption.", duration_s=58.0, max_duration_s=60.0,
        burned_caption_status="pass", visual_coverage_status="pass", thumbnail_status="pass",
    )
    kwargs.update(overrides)
    return kwargs


class TestValidateRepairPreconditions:
    def test_passes_when_every_precondition_is_satisfied(self):
        assert validate_repair_preconditions(**_ok_kwargs()) == []

    def test_blocks_on_empty_title(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(title=""))
        assert any("title" in r for r in reasons)

    def test_blocks_on_empty_content(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(content="   "))
        assert any("content" in r for r in reasons)

    def test_blocks_on_duration_exceeding_max(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(duration_s=61.0))
        assert any("duration" in r for r in reasons)

    def test_blocks_on_missing_duration(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(duration_s=None))
        assert any("duration" in r for r in reasons)

    def test_blocks_on_failed_burned_caption_check(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(burned_caption_status="fail"))
        assert any("burned-caption" in r for r in reasons)

    def test_blocks_on_failed_visual_coverage_check(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(visual_coverage_status="fail"))
        assert any("visual coverage" in r for r in reasons)

    def test_blocks_on_missing_or_invalid_thumbnail(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(thumbnail_status="fail"))
        assert any("thumbnail" in r for r in reasons)

    def test_reports_every_blocking_reason_at_once_not_just_the_first(self):
        reasons = validate_repair_preconditions(**_ok_kwargs(title="", content="", thumbnail_status="fail"))
        assert len(reasons) >= 3


_EXISTING_POST = {
    "_id": "6a89752c7a56b107295b2b2c",
    "scheduledFor": "2026-08-24T18:00:00.000Z",
    "timezone": "UTC",
    "platforms": [{"platform": "youtube", "accountId": "acct_youtube_existing"}],
}
_ACCOUNTS = {
    "youtube": {"external_account_id": "acct_youtube"},
    "tiktok": {"external_account_id": "acct_tiktok"},
    "instagram": {"external_account_id": "acct_instagram"},
    "facebook": {"external_account_id": "acct_facebook"},
}


class TestBuildProposedUpdateBody:
    def test_preserves_existing_post_schedule_verbatim(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS,
            media_url=None, thumbnail_url=None,
        )
        assert body["scheduledFor"] == _EXISTING_POST["scheduledFor"]
        assert body["timezone"] == _EXISTING_POST["timezone"]

    def test_flags_media_staging_required_when_no_url_supplied(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS,
            media_url=None, thumbnail_url=None,
        )
        assert body["media_staging_required"] is True
        assert "url" not in body["mediaItems"][0]

    def test_no_staging_flag_and_url_present_when_media_url_supplied(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS,
            media_url="https://media.zernio.com/repaired.mp4", thumbnail_url="https://media.zernio.com/thumb.jpg",
        )
        assert "media_staging_required" not in body
        assert body["mediaItems"][0]["url"] == "https://media.zernio.com/repaired.mp4"
        assert body["mediaItems"][0]["thumbnail"] == "https://media.zernio.com/thumb.jpg"

    def test_contains_all_four_target_platforms(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS,
            media_url=None, thumbnail_url=None,
        )
        assert {p["platform"] for p in body["platforms"]} == set(ALL_PLATFORMS)

    def test_title_and_content_are_non_empty(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="A Real Title", content="A real caption.",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS, media_url=None, thumbnail_url=None,
        )
        assert body["title"].strip() != ""
        assert body["content"].strip() != ""

    def test_tiktok_settings_only_attached_when_tiktok_is_targeted(self):
        body = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=["youtube", "instagram", "facebook"], accounts=_ACCOUNTS,
            media_url=None, thumbnail_url=None, tiktok_settings={"privacy_level": "PUBLIC_TO_EVERYONE"},
        )
        assert "tiktokSettings" not in body

        body_with_tiktok = build_proposed_update_body(
            existing_post=_EXISTING_POST, title="T", content="C",
            target_platforms=ALL_PLATFORMS, accounts=_ACCOUNTS,
            media_url=None, thumbnail_url=None, tiktok_settings={"privacy_level": "PUBLIC_TO_EVERYONE"},
        )
        assert body_with_tiktok["tiktokSettings"] == {"privacy_level": "PUBLIC_TO_EVERYONE"}

    def test_existing_platform_entry_not_in_target_platforms_is_preserved(self):
        existing = dict(_EXISTING_POST, platforms=[
            {"platform": "youtube", "accountId": "acct_youtube_existing"},
            {"platform": "email", "accountId": "acct_email_unrelated"},
        ])
        body = build_proposed_update_body(
            existing_post=existing, title="T", content="C",
            target_platforms=["youtube"], accounts=_ACCOUNTS, media_url=None, thumbnail_url=None,
        )
        platforms_sent = {p["platform"] for p in body["platforms"]}
        assert "email" in platforms_sent


# ===========================================================================
# ZernioProvider.repair_existing_post() -- integration through the real method
# ===========================================================================

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "repair_test.db"
    initialize_schema(p)
    return p


class FakeRepairTransport:
    """Records every call by TYPE -- proves repair never issues a POST."""

    def __init__(self, get_response=None, put_response=None, put_error=None):
        self.post_calls = []
        self.get_calls = []
        self.put_calls = []
        self.put_urls = []
        self._get_response = get_response
        self._put_response = put_response
        self._put_error = put_error

    def post(self, *, url, headers, json_body, timeout):
        self.post_calls.append(json_body)
        return ZernioTransportResponse(status_code=201, json_body={"message": "created", "post": {"_id": "should_never_happen", "platforms": []}})

    def get(self, *, url, headers, timeout):
        self.get_calls.append(url)
        return self._get_response

    def put(self, *, url, headers, json_body, timeout):
        self.put_calls.append(json_body)
        self.put_urls.append(url)
        if self._put_error is not None:
            raise self._put_error
        return self._put_response


def _bind_all(db_path, *, channel_id=CHANNEL_ID):
    create_secret_profile(
        provider="zernio", label="k", credential_type="api_key",
        credential_data="sk_test_never_real", scope="factory", db_path=db_path,
    )
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
    for platform in ALL_PLATFORMS:
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform=platform, external_account_id=f"acct_{platform}",
            provider_profile_id=profile["provider_profile_id"], status="active",
        )
        bind_channel_provider(
            db_path, channel_id=channel_id, capability="publishing", platform=platform,
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=account["provider_account_id"],
        )


def _existing_post_get_response(*, post_id="post_short_001", platforms=("youtube",), platform_specific_data=None):
    psd_by_platform = platform_specific_data or {}
    entries = []
    for p in platforms:
        entry = {"platform": p, "accountId": f"acct_{p}", "status": "pending"}
        if p in psd_by_platform:
            entry["platformSpecificData"] = dict(psd_by_platform[p])
        entries.append(entry)
    return ZernioTransportResponse(
        status_code=200,
        json_body={"post": {
            "_id": post_id,
            "scheduledFor": "2026-08-24T18:00:00.000Z",
            "timezone": "UTC",
            "platforms": entries,
        }},
    )


def _successful_put_response(*, post_id="post_short_001", platforms=ALL_PLATFORMS):
    return ZernioTransportResponse(
        status_code=200,
        json_body={"message": "updated", "post": {
            "_id": post_id,
            "platforms": [
                {"platform": p, "platformPostId": f"native_{p}", "status": "published", "errorMessage": None}
                for p in platforms
            ],
        }},
    )


def _repair(provider, db_path, *, platforms=ALL_PLATFORMS, title="A Real Short Title", content="A real caption.",
            post_id="post_short_001", deliverable_id="short_001",
            platform_specific_data=None, root_fields=None):
    return provider.repair_existing_post(
        post_id, channel_id=CHANNEL_ID, deliverable_id=deliverable_id, episode_id=EPISODE_ID,
        deliverable_type="short", platforms=list(platforms),
        media=[MediaReference(url="https://media.zernio.com/repaired.mp4", media_type="video", title=title)],
        title=title, content=content, db_path=db_path,
        platform_specific_data=platform_specific_data, root_fields=root_fields,
    )


class TestRepairExistingPostUpdatesNotCreates:
    def test_never_calls_post(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path)
        assert transport.post_calls == []

    def test_exactly_one_get_and_one_put(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path)
        assert len(transport.get_calls) == 1
        assert len(transport.put_calls) == 1

    def test_get_and_put_target_the_same_existing_post_id(self, db_path):
        _bind_all(db_path)
        post_id = "6a89752c7a56b107295b2b2c"
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(post_id=post_id),
            put_response=_successful_put_response(post_id=post_id),
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path, post_id=post_id)
        assert transport.get_calls[0].endswith(post_id)
        assert transport.put_urls[0].endswith(post_id)
        for r in results.values():
            assert r["provider_content_id"] == post_id

    def test_put_body_preserves_existing_schedule(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path)
        put_body = transport.put_calls[0]
        assert put_body["scheduledFor"] == "2026-08-24T18:00:00.000Z"
        assert put_body["timezone"] == "UTC"

    def test_put_body_contains_all_four_platforms(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path)
        sent = {p["platform"] for p in transport.put_calls[0]["platforms"]}
        assert sent == set(ALL_PLATFORMS)

    def test_title_and_content_non_empty_in_put_body(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path)
        put_body = transport.put_calls[0]
        assert put_body["title"].strip() != ""
        assert put_body["content"].strip() != ""


class TestRepairExistingPostCorrectsPlatformSpecificData:
    """
    Fixed 2026-08-23 (Zernio YouTube field mapping incident, see
    docs/operations/audits/ZERNIO_YOUTUBE_FIELD_MAPPING_2026-08-23.md):
    repair_existing_post() previously preserved every existing
    platforms[] entry byte-for-byte, which made it structurally
    impossible to ever correct a wrong/blank platformSpecificData
    (categoryId, tags, firstComment, etc.) on a post whose platform
    entries already exist -- exactly the situation for an already-
    created Short post. It now performs a scoped merge, proven here.
    """

    def test_platform_specific_data_is_applied_to_an_existing_entry(self, db_path):
        # youtube already has an entry on the post (no platformSpecificData
        # at all) -- proves new data reaches an EXISTING entry, not just
        # a freshly-appended one.
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(platforms=("youtube",)),
            put_response=_successful_put_response(platforms=["youtube"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path, platforms=["youtube"],
            platform_specific_data={"youtube": {"categoryId": "27", "firstComment": "Real first comment"}},
        )
        put_body = transport.put_calls[0]
        youtube_entries = [p for p in put_body["platforms"] if p["platform"] == "youtube"]
        assert len(youtube_entries) == 1
        assert youtube_entries[0]["platformSpecificData"] == {
            "categoryId": "27", "firstComment": "Real first comment",
        }

    def test_merge_preserves_existing_platform_specific_data_keys_not_mentioned(self, db_path):
        # youtube's existing entry already carries a real, previously-set
        # field (visibility) -- the merge must NOT drop it just because
        # this repair call only supplies categoryId.
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(
                platforms=("youtube",),
                platform_specific_data={"youtube": {"visibility": "private"}},
            ),
            put_response=_successful_put_response(platforms=["youtube"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path, platforms=["youtube"],
            platform_specific_data={"youtube": {"categoryId": "27"}},
        )
        put_body = transport.put_calls[0]
        youtube_entry = next(p for p in put_body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"] == {"visibility": "private", "categoryId": "27"}

    def test_new_value_wins_on_key_collision(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(
                platforms=("youtube",),
                platform_specific_data={"youtube": {"categoryId": "22"}},  # stale/default
            ),
            put_response=_successful_put_response(platforms=["youtube"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path, platforms=["youtube"],
            platform_specific_data={"youtube": {"categoryId": "27"}},
        )
        put_body = transport.put_calls[0]
        youtube_entry = next(p for p in put_body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"]["categoryId"] == "27"

    def test_platform_not_mentioned_in_platform_specific_data_is_untouched(self, db_path):
        # tiktok's existing entry (with its own real settings) must be
        # byte-for-byte unaffected when platform_specific_data only
        # targets youtube.
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(
                platforms=("youtube", "tiktok"),
                platform_specific_data={"tiktok": {"unrelated_field": "unchanged"}},
            ),
            put_response=_successful_put_response(platforms=["youtube", "tiktok"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path, platforms=["youtube", "tiktok"],
            platform_specific_data={"youtube": {"categoryId": "27"}},
        )
        put_body = transport.put_calls[0]
        tiktok_entry = next(p for p in put_body["platforms"] if p["platform"] == "tiktok")
        assert tiktok_entry["platformSpecificData"] == {"unrelated_field": "unchanged"}

    def test_missing_platform_still_gets_platform_specific_data_on_its_new_entry(self, db_path):
        # instagram has NO existing entry -- platform_specific_data for
        # it must still reach the freshly-appended entry (the original,
        # already-correct append path).
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(platforms=("youtube",)),
            put_response=_successful_put_response(platforms=["youtube", "instagram"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path, platforms=["youtube", "instagram"],
            platform_specific_data={"instagram": {"aiGenerated": True}},
        )
        put_body = transport.put_calls[0]
        instagram_entry = next(p for p in put_body["platforms"] if p["platform"] == "instagram")
        assert instagram_entry["platformSpecificData"] == {"aiGenerated": True}

    def test_root_fields_tags_and_hashtags_reach_the_put_body(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        _repair(
            provider, db_path,
            root_fields={"tags": ["erdstall", "medieval tunnels"], "hashtags": ["#Erdstall"]},
        )
        put_body = transport.put_calls[0]
        assert put_body["tags"] == ["erdstall", "medieval tunnels"]
        assert put_body["hashtags"] == ["#Erdstall"]

    def test_no_platform_specific_data_leaves_existing_entries_exactly_as_before(self, db_path):
        # Regression guard: omitting platform_specific_data entirely must
        # reproduce the pre-fix byte-for-byte preservation exactly.
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(
                platforms=("youtube",),
                platform_specific_data={"youtube": {"categoryId": "22"}},
            ),
            put_response=_successful_put_response(platforms=["youtube"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path, platforms=["youtube"])
        put_body = transport.put_calls[0]
        youtube_entry = next(p for p in put_body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"] == {"categoryId": "22"}


class TestRepairFailsClosedBeforeAnyCall:
    def test_empty_title_causes_zero_external_calls(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(get_response=_existing_post_get_response(), put_response=_successful_put_response())
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ValueError):
            _repair(provider, db_path, title="")
        assert transport.get_calls == []
        assert transport.put_calls == []
        assert transport.post_calls == []

    def test_empty_content_causes_zero_external_calls(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(get_response=_existing_post_get_response(), put_response=_successful_put_response())
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ValueError):
            _repair(provider, db_path, content="   ")
        assert transport.get_calls == []
        assert transport.put_calls == []

    def test_no_platforms_causes_zero_external_calls(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(get_response=_existing_post_get_response(), put_response=_successful_put_response())
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ValueError):
            _repair(provider, db_path, platforms=[])
        assert transport.get_calls == []
        assert transport.put_calls == []

    def test_blocked_guard_raises_before_any_network_call(self, db_path):
        _bind_all(db_path)
        # Put youtube's latest attempt into 'ambiguous' -- guard_and_prepare_attempt()
        # must then report 'blocked' for it, and repair_existing_post() must
        # refuse the WHOLE call before touching the network at all.
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_ambiguous(db_path, guard.attempt_id, error_category="transport_error", error_message="simulated timeout")

        transport = FakeRepairTransport(get_response=_existing_post_get_response(), put_response=_successful_put_response())
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioAmbiguousRetryBlockedError):
            _repair(provider, db_path)
        assert transport.get_calls == []
        assert transport.put_calls == []


class TestRepairAmbiguousResponsesStayFailClosed:
    def test_malformed_get_response_stays_fail_closed_and_never_puts(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=ZernioTransportResponse(status_code=200, json_body={"post": {"_id": "x"}}),  # no platforms[]
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path)
        assert transport.put_calls == []
        assert all(r["status"] == "ambiguous" for r in results.values())

    def test_undocumented_put_status_stays_ambiguous_never_published(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
            put_response=ZernioTransportResponse(status_code=207, json_body={}),  # documented status, undocumented body
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path)
        for r in results.values():
            assert r["status"] != "published"

    def test_put_transport_error_leaves_every_platform_ambiguous(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(),
        )
        from hypogeum.publishing.zernio import ZernioTransportError
        transport._put_error = ZernioTransportError("connection reset")
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path)
        assert all(r["status"] == "ambiguous" for r in results.values())


class TestRepairAlreadySucceededPlatform:
    def test_already_succeeded_platform_gets_content_corrected_without_new_attempt(self, db_path):
        _bind_all(db_path)
        # Simulate youtube already succeeded from the original (broken) publish.
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="post_short_001", provider_status="pending")

        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM publishing_attempts WHERE episode_id=? AND deliverable_id=? AND platform='youtube'",
                (EPISODE_ID, "short_001"),
            ).fetchone()["n"]
        finally:
            conn.close()
        assert before == 1

        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(post_id="post_short_001", platforms=["youtube"]),
            put_response=_successful_put_response(post_id="post_short_001"),
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path, post_id="post_short_001")

        assert results["youtube"]["status"] == "content_corrected"
        assert results["youtube"]["provider_content_id"] == "post_short_001"

        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM publishing_attempts WHERE episode_id=? AND deliverable_id=? AND platform='youtube'",
                (EPISODE_ID, "short_001"),
            ).fetchone()["n"]
            intent_status = conn.execute(
                "SELECT status FROM publication_intents WHERE episode_id=? AND deliverable_id=? AND platform='youtube'",
                (EPISODE_ID, "short_001"),
            ).fetchone()["status"]
        finally:
            conn.close()
        assert after == 1  # no new attempt row created for the already-succeeded platform
        assert intent_status == "succeeded"  # untouched by the content-only correction

    def test_still_adds_new_platforms_alongside_the_already_succeeded_one(self, db_path):
        _bind_all(db_path)
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="post_short_001", provider_status="pending")

        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(post_id="post_short_001", platforms=["youtube"]),
            put_response=_successful_put_response(post_id="post_short_001"),
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path, post_id="post_short_001")

        for platform in ("tiktok", "instagram", "facebook"):
            assert results[platform]["status"] == "published"
            assert results[platform]["provider_content_id"] == "post_short_001"


# ===========================================================================
# 2026-08-23 hardening: existing platform entries (incl. undocumented
# provider-internal fields) must survive a repair PUT byte-for-byte.
# ===========================================================================

_RICH_EXISTING_YOUTUBE_ENTRY = {
    "platform": "youtube",
    "accountId": {"_id": "6a83555477555aae01afa569", "platform": "youtube", "displayName": "The Hypogeum"},
    "profileId": "6a8353affd55232edf6841c9",
    "customMedia": [],
    "scheduledFor": "2026-08-24T18:00:00.000Z",
    "platformSpecificData": {
        "__platformUserIdSnapshot": "UC6lbORr6RBYDPObPk80yGcQ",
        "__usernameSnapshot": "thehypogeum",
    },
    "status": "pending",
    "publishAttempts": 0,
    "contentHash": "81780859187c104e6c154025f042f973",
    "_id": "6a89752c7a56b107295b2b2e",
}


def _rich_existing_post_get_response(*, post_id="post_short_001"):
    return ZernioTransportResponse(
        status_code=200,
        json_body={"post": {
            "_id": post_id,
            "scheduledFor": "2026-08-24T18:00:00.000Z",
            "timezone": "UTC",
            "platforms": [dict(_RICH_EXISTING_YOUTUBE_ENTRY)],
        }},
    )


# 2026-08-23 (item 6, PUT editable-field whitelist -- see
# docs/operations/audits/ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md):
# confirmed via a real diagnostic PUT against the live API that GET's
# expanded accountId object is rejected by PUT ("Invalid input: expected
# string, received object", param="platforms.0.accountId") -- direct
# evidence Zernio's update endpoint performs strict schema validation,
# not silent tolerance of arbitrary echoed fields. repair_existing_post()
# now reduces every existing entry to ONLY its documented-editable fields
# (platform, accountId normalized to a plain string, customMedia,
# scheduledFor, platformSpecificData) via
# ZernioProvider._whitelist_platform_entry_for_put() -- response-only/
# provider-internal fields (profileId, status, publishAttempts,
# contentHash, provider _id) are dropped, never echoed back. This is
# what a repair PUT actually sends for _RICH_EXISTING_YOUTUBE_ENTRY.
_RICH_EXISTING_YOUTUBE_ENTRY_AS_SENT = {
    "platform": "youtube",
    "accountId": "6a83555477555aae01afa569",
    "customMedia": [],
    "scheduledFor": "2026-08-24T18:00:00.000Z",
    "platformSpecificData": {
        "__platformUserIdSnapshot": "UC6lbORr6RBYDPObPk80yGcQ",
        "__usernameSnapshot": "thehypogeum",
    },
}


class TestRepairWhitelistsExistingPlatformEntries:
    def test_existing_entry_is_reduced_to_the_editable_field_whitelist(self, db_path):
        _bind_all(db_path)
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="post_short_001", provider_status="pending")

        transport = FakeRepairTransport(
            get_response=_rich_existing_post_get_response(),
            put_response=_successful_put_response(post_id="post_short_001"),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path, post_id="post_short_001")

        sent_platforms = transport.put_calls[0]["platforms"]
        youtube_entries = [e for e in sent_platforms if e.get("platform") == "youtube"]
        assert len(youtube_entries) == 1, "must never duplicate an existing platform entry"
        assert youtube_entries[0] == _RICH_EXISTING_YOUTUBE_ENTRY_AS_SENT, (
            "an existing platform entry must be reduced to the documented-"
            "editable whitelist (platform/accountId/customMedia/scheduledFor/"
            "platformSpecificData); response-only fields must never be echoed back"
        )
        for response_only_field in ("profileId", "status", "publishAttempts", "contentHash", "_id"):
            assert response_only_field not in youtube_entries[0], (
                f"{response_only_field!r} is response-only/provider-internal and "
                f"must never be echoed back in a PUT body"
            )

    def test_only_genuinely_missing_platforms_get_a_freshly_built_entry(self, db_path):
        _bind_all(db_path)
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="post_short_001", provider_status="pending")

        transport = FakeRepairTransport(
            get_response=_rich_existing_post_get_response(),
            put_response=_successful_put_response(post_id="post_short_001"),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path, post_id="post_short_001")

        sent_platforms = transport.put_calls[0]["platforms"]
        by_platform = {e["platform"]: e for e in sent_platforms}
        assert set(by_platform) == set(ALL_PLATFORMS)
        # youtube keeps its documented-editable fields -- never reduced
        # further than the whitelist, and never carrying response-only fields.
        assert "platformSpecificData" in by_platform["youtube"]
        assert "contentHash" not in by_platform["youtube"]
        # the three genuinely new platforms get the minimal freshly built shape
        for platform in ("tiktok", "instagram", "facebook"):
            assert set(by_platform[platform].keys()) == {"platform", "accountId"}

    def test_repaired_post_id_is_identical_and_no_post_call_occurs(self, db_path):
        _bind_all(db_path)
        guard = guard_and_prepare_attempt(
            db_path, channel_id=CHANNEL_ID, episode_id=EPISODE_ID,
            deliverable_id="short_001", platform="youtube", provider_id="zernio",
        )
        record_success(db_path, guard.attempt_id, provider_publication_id="post_short_001", provider_status="pending")

        transport = FakeRepairTransport(
            get_response=_rich_existing_post_get_response(),
            put_response=_successful_put_response(post_id="post_short_001"),
        )
        provider = ZernioProvider(transport=transport)
        results = _repair(provider, db_path, post_id="post_short_001")

        assert transport.post_calls == []
        assert transport.get_calls[0].endswith("post_short_001")
        assert transport.put_urls[0].endswith("post_short_001")
        for platform in ALL_PLATFORMS:
            assert results[platform]["provider_content_id"] == "post_short_001"

    def test_undocumented_existing_entry_survives_even_when_that_platform_is_not_retargeted(self, db_path):
        # youtube is on the existing post but this repair call only targets
        # tiktok/instagram/facebook -- youtube's rich entry must still be
        # carried through untouched, never dropped.
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_rich_existing_post_get_response(),
            put_response=_successful_put_response(post_id="post_short_001", platforms=["tiktok", "instagram", "facebook"]),
        )
        provider = ZernioProvider(transport=transport)
        _repair(provider, db_path, post_id="post_short_001", platforms=["tiktok", "instagram", "facebook"])

        sent_platforms = transport.put_calls[0]["platforms"]
        youtube_entries = [e for e in sent_platforms if e.get("platform") == "youtube"]
        assert youtube_entries == [_RICH_EXISTING_YOUTUBE_ENTRY_AS_SENT]


class TestRepairFacebookPlatformSpecificData:
    """
    2026-08-23: Facebook's required contentType='reel' marker belongs in
    platforms[].platformSpecificData for the NEW facebook entry -- confirmed
    live via docs.zernio.com/platforms/facebook, distinct from the
    unrelated root-level facebookSettings object (draft/carouselCards/
    textFormatPresetId). Never applied to tiktok (which uses the existing
    root-level tiktokSettings path instead).
    """

    def test_facebook_gets_platform_specific_data_on_the_new_entry(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(platforms=["youtube"]),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        provider.repair_existing_post(
            "post_short_001", channel_id=CHANNEL_ID, deliverable_id="short_001", episode_id=EPISODE_ID,
            deliverable_type="short", platforms=["youtube", "facebook"],
            media=[MediaReference(url="https://media.zernio.com/repaired.mp4", media_type="video", title="T")],
            title="A Real Short Title", content="A real caption.",
            platform_settings={"facebook": {"contentType": "reel"}},
            db_path=db_path,
        )
        sent = transport.put_calls[0]["platforms"]
        fb_entry = next(p for p in sent if p["platform"] == "facebook")
        assert fb_entry["platformSpecificData"] == {"contentType": "reel"}
        assert "platformSpecificData" not in transport.put_calls[0].get("facebookSettings", {})

    def test_tiktok_settings_never_leak_into_platform_specific_data(self, db_path):
        _bind_all(db_path)
        transport = FakeRepairTransport(
            get_response=_existing_post_get_response(platforms=["youtube"]),
            put_response=_successful_put_response(),
        )
        provider = ZernioProvider(transport=transport)
        provider.repair_existing_post(
            "post_short_001", channel_id=CHANNEL_ID, deliverable_id="short_001", episode_id=EPISODE_ID,
            deliverable_type="short", platforms=["youtube", "tiktok"],
            media=[MediaReference(url="https://media.zernio.com/repaired.mp4", media_type="video", title="T")],
            title="A Real Short Title", content="A real caption.",
            platform_settings={"tiktok": {"privacy_level": "PUBLIC_TO_EVERYONE"}},
            db_path=db_path,
        )
        put_body = transport.put_calls[0]
        assert put_body["tiktokSettings"] == {"privacy_level": "PUBLIC_TO_EVERYONE"}
        tiktok_entry = next(p for p in put_body["platforms"] if p["platform"] == "tiktok")
        assert "platformSpecificData" not in tiktok_entry
