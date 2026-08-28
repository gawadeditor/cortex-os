"""
tests/test_signed_grant_mar_shadow.py

SCOPE (read before citing this file as evidence):

  This is a TEST-KEY INTEGRATION SHADOW of signed-grant admission, domain
  enforcement, truthful planning, and reservation upgrade. It is NOT:
    - a real signed production-grant shadow (the grant here is signed with
      an ephemeral TEST Ed25519 key, in a temp DB);
    - a complete real default-pipeline shadow (the pipeline is a custom
      injected `pipeline_runner`, NOT `hypogeum.autonomous_runtime.
      _default_pipeline_runner` / `hypogeum.pipeline.run_pipeline`);
    - a full fake-provider end-to-end render (no agent, no FFmpeg, no
      provider adapter is exercised by the planning test);
    - proof that every production pipeline stage reached `ready_to_publish`
      (the custom runner returns that status directly).

  What it DOES exercise, with a hard network sentinel and an injected
  deterministic clock:
    - the real `run_autonomous_cycle` admission path;
    - real operating-grant resolution + row-integrity + ceiling ==
      configured-limit;
    - the real `issue_child_authorization` path, with ADR-016 provider
      -domain enforcement (canonical domain present in the signed grant
      AND exact provider/model/operation binding);
    - the real truthful complete-plan compiler on the real Hypogeum
      deliverable contract (ep_f3619022);
    - the real atomic PRE_PLAN -> plan reservation upgrade.

  The real signed-production-grant / real-default-pipeline / fake-provider
  end-to-end shadow is a documented integration gap: the repository has no
  harness that runs `hypogeum.pipeline.run_pipeline` through all ten
  agents with deterministic fake provider adapters. See DECISION_LOG.md
  ADR-016 amendment (2026-08-29).
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.autonomous_runtime as autoruntime
import hypogeum.channel_budget_policy as bp
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.policy_cascade as policy_cascade
import hypogeum.runtime_admission as adm
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.provider_domains import PROVIDER_DOMAINS

_CH = "hypogeum"
_WF_ID = "hypogeum_main_production"
_WF_VER = "1.0.0"
_WF_SUM = "1ad2f646d875afa056a581683c3a4d2a095db17b18d0c0eb206a6a9fe4de2776"
_CP_VER = "v1"
_CP_SUM = "e" * 64
_CADENCE = "videos_per_week:1"
_CEIL = Decimal("135.00")
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)   # ISO 2026-W35


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _cfg(**over):
    base = dict(FINANCE_BUDGET_CEILING_USD=135.0, MUAPI_DEFAULT_TIER=2,
                MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1)
    base.update(over)
    return SimpleNamespace(**base)


class _Shim:
    def __init__(self, base, ov):
        self._b, self._o = base, ov

    def __getattr__(self, n):
        return self._o[n] if n in self._o else getattr(self._b, n)


def _real_cfg(**extra):
    from hypogeum.config import Config
    ov = dict(MUAPI_DEFAULT_TIER=2, MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
              MUAPI_T3_MODEL="veo3.1-lite-text-to-video", MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1,
              THUMBNAIL_IMAGE_MODEL="gpt-image-2-text-to-image", THUMBNAIL_SHORT_CANDIDATE_CAP=3,
              ELEVENLABS_MUSIC_DEFAULT_DURATION=120, FINANCE_BUDGET_CEILING_USD=135.0)
    ov.update(extra)
    return _Shim(Config(), ov)


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "shadow.db"
    db.initialize_schema(dbp)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", dbp)
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())
    monkeypatch.setattr(
        policy_cascade, "get_effective_budget_ceiling",
        lambda env_ceiling=0.0, channel_id=None, factory_id=None, db_path=None: float(env_ceiling),
    )
    channel_config.create_channel_profile(channel_id=_CH, name="Hypogeum", db_path=dbp)
    channel_config.create_channel_operations_profile(
        channel_id=_CH, channel_name="Hypogeum", timezone="UTC", operator_enabled=True,
        production_cadence={"videos_per_week": 1}, weekly_video_budget_usd=135.0, db_path=dbp,
    )
    import hypogeum.production_plan as _pp
    monkeypatch.setattr(_pp, "canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep)
    # ---- hard network sentinel ----
    def _no_net(*a, **k):
        raise AssertionError("NETWORK ACCESS ATTEMPTED IN SHADOW TEST")
    monkeypatch.setattr(socket, "socket", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    return SimpleNamespace(tmp_path=tmp_path, db_path=dbp, priv=priv)


def _sign_operating_grant(priv, db_path, *, domains=PROVIDER_DOMAINS, grant_id="op_shadow"):
    bindings = [
        store.GrantBinding(provider=p, model=m, provider_operation=o, currency="USD",
                           max_cost_per_call=Decimal("3.00"), max_calls=2000, max_total_spend=Decimal("135.00"))
        for (p, m, o) in [
            ("openrouter", "anthropic/claude-sonnet-4.6", "text.completion"),
            ("perplexity", "sonar-pro", "research.query"),
            ("elevenlabs", "eleven_multilingual_v2", "voice.narration_segment"),
            ("elevenlabs", "music_v1", "music.track"),
            ("muapi", "veo3.1-lite-text-to-video", "video.text_to_video"),
            ("muapi", "gpt-image-2-text-to-image", "image.thumbnail"),
            ("muapi", "gpt-image-2-text-to-image", "image.reference"),
            ("muapi", "veo3.1-lite-text-to-video", "upscale.video"),
        ]
    ]
    valid_from = "2026-08-01T00:00:00Z"
    expires_at = "2026-12-01T00:00:00Z"
    period = "hypogeum:shadow"
    checksum = authority.compute_grant_checksum(
        channel_id=_CH, episode_id="*", workflow_id=_WF_ID, workflow_version=_WF_VER,
        workflow_checksum=_WF_SUM, production_plan_content_hash=None, bindings=bindings,
        max_total_spend_all_bindings=_CEIL, currency="USD", expires_at=expires_at,
        grant_kind="channel_operating", channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM,
        allowed_provider_domains=list(domains), cadence_snapshot=_CADENCE,
        weekly_exposure_ceiling_usd=_CEIL, valid_from=valid_from, operating_period_ref=period,
    )
    fields = dict(
        approval_id=f"appr_{grant_id}", approval_version=1, approval_type="channel_operating_grant",
        subject_checksum=checksum, channel_id=_CH, scope_id="*", workflow_id=_WF_ID,
        workflow_version=_WF_VER, workflow_checksum=_WF_SUM, channel_package_version=_CP_VER,
        channel_package_checksum=_CP_SUM,
        allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings),
        currency="USD", max_cost_per_call=Decimal("3.00"), max_total_spend=_CEIL, max_calls=2000,
        retry_authorized=False, fallback_authorized=False, issued_at=valid_from, expires_at=expires_at,
        approver_identity="operator_test", nonce=f"nonce_{grant_id}",
    )
    art = sign_approval_artifact(fields, private_key=priv)
    return authority.register_channel_operating_grant(
        grant_id=grant_id, grant_version=1, channel_id=_CH, workflow_id=_WF_ID,
        workflow_version=_WF_VER, workflow_checksum=_WF_SUM, channel_package_version=_CP_VER,
        channel_package_checksum=_CP_SUM, allowed_provider_domains=list(domains),
        cadence_snapshot=_CADENCE, weekly_exposure_ceiling_usd=_CEIL, bindings=bindings, currency="USD",
        valid_from=valid_from, expires_at=expires_at, operating_period_ref=period,
        approval_artifact=art, db_path=db_path,
    )


def _seed_topic(db_path):
    from hypogeum import topics
    return topics.enqueue_topic(channel_id=_CH, topic_text="The Erdstall Tunnels", db_path=db_path)


def _real_artifacts():
    import os
    d = os.path.join(os.path.dirname(__file__), "..", ".hypogeum-assets", "episodes", "ep_f3619022")
    out = {}
    for n in ("script_package", "narration_package", "visual_package"):
        p = os.path.join(d, f"{n}_ep_f3619022.json")
        if not os.path.exists(p):
            pytest.skip("real ep_f3619022 artifacts not present")
        out[n] = json.load(open(p))
    return out


# PRE_PLAN cost bounds are derived authoritatively by
# hypogeum.preplan_cost_model.compute_preplan_cost_model from the workflow
# definition + signed grant bindings + structural limits -- NOT hardcoded.
# The legacy ~USD 4.73 / ~USD 9.40 estimates are non-authoritative
# historical context only; see tests/test_preplan_cost_model.py.


class TestSignedGrantIntegrationShadow:
    """Test-key integration shadow -- see the module docstring for the
    precise (narrow) evidence boundary this class establishes."""

    def test_signed_grant_admission_domain_enforcement_and_truthful_plan(self, env, monkeypatch):
        grant = _sign_operating_grant(env.priv, env.db_path)
        assert grant.grant_kind == "channel_operating"
        _seed_topic(env.db_path)
        arts = _real_artifacts()

        # public-key verification of the freshly registered grant
        resolved = authority.resolve_active_operating_grant(_CH, at=NOW.isoformat(), db_path=env.db_path)
        assert resolved is not None
        assert resolved.grant_id == grant.grant_id
        assert Decimal(str(resolved.weekly_exposure_ceiling_usd)) == _CEIL
        assert set(resolved.allowed_provider_domains) == set(PROVIDER_DOMAINS)

        seen = {}
        real_cfg = _real_cfg(MUAPI_T1_MODEL="veo3-text-to-video",
                             MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
                             MUAPI_T3_MODEL="veo3.1-lite-text-to-video")

        def _pipeline_runner(*, channel_id, episode_id, config, db_path, admission):
            # CUSTOM injected runner -- this is NOT the real default pipeline.
            # It manually exercises the child-auth + truthful-plan + reservation
            # -upgrade paths so those integrations are covered; it does not run
            # any agent, provider adapter, or FFmpeg, and it returns
            # ready_to_publish directly. See the module docstring.

            # (a) the admission is bound to the test-key SIGNED grant, ceiling == configured
            assert admission.operating_grant_id == grant.grant_id
            assert admission.configured_weekly_limit_usd == "135.00"

            # (b) a domain-enforced child authorization for a PRE_PLAN
            #     (openrouter text) call -- exercised through the real
            #     issue_child_authorization path.
            from hypogeum.execution_envelope import GenerationExecutionEnvelope
            env_ok = GenerationExecutionEnvelope.create(
                envelope_schema_version="1.0", envelope_version=1, channel_id=_CH,
                channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM, execution_id=episode_id,
                workflow_version=_WF_VER, plan_version="live", plan_checksum="c" * 64, planned_shot_id="ep",
                structured_spec_checksum="d" * 64, compiler_identity="pkg", compiler_version="1",
                operation_payload_content="hi", operation_payload_sha256=hashlib.sha256(b"hi").hexdigest(),
                operation_payload_schema_version="text.completion/1.0",
                content_provenance={"compiler": "pkg", "compiler_version": "1",
                                    "source_work_item_id": "ep", "rendered_at": NOW.isoformat()},
                provider="openrouter", model="anthropic/claude-sonnet-4.6", provider_operation="text.completion",
                deterministic_output_path="/tmp/x.json", expected_output_identity="asset_x",
                authorization_id=f"{episode_id}:preplan", authorization_version=1,
                max_cost_per_call=Decimal("0.50"), run_level_budget_ref="run", episode_level_budget_ref=episode_id,
                currency="USD", retry_authorized=False, fallback_authorized=False, idempotency_key=f"{episode_id}:preplan",
                created_at=NOW.isoformat(),
                expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={"channel_package": _CP_SUM, "plan": "c" * 64,
                                      "structured_spec": "d" * 64, "compiler": "e" * 64},
            )
            rec = authority.issue_child_authorization(
                env_ok, grant.grant_id, grant.grant_version, at=NOW.isoformat(), db_path=db_path,
            )
            seen["preplan_auth"] = rec.current_state.value

            # (c) an unknown provider operation must fail closed (no canonical
            #     domain, no binding) -- proves the child-auth path refuses
            #     anything outside the signed grant.
            env_bad = GenerationExecutionEnvelope.create(
                envelope_schema_version="1.0", envelope_version=1, channel_id=_CH,
                channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM, execution_id=episode_id,
                workflow_version=_WF_VER, plan_version="live", plan_checksum="c" * 64, planned_shot_id="ep2",
                structured_spec_checksum="d" * 64, compiler_identity="pkg", compiler_version="1",
                operation_payload_content="x", operation_payload_sha256=hashlib.sha256(b"x").hexdigest(),
                operation_payload_schema_version="video.magic/1.0",
                content_provenance={"compiler": "pkg", "compiler_version": "1",
                                    "source_work_item_id": "ep2", "rendered_at": NOW.isoformat()},
                provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.magic",
                deterministic_output_path="/tmp/y.mp4", expected_output_identity="asset_y",
                authorization_id=f"{episode_id}:bad", authorization_version=1,
                max_cost_per_call=Decimal("0.50"), run_level_budget_ref="run", episode_level_budget_ref=episode_id,
                currency="USD", retry_authorized=False, fallback_authorized=False, idempotency_key=f"{episode_id}:bad",
                created_at=NOW.isoformat(),
                expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={"channel_package": _CP_SUM, "plan": "c" * 64,
                                      "structured_spec": "d" * 64, "compiler": "e" * 64},
            )
            with pytest.raises(authority.AuthorizationAuthorityError):
                authority.issue_child_authorization(
                    env_bad, grant.grant_id, grant.grant_version, at=NOW.isoformat(), db_path=db_path,
                )

            # (c2) a VALID binding cannot be used when its canonical domain is
            #     absent from the signed grant -- the §1 guarantee, exercised
            #     end-to-end here against a domain-stripped sibling grant.
            stripped = [d for d in PROVIDER_DOMAINS if d != "muapi_video"]
            _sign_operating_grant(env.priv, db_path, domains=stripped, grant_id="op_shadow_nodomain")
            env_vid = GenerationExecutionEnvelope.create(
                envelope_schema_version="1.0", envelope_version=1, channel_id=_CH,
                channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM, execution_id=episode_id,
                workflow_version=_WF_VER, plan_version="live", plan_checksum="c" * 64, planned_shot_id="ep3",
                structured_spec_checksum="d" * 64, compiler_identity="pkg", compiler_version="1",
                operation_payload_content="v", operation_payload_sha256=hashlib.sha256(b"v").hexdigest(),
                operation_payload_schema_version="video.text_to_video/1.0",
                content_provenance={"compiler": "pkg", "compiler_version": "1",
                                    "source_work_item_id": "ep3", "rendered_at": NOW.isoformat()},
                provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
                deterministic_output_path="/tmp/z.mp4", expected_output_identity="asset_z",
                authorization_id=f"{episode_id}:vid", authorization_version=1,
                max_cost_per_call=Decimal("0.50"), run_level_budget_ref="run", episode_level_budget_ref=episode_id,
                currency="USD", retry_authorized=False, fallback_authorized=False, idempotency_key=f"{episode_id}:vid",
                created_at=NOW.isoformat(),
                expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={"channel_package": _CP_SUM, "plan": "c" * 64,
                                      "structured_spec": "d" * 64, "compiler": "e" * 64},
            )
            with pytest.raises(authority.GrantScopeMismatchError):
                authority.issue_child_authorization(
                    env_vid, "op_shadow_nodomain", 1, at=NOW.isoformat(), db_path=db_path,
                )

            # (d) real truthful complete plan from the real deliverable contract
            res = bp.compile_truthful_production_plan(
                script_package=arts["script_package"], narration_package=arts["narration_package"],
                visual_package=arts["visual_package"], channel_id=_CH, episode_id=episode_id,
                config=real_cfg, now=NOW, db_path=db_path,
            )
            seen["plan"] = res

            # (e) atomic PRE_PLAN -> plan reservation upgrade (real)
            from hypogeum.preplan_reservation import atomic_upgrade_to_plan_reservation
            import hypogeum.production_plan as _pp
            cdir = _pp.canonical_episode_dir(episode_id)
            cdir.mkdir(parents=True, exist_ok=True)
            _pp.persist_production_plan(res.production_plan, cdir)
            up = atomic_upgrade_to_plan_reservation(channel_id=_CH, episode_id=episode_id,
                                                    config=real_cfg, log=_Log(), db_path=db_path)
            seen["upgrade_reservation_id"] = up.reservation_id
            return SimpleNamespace(status="ready_to_publish", detail="shadow ok")

        # cycle 1: admit
        r1 = autoruntime.run_autonomous_cycle(_CH, config=real_cfg, worker_id="w1", now=NOW,
                                              db_path=env.db_path, pipeline_runner=_pipeline_runner)
        assert r1.state == autoruntime.RuntimeState.ADMITTED
        # cycle 2: run
        r2 = autoruntime.run_autonomous_cycle(_CH, config=real_cfg, worker_id="w1",
                                              now=NOW + timedelta(minutes=5), db_path=env.db_path,
                                              pipeline_runner=_pipeline_runner)
        assert r2.state == autoruntime.RuntimeState.READY_TO_PUBLISH
        assert seen["preplan_auth"] == "authorized"

        res = seen["plan"]
        cm = res.cost_model
        assert cm.configured_weekly_budget_limit == Decimal("135.00")
        assert res.is_alternative_plan is False

        # ---- authoritative cost classification (no hardcoded PRE_PLAN) ----
        #
        # PRE_PLAN bounds come from hypogeum.preplan_cost_model; the complete
        # arithmetic from hypogeum.channel_budget_policy.
        # compute_complete_episode_cost, which nets realized narration out of
        # the POST_PLAN remaining so nothing is double counted.
        from hypogeum.preplan_cost_model import compute_preplan_cost_model
        from hypogeum.authorization_store import GrantBinding as _GB

        _wf = json.loads(
            (__import__("pathlib").Path(__file__).resolve().parent.parent
             / "channel_packages" / "hypogeum" / "workflow"
             / "hypogeum_main_production.json").read_text()
        )
        pm = compute_preplan_cost_model(
            bindings=[_GB(provider=p, model=m, provider_operation=o, currency="USD",
                          max_cost_per_call=c, max_calls=1000, max_total_spend=t)
                      for (p, m, o, c, t) in [
                          ("openrouter", "anthropic/claude-sonnet-4.6", "text.completion", Decimal("0.60"), Decimal("25.00")),
                          ("openrouter", "anthropic/claude-opus-4.6", "text.completion", Decimal("1.50"), Decimal("15.00")),
                          ("perplexity", "sonar-pro", "research.query", Decimal("0.10"), Decimal("5.00")),
                          ("elevenlabs", "eleven_multilingual_v2", "voice.narration_segment", Decimal("0.05"), Decimal("12.00")),
                      ]],
            workflow_definition=_wf,
            script_package=arts["script_package"], narration_package=arts["narration_package"],
        )
        assert pm.coverage_complete is True
        cec = bp.compute_complete_episode_cost(
            _CH, r2.episode_id, preplan_model=pm, production_plan=res.production_plan,
            narration_package=arts["narration_package"], db_path=env.db_path,
        )

        # (1) PRE_PLAN exposure -- authoritative, policy-capped
        assert cec.preplan_maximum_bounded == pm.preplan_policy_capped_maximum_cost
        # (2) POST_PLAN required future reservation -- realized narration netted out
        assert cec.realized_narration_netted_out == Decimal(str(res.production_plan.narration_cost_usd))
        assert cec.postplan_required_future_reservation == (
            Decimal(str(res.production_plan.maximum_bounded_cost_usd)) - cec.realized_narration_netted_out
        )
        # (3) COMPLETE expected = PRE_PLAN expected + POST_PLAN expected
        assert cec.complete_expected_cost == cec.preplan_expected + cec.postplan_expected_remaining
        # (4) COMPLETE maximum bounded = PRE_PLAN max + POST_PLAN max (NOT the POST_PLAN figure alone)
        assert cec.complete_maximum_bounded_cost == (
            cec.preplan_maximum_bounded + cec.postplan_maximum_bounded_remaining
        )
        assert cec.complete_maximum_bounded_cost > cec.postplan_maximum_bounded_remaining
        # (5) final weekly exposure (ledger form) never exceeds the ceiling
        assert cec.final_weekly_exposure <= Decimal("135.00")
        assert cec.ledger_upgrade_ok is True
        # As of 2026-08-29 the authoritative fully-bounded forecast does NOT
        # fit USD 135 (deficit reported, budget NOT adjusted). Documents the
        # current truthful state -- change deliberately, never silently.
        assert cec.forecast_feasible is False
        assert cec.forecast_deficit > Decimal("0")
        # the POST_PLAN reservation itself still fits the future capacity
        assert cm.fits()

        # 130 coverage-required / 23 true contingency (the corrected classification)
        _rc, req_shots, cont_shots = bp.coverage_required_video_cost(
            res.production_plan, arts["narration_package"],
        )
        assert req_shots == 130 and cont_shots == 23
        # all four required deliverables represented
        assert set(res.production_plan.deliverable_ids) >= {"main_episode", "short_001", "short_002", "short_003"}

        # (5) final weekly exposure recorded in the ledger never exceeds the ceiling
        conn = db.get_connection(env.db_path)
        try:
            weekly_reserved = conn.execute(
                "SELECT COALESCE(SUM(CAST(reserved_amount AS REAL)),0) FROM budget_reservations "
                "WHERE scope_kind='channel_weekly' AND status IN ('reserved','committed')"
            ).fetchone()[0]
        finally:
            conn.close()
        assert Decimal(str(weekly_reserved)) <= Decimal("135.00")

    def test_replay_resumes_same_execution_no_duplicates(self, env):
        _sign_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        calls = {"n": 0}

        def _runner(*, channel_id, episode_id, config, db_path, admission):
            calls["n"] += 1
            calls["ep"] = episode_id
            return SimpleNamespace(status="ready_to_publish", detail="ok")

        r1 = autoruntime.run_autonomous_cycle(_CH, config=_cfg(), worker_id="w1", now=NOW,
                                              db_path=env.db_path, pipeline_runner=_runner)
        r2 = autoruntime.run_autonomous_cycle(_CH, config=_cfg(), worker_id="w1",
                                              now=NOW + timedelta(minutes=3), db_path=env.db_path,
                                              pipeline_runner=_runner)
        r3 = autoruntime.run_autonomous_cycle(_CH, config=_cfg(), worker_id="w1",
                                              now=NOW + timedelta(minutes=6), db_path=env.db_path,
                                              pipeline_runner=_runner)
        assert r2.episode_id == r1.episode_id
        assert calls["n"] == 1                       # pipeline invoked exactly once
        conn = db.get_connection(env.db_path)
        try:
            a = conn.execute("SELECT COUNT(*) FROM execution_admissions").fetchone()[0]
            q = conn.execute("SELECT COUNT(*) FROM run_queue").fetchone()[0]
            cw = conn.execute("SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='channel_weekly'").fetchone()[0]
            g = conn.execute("SELECT COUNT(*) FROM run_authorization_grants").fetchone()[0]
        finally:
            conn.close()
        assert a == 1 and q == 1 and g == 1
        assert cw <= 1                               # one PRE_PLAN hold

    def test_generic_submission_gate_restart_idempotency(self, env, tmp_path):
        """GENERIC submission-gate restart idempotency (NOT signed-grant
        specific): a paid operation that has crossed the durable
        provider-I/O entry claim is not silently retried after a simulated
        process crash + restart. The authorization envelope here comes from
        the generic GateHarness, NOT from an admitted execution's signed
        Operating Grant -- this exercises the durable-claim contract that
        the signed-grant path also relies on, at the gate level only.
        (End-to-end binding through admission + child auth + capability +
        persisted execution context is covered by the integration-gap
        follow-up, not here.)"""
        from tests.test_submission_gate import GateHarness, _make_env

        h1 = GateHarness(tmp_path / "gate")
        genv = _make_env(idempotency_key="idem_signed_shadow", authorization_id="auth_signed_shadow")
        h1.acquire_lease(genv)
        first = h1.submit(genv)
        assert first.approved is True
        assert h1.adapter.invocation_count == 1

        # ---- simulate a whole-process crash + restart: brand-new harness
        #      over the SAME db + receipt root; adapter counter starts at 0.
        h2 = GateHarness(tmp_path / "gate")
        assert h2.adapter.invocation_count == 0
        h2.acquire_lease(genv)
        second = h2.submit(genv, attempt_id="attempt_after_restart")
        assert second.approved is False              # recover-or-fail-closed
        assert h2.adapter.invocation_count == 0      # the spent attempt is NOT retried

        events = h2.store.read_events(genv.execution_id, genv.idempotency_key)
        assert any(e.event_type.name == "ACCEPTED" for e in events)

    def test_stale_policy_after_signing_rejects_the_grant(self, env):
        _sign_operating_grant(env.priv, env.db_path)
        _seed_topic(env.db_path)
        # operator changes the configured limit AFTER the grant was signed
        conn = db.get_connection(env.db_path)
        try:
            conn.execute("UPDATE channel_operations_profiles SET weekly_video_budget_usd=90.0 WHERE channel_id=?", (_CH,))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(adm.OperatingGrantStalePolicyError):
            adm.admit_channel_execution(_CH, config=_cfg(), worker_id="w1", log=_Log(),
                                        now=NOW, db_path=env.db_path)
