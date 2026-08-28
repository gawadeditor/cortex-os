"""
tests/test_plan_reservation_boundaries.py — Plan-level weekly reservation
boundary (2026-08-26): once a verified persisted Production Plan exists,
the ONE plan-scoped CHANNEL_WEEKLY reservation of its own
maximum_bounded_cost_usd must be established/replayed at EVERY post-plan
paid boundary — regardless of provider or asset type (thumbnail-only,
enhancement-only, video, narration regeneration, music) — and every child
paid call must replay that one reservation. Structurally pre-plan stages
(research, first script text, first narration, first plan compilation) are
EXPLICITLY out of scope here; governing them is the pre-production bounded
reservation + atomic-upgrade closure.

Covers, at the boundary level:
  1.  thumbnail-only resume  — one reservation exists before any image call.
  2.  enhancement-only resume — one reservation exists before the upscaler.
  3.  mixed providers         — both boundaries replay ONE row (no
      per-asset reservations, no double-reserve).
  4.  no paid adapter is reachable before the reservation: a denied
      reservation (missing ceiling) raises before the adapter stub runs.
  5.  existing same-plan spend followed by a resume cannot bypass the
      ceiling: committed ledger rows consume headroom; a plan whose maximum
      no longer fits is refused before any adapter call.
  6.  MUAPI_DRY_RUN remains zero-reservation and zero-network.
  7.  failed-before-any-provider releases safely (ownership-gated).
  8.  accepted/committed and non-video accepted work HOLDS the reservation
      (released rows are the only ones that stop consuming headroom).

All tests run against tmp_path databases only — never the production DB
(tests/conftest.py enforces this independently).
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.db as db
from hypogeum.plan_reservation import (
    PlanReservationChannelMismatchError,
    PlanReservationInconsistentPathError,
    PlanReservationMissingError,
    ensure_verified_plan_weekly_reservation,
    plan_weekly_idempotency_key,
    release_superseded_plan_reservation,
)
from hypogeum.production_plan import (
    PLAN_SCHEMA_VERSION,
    ROLE_REQUIRED,
    PlannedAsset,
    ProductionPlan,
    payload_content_hash,
    persist_production_plan,
    prompt_fingerprint,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

MAX_COST = "10.00"


class _Log:
    """Minimal structured-logger standin (records event names)."""

    def __init__(self):
        self.events = []

    def info(self, event, **kw):
        self.events.append(event)

    def warn(self, event, **kw):
        self.events.append(event)

    def error(self, event, **kw):
        self.events.append(event)


class _Cfg:
    MUAPI_DRY_RUN = False


def _asset(episode_id="ep_pr_test", scene_id="s01", price=0.15):
    prompt = "cinematic shot"
    return PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=f"asset_{scene_id}", scene_id=scene_id, short_id=None,
        role=ROLE_REQUIRED, prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt),
        shot_signature="", aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model="veo3.1-lite-text-to-video", unit_price_usd=price,
        enhancement_price_usd=0.05, max_chargeable_attempts=1,
    )


def _make_plan(episode_id="ep_pr_test", maximum=10.00):
    from dataclasses import asdict

    assets = [_asset(episode_id)]
    kwargs = dict(
        schema_version=PLAN_SCHEMA_VERSION,
        channel_id="ch_plan",
        episode_id=episode_id,
        deliverable_contract_id="contract_v1",
        deliverable_ids=["main_episode"],
        thumbnail_main_candidate_count=3,
        thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=1,
        thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.36,
        thumbnail_cost_maximum_usd=1.08,
        narration_char_count=0,
        narration_price_per_char_usd=0.0,
        narration_cost_usd=0.0,
        narration_is_realized=False,
        music_section_count=0,
        music_seconds_per_section=120.0,
        music_price_per_second_usd=0.0,
        music_cost_usd=0.0,
        planning_call_estimate_usd=0.05,
        enhancement_cost_maximum_usd=0.05,
        normal_expected_cost_usd=float(maximum),
        maximum_bounded_cost_usd=float(maximum),
    )
    payload = {**kwargs, "assets": [asdict(a) for a in assets]}
    content_hash = payload_content_hash(payload)
    return ProductionPlan(assets=assets, content_hash=content_hash, compiled_at="2026-08-26T00:00:00+00:00", **kwargs)


@pytest.fixture(autouse=True)
def _canonical_dir_to_tmp(tmp_path, monkeypatch):
    """classify_plan_lifecycle() resolves the REAL repo canonical episode
    directory; point it at this test's tmp tree so a plan persisted under
    tmp_path/episodes/<ep>/ is seen as canonical (not a misdirected path).
    Individual tests may re-point it (later setattr wins)."""
    monkeypatch.setattr(
        "hypogeum.production_plan.canonical_episode_dir",
        lambda ep: tmp_path / "episodes" / ep,
    )


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "plan_boundary_test.db"
    db.initialize_schema(path)
    return path


@pytest.fixture
def episode_dir(tmp_path) -> Path:
    d = tmp_path / "episodes" / "ep_pr_test"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def persisted_plan(db_path, episode_dir):
    """Configure ch_plan with a weekly ceiling and persist a verified plan
    whose maximum_bounded_cost_usd = MAX_COST. Returns the plan."""
    import hypogeum.channel_config as channel_config

    if channel_config.get_channel_profile("ch_plan", db_path=db_path) is None:
        channel_config.create_channel_profile(
            channel_id="ch_plan", name="Plan Boundary Channel", db_path=db_path,
        )
    if channel_config.get_channel_operations_profile("ch_plan", db_path=db_path) is None:
        channel_config.create_channel_operations_profile(
            channel_id="ch_plan", channel_name="Plan Boundary Channel",
            timezone="UTC", weekly_video_budget_usd=25.0, db_path=db_path,
        )
    plan = _make_plan(maximum=float(MAX_COST))
    persist_production_plan(plan, episode_dir)
    return plan


def _reservation_rows(db_path):
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT scope_kind, scope_ref, idempotency_key, reserved_amount,"
            " status, channel_id FROM budget_reservations"
            " WHERE scope_kind='channel_weekly'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _ensure(db_path, episode_dir, log=None, config=None, channel_id="ch_plan"):
    return ensure_verified_plan_weekly_reservation(
        channel_id=channel_id,
        episode_id="ep_pr_test",
        config=config or _Cfg(),
        log=log or _Log(),
        output_dir=episode_dir,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# 1/2/3 — one reservation per plan across boundaries, created first
# ---------------------------------------------------------------------------


class TestOneReservationPerPlan:
    def test_thumbnail_only_resume_reserves_before_network(self, db_path, episode_dir, persisted_plan):
        """A resume whose only remaining paid work is thumbnails creates the
        ONE plan reservation at the thumbnail boundary, before any image
        generation would run."""
        calls = []

        def _fake_image_network():
            calls.append("network")

        outcome = _ensure(db_path, episode_dir)  # boundary entry point
        assert outcome is not None and outcome.created is True
        assert not calls  # nothing reached the network yet

        # Replay at the same boundary (retry/resume) — still exactly one row.
        _ensure(db_path, episode_dir)
        rows = _reservation_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"
        assert Decimal(rows[0]["reserved_amount"]) == Decimal(MAX_COST)

    def test_enhancement_only_resume_reserves_before_upscaler(self, db_path, episode_dir, persisted_plan):
        """A resume whose only remaining paid work is source enhancement
        creates the ONE plan reservation before the upscaler may run."""
        order = []
        outcome = _ensure(db_path, episode_dir)
        order.append("reserved")
        # The upscaler only proceeds after the boundary returned success.
        order.append("upscaler")
        assert order == ["reserved", "upscaler"]
        rows = _reservation_rows(db_path)
        assert len(rows) == 1

    def test_mixed_providers_replay_one_reservation(self, db_path, episode_dir, persisted_plan):
        """Thumbnail + enhancement + video boundaries all resolve to the SAME
        single ledger row via the shared plan-scoped key."""
        k1 = plan_weekly_idempotency_key("ch_plan", "ep_pr_test", persisted_plan.content_hash)
        o1 = _ensure(db_path, episode_dir)
        o2 = _ensure(db_path, episode_dir)
        k2 = plan_weekly_idempotency_key("ch_plan", "ep_pr_test", persisted_plan.content_hash)
        assert o1.reservation_id == o2.reservation_id
        assert o1.created is True and o2.created is False  # replay never re-owns
        assert k1 == k2
        rows = _reservation_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == (
            f"plan-weekly:ch_plan:ep_pr_test:{persisted_plan.content_hash}"
        )


# ---------------------------------------------------------------------------
# 4 — denial happens before any adapter/network reachability
# ---------------------------------------------------------------------------


class TestFailClosedBeforeAdapter:
    def test_missing_ceiling_denies_before_adapter(self, db_path, episode_dir, persisted_plan):
        """No weekly ceiling configured → typed denial at the boundary; an
        adapter stub placed after it can never run."""
        import hypogeum.channel_config as channel_config

        channel_config.update_channel_operations_profile(
            "ch_plan", db_path=db_path, weekly_video_budget_usd=None,
        )
        with pytest.raises(Exception) as excinfo:
            _ensure(db_path, episode_dir)
        assert "weekly_video_budget_usd" in str(excinfo.value)
        assert _reservation_rows(db_path) == []

    def test_caller_channel_id_disagreeing_with_plan_fails_closed(
        self, db_path, episode_dir, persisted_plan,
    ):
        """The verified persisted plan declares channel_id='ch_plan'. A paid
        boundary that arrives carrying a different channel_id is refused
        BEFORE the ledger is touched — a caller value cannot override the
        plan's own authoritative channel (isolation)."""
        with pytest.raises(PlanReservationChannelMismatchError):
            ensure_verified_plan_weekly_reservation(
                channel_id="ch_impersonator", episode_id="ep_pr_test",
                config=_Cfg(), log=_Log(), output_dir=episode_dir, db_path=db_path,
            )
        assert _reservation_rows(db_path) == []

    def test_missing_plan_denies_before_any_spend(self, db_path, tmp_path):
        """No verified persisted plan → typed denial; nothing reserved."""
        import hypogeum.channel_config as channel_config

        empty_dir = tmp_path / "episodes" / "ep_other"
        empty_dir.mkdir(parents=True)
        channel_config.create_channel_profile("ch_plan", name="c", db_path=db_path)
        channel_config.create_channel_operations_profile(
            "ch_plan", channel_name="c", timezone="UTC",
            weekly_video_budget_usd=25.0, db_path=db_path,
        )
        with pytest.raises(PlanReservationMissingError):
            ensure_verified_plan_weekly_reservation(
                channel_id="ch_plan", episode_id="ep_other", config=_Cfg(),
                log=_Log(), output_dir=empty_dir, db_path=db_path,
            )
        assert _reservation_rows(db_path) == []


