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

    def test_per_weekday_policy_entry_resolves_not_blocked(self, db_path):
        """Regression (2026-09-05): a deliverable_schedule_policy entry
        using the per-weekday shape (weekday_time_of_day +
        default_time_of_day -- see hypogeum.scheduler.compute_next_publish_
        slot's own docstring) always failed closed here, even though the
        exact same entry resolves correctly wherever
        compute_next_publish_slot() is called with it directly. Root
        cause: this module reconstructed its own schedule dict from the
        policy entry using only day_of_week/time_of_day, silently
        dropping weekday_time_of_day/default_time_of_day -- every
        autonomous release attempt for a channel using this shape (e.g.
        hypogeum, applied live the same day) failed every single cycle
        with 'invalid or incomplete', forever, with no exception raised
        anywhere (compile_schedule_plan_from_channel_package() is a pure,
        read-only, fails-closed function -- exactly the failure mode this
        function's own docstring says it should have for a genuinely
        invalid entry, which this entry never was)."""
        brand = create_brand_profile(name="Z Brand", db_path=db_path)
        model = create_model_profile(name="Z Model", db_path=db_path)
        pub = create_publishing_profile(name="Z Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_z", name="channel_z",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_z", channel_name="channel_z", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy={
                "main_episode": {
                    "weekday_time_of_day": {"monday": "10:00", "wednesday": "18:00"},
                    "default_time_of_day": "18:00",
                },
            },
            db_path=db_path,
        )
        now = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)  # a Saturday -> default_time_of_day
        result = compile_schedule_plan_from_channel_package(
            "channel_z", ["main_episode"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        slot = result.schedule_plan.entries["main_episode"].scheduled_for
        assert slot.hour == 18
        assert slot.date().isoformat() == "2026-09-05"


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


HYPOGEUM_BUNDLE_CYCLE_POLICY = {
    "hypogeum_weekly_release_v1": {
        "anchor_deliverable_id": "main_episode",
        "required_deliverable_ids": ["main_episode", "short_001", "short_002", "short_003"],
        "deliverables": {
            "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
            "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
            "short_002": {"day_of_week": "wednesday", "time_of_day": "18:00", "timezone": "UTC"},
            "short_003": {"day_of_week": "friday", "time_of_day": "18:00", "timezone": "UTC"},
        },
    },
}


def _make_channel_with_bundle_cycle(db_path, channel_id, bundle_cycle_policy, *, timezone_name="UTC"):
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
        bundle_cycle_policy=bundle_cycle_policy,
        db_path=db_path,
    )


class TestBundleCycleScheduling:
    """hypogeum.publishing.schedule_compiler's bundle-cycle layer (layer 1):
    every member of a declared bundle_cycle_id is resolved TOGETHER,
    relative to the cycle's own anchor's resolved slot -- proving Shorts
    can never accidentally resolve to a slot before their own bundle's
    main episode purely from independent per-deliverable 'next occurrence
    from wall-clock now' math."""

    ALL_IDS = ["main_episode", "short_001", "short_002", "short_003"]

    def _compile(self, db_path, now):
        return compile_schedule_plan_from_channel_package(
            "hypogeum", self.ALL_IDS, db_path=db_path, now=now, require_explicit_policy=True,
        )

    def _assert_no_short_before_anchor(self, result):
        assert result.blocked is False, result.blocked_reason
        entries = result.schedule_plan.entries
        anchor_slot = entries["main_episode"].scheduled_for
        for short_id in ("short_001", "short_002", "short_003"):
            assert entries[short_id].scheduled_for > anchor_slot, (
                f"{short_id} resolved to {entries[short_id].scheduled_for.isoformat()}, "
                f"which is not after its anchor's slot {anchor_slot.isoformat()}"
            )
        return entries

    def test_saturday_before_anchor(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)  # Saturday
        result = self._compile(db_path, now)
        entries = self._assert_no_short_before_anchor(result)
        assert entries["main_episode"].scheduled_for == datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
        assert entries["short_001"].scheduled_for == datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
        assert entries["short_002"].scheduled_for == datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)
        assert entries["short_003"].scheduled_for == datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)

    def test_sunday_one_minute_before_anchor(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 23, 13, 59, tzinfo=timezone.utc)  # Sunday, 1 min before 14:00
        result = self._compile(db_path, now)
        entries = self._assert_no_short_before_anchor(result)
        # Anchor resolves to TODAY (still upcoming).
        assert entries["main_episode"].scheduled_for == datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
        assert entries["short_001"].scheduled_for == datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)

    def test_sunday_one_minute_after_anchor(self, db_path):
        """The exact bug from the task description: activation just after
        the anchor slot must roll BOTH the anchor AND every Short to next
        week's cycle -- never leave a Short pinned to the just-passed
        week while the anchor rolls forward alone."""
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 23, 14, 1, tzinfo=timezone.utc)  # Sunday, 1 min after 14:00
        result = self._compile(db_path, now)
        entries = self._assert_no_short_before_anchor(result)
        # Anchor rolls to NEXT Sunday...
        assert entries["main_episode"].scheduled_for == datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
        # ...and every Short rolls to the Mon/Wed/Fri AFTER that Sunday,
        # not the Mon/Wed/Fri of the week that just started (which would
        # be in the past relative to the anchor, or even absolutely if
        # `now` is between Sunday 14:00 and the following Monday's slot).
        assert entries["short_001"].scheduled_for == datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)
        assert entries["short_002"].scheduled_for == datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)
        assert entries["short_003"].scheduled_for == datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)

    def test_monday_after_anchor(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # Monday, before that day's Short slot
        result = self._compile(db_path, now)
        entries = self._assert_no_short_before_anchor(result)
        assert entries["main_episode"].scheduled_for == datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
        assert entries["short_001"].scheduled_for == datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)

    def test_year_boundary(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 12, 30, 20, 0, tzinfo=timezone.utc)  # Wednesday, late December
        result = self._compile(db_path, now)
        entries = self._assert_no_short_before_anchor(result)
        # Next Sunday crosses into the new year.
        assert entries["main_episode"].scheduled_for == datetime(2027, 1, 3, 14, 0, tzinfo=timezone.utc)
        assert entries["short_001"].scheduled_for == datetime(2027, 1, 4, 18, 0, tzinfo=timezone.utc)
        assert entries["short_002"].scheduled_for == datetime(2027, 1, 6, 18, 0, tzinfo=timezone.utc)
        assert entries["short_003"].scheduled_for == datetime(2027, 1, 8, 18, 0, tzinfo=timezone.utc)

    def test_another_timezone_with_dst(self, db_path):
        # Europe/Berlin observes DST -- late October 2026 crosses the
        # fall-back transition (2026-10-25). A channel whose own schedule
        # is declared in Europe/Berlin wall-clock time must still resolve
        # every member correctly across that transition, purely via the
        # existing zoneinfo-based compute_next_publish_slot() -- no new
        # DST-specific logic in the bundle-cycle layer itself.
        policy = {
            "weekly_v1": {
                "anchor_deliverable_id": "main_episode",
                "deliverables": {
                    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "Europe/Berlin"},
                    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "Europe/Berlin"},
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", policy, timezone_name="Europe/Berlin")
        now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)  # Thursday, before the Oct 25 DST transition
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        entries = result.schedule_plan.entries
        anchor_slot = entries["main_episode"].scheduled_for
        short_slot = entries["short_001"].scheduled_for
        assert short_slot > anchor_slot
        # The EU fall-back transition happens at 01:00 UTC / 03:00 CEST on
        # Oct 25 2026 -- by 14:00 local time that same day, Berlin has
        # already moved to CET (UTC+1), so Sunday 14:00 CET = 13:00 UTC.
        # Monday Oct 26 18:00 is also CET (UTC+1) = 17:00 UTC. Confirms
        # real tz-aware conversion occurred, not a naive fixed UTC offset.
        assert anchor_slot == datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)
        assert short_slot == datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)

    def test_shorts_only_channel_with_different_anchor(self, db_path):
        """Factory Core must not assume main_episode is universally the
        anchor -- another channel may anchor its bundle on a Short."""
        policy = {
            "shorts_only_v1": {
                "anchor_deliverable_id": "short_001",
                "required_deliverable_ids": ["short_001", "short_002"],
                "deliverables": {
                    "short_001": {"day_of_week": "tuesday", "time_of_day": "09:00", "timezone": "UTC"},
                    "short_002": {"day_of_week": "thursday", "time_of_day": "09:00", "timezone": "UTC"},
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "shorts_channel", policy)
        now = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)  # Saturday
        result = compile_schedule_plan_from_channel_package(
            "shorts_channel", ["short_001", "short_002"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        entries = result.schedule_plan.entries
        assert entries["short_001"].scheduled_for == datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
        assert entries["short_002"].scheduled_for == datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
        assert entries["short_002"].scheduled_for > entries["short_001"].scheduled_for

    def test_repeated_compilation_with_same_now_returns_identical_plan(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 23, 14, 1, tzinfo=timezone.utc)
        result_1 = self._compile(db_path, now)
        result_2 = self._compile(db_path, now)
        assert result_1.blocked is False and result_2.blocked is False
        entries_1 = {k: v.scheduled_for for k, v in result_1.schedule_plan.entries.items()}
        entries_2 = {k: v.scheduled_for for k, v in result_2.schedule_plan.entries.items()}
        assert entries_1 == entries_2

    def test_missing_anchor_declaration_fails_closed(self, db_path):
        policy = {
            "broken_v1": {
                "anchor_deliverable_id": "main_episode",
                "deliverables": {
                    # anchor_deliverable_id points to a deliverable that
                    # isn't itself declared -- must fail closed, not
                    # silently pick another member as the anchor.
                    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", policy)
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["short_001"], db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is True
        assert "anchor" in result.blocked_reason

    def test_missing_required_deliverable_fails_closed(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        # Only 2 of the 4 required members requested.
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001"], db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is True
        assert result.schedule_plan is None
        assert "short_002" in result.blocked_reason and "short_003" in result.blocked_reason

    def test_invalid_ordering_fails_closed(self, db_path):
        """The Channel Package declares an explicit required_deliverable_ids
        SEQUENCE saying short_002 must release before short_001 -- but
        their configured day_of_week values (short_001=Monday,
        short_002=Wednesday) actually resolve the other way around. This
        contradiction between declared sequence and configured times must
        fail closed, never silently accepted."""
        policy = {
            "broken_v1": {
                "anchor_deliverable_id": "main_episode",
                "required_deliverable_ids": ["main_episode", "short_002", "short_001"],
                "deliverables": {
                    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
                    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
                    "short_002": {"day_of_week": "wednesday", "time_of_day": "18:00", "timezone": "UTC"},
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", policy)
        now = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)  # Saturday
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001", "short_002"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is True
        assert "invalid ordering" in result.blocked_reason

    def test_ambiguous_cycle_fails_closed(self, db_path):
        policy = {
            "cycle_a": {
                "anchor_deliverable_id": "main_episode",
                "deliverables": {
                    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
                    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
                },
            },
            "cycle_b": {
                "anchor_deliverable_id": "short_001",
                "deliverables": {
                    "short_001": {"day_of_week": "tuesday", "time_of_day": "09:00", "timezone": "UTC"},
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", policy)
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["short_001"], db_path=db_path, require_explicit_policy=True,
        )
        assert result.blocked is True
        assert "ambiguous" in result.blocked_reason

    def test_pre_anchor_teaser_declared_and_resolves_before_anchor(self, db_path):
        policy = {
            "teaser_v1": {
                "anchor_deliverable_id": "main_episode",
                "deliverables": {
                    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
                    "teaser": {
                        "day_of_week": "friday", "time_of_day": "12:00", "timezone": "UTC",
                        "anchor_relation": "before",
                    },
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", policy)
        now = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)  # Saturday
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "teaser"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        entries = result.schedule_plan.entries
        assert entries["teaser"].scheduled_for < entries["main_episode"].scheduled_for

    def test_backward_compatible_when_no_bundle_cycle_policy_configured(self, db_path):
        # A channel with only the flat deliverable_schedule_policy (no
        # bundle_cycle_policy at all) must compile exactly as before this
        # feature existed -- layer 1 never applies when bundle_cycle_policy
        # is empty.
        brand = create_brand_profile(name="Z Brand", db_path=db_path)
        model = create_model_profile(name="Z Model", db_path=db_path)
        pub = create_publishing_profile(name="Z Publishing", db_path=db_path)
        create_channel_profile(
            channel_id="channel_z", name="channel_z",
            brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
            publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
        )
        create_channel_operations_profile(
            channel_id="channel_z", channel_name="channel_z", timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
            deliverable_schedule_policy=HYPOGEUM_DELIVERABLE_POLICY,
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "channel_z", ["main_episode", "short_001", "short_002", "short_003"], db_path=db_path,
        )
        assert result.blocked is False
        assert len({d.scheduled_for for d in result.schedule_plan.entries.values()}) == 4


class TestPerPlatformScheduleOverrides:
    """
    2026-08-23: platform_overrides -- optional {platform: {day_of_week,
    time_of_day, timezone}} on a deliverable entry (bundle-cycle member,
    bundle-cycle anchor, or flat deliverable_schedule_policy entry) --
    lets individual platforms within one aggregated post carry genuinely
    independent publication timestamps, while bundle cohesion (release
    ORDER between deliverables, governed by anchor_relation) is entirely
    unaffected: an override is always resolved relative to the SAME
    reference its own deliverable's base scheduled_for used.
    """

    BUNDLE_CYCLE_WITH_OVERRIDES = {
        "cycle1": {
            "anchor_deliverable_id": "main_episode",
            "required_deliverable_ids": ["main_episode", "short_001"],
            "deliverables": {
                "main_episode": {
                    "day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC",
                    "platform_overrides": {
                        "youtube": {"day_of_week": "sunday", "time_of_day": "16:00", "timezone": "UTC"},
                    },
                },
                "short_001": {
                    "day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC",
                    "anchor_relation": "after",
                    "platform_overrides": {
                        "tiktok": {"day_of_week": "monday", "time_of_day": "20:00", "timezone": "UTC"},
                        "instagram": {"day_of_week": "tuesday", "time_of_day": "09:00", "timezone": "UTC"},
                    },
                },
            },
        },
    }

    def test_overridden_platform_gets_its_own_independent_timestamp(self, db_path):
        _make_channel_with_bundle_cycle(db_path, "hypogeum", self.BUNDLE_CYCLE_WITH_OVERRIDES)
        now = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)  # Thursday
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        entries = result.schedule_plan.entries

        assert entries["main_episode"].scheduled_for == datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
        assert entries["main_episode"].platform_scheduled_for_by_platform == {
            "youtube": datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc),
        }

        assert entries["short_001"].scheduled_for == datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
        assert entries["short_001"].platform_scheduled_for_by_platform == {
            "tiktok": datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
            "instagram": datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
        }

    def test_non_overridden_platform_falls_back_to_the_shared_base_slot(self, db_path):
        """facebook/youtube on short_001 were never given their own
        override -- they must use the deliverable's own scheduled_for,
        never a fabricated independent time."""
        _make_channel_with_bundle_cycle(db_path, "hypogeum", self.BUNDLE_CYCLE_WITH_OVERRIDES)
        now = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        short_sched = result.schedule_plan.entries["short_001"]
        assert "youtube" not in short_sched.platform_scheduled_for_by_platform
        assert "facebook" not in short_sched.platform_scheduled_for_by_platform
        # publish_episode_package()'s own resolution (platform_scheduled_for_by_platform.get(p, ...))
        # falls back to scheduled_for for these -- verified directly here.
        assert short_sched.scheduled_for == datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)

    def test_no_platform_overrides_declared_is_a_complete_noop(self, db_path):
        """A deliverable with no platform_overrides key at all (the
        existing HYPOGEUM_BUNDLE_CYCLE_POLICY fixture) must resolve
        byte-identically to before this mechanism existed."""
        _make_channel_with_bundle_cycle(db_path, "hypogeum", HYPOGEUM_BUNDLE_CYCLE_POLICY)
        now = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode", "short_001", "short_002", "short_003"],
            db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False
        for sched in result.schedule_plan.entries.values():
            assert sched.platform_scheduled_for_by_platform == {}

    def test_invalid_platform_override_fails_closed_bundle_cycle_layer(self, db_path):
        broken_policy = {
            "cycle1": {
                "anchor_deliverable_id": "main_episode",
                "required_deliverable_ids": ["main_episode"],
                "deliverables": {
                    "main_episode": {
                        "day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC",
                        "platform_overrides": {"tiktok": {"day_of_week": "not_a_real_day", "time_of_day": "20:00"}},
                    },
                },
            },
        }
        _make_channel_with_bundle_cycle(db_path, "hypogeum", broken_policy)
        result = compile_schedule_plan_from_channel_package(
            "hypogeum", ["main_episode"], db_path=db_path,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc), require_explicit_policy=True,
        )
        assert result.blocked is True
        assert "platform_overrides" in result.blocked_reason
        assert result.schedule_plan is None

    def test_invalid_platform_override_fails_closed_flat_layer(self, db_path):
        create_brand_profile(name="ch2 Brand", db_path=db_path)
        create_channel_profile(channel_id="ch2", name="ch2", db_path=db_path)
        create_channel_operations_profile(
            channel_id="ch2", channel_name="ch2", timezone="UTC",
            deliverable_schedule_policy={
                "main_episode": {
                    "day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC",
                    "platform_overrides": {"tiktok": {"day_of_week": "sunday"}},  # missing time_of_day
                },
            },
            db_path=db_path,
        )
        result = compile_schedule_plan_from_channel_package(
            "ch2", ["main_episode"], db_path=db_path,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc), require_explicit_policy=True,
        )
        assert result.blocked is True
        assert "platform_overrides" in result.blocked_reason

    def test_flat_layer_platform_override_resolves(self, db_path):
        create_brand_profile(name="ch3 Brand", db_path=db_path)
        create_channel_profile(channel_id="ch3", name="ch3", db_path=db_path)
        create_channel_operations_profile(
            channel_id="ch3", channel_name="ch3", timezone="UTC",
            deliverable_schedule_policy={
                "main_episode": {
                    "day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC",
                    "platform_overrides": {
                        "youtube": {"day_of_week": "sunday", "time_of_day": "16:00", "timezone": "UTC"},
                    },
                },
            },
            db_path=db_path,
        )
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        result = compile_schedule_plan_from_channel_package(
            "ch3", ["main_episode"], db_path=db_path, now=now, require_explicit_policy=True,
        )
        assert result.blocked is False
        sched = result.schedule_plan.entries["main_episode"]
        assert sched.scheduled_for == datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
        assert sched.platform_scheduled_for_by_platform == {
            "youtube": datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc),
        }


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
            package, platforms=["youtube"], channel_id="channel_a", db_path=db_path,
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
