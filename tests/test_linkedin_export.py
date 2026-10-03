"""
tests/test_linkedin_export.py — LinkedIn creator-analytics Export import.
The fixture workbook mirrors the layout of a real export (2026-10-03):
string cells, two side-by-side TOP POSTS tables, negative daily values,
'< 1%' percentages. tmp DB only.
"""
from __future__ import annotations

import json

import pytest

openpyxl = pytest.importorskip("openpyxl")

from hypogeum import client_manager as cm  # noqa: E402
from hypogeum.db import get_connection, initialize_schema  # noqa: E402
from hypogeum.publishing.client_content_sync import sync_client_content  # noqa: E402
from hypogeum.publishing.linkedin_export import (  # noqa: E402
    LinkedInExportError,
    import_linkedin_export,
    parse_export,
    post_id_from_url,
    title_from_url,
)

U1 = "https://www.linkedin.com/posts/someone_documentaryediting-colorgrading-ugcPost-7498705614441902080-614o"
U2 = "https://www.linkedin.com/posts/someone_%D8%BA%D8%B2%D8%A9-share-7459865451066712064-EYkW"
U3 = "https://www.linkedin.com/posts/someone_the-footage-came-share-7459866966321795072-AxeY"


def _workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DISCOVERY"
    for r in (["Overall Performance", "10/4/2025 - 10/3/2026"], ["Impressions", "2307"], ["Members reached", "1487"]):
        ws.append(r)
    ws = wb.create_sheet("ENGAGEMENT")
    for r in (["Date", "Impressions", "Engagements"], ["10/4/2025", "0", "0"],
              ["10/6/2025", "2", "-2"], ["10/7/2025", "5", "1"]):
        ws.append(r)
    ws = wb.create_sheet("TOP POSTS")
    ws.append(["Maximum of 50 posts available to include in this list"])
    ws.append([])
    ws.append(["Post URL", "Post Publish Date", "Engagements", "", "Post URL", "Post Publish Date", "Impressions"])
    ws.append([U1, "8/27/2026", "57", "", U2, "5/12/2026", "1339"])
    ws.append([U2, "5/12/2026", "4", "", U1, "8/27/2026", "857"])
    ws.append(["", "", "", "", U3, "5/12/2026", "106"])
    ws = wb.create_sheet("FOLLOWERS")
    for r in (["Total followers on 10/3/2026", "2084"], [], ["Date", "New followers"],
              ["10/4/2025", "0"], ["10/5/2025", "1"]):
        ws.append(r)
    ws = wb.create_sheet("AUDIENCE DEMOGRAPHICS")
    for r in (["Top Demographics", "Value", "Percentage"], ["Company", "MBC GROUP", "2%"],
              ["Job title", "Editor", "< 1%"]):
        ws.append(r)
    ws = wb.create_sheet("CONTENT DEMOGRAPHICS")
    ws.append(["Top Demographics", "Value", "Percentage"])
    ws.append(["Company", "Sky News Arabia", "3%"])
    wb.save(path)
    return path


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "li.db"
    initialize_schema(p)
    return p


def test_parse_reads_every_sheet(tmp_path):
    d = parse_export(_workbook(tmp_path / "x.xlsx"))
    assert (d["period_start"], d["period_end"]) == ("2025-10-04", "2026-10-03")
    assert (d["impressions"], d["members_reached"], d["followers_total"]) == (2307, 1487, 2084)
    assert d["handle"] == "someone"
    posts = {p["post_id"]: p for p in d["posts"]}
    assert posts["ugcPost:7498705614441902080"] == {
        "url": U1, "post_id": "ugcPost:7498705614441902080", "published": "2026-08-27",
        "title": "documentaryediting colorgrading", "engagements": 57, "impressions": 857}
    assert posts["share:7459865451066712064"]["title"] == "غزة"            # percent-decoded slug
    assert "engagements" not in posts["share:7459866966321795072"]          # only in the impressions table
    daily = {x["date"]: x for x in d["daily"]}
    assert daily["2025-10-06"]["engagements"] == -2                         # LinkedIn corrections kept raw
    assert daily["2025-10-05"]["new_followers"] == 1
    assert d["audience_demographics"][1] == {"category": "Job title", "value": "Editor", "pct": 0.5, "pct_raw": "< 1%"}


def test_rejects_a_non_linkedin_workbook(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    p = tmp_path / "other.xlsx"
    wb.save(p)
    with pytest.raises(LinkedInExportError, match="missing sheets"):
        parse_export(p)


def test_url_helpers():
    assert post_id_from_url(U1) == "ugcPost:7498705614441902080"
    assert post_id_from_url("https://example.com") is None
    assert title_from_url(U3) == "the footage came"


def test_import_stores_posts_metrics_and_snapshot_idempotently(db_path, tmp_path):
    cm.create_client("Me", client_id="me", db_path=db_path)
    f = _workbook(tmp_path / "x.xlsx")
    s = import_linkedin_export("me", f, db_path=db_path)
    assert s["posts"] == 3 and s["posts_with_metrics"] == 3 and s["followers_total"] == 2084
    import_linkedin_export("me", f, db_path=db_path)  # same file again
    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM content_items WHERE platform='linkedin'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM client_linkedin_snapshots").fetchone()[0] == 1
        row = conn.execute("SELECT impressions, engagement_rate FROM platform_analytics "
                           "WHERE platform='linkedin' AND impressions=857").fetchone()
        assert row["engagement_rate"] == 6.65          # 57/857 as a PERCENT
        snap = conn.execute("SELECT * FROM client_linkedin_snapshots").fetchone()
        assert json.loads(snap["audience_demographics_json"])[0]["value"] == "MBC GROUP"
        acct = conn.execute("SELECT provider_id, handle FROM managed_accounts WHERE platform='linkedin'").fetchone()
        assert (acct["provider_id"], acct["handle"]) == ("linkedin_export", "someone")
    finally:
        conn.close()


def test_hourly_sync_never_sends_a_linkedin_account_to_zernio(db_path, tmp_path):
    cm.create_client("Me", client_id="me", db_path=db_path)
    import_linkedin_export("me", _workbook(tmp_path / "x.xlsx"), db_path=db_path)

    class NoZernio:
        def __getattr__(self, name):
            raise AssertionError(f"Zernio called: {name}")

    out = sync_client_content("me", provider=NoZernio(), db_path=db_path)
    assert out["errors"] == {}


def test_dashboard_upload_imports_and_reports(tmp_path):
    import io
    import run_dashboard as rd
    db = tmp_path / "dash.db"
    initialize_schema(db)
    cm.create_client("Me", client_id="me", db_path=db)
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(db)
    rd.app.config["TESTING"] = True
    try:
        cl = rd.app.test_client()
        assert b"LinkedIn analytics" in cl.get("/client/me/accounts").data
        data = _workbook(tmp_path / "x.xlsx").read_bytes()
        r = cl.post("/client/me/accounts/linkedin_import",
                    data={"file": (io.BytesIO(data), "AggregateAnalytics_Me.xlsx")},
                    content_type="multipart/form-data")
        assert r.status_code == 302 and "LinkedIn imported: 3 posts" in r.headers["Location"].replace("%20", " ")
        bad = cl.post("/client/me/accounts/linkedin_import",
                      data={"file": (io.BytesIO(b"not excel"), "x.xlsx")}, content_type="multipart/form-data")
        assert "isn" in bad.headers["Location"]       # friendly error, no 500
        assert cl.get("/client/me/content").status_code == 200
    finally:
        rd.app.config["DB_PATH"] = old