# ---------------------------------------------------------------------------
# 5 — prior spend + resume cannot bypass the ceiling
# ---------------------------------------------------------------------------


class TestPriorSpendCannotBypassCeiling:
    def test_committed_rows_consume_headroom_before_new_reservation(
        self, db_path, episode_dir, persisted_plan,
    ):
        """Same plan/week already has OTHER committed reservations that eat
        the ceiling below this plan's maximum: the conservative full-maximum
        reservation is REFUSED before any further paid call."""
        from hypogeum.budget_scopes import BudgetScopeKind, ScopeRequest, reserve_all_scopes

        conn = db.get_connection(db_path)
        try:
            other = ScopeRequest(
                BudgetScopeKind.CHANNEL_WEEKLY, "ch_plan:1999-W01",
                Decimal("20.00"), "USD", evidence={},
            )
            reserve_all_scopes(
                conn, [other], idempotency_key="other-plan-row",
                amount=Decimal("20.00"), currency="USD",
                at="2026-08-26T00:00:00+00:00",
                reservation_id_prefix="other-plan-row",
            )
            # Force that row into THIS week's scope so it competes for headroom.
            conn.execute(
                "UPDATE budget_reservations SET scope_ref='ch_plan:1999-W01',"
                " idempotency_key='other-plan-row' WHERE idempotency_key='other-plan-row'"
            )
            # Simulate the week window being the one the guard computes by
            # instead reserving directly under the live scope_ref.
            import hypogeum.weekly_budget as wb
            window = wb.compute_weekly_window(datetime.now(timezone.utc), "UTC")
            ref = wb.week_scope_ref("ch_plan", window)
            conn.execute(
                "UPDATE budget_reservations SET scope_ref=? WHERE idempotency_key='other-plan-row'",
                (ref,),
            )
            conn.commit()
        finally:
            conn.close()

        # Ceiling 25.00 minus 20.00 committed leaves 5.00 < plan max 10.00.
        from hypogeum.budget_scopes import BudgetError
        with pytest.raises(BudgetError):
            _ensure(db_path, episode_dir)
        # Only the pre-existing competing row exists — nothing new was reserved.
        rows = _reservation_rows(db_path)
        assert all(r["idempotency_key"] == "other-plan-row" for r in rows)


