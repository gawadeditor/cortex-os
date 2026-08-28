"""
tests/test_pipeline_db_isolation.py -- regression for the 2026-08-28
incident: `run_pipeline(..., db_path=<temp>)` must bind EVERY stateful
write to <temp>, never the process default or the production DB.

Mechanism under test: hypogeum.db.execution_db (contextvar binding) +
hypogeum.db.resolve_db_path, wired into run_pipeline, run_registry,
finance, ois. See `fix: keep pipeline execution bound to its database`.

The process-level tripwire (tests/_prod_tripwire.py, armed in conftest)
independently fails if anything opens the real production DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import hypogeum.db as db
import hypogeum.pipeline as pipeline
from hypogeum.config import Config


class _Boom(RuntimeError):
    pass


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A temp execution DB + a FORBIDDEN default DB that must stay untouched."""
    exec_db = tmp_path / "execution.db"
    db.initialize_schema(exec_db)

    forbidden_default = tmp_path / "FORBIDDEN_default.db"   # must never be created

    # Point the module default at the forbidden path so any fallthrough is
    # visible (the real production path is separately blocked by the tripwire).
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", forbidden_default)
    monkeypatch.setattr("hypogeum.run_registry._DEFAULT_DB_PATH", forbidden_default, raising=False)
    monkeypatch.setattr("hypogeum.finance._DEFAULT_DB_PATH", forbidden_default, raising=False)
    monkeypatch.setattr("hypogeum.ois._DEFAULT_DB_PATH", forbidden_default, raising=False)
    monkeypatch.setattr("hypogeum.channel_config._DEFAULT_DB_PATH", forbidden_default, raising=False)

    # Defeat conftest's autouse run-registry / ois stubs -- this test needs
    # the REAL functions so it can prove WHERE they write.
    import hypogeum.run_registry as _rr
    import hypogeum.finance as _fin
    monkeypatch.setattr(pipeline, "register_run", _rr.register_run)
    monkeypatch.setattr(pipeline, "fail_run", _rr.fail_run)
    monkeypatch.setattr(pipeline, "complete_run", _rr.complete_run)
    monkeypatch.setattr(pipeline, "set_run_context", _fin.set_run_context)
    monkeypatch.setattr(pipeline, "write_episode_rollup", _fin.write_episode_rollup)
    monkeypatch.setattr(pipeline, "clear_run_context", _fin.clear_run_context)

    # A guard on sqlite3.connect that records every path opened.
    opened: list[Path] = []
    real_connect = sqlite3.connect

    def _recording_connect(database, *a, **kw):
        try:
            p = Path(str(database))
            if p.is_absolute():
                opened.append(p.resolve())
        except (OSError, ValueError):
            pass
        return real_connect(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _recording_connect)

    # Fake the channel/workflow the pipeline needs, in the temp DB only.
    from scripts.bootstrap_channel import bootstrap_channel
    from hypogeum import workflow_registry as wr

    bootstrap_channel(
        {
            "channel_id": "hypogeum", "name": "H", "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {
                "episode_producer": "anthropic/claude-sonnet-4.6",
                "research_director": "anthropic/claude-opus-4.6",
                "thumbnail_planner": "openai/gpt-5.5-pro",
                "brand_guardian_p1": "anthropic/claude-sonnet-4.6",
                "brand_guardian_p2": "openai/gpt-5.5-pro",
                "narration_voice_id": "j9jfwdrw7BRfcR43Qohk",
                "image_provider": "muapi", "video_provider": "muapi",
            },
        },
        db_path=exec_db,
    )
    wf_bytes = (Path(__file__).resolve().parent.parent
                / "channel_packages" / "hypogeum" / "workflow"
                / "hypogeum_main_production.json").read_bytes()
    wr.register_workflow("hypogeum", "hypogeum_main_production", "1.0.0", wf_bytes, db_path=exec_db)

    # Healthy startup, no network.
    from hypogeum.startup import StartupReport
    monkeypatch.setattr(
        pipeline, "run_startup_checks",
        lambda *a, **k: StartupReport(
            overall_status="healthy", openrouter_reachable=True, models_verified=True,
            fallback_active=False, fallback_configured=True, checked_at="2026-08-29T00:00:00Z",
        ),
    )

    return dict(exec_db=exec_db, forbidden=forbidden_default, opened=opened, tmp=tmp_path)


def _counts(dbfile: Path) -> dict:
    c = sqlite3.connect(str(dbfile))
    try:
        return {
            t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("runs", "run_state_transitions", "runtime_leases",
                      "signal_records", "budget_reservations", "costs")
        }
    finally:
        c.close()


