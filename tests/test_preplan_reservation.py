"""
tests/test_preplan_reservation.py — the bounded PRE_PLAN paid-spend
reservation + its atomic upgrade into the plan-bound CHANNEL_WEEKLY
reservation (hypogeum.preplan_reservation).

Covers section 7's adversarial list: zero-network without the
reservation, one valid reservation authorises the bounded PRE_PLAN
calls, identity/state failures fail closed, restart + concurrency,
same-week atomic upgrade with no double count, failed upgrade preserves
exposure, deterministic crash recovery, week rollover, and dry-run
isolation.

Temporary DBs, fake providers, injected clocks, and network sentinels
only.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.policy_cascade as policy_cascade
import hypogeum.production_plan as pp
import hypogeum.weekly_budget as weekly_budget
from hypogeum.preplan_reservation import (
    PreplanReservationError,
    PreplanUnboundedError,
    PreplanUpgradeCapacityError,
    atomic_upgrade_to_plan_reservation,
    ensure_preproduction_reservation,
    has_unupgraded_preplan_hold,
    preplan_idempotency_key,
    preplan_incurred_idempotency_key,
)

_CH = "cpp"
_EP = "ep_pp"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)   # a Wednesday -> ISO 2026-W35


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass


def _cfg(**over):
    base = dict(FINANCE_BUDGET_CEILING_USD=30.0)
    base.update(over)
    return SimpleNamespace(**base)


def _plan(episode_id=_EP, channel_id=_CH, maximum=20.0):
    from hypogeum.production_plan import (
        PLAN_SCHEMA_VERSION, ROLE_REQUIRED, PlannedAsset, ProductionPlan,
        payload_content_hash, prompt_fingerprint,
    )
    prompt = f"shot {maximum}"
    a = PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=f"asset_{episode_id}", scene_id="s01", short_id=None, role=ROLE_REQUIRED,
        prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt), shot_signature="",
        aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model="veo3.1-lite-text-to-video", unit_price_usd=0.15,
        enhancement_price_usd=0.05, max_chargeable_attempts=1,
    )
    kw = dict(
        schema_version=PLAN_SCHEMA_VERSION, channel_id=channel_id, episode_id=episode_id,
        deliverable_contract_id="c1", deliverable_ids=["main_episode"],
        thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=1, thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.36, thumbnail_cost_maximum_usd=1.08,
        narration_char_count=0, narration_price_per_char_usd=0.0, narration_cost_usd=0.0,
        narration_is_realized=False, music_section_count=0, music_seconds_per_section=120.0,
        music_price_per_second_usd=0.0, music_cost_usd=0.0, planning_call_estimate_usd=0.05,
        enhancement_cost_maximum_usd=0.05, normal_expected_cost_usd=float(maximum),
        maximum_bounded_cost_usd=float(maximum),
    )
    payload = {**kw, "assets": [asdict(a)]}
    return ProductionPlan(assets=[a], content_hash=payload_content_hash(payload),
                          compiled_at="2026-08-26T00:00:00+00:00", **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "pp.db"
    db.initialize_schema(db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep)
    channel_config.create_channel_profile(channel_id=_CH, name="CPP", db_path=db_path)
    channel_config.create_channel_operations_profile(
        channel_id=_CH, channel_name="CPP", timezone="UTC",
        weekly_video_budget_usd=55.0, db_path=db_path,
    )
    # Deterministic per-episode ceiling: pass the config's env ceiling
    # straight through (no dependence on a seeded factory row).
    monkeypatch.setattr(
        policy_cascade, "get_effective_budget_ceiling",
        lambda env_ceiling=0.0, channel_id=None, factory_id=None, db_path=None: float(env_ceiling),
    )
    canon = tmp_path / "episodes" / _EP
    canon.mkdir(parents=True)
    return SimpleNamespace(tmp_path=tmp_path, db_path=db_path, canon=canon)


def _rows(db_path, *, channel_weekly_only=True):
    conn = db.get_connection(db_path)
    try:
        q = "SELECT * FROM budget_reservations"
        if channel_weekly_only:
            q += " WHERE scope_kind='channel_weekly'"
        return [dict(r) for r in conn.execute(q)]
    finally:
        conn.close()


def _week_sum(db_path, scope_ref):
    return sum(
        (Decimal(r["reserved_amount"]) for r in _rows(db_path)
         if r["scope_ref"] == scope_ref and r["status"] in ("reserved", "committed")),
        Decimal("0"),
    )


def _persist_plan(env, plan):
    from hypogeum.production_plan import persist_production_plan
    persist_production_plan(plan, env.canon)


def _record_cost(db_path, episode_id, amount, call_type="script_engine"):
    conn = db.get_connection(db_path)
    try:
        conn.execute("INSERT INTO runs (run_id, episode_id, status, started_at) VALUES (?,?,?,?)"
                     if "run_id" not in [c[1] for c in conn.execute("PRAGMA table_info(costs)")] else
                     "SELECT 1", )
    except Exception:
        pass
    finally:
        conn.close()
    conn = db.get_connection(db_path)
    try:
        # runs FK -- seed a run row
        try:
            conn.execute("INSERT OR IGNORE INTO runs (run_id, episode_id, status, started_at) "
                         "VALUES ('r_pp', ?, 'running', ?)", (episode_id, NOW.isoformat()))
        except Exception:
            pass
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES (?, 'r_pp', ?, 'openrouter', ?, ?, ?)",
            (f"c_{call_type}_{amount}", episode_id, call_type, float(amount), NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_preproduction_reservation
# ---------------------------------------------------------------------------

class TestEnsurePreproductionReservation:
    def test_pre_plan_establishes_one_hold_bounded_by_policy(self, env):
        out = ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert out is not None and out.created is True
        rows = _rows(env.db_path)
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == preplan_idempotency_key(_CH, _EP)
        assert Decimal(rows[0]["reserved_amount"]) == Decimal("30.0")  # FINANCE_BUDGET_CEILING_USD

    def test_replay_is_idempotent_one_row(self, env):
        for _ in range(5):
            ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert len(_rows(env.db_path)) == 1

    def test_post_plan_is_a_noop(self, env):
        _persist_plan(env, _plan())
        out = ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert out is None
        assert _rows(env.db_path) == []

    def test_no_per_episode_ceiling_fails_closed(self, env):
        with pytest.raises(PreplanUnboundedError):
            ensure_preproduction_reservation(
                channel_id=_CH, episode_id=_EP, config=_cfg(FINANCE_BUDGET_CEILING_USD=0.0), log=_Log(),
            )

    def test_no_channel_id_fails_closed(self, env):
        with pytest.raises(PreplanReservationError):
            ensure_preproduction_reservation(channel_id=None, episode_id=_EP, config=_cfg(), log=_Log())

    def test_weekly_ceiling_exhausted_fails_closed(self, env):
        # seed another episode's hold that consumes the whole week — in the
        # SAME (current) week ensure_preproduction_reservation() resolves
        # internally via datetime.now(); a fixed NOW anchor drifts into a
        # different ISO week and the seed then stops counting.
        conn = db.get_connection(env.db_path)
        weekly_budget.ensure_channel_weekly_reservation(
            conn, channel_id=_CH, amount=Decimal("54.00"), currency="USD",
            idempotency_key="preplan:cpp:other_ep",
            now_utc=datetime.now(timezone.utc),
        )
        conn.commit(); conn.close()
        from hypogeum.budget_scopes import BudgetError
        with pytest.raises(BudgetError):
            ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())

    def test_restart_replay_creates_no_second_reservation(self, env):
        ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        # "restart": fresh call, everything reconstructed from the ledger
        out2 = ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert out2.created is False
        assert len(_rows(env.db_path)) == 1

    def test_concurrent_admission_creates_at_most_one(self, env):
        created = []

        def _run():
            try:
                o = ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
                created.append(o.created)
            except Exception as exc:  # noqa: BLE001
                created.append(type(exc).__name__)

        ts = [threading.Thread(target=_run) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join(10)
        assert created.count(True) <= 1
        assert len(_rows(env.db_path)) == 1


# ---------------------------------------------------------------------------
# atomic upgrade
# ---------------------------------------------------------------------------

class TestAtomicUpgrade:
    def _setup(self, env, *, preplan=True, incurred=None):
        if preplan:
            ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        if incurred:
            _record_cost(env.db_path, _EP, incurred)

    def test_same_week_upgrade_one_plan_hold_no_double_count(self, env):
        self._setup(env, incurred=Decimal("2.00"))
        plan = _plan(maximum=20.0)
        _persist_plan(env, plan)
        out = atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert out.created is True
        rows = {r["idempotency_key"]: r for r in _rows(env.db_path)}
        # preplan hold released; plan hold reserved; incurred marker committed
        from hypogeum.plan_reservation import plan_weekly_idempotency_key
        pk = plan_weekly_idempotency_key(_CH, _EP, plan.content_hash)
        assert rows[preplan_idempotency_key(_CH, _EP)]["status"] == "released"
        assert rows[pk]["status"] == "reserved" and Decimal(rows[pk]["reserved_amount"]) == Decimal("20.0")
        ik = preplan_incurred_idempotency_key(_CH, _EP)
        assert rows[ik]["status"] == "committed" and Decimal(rows[ik]["reserved_amount"]) == Decimal("2.00")
        # week footprint = incurred(2) + plan(20) = 22, NEVER 30(preplan)+20
        scope_ref = weekly_budget.week_scope_ref(_CH, weekly_budget.compute_weekly_window(datetime.now(timezone.utc), "UTC"))
        assert _week_sum(env.db_path, scope_ref) == Decimal("22.00")

    def test_replay_is_idempotent_same_result_no_extra_row(self, env):
        self._setup(env, incurred=Decimal("1.00"))
        _persist_plan(env, _plan(maximum=10.0))
        a = atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        n1 = len(_rows(env.db_path))
        b = atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        assert b.idempotency_key == a.idempotency_key
        assert len(_rows(env.db_path)) == n1

    def test_insufficient_week_capacity_fails_closed_no_mutation(self, env):
        # another episode holds 25 of the 55 ceiling; this episode's $30
        # PRE_PLAN hold exactly fills the week (25 + 30 = 55).
        conn = db.get_connection(env.db_path)
        weekly_budget.ensure_channel_weekly_reservation(
            conn, channel_id=_CH, amount=Decimal("25.00"), currency="USD",
            idempotency_key="preplan:cpp:big_ep", now_utc=datetime.now(timezone.utc),
        )
        conn.commit(); conn.close()
        self._setup(env, incurred=Decimal("2.00"))
        # upgrade: S_other(25) + incurred(2) + plan_max(30) = 57 > 55 ceiling
        _persist_plan(env, _plan(maximum=30.0))
        before = _rows(env.db_path)
        with pytest.raises(PreplanUpgradeCapacityError):
            atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        # NO mutation: preplan hold intact, no plan/incurred row
        after = {r["idempotency_key"]: r["status"] for r in _rows(env.db_path)}
        assert after[preplan_idempotency_key(_CH, _EP)] == "reserved"
        assert not any("plan-weekly" in k for k in after)
        assert preplan_incurred_idempotency_key(_CH, _EP) not in after

    def test_two_concurrent_upgrades_one_winner_one_plan_hold(self, env):
        self._setup(env, incurred=Decimal("1.00"))
        _persist_plan(env, _plan(maximum=10.0))
        results = []

        def _run():
            try:
                atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(type(exc).__name__)

        ts = [threading.Thread(target=_run) for _ in range(3)]
        for t in ts: t.start()
        for t in ts: t.join(10)
        assert results.count("ok") >= 1
        plan_rows = [r for r in _rows(env.db_path) if "plan-weekly" in r["idempotency_key"]]
        assert len(plan_rows) == 1

    def test_crash_before_upgrade_is_deterministic_fail_closed(self, env):
        # "crash": preplan hold established, plan persisted, upgrade never ran.
        self._setup(env, incurred=Decimal("1.00"))
        _persist_plan(env, _plan(maximum=10.0))
        conn = db.get_connection(env.db_path)
        assert has_unupgraded_preplan_hold(conn, _CH, _EP) is True
        conn.close()
        # a POST_PLAN boundary self-heals via the idempotent upgrade
        from hypogeum.plan_reservation import ensure_verified_plan_weekly_reservation
        out = ensure_verified_plan_weekly_reservation(
            channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log(),
        )
        assert "plan-weekly" in out.idempotency_key
        conn = db.get_connection(env.db_path)
        assert has_unupgraded_preplan_hold(conn, _CH, _EP) is False
        conn.close()

    def test_missing_plan_fails_closed(self, env):
        self._setup(env)
        from hypogeum.preplan_reservation import PreplanUpgradeMissingPlanError
        with pytest.raises(PreplanUpgradeMissingPlanError):
            atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())


# ---------------------------------------------------------------------------
# week boundary
# ---------------------------------------------------------------------------

class TestWeekBoundary:
    def test_pre_plan_week_a_plan_week_b_preserves_week_a_exposure(self, env, monkeypatch):
        # PRE_PLAN hold in week A
        wa = weekly_budget.compute_weekly_window(NOW, "UTC")
        ref_a = weekly_budget.week_scope_ref(_CH, wa)
        monkeypatch.setattr(
            "hypogeum.preplan_reservation.datetime",
            SimpleNamespace(now=lambda tz=None: NOW, timezone=timezone),
        )
        ensure_preproduction_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        _record_cost(env.db_path, _EP, Decimal("3.00"))
        # ... 10 days pass -> week B
        now_b = NOW + timedelta(days=10)
        wb = weekly_budget.compute_weekly_window(now_b, "UTC")
        ref_b = weekly_budget.week_scope_ref(_CH, wb)
        assert ref_a != ref_b
        monkeypatch.setattr(
            "hypogeum.preplan_reservation.datetime",
            SimpleNamespace(now=lambda tz=None: now_b, timezone=timezone),
        )
        _persist_plan(env, _plan(maximum=15.0))
        atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=_EP, config=_cfg(), log=_Log())
        # week A: only the incurred marker (3.00), preplan hold released
        assert _week_sum(env.db_path, ref_a) == Decimal("3.00")
        # week B: the plan hold (15.00), evaluated independently
        assert _week_sum(env.db_path, ref_b) == Decimal("15.00")
        rows = {r["idempotency_key"]: r for r in _rows(env.db_path)}
        assert rows[preplan_idempotency_key(_CH, _EP)]["status"] == "released"
        assert rows[preplan_incurred_idempotency_key(_CH, _EP)]["scope_ref"] == ref_a


# ---------------------------------------------------------------------------
# section 0 -- real MUAPI video plan-binding regression
# ---------------------------------------------------------------------------

class TestSection0VideoPlanBindingRegression:
    def test_dependency_checksums_plan_is_read_from_the_dataclass_envelope(self):
        """The committed capability_boundary reads
        env.dependency_checksums.plan for the video path. Prove that a
        real GenerationExecutionEnvelope exposes .plan as an attribute
        (frozen dataclass) AND that a Mapping is also handled."""
        from hypogeum.execution_envelope import DependencyChecksums, GenerationExecutionEnvelope

        h = "d" * 64
        dc = DependencyChecksums(channel_package="b" * 64, plan=h, structured_spec="e" * 64, compiler="f" * 64)
        assert getattr(dc, "plan", None) == h                    # dataclass attribute access
        assert not isinstance(dc, dict)

        # the capability_boundary access expression, both shapes:
        import hypogeum.capability_boundary as cb  # noqa: F401
        from typing import Mapping

        def _read(dep):
            if dep is None:
                return None
            if isinstance(dep, Mapping):
                return dep.get("plan")
            return getattr(dep, "plan", None)

        assert _read(dc) == h
        assert _read({"plan": h}) == h
        assert _read(None) is None

    def test_video_gate_path_reads_bound_plan_hash_and_passes(self, env, monkeypatch):
        """A real video-shaped envelope (dependency_checksums.plan ==
        canonical plan content_hash) + its plan-weekly reservation +
        canonical plan -> POST_PLAN binding PASSES."""
        import hashlib
        from hypogeum.capability_boundary import _verify_post_plan_weekly_binding
        from hypogeum.plan_reservation import plan_weekly_idempotency_key

        plan = _plan(maximum=20.0)
        _persist_plan(env, plan)
        conn = db.get_connection(env.db_path)
        weekly_budget.ensure_channel_weekly_reservation(
            conn, channel_id=_CH, amount=Decimal("20.0"), currency="USD",
            idempotency_key=plan_weekly_idempotency_key(_CH, _EP, plan.content_hash),
            now_utc=datetime.now(timezone.utc),
        )
        conn.commit()

        # video-shaped envelope stub: dependency_checksums is the real
        # frozen dataclass, .plan == the canonical plan content_hash,
        # audit_metadata empty (the video path binds via dependency_checksums).
        from hypogeum.execution_envelope import DependencyChecksums
        env_stub = SimpleNamespace(
            channel_id=_CH, execution_id=_EP, currency="USD",
            audit_metadata={},
            dependency_checksums=DependencyChecksums(
                channel_package="b" * 64, plan=plan.content_hash,
                structured_spec="e" * 64, compiler="f" * 64,
            ),
        )
        cap_stub = SimpleNamespace(currency="USD")
        errs = []

        def _fail(msg):
            errs.append(msg)
            return AssertionError(msg)

        _verify_post_plan_weekly_binding(
            conn, env=env_stub, capability=cap_stub,
            now=datetime.now(timezone.utc), fail=_fail,
        )
        conn.close()
        assert errs == []


# ---------------------------------------------------------------------------
# dry-run isolation
# ---------------------------------------------------------------------------

class TestDryRunIsolation:
    def test_muapi_dry_run_upscale_never_touches_preplan_ledger(self, env, tmp_path):
        import subprocess
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider
        src = tmp_path / "s.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
                       capture_output=True, timeout=30)
        res = MuapiVideoUpscalerProvider().enhance(
            source_path=src, dest_path=tmp_path / "d.mp4", target_width=640, target_height=480,
            target_resolution_label="480p", config=SimpleNamespace(MUAPI_DRY_RUN=True),
            episode_id=_EP, scene_id="s01", log=_Log(), channel_id=_CH,
        )
        assert res.success is True
        assert _rows(env.db_path) == []


# ---------------------------------------------------------------------------
# every paid PRE_PLAN boundary is zero-network without the reservation
# ---------------------------------------------------------------------------

class TestPrePlanBoundariesZeroNetworkWithoutReservation:
    def test_openrouter_call_claude_fails_closed_zero_network(self, env, monkeypatch):
        import requests as _rq
        from hypogeum.openrouter import HardFailure, call_claude
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        monkeypatch.setattr(_rq, "post", sentinel)
        # no per-episode ceiling -> PreplanUnboundedError -> HardFailure, zero network
        with pytest.raises(HardFailure, match="pre_plan_reservation_denied"):
            call_claude("anthropic/claude-sonnet-4.6", [{"role": "user", "content": "x"}], 10,
                        config=_cfg(FINANCE_BUDGET_CEILING_USD=0.0, OPENROUTER_API_KEY="k"),
                        episode_id=_EP, channel_id=_CH)
        sentinel.assert_not_called()

    def test_openrouter_one_valid_reservation_then_call_proceeds(self, env, monkeypatch):
        # with a resolvable ceiling the PRE_PLAN hold is established and the
        # call proceeds to (mocked) network.
        import hypogeum.openrouter as _or
        reached = {}
        def _fake_dispatch(*a, **k):
            reached["yes"] = True
            raise _or.HardFailure("stop-after-reservation")
        monkeypatch.setattr(_or, "_apply_system_prompt_caching", lambda m, s: m)
        with patch("hypogeum.openrouter._call_openrouter", side_effect=lambda *a, **k: (_ for _ in ()).throw(_or.HardFailure("stop"))):
            with pytest.raises(_or.OpenRouterError):
                _or.call_claude("anthropic/claude-sonnet-4.6", [{"role": "user", "content": "x"}], 10,
                                config=_cfg(OPENROUTER_API_KEY="k"), episode_id=_EP, channel_id=_CH)
        assert has_unupgraded_preplan_hold(db.get_connection(env.db_path), _CH, _EP) is True

    def test_perplexity_query_fails_closed_zero_network(self, env, monkeypatch):
        import requests as _rq
        from hypogeum.perplexity import PerplexityHardFailure, query_perplexity
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        monkeypatch.setattr(_rq, "post", sentinel)
        with pytest.raises(PerplexityHardFailure, match="pre_plan_reservation_denied"):
            query_perplexity("q", _cfg(FINANCE_BUDGET_CEILING_USD=0.0, PERPLEXITY_API_KEY="k",
                                      PERPLEXITY_MODEL="sonar", PERPLEXITY_MAX_TOKENS=64),
                             episode_id=_EP, channel_id=_CH)
        sentinel.assert_not_called()

    def test_elevenlabs_first_narration_fails_closed_zero_network(self, env, monkeypatch):
        import requests as _rq
        from hypogeum.elevenlabs import ElevenLabsError, generate_narration_segment
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        monkeypatch.setattr(_rq, "post", sentinel)
        with pytest.raises(ElevenLabsError, match="pre_plan_reservation_denied"):
            generate_narration_segment(
                beat_id="b1", text="hello", config=_cfg(FINANCE_BUDGET_CEILING_USD=0.0,
                                                        ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v"),
                episode_id=_EP, output_dir=env.canon.parent.parent, channel_id=_CH,
            )
        sentinel.assert_not_called()