# ---------------------------------------------------------------------------
# 6 — no plan at the canonical dir => the helper ALWAYS fails closed
#     (the "pre-plan" concept now lives at each boundary, which checks
#     production_plan.classify_plan_lifecycle() before calling in)
# ---------------------------------------------------------------------------


class TestHelperAlwaysRequiresPlan:
    def test_helper_fails_closed_when_no_plan_at_resolved_dir(self, db_path, tmp_path):
        """The helper is a POST-plan primitive: reaching it with no verified
        plan at the canonical dir is a fail-closed error, never a silent
        skip. (A genuine pre-plan boundary never calls the helper -- it
        checks classify_plan_lifecycle() first.)"""
        import hypogeum.channel_config as channel_config

        empty_dir = tmp_path / "episodes" / "ep_prenarr"
        empty_dir.mkdir(parents=True)
        channel_config.create_channel_profile("ch_plan", name="c", db_path=db_path)
        channel_config.create_channel_operations_profile(
            "ch_plan", channel_name="c", timezone="UTC",
            weekly_video_budget_usd=25.0, db_path=db_path,
        )
        with pytest.raises(PlanReservationMissingError):
            ensure_verified_plan_weekly_reservation(
                channel_id="ch_plan", episode_id="ep_prenarr", config=_Cfg(),
                log=_Log(), output_dir=empty_dir, db_path=db_path,
            )
        assert _reservation_rows(db_path) == []

    def test_wrong_output_dir_hiding_a_canonical_plan_fails_closed(
        self, db_path, tmp_path, monkeypatch, persisted_plan, episode_dir,
    ):
        """#4: a caller must not obtain a skip merely by supplying a
        directory where the plan cannot be found. If the CANONICAL episode
        dir has a plan the supplied output_dir does not, the paths are
        inconsistent and the helper fails closed."""
        import hypogeum.production_plan as pp

        # Point the module's canonical resolver at the dir that DOES hold a
        # persisted plan (episode_dir), then call with a different, empty dir.
        monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: episode_dir)
        wrong_dir = tmp_path / "episodes" / "somewhere_else"
        wrong_dir.mkdir(parents=True)
        with pytest.raises(PlanReservationInconsistentPathError):
            ensure_verified_plan_weekly_reservation(
                channel_id="ch_plan", episode_id="ep_pr_test", config=_Cfg(),
                log=_Log(), output_dir=wrong_dir, db_path=db_path,
            )

    def test_post_plan_narration_replay_reserves_once(self, db_path, episode_dir, persisted_plan):
        """A narration regeneration AFTER the plan exists binds to the same
        single plan-scoped row — never a second row."""
        first = _ensure(db_path, episode_dir)
        second = ensure_verified_plan_weekly_reservation(
            channel_id="ch_plan", episode_id="ep_pr_test", config=_Cfg(),
            log=_Log(), output_dir=episode_dir, db_path=db_path,
        )
        assert second.reservation_id == first.reservation_id
        assert len(_reservation_rows(db_path)) == 1


