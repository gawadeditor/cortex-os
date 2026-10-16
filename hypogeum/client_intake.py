"""
hypogeum/client_intake.py — the factory finds out WHO a client is by itself, before it writes a word for them.

Sources it reads on its own (no one copies anything by hand):
  * the LinkedIn data archive (.zip) the operator downloads from LinkedIn once a month: Profile, Positions, Skills,
    Honors, Projects, Certifications, Education and the client's own past posts (Shares) — read in memory, never unpacked;
  * the text notes (.txt / .md) in the client's own assets folder (brand_profile.assets_folder).
An LLM turns them into short, sourced facts (brand_profile.verified_facts); every draft is later checked against them
(brand_strategy_agent.check_draft). A client whose facts are missing or older than STALE_DAYS gets no new drafts and the
operator is told to upload a fresh archive. LinkedIn offers no API for profile content, and scraping the page is against its
terms — so the official archive is the one way in.

    save_archive(client_id, stream, db_path)    -> Path          validate + store the zip
    analyze(client_id, db_path, llm, force)     -> dict          read sources -> facts (skips when nothing changed)
    status(client_id, db_path, now)             -> dict          what the factory knows, from where, how fresh
    start_client_intake_loop(db_path)
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="client_intake")

STALE_DAYS = 35                 # a month of data, plus slack for the monthly download
MAX_ZIP_BYTES = 80 * 1024 * 1024
MAX_MEMBER_BYTES = 6 * 1024 * 1024
MAX_POSTS = 60
POST_CHARS = 700
MAX_SOURCE_CHARS = 60000
_ARCHIVE_FILES = ("profile.csv", "positions.csv", "skills.csv", "honors.csv", "projects.csv", "certifications.csv",
                  "education.csv", "languages.csv", "shares.csv")


class IntakeError(ValueError):
    """The upload cannot be used; the message is plain language for the operator."""


def _p(db_path):
    return Path(db_path) if db_path else None


def _root(client_id: str, db_path=None) -> Path:
    from hypogeum.db import _DEFAULT_DB_PATH  # noqa: PLC0415
    base = _p(db_path).parent if db_path else Path(_DEFAULT_DB_PATH).parent
    return base / "client_intake" / "".join(c for c in client_id if c.isalnum() or c in "_-")


def archive_path(client_id: str, db_path=None) -> Path:
    return _root(client_id, db_path) / "linkedin_archive.zip"


def save_archive(client_id: str, stream, db_path=None) -> Path:
    """Store the operator's LinkedIn data archive after checking it is a real zip that holds LinkedIn's profile files."""
    dest = archive_path(client_id, db_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    size = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ZIP_BYTES:
                    raise IntakeError(f"That file is too large (limit {MAX_ZIP_BYTES // (1024 * 1024)} MB).")
                out.write(chunk)
        if not zipfile.is_zipfile(tmp):
            raise IntakeError("That is not a .zip file. Use the archive LinkedIn emails you (Settings → Data privacy → Get a copy of your data).")
        with zipfile.ZipFile(tmp) as z:
            names = {Path(n).name.lower() for n in z.namelist()}
        if not names & {"profile.csv", "positions.csv", "shares.csv"}:
            raise IntakeError("This zip has no LinkedIn profile files (Profile.csv, Positions.csv or Shares.csv). Pick the profile/posts data when you request it.")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _csv_rows(raw: bytes) -> list:
    text = raw.decode("utf-8-sig", errors="replace")
    # LinkedIn puts a few "Notes:" lines above the header in some files: start at the first line that has a comma-separated header
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.count(",") >= 1 and not ln.lower().startswith("notes")), 0)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def _fmt(row: dict, keys: tuple) -> str:
    return " | ".join(f"{k}: {str(row.get(k) or '').strip()}" for k in keys if str(row.get(k) or "").strip())


def _read_archive(path: Path) -> dict:
    out = {}
    with zipfile.ZipFile(path) as z:
        members = {Path(i.filename).name.lower(): i for i in z.infolist() if not i.is_dir()}
        for name in _ARCHIVE_FILES:
            info = members.get(name)
            if info is None or info.file_size > MAX_MEMBER_BYTES:
                continue
            rows = _csv_rows(z.read(info))
            if name == "profile.csv":
                text = "\n".join(_fmt(r, ("Headline", "Summary", "Industry", "Geo Location")) for r in rows[:1])
            elif name == "positions.csv":
                text = "\n".join(_fmt(r, ("Title", "Company Name", "Started On", "Finished On", "Description")) for r in rows[:25])
            elif name == "shares.csv":
                text = "\n\n".join(f"[{r.get('Date', '')}] {(r.get('ShareCommentary') or '').strip()[:POST_CHARS]}"
                                   for r in sorted(rows, key=lambda r: r.get("Date", ""), reverse=True)[:MAX_POSTS]
                                   if (r.get("ShareCommentary") or "").strip())
            else:
                text = "\n".join(_fmt(r, tuple(r.keys())) for r in rows[:40])
            if text.strip():
                out[f"linkedin_archive/{name}"] = text
    return out


def _read_notes(folder: Optional[str]) -> dict:
    out = {}
    if not folder:
        return out
    root = Path(folder)
    try:
        for f in sorted(root.iterdir())[:40]:
            if f.is_file() and f.suffix.lower() in (".txt", ".md") and f.stat().st_size < 200_000:
                out[f"folder/{f.name}"] = f.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        pass                                                  # folder missing / no permission: simply no notes
    return out


def read_sources(client_id: str, profile: dict, db_path=None) -> dict:
    src = {}
    arc = archive_path(client_id, db_path)
    if arc.is_file():
        try:
            src.update(_read_archive(arc))
        except (zipfile.BadZipFile, OSError):
            log.warn("client_intake_archive_unreadable", client_id=client_id)
    src.update(_read_notes(profile.get("assets_folder")))
    return src


def _digest(src: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(src):
        h.update(k.encode())
        h.update(src[k].encode())
    return h.hexdigest()[:16]


def analyze(client_id: str, *, db_path=None, llm: Optional[Callable] = None, force: bool = False) -> dict:
    """Read every source, ask the model for sourced facts, store them. Returns {state, facts, sources}.
    state: 'updated' | 'unchanged' | 'no_sources'."""
    from hypogeum import client_manager as cm  # noqa: PLC0415
    from hypogeum.agents import brand_strategy_agent as bsa  # noqa: PLC0415
    from hypogeum.prompt_loader import load_prompt  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path))
    if not client:
        raise IntakeError(f"no client {client_id!r}")
    brief = dict(client.get("brief") or {})
    prof = dict(brief.get("brand_profile") or {})
    src = read_sources(client_id, prof, db_path)
    if not src:
        return {"state": "no_sources", "facts": len(prof.get("verified_facts") or []), "sources": []}
    digest = _digest(src)
    if not force and prof.get("facts_digest") == digest:
        prof["facts_checked_at"] = datetime.now(timezone.utc).isoformat()        # looked again, nothing new
        brief["brand_profile"] = prof
        cm.update_client(client_id, brief=brief, db_path=_p(db_path))
        return {"state": "unchanged", "facts": len(prof.get("verified_facts") or []), "sources": sorted(src)}
    llm = llm or bsa._default_llm()
    body = ""
    for k in sorted(src):
        body += f"\n=== SOURCE: {k} ===\n{src[k]}\n"
    raw, cost = llm(load_prompt(Path(__file__).resolve().parent / "prompts" / "client_intake.txt"), "Input:" + body[:MAX_SOURCE_CHARS],
                    3000, agent="client_intake", client_id=client_id)
    data = bsa._parse(raw)
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict) and str(f.get("text") or "").strip()]
    if not facts:
        raise IntakeError("the analysis returned no facts; nothing was changed")
    if prof.get("verified_facts"):
        prof["verified_facts_previous"] = prof["verified_facts"]
    prof["verified_facts"] = [str(f["text"]).strip()[:400] for f in facts][:40]
    prof["verified_facts_sources"] = [str(f.get("source") or "")[:80] for f in facts][:40]
    prof["facts_source"] = "read by the factory from: " + ", ".join(sorted(src))
    prof["facts_digest"] = digest
    prof["facts_refreshed_at"] = prof["facts_checked_at"] = datetime.now(timezone.utc).isoformat()
    prof["facts_required"] = True
    brief["brand_profile"] = prof
    cm.update_client(client_id, brief=brief, db_path=_p(db_path))
    log.info("client_intake_updated", client_id=client_id, facts=len(prof["verified_facts"]), cost_usd=cost)
    return {"state": "updated", "facts": len(prof["verified_facts"]), "sources": sorted(src), "cost": cost}


def age_days(prof: dict, now: Optional[datetime] = None) -> Optional[float]:
    stamp = prof.get("facts_refreshed_at")
    if not stamp:
        return None
    return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp)).total_seconds() / 86400


def facts_ready(prof: dict, now: Optional[datetime] = None) -> Optional[str]:
    """None when drafting may go on; otherwise the plain reason it may not. Only clients the factory has analysed are held."""
    if not prof.get("facts_required"):
        return None
    if not prof.get("verified_facts"):
        return "the factory has not read who this person is yet — upload the LinkedIn data archive"
    age = age_days(prof, now)
    if age is None or age > STALE_DAYS + 25:
        return "what the factory knows about this person is older than two months — upload a fresh LinkedIn data archive"
    return None


def status(client_id: str, *, db_path=None, now: Optional[datetime] = None) -> dict:
    from hypogeum import client_manager as cm  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path)) or {}
    prof = (client.get("brief") or {}).get("brand_profile") or {}
    age = age_days(prof, now)
    return {"facts": list(prof.get("verified_facts") or []), "refreshed_at": prof.get("facts_refreshed_at"),
            "source": prof.get("facts_source") or "", "age_days": None if age is None else int(age),
            "stale": age is None or age > STALE_DAYS, "has_archive": archive_path(client_id, db_path).is_file(),
            "required": bool(prof.get("facts_required"))}


def analyze_in_background(client_id: str, db_path=None) -> threading.Thread:
    def _run():
        try:
            analyze(client_id, db_path=db_path, force=True)
        except Exception as exc:  # noqa: BLE001
            log.warn("client_intake_failed", client_id=client_id, error=str(exc)[:200])
    t = threading.Thread(target=_run, name=f"intake-{client_id}", daemon=True)
    t.start()
    return t


def start_client_intake_loop(db_path=None, interval_s: int = 6 * 3600) -> threading.Thread:
    """Every few hours: any client whose sources changed (a new archive, an edited note) is re-read."""
    stop = threading.Event()

    def _loop():
        stop.wait(120)
        while not stop.is_set():
            try:
                from hypogeum import client_manager as cm  # noqa: PLC0415
                for c in cm.list_clients(db_path=_p(db_path)):
                    if (c.get("status") or "active") != "active" or not (c.get("brief") or {}).get("brand_strategy"):
                        continue
                    try:
                        analyze(c["client_id"], db_path=db_path)
                    except Exception as exc:  # noqa: BLE001 — one client's trouble must not stop the others
                        log.warn("client_intake_failed", client_id=c["client_id"], error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                log.warn("client_intake_loop_failed", error=str(exc)[:200])
            stop.wait(interval_s)

    t = threading.Thread(target=_loop, name="client-intake", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("client_intake_loop_started")
    return t