class TestPipelineDBIsolation:

    def test_failed_run_writes_only_to_the_execution_db(self, isolated, monkeypatch):
        exec_db, forbidden, opened = isolated["exec_db"], isolated["forbidden"], isolated["opened"]

        # Make the first agent blow up -- exercises register_run + a state
        # transition + fail_run, the exact helpers that leaked.
        monkeypatch.setattr(
            "hypogeum.pipeline._run_executive_producer",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("stop here")),
        )

        before_exec = _counts(exec_db)
        with pytest.raises(_Boom):
            pipeline.run_pipeline(
                "The Erdstall Tunnels", "ep_isoltest", Config(),
                channel_id="hypogeum", db_path=exec_db, output_dir=isolated["tmp"] / "ep",
                publishing_platform_filter=frozenset(),
            )
        after_exec = _counts(exec_db)

        # the run + its failure transition landed in the execution DB
        assert after_exec["runs"] == before_exec["runs"] + 1
        assert after_exec["run_state_transitions"] > before_exec["run_state_transitions"]
        assert after_exec["runtime_leases"] >= before_exec["runtime_leases"] + 1

        # the forbidden default DB was NEVER created or opened
        assert not forbidden.exists(), "pipeline fell through to the default DB"
        assert forbidden.resolve() not in opened

        # the binding is cleared after the run
        assert db.current_execution_db() is None

    def test_binding_is_active_during_the_run_and_cleared_after(self, isolated, monkeypatch):
        exec_db = isolated["exec_db"]
        seen = {}

        def _spy_ep(*a, **k):
            seen["bound_during_run"] = db.current_execution_db()
            raise _Boom("stop")

        monkeypatch.setattr("hypogeum.pipeline._run_executive_producer", _spy_ep)
        with pytest.raises(_Boom):
            pipeline.run_pipeline(
                "T", "ep_bind", Config(), channel_id="hypogeum",
                db_path=exec_db, output_dir=isolated["tmp"] / "ep2",
                publishing_platform_filter=frozenset(),
            )
        assert seen["bound_during_run"] == exec_db
        assert db.current_execution_db() is None

    def test_default_db_path_none_is_byte_identical_passthrough(self, isolated, monkeypatch):
        # db_path=None: NO execution binding (current_execution_db() stays
        # None), writes fall through to hypogeum.db._DEFAULT_DB_PATH exactly
        # as before -- the live worker relies on that. Here _DEFAULT_DB_PATH
        # is a second temp DB; prove writes land there, not in the other
        # execution DB, and the run is not left with a dangling binding.
        from scripts.bootstrap_channel import bootstrap_channel
        from hypogeum import workflow_registry as wr

        default_db = isolated["forbidden"]
        db.initialize_schema(default_db)
        bootstrap_channel(
            {"channel_id": "hypogeum", "name": "H", "brand": {"language": "en"},
             "publishing": {"enabled_platforms": ["youtube"]},
             "models": {"episode_producer": "anthropic/claude-sonnet-4.6",
                        "research_director": "anthropic/claude-opus-4.6",
                        "thumbnail_planner": "openai/gpt-5.5-pro",
                        "brand_guardian_p1": "anthropic/claude-sonnet-4.6",
                        "brand_guardian_p2": "openai/gpt-5.5-pro",
                        "narration_voice_id": "j9jfwdrw7BRfcR43Qohk",
                        "image_provider": "muapi", "video_provider": "muapi"}},
            db_path=default_db,
        )
        wf_bytes = (Path(__file__).resolve().parent.parent / "channel_packages"
                    / "hypogeum" / "workflow" / "hypogeum_main_production.json").read_bytes()
        wr.register_workflow("hypogeum", "hypogeum_main_production", "1.0.0", wf_bytes, db_path=default_db)

        captured = {}

        def _spy(*a, **k):
            captured["bound"] = db.current_execution_db()
            raise _Boom("x")

        monkeypatch.setattr("hypogeum.pipeline._run_executive_producer", _spy)
        with pytest.raises(_Boom):
            pipeline.run_pipeline(
                "T", "ep_defnone", Config(), channel_id="hypogeum",
                db_path=None, output_dir=isolated["tmp"] / "ep3",
                publishing_platform_filter=frozenset(),
            )
        assert captured["bound"] is None                 # NO binding for db_path=None
        assert db.current_execution_db() is None
        assert _counts(default_db)["runs"] == 1          # run row in _DEFAULT_DB_PATH
        assert _counts(isolated["exec_db"])["runs"] == 0  # NOT in the other execution DB
