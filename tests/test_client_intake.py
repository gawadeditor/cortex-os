"""tests/test_client_intake.py — the factory reads who a client is (LinkedIn archive + notes) before it writes for them."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import hypogeum.db as db
from hypogeum import client_intake as ci
from hypogeum import client_manager as cm
from hypogeum import operator_alerts as oa
from hypogeum.agents import brand_strategy_agent as bsa

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _zip(files: dict) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    buf.seek(0)
    return buf


ARCHIVE = {
    "Profile.csv": "First Name,Last Name,Headline,Summary,Industry,Geo Location\nMo,G,Producer & Filmmaker,15+ years across broadcast and documentary,Media,Abu Dhabi\n",
    "Positions.csv": "Company Name,Title,Description,Location,Started On,Finished On\nChannel X,Senior Editor,Edited investigative films,Dubai,May 2025,Aug 2026\n",
    "Shares.csv": "Date,ShareLink,ShareCommentary,SharedUrl,MediaUrl,Visibility\n2026-08-27,u,Some stories are edited to bear witness.,,,\n",
    "Connections.csv": "First Name\nSECRET PERSON\n",
}


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "in.db"
    db.initialize_schema(p)
    cm.create_client("Me", client_id="me", brief={"brand_strategy": {"version": 1}, "brand_profile": {"goal": "x"}}, db_path=p)
    return p


def llm(system, user, max_tokens, *, agent, client_id):
    llm.seen = user
    return json.dumps({"facts": [{"text": "Senior Editor at Channel X from May 2025 to Aug 2026", "source": "linkedin_archive/positions.csv"},
                                 {"text": "15+ years across broadcast and documentary", "source": "linkedin_archive/profile.csv"}]}), 0.01


def test_archive_is_validated_and_only_the_useful_files_are_read(env):
    with pytest.raises(ci.IntakeError):
        ci.save_archive("me", io.BytesIO(b"not a zip"), db_path=env)
    with pytest.raises(ci.IntakeError):
        ci.save_archive("me", _zip({"random.txt": "x"}), db_path=env)
    ci.save_archive("me", _zip(ARCHIVE), db_path=env)
    r = ci.analyze("me", db_path=env, llm=llm)
    assert r["state"] == "updated" and r["facts"] == 2
    assert "SECRET PERSON" not in llm.seen and "Senior Editor" in llm.seen and "bear witness" in llm.seen     # connections never read
    prof = cm.get_client("me", db_path=env)["brief"]["brand_profile"]
    assert prof["verified_facts"][0].startswith("Senior Editor") and prof["facts_required"] is True


def test_nothing_changed_means_no_new_model_call(env):
    ci.save_archive("me", _zip(ARCHIVE), db_path=env)
    ci.analyze("me", db_path=env, llm=llm)
    boom = lambda *a, **k: pytest.fail("must not call the model again")
    assert ci.analyze("me", db_path=env, llm=boom)["state"] == "unchanged"
    assert ci.analyze("me", db_path=env, llm=llm, force=True)["state"] == "updated"


def test_no_sources_is_reported_not_invented(env):
    assert ci.analyze("me", db_path=env, llm=lambda *a, **k: pytest.fail("no sources, no call"))["state"] == "no_sources"


def test_notes_in_the_assets_folder_are_read(env, tmp_path):
    folder = tmp_path / "portfolio"
    folder.mkdir()
    (folder / "cv.md").write_text("Colorist on a Telly-winning film.")
    c = cm.get_client("me", db_path=env)
    b = c["brief"]
    b["brand_profile"]["assets_folder"] = str(folder)
    cm.update_client("me", brief=b, db_path=env)
    ci.analyze("me", db_path=env, llm=llm)
    assert "Telly-winning" in llm.seen


def test_drafting_is_held_until_the_factory_has_fresh_facts(env):
    assert ci.facts_ready({}) is None                                          # a client the factory never analysed is not blocked
    assert "not read" in ci.facts_ready({"facts_required": True})
    fresh = {"facts_required": True, "verified_facts": ["x"], "facts_refreshed_at": (NOW - timedelta(days=10)).isoformat()}
    assert ci.facts_ready(fresh, NOW) is None
    old = dict(fresh, facts_refreshed_at=(NOW - timedelta(days=70)).isoformat())
    assert "older than two months" in ci.facts_ready(old, NOW)
    b = cm.get_client("me", db_path=env)["brief"]
    b["brand_profile"].update(facts_required=True)
    cm.update_client("me", brief=b, db_path=env)
    with pytest.raises(bsa.BrandGenerationError, match="no new drafts"):
        bsa.draft_next_posts("me", db_path=env, llm=lambda *a, **k: pytest.fail("must not draft"))


def test_alert_asks_for_a_fresh_archive_when_the_facts_are_a_month_old(env):
    assert oa.check_client_intake(env, NOW) == {}                              # not analysed yet: nothing to refresh
    b = cm.get_client("me", db_path=env)["brief"]
    b["brand_profile"].update(facts_required=True, facts_refreshed_at=(NOW - timedelta(days=40)).isoformat(), verified_facts=["x"])
    cm.update_client("me", brief=b, db_path=env)
    got = oa.check_client_intake(env, NOW)
    assert "clientintake:me" in got and "/client/me/accounts" in got["clientintake:me"][1]


def test_dashboard_accepts_the_zip_and_shows_what_it_knows(env):
    import run_dashboard as rd
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(env)
    rd.app.config["TESTING"] = True
    try:
        cl = rd.app.test_client()
        assert b"has not read who you are yet" in cl.get("/client/me/accounts").data
        bad = cl.post("/client/me/accounts/linkedin_import", data={"file": (io.BytesIO(b"nope"), "x.zip")}, content_type="multipart/form-data")
        assert bad.status_code == 302 and "not" in bad.headers["Location"].replace("%20", " ")
        ci.save_archive("me", _zip(ARCHIVE), db_path=env)
        ci.analyze("me", db_path=env, llm=llm)
        page = cl.get("/client/me/accounts").data
        assert b"2 facts" in page and b"Senior Editor at Channel X" in page
    finally:
        rd.app.config["DB_PATH"] = old


def test_a_broken_json_answer_is_asked_again_then_fails_without_changing_anything(env):
    ci.save_archive("me", _zip(ARCHIVE), db_path=env)
    calls = []

    def flaky(system, user, max_tokens, *, agent, client_id):
        calls.append(user)
        return ('{"facts": [{"text": "He said "hi" once", "source": "x"}]}', 0.01) if len(calls) == 1 else llm(system, user, max_tokens, agent=agent, client_id=client_id)
    assert ci.analyze("me", db_path=env, llm=flaky)["state"] == "updated" and len(calls) == 2 and "single quotes" in calls[1]
    ci.save_archive("me", _zip(ARCHIVE), db_path=env)
    bad = lambda *a, **k: ("not json at all", 0.0)
    before = cm.get_client("me", db_path=env)["brief"]["brand_profile"]["verified_facts"]
    with pytest.raises(ci.IntakeError):
        ci.analyze("me", db_path=env, llm=bad, force=True)
    assert cm.get_client("me", db_path=env)["brief"]["brand_profile"]["verified_facts"] == before