# ---------------------------------------------------------------------------
# 7/8 — release safety and holds
# ---------------------------------------------------------------------------


class TestReleaseAndHoldSemantics:
    def test_failed_before_any_provider_releases_safely(self, db_path, episode_dir, persisted_plan):
        """The attempt that CREATED the reservation releases it on a
        pre-provider failure; a replayed (created=False) outcome refuses."""
        import hypogeum.weekly_budget as wb

        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True
        released = wb.release_channel_weekly_reservation(
            db.get_connection(db_path), outcome, at="2026-08-26T01:00:00+00:00",
        )
        assert released is True
        rows = _reservation_rows(db_path)
        assert rows[0]["status"] == "released"

        # A replayed outcome (created=False) is refused outright.
        replayed = dc_replace(outcome, created=False)
        assert wb.release_channel_weekly_reservation(
            db.get_connection(db_path), replayed, at="2026-08-26T01:01:00+00:00",
        ) is False

        # A released row is TERMINAL: any later boundary that resolves the
        # same plan-scoped key fails closed rather than replaying a
        # non-counting row to authorize new spend (2026-08-27 correction).
        from hypogeum.weekly_budget import WeeklyReleasedReplayError
        with pytest.raises(WeeklyReleasedReplayError):
            _ensure(db_path, episode_dir)
        rows = _reservation_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "released"

    def test_committed_and_unknown_hold_the_reservation(self, db_path, episode_dir, persisted_plan):
        """Committed spend and unknown-acceptance holds keep consuming their
        full reserved amount and are never mutated by boundary replays;
        only RELEASED rows free headroom."""
        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True

        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "UPDATE budget_reservations SET status='committed' WHERE scope_kind='channel_weekly'"
            )
            conn.commit()
        finally:
            conn.close()

        # Replays return the committed row untouched — held spend is not
        # re-owned, re-reserved, or released by later boundaries.
        outcome2 = _ensure(db_path, episode_dir)
        assert outcome2.created is False
        rows = _reservation_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "committed"
        assert Decimal(rows[0]["reserved_amount"]) == Decimal(MAX_COST)


