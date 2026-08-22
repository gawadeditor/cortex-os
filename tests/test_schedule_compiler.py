"""
tests/test_schedule_compiler.py — Compile a SchedulePlan from a channel's
authoritative Channel Package configuration.

Covers hypogeum.publishing.schedule_compiler.compile_schedule_plan_from_channel_package():
reads ChannelOperationsProfile.publishing_schedule/timezone only (never a
global default, never another channel's), applies the one real computed
slot to every requested deliverable_id, and fails closed when no profile
exists or the profile's schedule is missing/invalid. Proves two channels
with deliberately different cadences/timezones compile independently and
cannot leak into each other, and that the compiled plan flows end-to-end
through publish_episode_package() via fake transports.

All state uses an isolated tmp_path SQLite database — never the production
.hypogeum-assets/hypogeum.db. No network, no real Zernio call.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import initialize_schema
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import (
    PresignTransportResponse,
    UploadTransportResponse,
    ZernioMediaStagingProvider,
)
from hypogeum.publishing.zernio_publish_runtime import DeliverableFile, PublishPackage, publish_episode_package


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "schedule_compiler_test.db"
    initialize_schema(p)
    return p


def _make_channel(db_path, channel_id, *, timezone_name, day_of_week, time_of_day):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone=timezone_name,
        publishing_schedule={"day_of_week": day_of_week, "time_of_day": time_of_day},
        db_path=db_path,
    )


# Canonical deliverable_id convention: 'main_episode' for the long-form,
# 'short_<short_id>' for each Short -- matching
# hypogeum.agents.publishing_agent._deliverable_id_for()'s own,
# already-established convention (see hypogeum.publishing.deliverable_ids).
# 'short_001'/'short_002'/'short_003' are the REAL values verified against
# ep_f3619022's own sealed publishing_record_ep_f3619022.json and
# publish_calendar.short_id -- not an invented short_0/1/2 scheme.
HYPOGEUM_DELIVERABLE_POLICY = {
    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
    "short_002": {"day_of_week": "wednesday", "time_of_day": "18:00", "timezone": "UTC"},
    "short_003": {"day_of_week": "friday", "time_of_day": "18:00", "timezone": "UTC"},
}


class TestPerDeliverableSchedulePolicy:
    def test_matches_approved_hypogeum_policy_shape(self, db_path):
        brand = create_brand_profile(name="hypogeum Brand", db_path=db_path)
        model = create_model_profile(name="hypogeum Model", db_path=db_path)
        pub = create_publishing_profile(name="hypogeum Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="hypogeum", name="hypogeum",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="hypogeum", channel_name="hypogeum", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy=HYPOGEUM_DELIVERABLE_POLICY,
            db_path=db_path,
        )

        now = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)  # a Friday
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001", "short_002", "short_003"], db_path=db_path, now=now,
        )
        assert result.blocked is False
        entries = result.schedule_plan.entries
        assert entries["main_episode"].scheduled_for.weekday() == 6  # Sunday
        assert entries["main_episode"].scheduled_for.hour == 14
        assert entries["short_001"].scheduled_for.weekday() == 0  # Monday
        assert entries["short_002"].scheduled_for.weekday() == 2  # Wednesday
        assert entries["short_003"].scheduled_for.weekday() == 4  # Friday
        for d in entries.values():
            assert d.scheduled_for.hour in (14, 18)
        # Every deliverable's own slot is distinct -- not the single
        # fallback publishing_schedule (Thursday) applied uniformly.
        assert len({d.scheduled_for for d in entries.values()}) == 4

    def test_deliverable_not_covered_by_policy_falls_back_to_single_slot(self, db_path):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_x", name="channel_x",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_x", channel_name="channel_x", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy={"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}},
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "channel_x", ["main_episode", "bonus_deliverable"], db_path=db_path,
        )
        assert result.blocked is False
        assert result.schedule_plan.entries["main_episode"].scheduled_for.weekday() == 6
        assert result.schedule_plan.entries["bonus_deliverable"].scheduled_for.weekday() == 3  # Thursday fallback

    def test_invalid_policy_entry_fails_closed(self, db_path):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_x", name="channel_x",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_x", channel_name="channel_x", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy={"main_episode": {"day_of_week": "not_a_real_day", "time_of_day": "14:00"}},
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package("channel_x", ["main_episode"], db_path=db_path)
        assert result.blocked is True
        assert "invalid or incomplete" in result.blocked_reason

    def test_no_policy_configured_behaves_exactly_as_before(self, db_path):
        # Backward-compatibility proof: a channel with deliverable_schedule_policy
        # unset (the default {}) must produce the same single-slot-for-every-
        # deliverable behavior this module had before the field existed.
        brand = create_brand_profile(name="Y Brand", db_path=db_path)
        model = create_model_profile(name="Y Model", db_path=db_path)
        pub = create_publishing_profile(name="Y Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_y", name="channel_y",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_y", channel_name="channel_y", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "channel_y", ["main_episode", "short_0"], db_path=db_path,
        )
        assert result.blocked is False
        slots = {d.scheduled_for for d in result.schedule_plan.entries.values()}
        assert len(slots) == 1


class TestRequireExplicitPolicy:
    """The autonomous publishing path (require_explicit_policy=True) must
    never silently fall back to the channel's single legacy schedule slot
    for a deliverable missing from deliverable_schedule_policy."""

    def test_full_hypogeum_policy_compiles_with_four_distinct_slots(self, db_path):
        brand = create_brand_profile(name="hypogeum Brand", db_path=db_path)
        model = create_model_profile(name="hypogeum Model", db_path=db_path)
        pub = create_publishing_profile(name="hypogeum Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="hypogeum", name="hypogeum",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="hypogeum", channel_name="hypogeum", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy=HYPOGEUM_DELIVERABLE_POLICY,
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001", "short_002", "short_003"],
            db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is False
        slots = {d.scheduled_for for d in result.schedule_plan.entries.values()}
        assert len(slots) == 4, "the Channel Package declares 4 distinct release times -- all 4 must compile distinctly"

    def test_deliverable_missing_from_policy_blocks_before_staging(self, db_path):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_x", name="channel_x",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_x", channel_name="channel_x", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},  # legacy fallback exists...
            deliverable_schedule_policy={"main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"}},  # ...but short_001 has no entry
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "channel_x", ["main_episode", "short_001"], db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is True
        assert result.schedule_plan is None
        assert "short_001" in result.blocked_reason
        assert "never applied" in result.blocked_reason

    def test_extra_unrelated_policy_entries_do_not_affect_the_selected_bundle(self, db_path):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_x", name="channel_x",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_x", channel_name="channel_x", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy={
                "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00"},
                "some_other_bundle_deliverable": {"day_of_week": "tuesday", "time_of_day": "09:00"},
                "yet_another_unrelated_entry": {"day_of_week": "not_even_valid"},
            },
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "channel_x", ["main_episode"], db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is False
        assert set(result.schedule_plan.entries.keys()) == {"main_episode"}


class TestFailsClosed:
    def test_no_channel_operations_profile_blocks(self, db_path):
        result = compile_schedule_plan_from_channel_package(
            "channel_unconfigured", ["main_episode"], db_path=db_path,
        )
        assert result.blocked is True
        assert result.schedule_plan is None
        assert "no channel_operations_profile" in result.blocked_reason

    def test_profile_with_no_schedule_blocks(self, db_path):
        brand = create_brand_profile(name="X Brand", db_path=db_path)
        model = create_model_profile(name="X Model", db_path=db_path)
        pub = create_publishing_profile(name="X Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_x", name="channel_x",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_x", channel_name="channel_x", timezone="UTC",
            publishing_schedule={}, db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package("channel_x", ["main_episode"], db_path=db_path)
        assert result.blocked is True
        assert "refusing to invent a release time" in result.blocked_reason


class TestPerChannelCompilation:
    def test_covers_every_requested_deliverable_at_the_same_real_slot(self, db_path):
        _make_channel(db_path, "channel_a", timezone_name="Asia/Dubai", day_of_week="thursday", time_of_day="18:00")
        result = compile_schedule_plan_from_channel_package(
            "channel_a", ["main_episode", "short_0", "short_1", "short_2"], db_path=db_path,
        )
        assert result.blocked is False
        assert result.schedule_plan.channel_id == "channel_a"
        assert set(result.schedule_plan.entries.keys()) == {"main_episode", "short_0", "short_1", "short_2"}
        slots = {d.scheduled_for for d in result.schedule_plan.entries.values()}
        assert len(slots) == 1, "current schema has one recurring slot per channel — every deliverable shares it"
        assert result.slot.tzinfo is not None
        for d in result.schedule_plan.entries.values():
            assert d.timezone_name == "Asia/Dubai"

    def test_two_channels_with_different_cadence_compile_independently(self, db_path):
        _make_channel(db_path, "channel_a", timezone_name="Asia/Dubai", day_of_week="thursday", time_of_day="18:00")
        _make_channel(db_path, "channel_b", timezone_name="America/New_York", day_of_week="monday", time_of_day="09:00")

        now = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)  # a Thursday
        result_a = compile_schedule_plan_from_channel_package("channel_a", ["main_episode"], db_path=db_path, now=now)
        result_b = compile_schedule_plan_from_channel_package("channel_b", ["short_0"], db_path=db_path, now=now)

        assert result_a.blocked is False
        assert result_b.blocked is False
        assert result_a.schedule_plan.channel_id == "channel_a"
        assert result_b.schedule_plan.channel_id == "channel_b"
        # Different day/time/timezone configuration -> different computed slots.
        assert result_a.slot != result_b.slot
        assert result_a.schedule_plan.entries["main_episode"].timezone_name == "Asia/Dubai"
        assert result_b.schedule_plan.entries["short_0"].timezone_name == "America/New_York"
        # Neither plan's entries leak the other channel's deliverable_id.
        assert "short_0" not in result_a.schedule_plan.entries
        assert "main_episode" not in result_b.schedule_plan.entries

    def test_channel_b_missing_profile_does_not_fall_back_to_channel_as_schedule(self, db_path):
        _make_channel(db_path, "channel_a", timezone_name="Asia/Dubai", day_of_week="thursday", time_of_day="18:00")
        # channel_b has no channel_operations_profile at all.
        result_b = compile_schedule_plan_from_channel_package("channel_b", ["short_0"], db_path=db_path)
        assert result_b.blocked is True
        assert result_b.schedule_plan is None


class TestEndToEndThroughPublishEpisodePackage:
    def test_compiled_plan_flows_through_staging_and_publish_via_fakes(self, db_path, tmp_path):
        _make_channel(db_path, "channel_a", timezone_name="Asia/Dubai", day_of_week="thursday", time_of_day="18:00")

        create_secret_profile(
            provider="zernio", label="k", credential_type="api_key",
            credential_data="sk_test_never_real", scope="factory", db_path=db_path,
        )
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="acct_1",
            provider_profile_id=profile["provider_profile_id"], status="active",
        )
        bind_channel_provider(
            db_path, channel_id="channel_a", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=account["provider_account_id"],
        )

        main_file = tmp_path / "main_ep_sched01.mp4"
        main_file.write_bytes(b"main video bytes" * 500)
        package = PublishPackage(
            channel_id="channel_a", episode_id="ep_sched01", bundle_id="main_episode_bundle",
            main_episode=DeliverableFile(
                deliverable_id="main_episode", local_path=main_file, mime_type="video/mp4",
                media_type="video", filename=main_file.name,
            ),
        )

        result = compile_schedule_plan_from_channel_package("channel_a", ["main_episode"], db_path=db_path)
        assert result.blocked is False

        class FakeMediaTransport:
            def __init__(self):
                self.presign_calls = []

            def presign(self, *, url, headers, json_body, timeout):
                self.presign_calls.append(json_body)
                return PresignTransportResponse(
                    status_code=200,
                    json_body={"uploadUrl": "https://upload.example.com/1", "publicUrl": "https://cdn.example.com/1",
                               "key": "key_1", "expiresIn": 3600},
                )

            def upload(self, *, url, headers, data, timeout):
                return UploadTransportResponse(status_code=200)

        class FakePublishTransport:
            def __init__(self):
                self.post_calls = []

            def post(self, *, url, headers, json_body, timeout):
                self.post_calls.append(json_body)
                return ZernioTransportResponse(
                    status_code=201,
                    json_body={"message": "created", "post": {
                        "_id": "post_1",
                        "platforms": [{"platform": "youtube", "platformPostId": "native_1", "status": "published"}],
                    }},
                )

        media_transport = FakeMediaTransport()
        publish_transport = FakePublishTransport()

        outcome = publish_episode_package(
            package, platform="youtube", channel_id="channel_a", db_path=db_path,
            schedule_plan=result.schedule_plan,
            media_staging_provider=ZernioMediaStagingProvider(presign_transport=media_transport, upload_transport=media_transport),
            publishing_provider=ZernioProvider(transport=publish_transport),
        )

        assert outcome.blocked is False
        assert outcome.all_published is True
        # The real computed slot (not a fabricated/hardcoded value) reached
        # the actual publish request body.
        sent_scheduled_for = publish_transport.post_calls[0].get("scheduledFor")
        assert sent_scheduled_for is not None
        assert publish_transport.post_calls[0]["platforms"][0]["platform"] == "youtube"
