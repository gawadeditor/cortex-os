"""
tests/test_minimum_autonomous_runtime.py — ADR-016 zero-network shadow
validation of the Minimum Autonomous Runtime for one Channel Package.

Temporary DBs, a test Ed25519 keypair, injected clocks, isolated asset
roots, fake providers, a fake pipeline runner. No production DB, no
network, no worker, no publishing.

Covers the twelve required MAR shadow scenarios plus focused unit
coverage of the Channel Operating Grant and the bounded budget policy.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.policy_cascade as policy_cascade
import hypogeum.runtime_admission as adm
import hypogeum.autonomous_runtime as autoruntime
import hypogeum.channel_budget_policy as budget_policy
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.production_plan import ProductionPlanCoverageError

_CH = "hypogeum"
_WF_ID = "hypogeum_main_production"
_WF_VER = "1.0.0"
_WF_SUM = "a" * 64
_CP_VER = "1.0"
_CP_SUM = "b" * 64
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # Wed -> ISO 2026-W35

# The configured weekly limit is an OPERATOR POLICY VALUE, not an
# architectural constant. The fixture deliberately uses a non-55 value so
# that no test can silently depend on "55 is special"; individual tests
# reconfigure it (55, 80, 120) and prove the code path is unchanged.
_DEFAULT_LIMIT = Decimal("80.00")


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _cfg(**over):
    base = dict(FINANCE_BUDGET_CEILING_USD=float(_DEFAULT_LIMIT), MUAPI_DEFAULT_TIER=2,
                MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=3)
    base.update(over)
    return SimpleNamespace(**base)


def _bindings():
    return [
        store.GrantBinding(
            provider="openrouter", model="anthropic/claude-sonnet-4",
            provider_operation="text.completion", currency="USD",
            max_cost_per_call=Decimal("0.50"), max_calls=2000, max_total_spend=Decimal("200.00"),
        ),
        store.GrantBinding(
            provider="perplexity", model="sonar-pro",
            provider_operation="research.search", currency="USD",
            max_cost_per_call=Decimal("0.10"), max_calls=500, max_total_spend=Decimal("10.00"),
        ),
        store.GrantBinding(
            provider="elevenlabs", model="eleven_multilingual_v2",
            provider_operation="voice.narration_segment", currency="USD",
            max_cost_per_call=Decimal("1.00"), max_calls=2000, max_total_spend=Decimal("30.00"),
        ),
    ]


def _set_weekly_limit(db_path, amount):
    """Reconfigure the operator-controlled weekly budget limit. No code
    change -- just the Channel Operations Profile policy value."""
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "UPDATE channel_operations_profiles SET weekly_video_budget_usd = ?, updated_at = ? "
            "WHERE channel_id = ?",
            (float(amount), f"upd-{amount}", _CH),
        )
        conn.commit()
    finally:
        conn.close()


def _sign_and_register_operating_grant(
    priv, db_path, *, channel_id=_CH, grant_id="op_g1", grant_version=1,
    valid_from="2026-08-01T00:00:00Z", expires_at="2026-12-31T23:59:59Z",
    weekly_ceiling=_DEFAULT_LIMIT, cadence="videos_per_week:1", workflow_version=_WF_VER,
    domains=("openrouter_text", "perplexity_research", "elevenlabs_voice"),
    tamper=False, approver="operator_x", nonce=None,
):
    bindings = _bindings()
    checksum = authority.compute_grant_checksum(
        channel_id=channel_id, episode_id="*", workflow_id=_WF_ID, workflow_version=workflow_version,
        workflow_checksum=_WF_SUM, production_plan_content_hash=None, bindings=bindings,
        max_total_spend_all_bindings=weekly_ceiling, currency="USD", expires_at=expires_at,
        grant_kind="channel_operating", channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM,
        allowed_provider_domains=list(domains), cadence_snapshot=cadence,
        weekly_exposure_ceiling_usd=weekly_ceiling, valid_from=valid_from,
        operating_period_ref=f"{channel_id}:2026-W35..W48",
    )
    fields = dict(
        approval_id=f"appr_{grant_id}", approval_version=1, approval_type="channel_operating_grant",
        subject_checksum=checksum, channel_id=channel_id, scope_id="*", workflow_id=_WF_ID,
        workflow_version=workflow_version, workflow_checksum=_WF_SUM,
        channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM,
        allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings),
        currency="USD", max_cost_per_call=Decimal("1.00"), max_total_spend=weekly_ceiling, max_calls=2000,
        retry_authorized=False, fallback_authorized=False, issued_at=valid_from, expires_at=expires_at,
        approver_identity=approver, nonce=nonce or f"nonce_{grant_id}",
    )
    artifact = sign_approval_artifact(fields, private_key=priv)
    grant = authority.register_channel_operating_grant(
        grant_id=grant_id, grant_version=grant_version, channel_id=channel_id, workflow_id=_WF_ID,
        workflow_version=workflow_version, workflow_checksum=_WF_SUM,
        channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM,
        allowed_provider_domains=list(domains), cadence_snapshot=cadence,
        weekly_exposure_ceiling_usd=weekly_ceiling, bindings=bindings, currency="USD",
        valid_from=valid_from, expires_at=expires_at,
        operating_period_ref=f"{channel_id}:2026-W35..W48",
        approval_artifact=artifact, db_path=db_path,
    )
    if tamper:
        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "UPDATE run_authorization_grants SET weekly_exposure_ceiling_usd = '999.00' "
                "WHERE grant_id = ?", (grant_id,),
            )
            conn.commit()
        finally:
            conn.close()
    return grant


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "mar.db"
    db.initialize_schema(db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())
    monkeypatch.setattr(
        policy_cascade, "get_effective_budget_ceiling",
        lambda env_ceiling=0.0, channel_id=None, factory_id=None, db_path=None: float(env_ceiling),
    )
    channel_config.create_channel_profile(channel_id=_CH, name="Hypogeum", db_path=db_path)
    channel_config.create_channel_operations_profile(
        channel_id=_CH, channel_name="Hypogeum", timezone="UTC", operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        weekly_video_budget_usd=float(_DEFAULT_LIMIT), db_path=db_path,
    )
    return SimpleNamespace(tmp_path=tmp_path, db_path=db_path, priv=priv)


def _seed_topic(db_path, text="The Antikythera Mechanism"):
    from hypogeum import topics
    return topics.enqueue_topic(channel_id=_CH, topic_text=text, db_path=db_path)


def _record_cost(db_path, episode_id, amount, call_type="script_engine"):
    conn = db.get_connection(db_path)
    try:
        try:
            conn.execute("INSERT OR IGNORE INTO runs (run_id, episode_id, status, started_at) "
                         "VALUES ('r_mar', ?, 'running', ?)", (episode_id, NOW.isoformat()))
        except Exception:
            pass
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES (?, 'r_mar', ?, 'openrouter', ?, ?, ?)",
            (f"c_{episode_id}_{call_type}_{amount}", episode_id, call_type, float(amount), NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _admit(env, *, worker_id="w1", now=NOW):
    return adm.admit_channel_execution(
        _CH, config=_cfg(), worker_id=worker_id, log=_Log(), now=now, db_path=env.db_path,
    )


def _reservation_counts(db_path):
    conn = db.get_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0]
        cw = conn.execute(
            "SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='channel_weekly'"
        ).fetchone()[0]
        adm_rows = conn.execute("SELECT COUNT(*) FROM execution_admissions").fetchone()[0]
        q_rows = conn.execute("SELECT COUNT(*) FROM run_queue").fetchone()[0]
        return total, cw, adm_rows, q_rows
    finally:
        conn.close()


# ===========================================================================
# Scenario 1 — successful scheduled admission
# ===========================================================================

class TestScenario01SuccessfulAdmission:
    def test_due_channel_admits_one_execution(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        rec = _admit(env)
        assert rec.status == "admitted"
        assert rec.channel_id == _CH
        assert rec.operating_grant_id == "op_g1"
        assert rec.configured_weekly_limit_usd == "80.00"   # the operator policy value, not a constant
        assert rec.budget_policy_ref.startswith("channel_operations_profile:hypogeum:")
        assert rec.channel_week_ref.endswith("2026-W35")
        assert rec.lease_fencing_token >= 1
        assert rec.preplan_reservation_key.startswith("preplan:")
        _tot, cw, admissions, qrows = _reservation_counts(env.db_path)
        assert admissions == 1
        assert qrows == 1              # enqueued exactly once
        assert cw == 1                 # exactly one PRE_PLAN hold
        # topic consumed/claimed, bound to the episode
        from hypogeum import topics
        t = topics.list_topics(channel_id=_CH, db_path=env.db_path)[0]
        assert t["status"] in ("claimed", "consumed")
        assert t["episode_id"] == rec.episode_id


# ===========================================================================
# Scenario 2 — idempotent admission replay
# ===========================================================================

class TestScenario02IdempotentReplay:
    def test_second_admit_same_week_returns_same_identity_no_new_rows(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        first = _admit(env)
        c1 = _reservation_counts(env.db_path)
        second = _admit(env, now=NOW + timedelta(hours=6))
        c2 = _reservation_counts(env.db_path)
        assert second.admission_id == first.admission_id
        assert second.episode_id == first.episode_id
        assert second.execution_id == first.execution_id
        assert second.preplan_reservation_key == first.preplan_reservation_key
        assert c2 == c1  # no new reservation, admission, or queue rows


# ===========================================================================
# Scenario 3 — expired grant
# ===========================================================================

class TestScenario03ExpiredGrant:
    def test_expired_operating_grant_blocks_admission(self, env):
        _sign_and_register_operating_grant(
            env.priv, env.db_path, valid_from="2026-01-01T00:00:00Z",
            expires_at="2026-08-20T00:00:00Z",  # already past at NOW
        )
        _seed_topic(env.db_path)
        with pytest.raises(adm.NoActiveOperatingGrantError):
            _admit(env)
        assert _reservation_counts(env.db_path) == (0, 0, 0, 0)


# ===========================================================================
# Scenario 4 — revoked grant
# ===========================================================================

class TestScenario04RevokedGrant:
    def test_revoked_operating_grant_blocks_admission(self, env):
        g = _sign_and_register_operating_grant(env.priv, env.db_path)
        authority.revoke_grant(g.grant_id, g.grant_version, revoked_by="operator",
                               reason="rotation", at=NOW.isoformat(), db_path=env.db_path)
        _seed_topic(env.db_path)
        with pytest.raises(adm.NoActiveOperatingGrantError):
            _admit(env)
        assert _reservation_counts(env.db_path) == (0, 0, 0, 0)


# ===========================================================================
# Scenario 5 — foreign-channel grant
# ===========================================================================

class TestScenario05ForeignChannelGrant:
    def test_grant_for_another_channel_does_not_admit_hypogeum(self, env):
        channel_config.create_channel_profile(channel_id="other_channel", name="Other", db_path=env.db_path)
        channel_config.create_channel_operations_profile(
            channel_id="other_channel", channel_name="Other", timezone="UTC",
            operator_enabled=True, production_cadence={"videos_per_week": 1},
            weekly_video_budget_usd=float(_DEFAULT_LIMIT), db_path=env.db_path,
        )
        _sign_and_register_operating_grant(env.priv, env.db_path, channel_id="other_channel",
                                           grant_id="op_other")
        _seed_topic(env.db_path)
        with pytest.raises(adm.NoActiveOperatingGrantError):
            _admit(env)


# ===========================================================================
# Scenario 6 — tampered grant
# ===========================================================================

class TestScenario06TamperedGrant:
    def test_post_signature_row_tamper_fails_verification(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path, tamper=True)
        _seed_topic(env.db_path)
        # resolve returns None because the recomputed checksum no longer
        # matches the signed artifact's subject_checksum
        assert authority.resolve_active_operating_grant(_CH, at=NOW.isoformat(), db_path=env.db_path) is None
        with pytest.raises(adm.NoActiveOperatingGrantError):
            _admit(env)


# ===========================================================================
# Scenario 7 — concurrent admission
# ===========================================================================

class TestScenario07ConcurrentAdmission:
    def test_two_concurrent_admits_one_winner_one_execution(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        results, errors = [], []

        def _worker(wid):
            try:
                results.append(_admit(env, worker_id=wid))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        _tot, cw, admissions, qrows = _reservation_counts(env.db_path)
        assert admissions == 1
        assert qrows == 1
        assert cw == 1
        # every successful admit returns the SAME identity
        ids = {r.admission_id for r in results}
        assert len(ids) == 1
        # losers fail closed on the lease, not with a half-created execution
        assert all(isinstance(e, adm.AdmissionLeaseUnavailableError) for e in errors)


# ===========================================================================
# Scenario 8 — fenced worker race
# ===========================================================================

class TestScenario08FencedWorkerRace:
    def test_stale_worker_cannot_heartbeat_after_takeover(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        rec = _admit(env, worker_id="w_old")
        # lease expires; a new worker takes over on the replay path
        later = NOW + timedelta(seconds=adm.LEASE_TTL_SECONDS + 60)
        rec2 = adm.admit_channel_execution(
            _CH, config=_cfg(), worker_id="w_new", log=_Log(), now=later, db_path=env.db_path,
        )
        assert rec2.admission_id == rec.admission_id
        assert rec2.lease_fencing_token > rec.lease_fencing_token
        # the old worker's fencing token is now stale -> heartbeat fails closed
        with pytest.raises(adm.AdmissionLeaseUnavailableError):
            adm.renew_admission_lease(rec.admission_id, "w_old", now=later + timedelta(seconds=1),
                                      db_path=env.db_path)


# ===========================================================================
# Scenario 9 — restart from checkpoint (same episode/execution resume)
# ===========================================================================

class TestScenario09RestartResumesSameExecution:
    def test_cycle_restart_resumes_same_execution_no_duplicate(self, env):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)

        calls = {"n": 0}

        def _fake_runner(*, channel_id, episode_id, config, db_path, admission):
            calls["n"] += 1
            calls["episode_id"] = episode_id
            return SimpleNamespace(status="ready_to_publish", detail="fake ok")

        # cycle 1: admits
        r1 = autoruntime.run_autonomous_cycle(
            _CH, config=_cfg(), worker_id="w1", now=NOW, db_path=env.db_path,
            pipeline_runner=_fake_runner,
        )
        assert r1.state == autoruntime.RuntimeState.ADMITTED
        # cycle 2: same worker restarts -> runs pipeline against the SAME episode
        r2 = autoruntime.run_autonomous_cycle(
            _CH, config=_cfg(), worker_id="w1", now=NOW + timedelta(minutes=5), db_path=env.db_path,
            pipeline_runner=_fake_runner,
        )
        assert r2.episode_id == r1.episode_id
        assert r2.state == autoruntime.RuntimeState.READY_TO_PUBLISH
        # cycle 3: episode already completed this week -> no new work
        r3 = autoruntime.run_autonomous_cycle(
            _CH, config=_cfg(), worker_id="w1", now=NOW + timedelta(hours=1), db_path=env.db_path,
            pipeline_runner=_fake_runner,
        )
        assert r3.state == autoruntime.RuntimeState.COMPLETED
        assert calls["n"] == 1  # pipeline invoked exactly once
        _tot, cw, admissions, qrows = _reservation_counts(env.db_path)
        assert admissions == 1 and qrows == 1


# ===========================================================================
# Scenario 10 — crash after provider-I/O claim: no duplicate provider execution
# ===========================================================================

class TestScenario10NoDuplicateAfterIoClaim:
    def test_restart_after_plan_upgrade_does_not_re_run_preplan_or_re_admit(self, env):
        """Once the admission has transitioned to plan_upgraded (the
        PRE_PLAN -> plan-bound weekly reservation upgrade committed), a
        restarted cycle resumes the SAME execution: it does not re-admit,
        does not create a second PRE_PLAN reservation, and does not
        enqueue a second queue item. Combined with the capability
        boundary's durable os.link provider-I/O entry claim (ADR-014,
        proven in tests/test_capability_boundary.py), a crash after a
        provider I/O claim cannot cause a duplicate provider execution."""
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        rec = _admit(env)
        adm.mark_admission_plan_upgraded(rec.admission_id, db_path=env.db_path)

        c_before = _reservation_counts(env.db_path)
        runs = {"n": 0}

        def _fake_runner(*, channel_id, episode_id, config, db_path, admission):
            runs["n"] += 1
            runs["episode_id"] = episode_id
            assert admission.status == "plan_upgraded"
            return SimpleNamespace(status="ready_to_publish", detail="ok")

        r = autoruntime.run_autonomous_cycle(
            _CH, config=_cfg(), worker_id="w1", now=NOW + timedelta(minutes=30),
            db_path=env.db_path, pipeline_runner=_fake_runner,
        )
        assert runs["n"] == 1
        assert runs["episode_id"] == rec.episode_id
        assert r.state == autoruntime.RuntimeState.READY_TO_PUBLISH
        c_after = _reservation_counts(env.db_path)
        # no new admission / queue / reservation rows created on resume
        assert c_after[2] == c_before[2]  # admissions
        assert c_after[3] == c_before[3]  # run_queue

    def test_durable_io_entry_claim_primitive_is_covered(self):
        """The capability boundary's durable provider-I/O entry claim
        (os.link create-if-absent, ADR-014) is what makes a crash AFTER
        the claim leave the attempt spent -- never silently retryable.
        Its dedicated coverage lives in tests/test_capability_boundary.py
        (test_concurrent_invocations_enter_provider_io_at_most_once and
        the durable-consumption tests). This assertion documents the
        dependency for the MAR shadow suite."""
        import importlib
        cb = importlib.import_module("hypogeum.capability_boundary")
        assert hasattr(cb, "_IO_ENTRY_PHASE")
        assert cb._IO_ENTRY_PHASE == "capability_boundary_provider_io_entry_claim"


# ===========================================================================
# Scenario 11 — truthful complete-episode plan vs. OPERATOR-CONFIGURABLE limit
# (USD 55 is NOT an architectural constant)
# ===========================================================================

def _complete_visual_package(n_scenes, *, tier=2, contingency_per_scene=2, shorts=2):
    scenes = []
    for i in range(n_scenes):
        shots = [{"shot_order": 1, "primary_prompt": f"scene {i} primary"}]
        for k in range(2, 2 + contingency_per_scene):
            shots.append({"shot_order": k, "primary_prompt": f"scene {i} contingency {k}",
                          "shot_id": f"s{i}_shot{k}"})
        scenes.append({
            "scene_id": f"s{i:02d}", "narration_reference": f"b_{i:03d}", "short_id": None,
            "production_spec": {"provider_routing": {"primary_tier": tier, "primary_prompt": f"scene {i}"},
                               "aspect_ratio": "16:9"},
            "cinematic_coverage_plan": {"shots": shots},
        })
    for s in range(shorts):
        scenes.append({
            "scene_id": f"short{s:02d}", "narration_reference": f"sh_{s:03d}", "short_id": f"short{s}",
            "production_spec": {"provider_routing": {"primary_tier": tier, "primary_prompt": f"short {s}"},
                               "aspect_ratio": "9:16"},
            "cinematic_coverage_plan": {"shots": [{"shot_order": 1, "primary_prompt": f"short {s}"}]},
        })
    return {"scenes": scenes}


def _script_pkg(n_scenes, shorts=2):
    return {
        "sections": [{"section_id": "sec1"}],
        "shorts_scripts": [{"short_id": f"short{s}"} for s in range(shorts)],
        "beats": (
            [{"beat_id": f"b_{i:03d}", "deliverable_id": "main_episode"} for i in range(n_scenes)]
            + [{"beat_id": f"sh_{s:03d}", "deliverable_id": f"short{s}"} for s in range(shorts)]
        ),
    }


def _install_pricing_model(monkeypatch, *, per_shot_by_tier=None, fixed_overhead=Decimal("4.00")):
    """A deterministic zero-network pricing model in place of the real
    compile_production_plan. `maximum_bounded_cost_usd` is a faithful
    function of (scene/shot count, tier, attempts, enhancement). It NEVER
    invents a lower number than the assets imply -- so a plan cannot be
    made affordable by understating the work."""
    prices = per_shot_by_tier or {1: Decimal("2.50"), 2: Decimal("0.39"), 3: Decimal("0.39")}

    def _compile(*, script_package, narration_package, visual_package, channel_id, episode_id, config):
        scenes = visual_package.get("scenes", [])
        attempts = Decimal(str(getattr(config, "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", 3)))
        enh_on = getattr(config, "SOURCE_ENHANCEMENT_ENABLED", True) is not False
        vid_norm = vid_max = enh = Decimal("0")
        n_shots = 0
        for sc in scenes:
            t = int(sc["production_spec"]["provider_routing"]["primary_tier"])
            p = prices.get(t, Decimal("0.39"))
            shots = sc["cinematic_coverage_plan"]["shots"]
            n_shots += len(shots)
            vid_norm += p * len(shots)
            vid_max += p * attempts * len(shots)
            if enh_on:
                enh += Decimal("0.08") * len(shots)
        norm = vid_norm + Decimal("2.00")
        mx = vid_max + enh + fixed_overhead
        # required beats must equal the plan's own asset beat set -> a
        # plan that drops a required scene fails coverage below
        beat_ids = {b["beat_id"] for b in script_package.get("beats", [])}
        covered = {sc["narration_reference"] for sc in scenes}
        return SimpleNamespace(
            maximum_bounded_cost_usd=float(mx), normal_expected_cost_usd=float(norm),
            enhancement_cost_maximum_usd=float(enh), thumbnail_cost_maximum_usd=1.0,
            narration_cost_usd=0.5, music_cost_usd=0.5, planning_call_estimate_usd=0.05,
            content_hash="c" * 64, channel_id=channel_id, episode_id=episode_id,
            assets=list(range(n_shots)), deliverable_ids=sorted({b["deliverable_id"] for b in script_package.get("beats", [])}),
            _covered=covered, _required=beat_ids,
        )

    def _coverage(plan, nar):
        missing = getattr(plan, "_required", set()) - getattr(plan, "_covered", set())
        return SimpleNamespace(
            sufficient=not missing,
            failing_beats=[{"beat_id": b} for b in sorted(missing)],
        )

    monkeypatch.setattr(budget_policy, "compile_production_plan", _compile)
    monkeypatch.setattr(budget_policy, "validate_plan_coverage", _coverage)


class TestScenario11ConfigurableBudget:
    def test_cost_model_keeps_the_six_concepts_separate(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        # incur some PRE_PLAN cost
        _record_cost(env.db_path, "ep_sep", Decimal("12.00"))
        vp = _complete_visual_package(8, tier=2)
        res = budget_policy.compile_truthful_production_plan(
            script_package=_script_pkg(8), narration_package={}, visual_package=vp,
            channel_id=_CH, episode_id="ep_sep", config=_cfg(), now=NOW, db_path=env.db_path,
        )
        cm = res.cost_model
        assert cm.actual_incurred_cost == Decimal("12.00")
        assert cm.expected_completion_cost > cm.actual_incurred_cost
        assert cm.maximum_bounded_completion_cost > cm.expected_completion_cost
        assert cm.configured_weekly_budget_limit == Decimal("80.00")
        assert cm.remaining_weekly_capacity == Decimal("80.00") - Decimal("12.00")
        assert cm.required_reservation_amount == Decimal(str(res.production_plan.maximum_bounded_cost_usd))
        # concept #7: none is a substitute for another
        assert len({cm.actual_incurred_cost, cm.expected_completion_cost,
                    cm.maximum_bounded_completion_cost, cm.configured_weekly_budget_limit}) == 4

    def test_1_complete_plan_below_configured_limit_is_admitted(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        vp = _complete_visual_package(8, tier=2, contingency_per_scene=1)
        res = budget_policy.compile_truthful_production_plan(
            script_package=_script_pkg(8), narration_package={}, visual_package=vp,
            channel_id=_CH, episode_id="ep_ok", config=_cfg(), now=NOW, db_path=env.db_path,
        )
        assert res.is_alternative_plan is False
        assert res.substitution_applied == []
        assert res.cost_model.fits()
        assert res.accounting_ok()

    def test_2_identical_plan_blocked_under_a_lower_configured_limit(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        vp = _complete_visual_package(14, tier=1, contingency_per_scene=2)  # expensive
        kwargs = dict(script_package=_script_pkg(14), narration_package={}, visual_package=vp,
                      channel_id=_CH, episode_id="ep_lim", config=_cfg(), now=NOW, db_path=env.db_path)
        # under a generous USD 500 limit it fits
        _set_weekly_limit(env.db_path, 500.00)
        ok = budget_policy.compile_truthful_production_plan(**kwargs)
        assert ok.cost_model.fits()
        # the SAME plan under USD 40 is blocked -- no code change, no reduction
        _set_weekly_limit(env.db_path, 40.00)
        with pytest.raises(budget_policy.ProductionPlanBudgetInfeasibleError) as ei:
            budget_policy.compile_truthful_production_plan(**kwargs)
        exc = ei.value
        assert exc.cost_model.configured_weekly_budget_limit == Decimal("40.00")
        assert exc.deficit > 0
        assert exc.substitution_attempted is False
        assert exc.component_breakdown["asset_count"] > 0

    def test_3_raising_the_policy_allows_it_without_code_changes(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        vp = _complete_visual_package(14, tier=1, contingency_per_scene=2)
        kwargs = dict(script_package=_script_pkg(14), narration_package={}, visual_package=vp,
                      channel_id=_CH, episode_id="ep_raise", config=_cfg(), now=NOW, db_path=env.db_path)
        _set_weekly_limit(env.db_path, 40.00)
        with pytest.raises(budget_policy.ProductionPlanBudgetInfeasibleError):
            budget_policy.compile_truthful_production_plan(**kwargs)
        # operator raises the configured policy -> same code, now admitted
        _set_weekly_limit(env.db_path, 500.00)
        res = budget_policy.compile_truthful_production_plan(**kwargs)
        assert res.cost_model.fits()
        assert res.is_alternative_plan is False

    def test_operating_grant_binds_to_configured_limit_120_works_unchanged(self, env, monkeypatch):
        """Admission works identically at USD 120 as at USD 80 -- the
        limit is data. A grant signed for the old limit is rejected as
        stale after the operator changes it."""
        _set_weekly_limit(env.db_path, 120.00)
        _sign_and_register_operating_grant(env.priv, env.db_path, weekly_ceiling=Decimal("120.00"))
        _seed_topic(env.db_path)
        rec = _admit(env)
        assert rec.configured_weekly_limit_usd == "120.00"
        # operator now lowers the limit; the existing grant is stale
        _set_weekly_limit(env.db_path, 60.00)
        adm.release_admission(rec.admission_id, db_path=env.db_path)
        _seed_topic(env.db_path, text="another")
        with pytest.raises(adm.OperatingGrantStalePolicyError):
            adm.admit_channel_execution(_CH, config=_cfg(), worker_id="w2", log=_Log(),
                                        now=NOW + timedelta(days=8), db_path=env.db_path)

    def test_4_plan_cannot_pass_by_omitting_required_scenes(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        # 12 required beats, but the visual package only covers 6 scenes
        vp_incomplete = _complete_visual_package(6, tier=2, shorts=0)
        with pytest.raises(ProductionPlanCoverageError):
            _ = budget_policy.compile_truthful_production_plan(
                script_package=_script_pkg(12, shorts=0), narration_package={}, visual_package=vp_incomplete,
                channel_id=_CH, episode_id="ep_omit", config=_cfg(), now=NOW, db_path=env.db_path,
            )

    def test_4b_substitution_never_drops_a_mandatory_deliverable(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        # permit substitution, give an expensive plan and a low limit
        conn = db.get_connection(env.db_path)
        try:
            conn.execute("UPDATE channel_operations_profiles SET retry_policy_json = ? WHERE channel_id=?",
                         ('{"budget_substitution_permitted": true}', _CH))
            conn.commit()
        finally:
            conn.close()
        _set_weekly_limit(env.db_path, 30.00)
        vp = _complete_visual_package(10, tier=1, contingency_per_scene=3, shorts=2)
        try:
            res = budget_policy.compile_truthful_production_plan(
                script_package=_script_pkg(10, shorts=2), narration_package={}, visual_package=vp,
                channel_id=_CH, episode_id="ep_sub", config=_cfg(), now=NOW, db_path=env.db_path,
            )
        except budget_policy.ProductionPlanBudgetInfeasibleError as exc:
            # still infeasible even with substitutions -> reported, not faked
            assert exc.substitution_attempted is True
            return
        # if it did fit: the alternative must still cover every required beat + every short
        assert res.coverage_sufficient is True
        assert set(res.production_plan.deliverable_ids) >= {"main_episode", "short0", "short1"}

    def test_7_actual_expected_and_maximum_remain_distinct_after_incurring(self, env, monkeypatch):
        _install_pricing_model(monkeypatch)
        _record_cost(env.db_path, "ep_d", Decimal("9.00"))
        cm = budget_policy.compute_episode_cost_model(_CH, "ep_d", now=NOW, db_path=env.db_path)
        assert cm.actual_incurred_cost == Decimal("9.00")
        assert cm.expected_completion_cost == Decimal("9.00")   # no plan yet -> only incurred
        assert cm.maximum_bounded_completion_cost == Decimal("9.00")
        assert cm.required_reservation_amount == Decimal("0")

    def test_5_reservation_is_bounded_to_planned_work_only(self, env, monkeypatch):
        """required_reservation_amount == the plan's own maximum bounded
        cost -- exactly the planned shots incl. their planned retry
        allowance, and nothing more. An unplanned extra shot would raise
        that number and therefore require a revised plan + a new
        reservation (it cannot be generated under the old lower number)."""
        _install_pricing_model(monkeypatch)
        vp = _complete_visual_package(8, tier=2, contingency_per_scene=1)
        res = budget_policy.compile_truthful_production_plan(
            script_package=_script_pkg(8), narration_package={}, visual_package=vp,
            channel_id=_CH, episode_id="ep_bound", config=_cfg(), now=NOW, db_path=env.db_path,
        )
        planned_max = Decimal(str(res.production_plan.maximum_bounded_cost_usd))
        assert res.cost_model.required_reservation_amount == planned_max
        # a plan with one MORE shot is strictly more expensive
        vp_more = _complete_visual_package(9, tier=2, contingency_per_scene=1)
        res_more = budget_policy.compile_truthful_production_plan(
            script_package=_script_pkg(9), narration_package={}, visual_package=vp_more,
            channel_id=_CH, episode_id="ep_bound", config=_cfg(), now=NOW, db_path=env.db_path,
        )
        assert Decimal(str(res_more.production_plan.maximum_bounded_cost_usd)) > planned_max

    def test_6_revised_higher_cost_plan_needs_additional_capacity_before_io(self, env, monkeypatch):
        """A revised Production Plan whose maximum bounded cost is higher
        must be evaluated against remaining capacity again; if the extra
        exposure does not fit, it fails closed BEFORE any provider I/O."""
        _install_pricing_model(monkeypatch)
        _set_weekly_limit(env.db_path, 60.00)
        # v1 fits
        v1 = budget_policy.compile_truthful_production_plan(
            script_package=_script_pkg(10), narration_package={}, visual_package=_complete_visual_package(10, tier=2, contingency_per_scene=1),
            channel_id=_CH, episode_id="ep_rev", config=_cfg(), now=NOW, db_path=env.db_path,
        )
        assert v1.cost_model.fits()
        # v2 (many premium tier-1 scenes) is far more expensive -> blocked
        with pytest.raises(budget_policy.ProductionPlanBudgetInfeasibleError) as ei:
            budget_policy.compile_truthful_production_plan(
                script_package=_script_pkg(10), narration_package={}, visual_package=_complete_visual_package(10, tier=1, contingency_per_scene=3),
                channel_id=_CH, episode_id="ep_rev", config=_cfg(), now=NOW, db_path=env.db_path,
            )
        assert ei.value.deficit > 0
        assert ei.value.cost_model.maximum_bounded_completion_cost > v1.cost_model.maximum_bounded_completion_cost

    def test_8_incomplete_historical_episode_is_not_a_complete_55_episode(self):
        """Read-only: the real incomplete Hypogeum episode's recorded
        spend is NOT its full complete-episode cost, and the cost model
        would not describe it as a USD-55 complete episode."""
        import os
        import sqlite3
        prod = os.path.join(os.path.dirname(__file__), "..", ".hypogeum-assets", "hypogeum.db")
        if not os.path.exists(prod):
            pytest.skip("production DB not present in this environment")
        c = sqlite3.connect(f"file:{prod}?mode=ro", uri=True)
        try:
            row = c.execute(
                "SELECT ROUND(SUM(cost_usd),2) FROM costs WHERE episode_id = 'ep_f3619022'"
            ).fetchone()
            incurred = Decimal(str(row[0] or "0"))
            if incurred == 0:
                pytest.skip("historical episode ep_f3619022 not in this DB")
            # its costs were recorded across canary shots + editorial batches,
            # each authorized by its own small manual grant -- not a single
            # complete-episode plan
            grants = c.execute(
                "SELECT COUNT(*) FROM run_authorization_grants WHERE episode_id = 'ep_f3619022'"
            ).fetchone()[0]
        finally:
            c.close()
        assert incurred > Decimal("55.00")   # already OVER 55 while still incomplete
        assert grants >= 2                    # piecemeal authorization, not one plan
        # therefore "USD 55 = a complete episode" is false by evidence


# ===========================================================================
# Scenario 12 — admitted PRE_PLAN calls traverse the gate; legacy fallback = zero network
# ===========================================================================

class TestScenario12NoPaidLegacyFallback:
    def test_admitted_execution_with_no_operating_grant_fails_closed_zero_network(self, env, monkeypatch):
        """An execution that IS admitted but whose Operating Grant does
        not resolve must fail closed in call_claude BEFORE any network
        I/O -- no paid legacy path."""
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        rec = _admit(env)
        # revoke the grant AFTER admission
        conn = db.get_connection(env.db_path)
        try:
            conn.execute("UPDATE run_authorization_grants SET status='revoked' WHERE grant_id='op_g1'")
            conn.commit()
        finally:
            conn.close()

        import hypogeum.openrouter as openrouter
        network_hit = {"n": 0}
        monkeypatch.setattr(openrouter, "_call_openrouter",
                            lambda *a, **k: network_hit.__setitem__("n", network_hit["n"] + 1))

        with pytest.raises(openrouter.HardFailure) as ei:
            openrouter.call_claude(
                messages=[{"role": "user", "content": "hi"}], model_slug="anthropic/claude-sonnet-4",
                max_tokens=10, config=_cfg(), episode_id=rec.episode_id, channel_id=_CH, agent="research",
            )
        assert "operating_grant" in str(ei.value)
        assert network_hit["n"] == 0

    def test_admitted_execution_routes_call_claude_through_the_gate(self, env, monkeypatch):
        _sign_and_register_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        rec = _admit(env)

        import hypogeum.openrouter as openrouter
        seen = {}

        def _fake_via_gate(*, channel_id, grant, **kw):
            seen["channel_id"] = channel_id
            seen["grant_kind"] = grant.grant_kind
            seen["grant_id"] = grant.grant_id
            return SimpleNamespace(content="ok", usage={}, model_used=kw.get("model_slug"),
                                   finish_reason="stop", response_time_ms=1, via_fallback=False,
                                   estimated_cost_usd=0.0)

        monkeypatch.setattr(openrouter, "_call_claude_via_gate", _fake_via_gate)
        monkeypatch.setattr(openrouter, "_call_openrouter",
                            lambda *a, **k: pytest.fail("legacy network path must not be reached"))

        out = openrouter.call_claude(
            messages=[{"role": "user", "content": "hi"}], model_slug="anthropic/claude-sonnet-4",
            max_tokens=10, config=_cfg(), episode_id=rec.episode_id, channel_id=_CH, agent="research",
        )
        assert out.content == "ok"
        assert seen["grant_kind"] == "channel_operating"
        assert seen["grant_id"] == "op_g1"
        assert seen["channel_id"] == _CH


# ===========================================================================
# Extra — no episode due, and isolation guard
# ===========================================================================

class TestIdleAndIsolation:
    def test_no_topic_no_cadence_channel_is_idle(self, env, monkeypatch):
        # operator_enabled but cadence removed
        conn = db.get_connection(env.db_path)
        try:
            conn.execute("UPDATE channel_operations_profiles SET production_cadence_json='{}' WHERE channel_id=?",
                         (_CH,))
            conn.commit()
        finally:
            conn.close()
        r = autoruntime.run_autonomous_cycle(_CH, config=_cfg(), worker_id="w1", now=NOW,
                                             db_path=env.db_path, pipeline_runner=lambda **k: None)
        assert r.state == autoruntime.RuntimeState.IDLE
        assert _reservation_counts(env.db_path) == (0, 0, 0, 0)

    def test_isolation_guard_blocks_prod_db_admission(self):
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            adm.admit_channel_execution(_CH, config=_cfg(), worker_id="w1", log=_Log(), now=NOW)