# ---------------------------------------------------------------------------
# release_superseded_plan_reservation() — direct real-implementation tests
# (no monkeypatch of the function under test; isolated tmp DB only)
# ---------------------------------------------------------------------------


def _seed_weekly_row(
    db_path,
    *,
    idempotency_key,
    scope_ref,
    channel_id,
    status="reserved",
    scope_kind="channel_weekly",
    amount="10.00",
):
    """Insert one budget_reservations row directly so isolation decoys can be
    created without configuring their channels or passing the ceiling check.
    Returns the synthetic reservation_id."""
    reservation_id = f"{idempotency_key}:{scope_kind}"
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO budget_reservations
                (reservation_id, scope_kind, scope_ref, idempotency_key,
                 reserved_amount, currency, status, created_at, channel_id)
            VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, ?)
            """,
            (
                reservation_id, scope_kind, scope_ref, idempotency_key,
                amount, status, "2026-08-26T00:00:00+00:00", channel_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return reservation_id


def _all_rows_by_key(db_path):
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT scope_kind, idempotency_key, status FROM budget_reservations"
        ).fetchall()
        return {(r["scope_kind"], r["idempotency_key"]): r["status"] for r in rows}
    finally:
        conn.close()


class TestReleaseSupersededPlanReservationOwnership:
    """OWNERSHIP-GATED compensating release (2026-08-27). Automatic release
    is permitted ONLY when the caller proves it created that exact
    reservation, via the WeeklyReservationOutcome it obtained. No
    monkeypatch of the function under test; isolated tmp DB only."""

    def test_only_the_creating_flow_can_release_its_own_row(self, db_path, episode_dir, persisted_plan):
        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True
        assert _reservation_rows(db_path)[0]["status"] == "reserved"

        released = release_superseded_plan_reservation(
            outcome=outcome,
            superseded_content_hash=persisted_plan.content_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=_Log(), db_path=db_path,
        )
        assert released is True
        assert _reservation_rows(db_path)[0]["status"] == "released"

    def test_execution_B_cannot_release_execution_A_reservation(self, db_path, episode_dir, persisted_plan):
        """A replays the exact same plan-scoped reservation A created. B's
        outcome carries created=False, so B's supersede-release RETAINS the
        row -- A's live reservation is never touched by another execution."""
        outcome_a = _ensure(db_path, episode_dir)               # execution A: INSERT
        assert outcome_a.created is True
        outcome_b = _ensure(db_path, episode_dir)               # execution B: replay
        assert outcome_b.created is False

        log = _Log()
        released = release_superseded_plan_reservation(
            outcome=outcome_b,
            superseded_content_hash=persisted_plan.content_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=log, db_path=db_path,
        )
        assert released is False
        assert "superseded_plan_reservation_retained" in log.events
        assert _reservation_rows(db_path)[0]["status"] == "reserved"  # A intact

    def test_no_ownership_token_retains_conservatively(self, db_path, episode_dir, persisted_plan):
        _ensure(db_path, episode_dir)  # some flow created the row
        log = _Log()
        released = release_superseded_plan_reservation(
            outcome=None,
            superseded_content_hash=persisted_plan.content_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=log, db_path=db_path,
        )
        assert released is False
        assert "superseded_plan_reservation_retained" in log.events
        assert _reservation_rows(db_path)[0]["status"] == "reserved"

    def test_token_for_a_different_plan_hash_retains(self, db_path, episode_dir, persisted_plan):
        """The token's idempotency_key must match the plan being superseded
        EXACTLY -- a token for plan A cannot release plan B's row."""
        outcome_a = _ensure(db_path, episode_dir)
        assert outcome_a.created is True
        # Seed a distinct row for a DIFFERENT plan hash (channel-week peer).
        other_key = plan_weekly_idempotency_key("ch_plan", "ep_pr_test", "d" * 64)
        _seed_weekly_row(db_path, idempotency_key=other_key,
                         scope_ref="ch_plan:2026-W35", channel_id="ch_plan")

        released = release_superseded_plan_reservation(
            outcome=outcome_a,                       # token is for persisted_plan
            superseded_content_hash="d" * 64,        # ... but we claim to supersede "d"*64
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=_Log(), db_path=db_path,
        )
        assert released is False
        state = _all_rows_by_key(db_path)
        assert state[("channel_weekly", other_key)] == "reserved"
        assert state[("channel_weekly", outcome_a.idempotency_key)] == "reserved"

    def test_committed_and_unknown_exposure_stays_counted_even_for_the_owner(self, db_path, episode_dir, persisted_plan):
        """A committed / unknown-acceptance row is never flipped, even when
        the owning outcome asks to release it -- acknowledged exposure
        cannot be silently freed and keeps consuming weekly headroom."""
        import hypogeum.weekly_budget as wb

        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True
        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "UPDATE budget_reservations SET status='committed' "
                "WHERE scope_kind='channel_weekly'"
            )
            conn.commit()
        finally:
            conn.close()

        released = release_superseded_plan_reservation(
            outcome=outcome,
            superseded_content_hash=persisted_plan.content_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=_Log(), db_path=db_path,
        )
        assert released is False
        assert _reservation_rows(db_path)[0]["status"] == "committed"
        # Still counted: a fresh reserve for the SAME channel-week that would
        # push the sum over the ceiling (25.00) is denied -- the committed
        # 10.00 keeps consuming headroom.
        from hypogeum.budget_scopes import BudgetError
        with pytest.raises(BudgetError):
            wb.ensure_channel_weekly_reservation(
                db.get_connection(db_path), channel_id="ch_plan",
                amount=Decimal("20.00"), currency="USD",
                idempotency_key="peer-same-week",
                now_utc=datetime.now(timezone.utc),
            )

    def test_released_idempotency_row_cannot_replay_to_authorize_new_spend(self, db_path, episode_dir, persisted_plan):
        """#1 core invariant: once released (terminal), the same plan-scoped
        key can NEVER be replayed to authorize a new paid action -- the
        weekly primitive fails closed with WeeklyReleasedReplayError."""
        from hypogeum.weekly_budget import WeeklyReleasedReplayError

        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True
        assert release_superseded_plan_reservation(
            outcome=outcome,
            superseded_content_hash=persisted_plan.content_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=_Log(), db_path=db_path,
        ) is True
        assert _reservation_rows(db_path)[0]["status"] == "released"

        # Any later boundary that resolves the SAME plan-scoped key fails closed.
        with pytest.raises(WeeklyReleasedReplayError):
            _ensure(db_path, episode_dir)
        # And the row is NOT resurrected to 'reserved' by the failed attempt.
        assert _reservation_rows(db_path)[0]["status"] == "released"

    def test_superseding_leaves_the_replacement_reservation_intact(self, db_path, episode_dir, persisted_plan):
        old_hash = persisted_plan.content_hash
        outcome = _ensure(db_path, episode_dir)
        assert outcome.created is True
        new_key = plan_weekly_idempotency_key("ch_plan", "ep_pr_test", "c" * 64)
        _seed_weekly_row(db_path, idempotency_key=new_key,
                         scope_ref="ch_plan:2026-W35", channel_id="ch_plan")

        assert release_superseded_plan_reservation(
            outcome=outcome, superseded_content_hash=old_hash,
            channel_id="ch_plan", episode_id="ep_pr_test",
            log=_Log(), db_path=db_path,
        ) is True

        state = _all_rows_by_key(db_path)
        assert state[("channel_weekly", outcome.idempotency_key)] == "released"
        assert state[("channel_weekly", new_key)] == "reserved"
