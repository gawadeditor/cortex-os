"""
hypogeum/self_heal.py — the factory tries to FIX a problem before it bothers the operator.

The operator alerts (hypogeum.operator_alerts) detect a problem; until now they only said so. This
module sits between detection and notification:

    detected problem (an alert key)
        └─ is there a remediation for it?
              yes ─ attempt it (bounded: max attempts, min interval, give-up) and WAIT -- the alert is
                    deferred while attempts are left; the next detection tick shows whether it worked
              no  ─ alert straight away

    * the problem clears after a remediation acted -> one message "اتصلّح لوحده" saying what was done
    * the problem clears by itself                  -> nothing is sent (the alert was never opened)
    * attempts exhausted / the remediation gives up -> the normal alert, now with what was tried

Every incident is kept in `self_heal_incidents` (history of attempts and results), and listed in the daily
summary. Remediations live in hypogeum.remediations and register themselves here; each one is idempotent,
bounded and never touches money (budget / credits / grants are never changed by a remediation).

    handle_detected(key, db_path, now) -> (verdict, note)    verdict: "defer" | "alert"
    handle_cleared(key, db_path, now)  -> message | None
    healing_keys(prefix, db_path)      -> {incident keys still being healed}
    recent_incidents(db_path, since)   -> [dict]
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_DDL = """
CREATE TABLE IF NOT EXISTS self_heal_incidents (
    incident_key     TEXT NOT NULL PRIMARY KEY,
    remediation      TEXT,
    status           TEXT NOT NULL DEFAULT 'healing',   -- healing | escalated | resolved
    detected_at      TEXT NOT NULL,
    last_attempt_at  TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    acted            INTEGER NOT NULL DEFAULT 0,        -- did any attempt actually change something
    history_json     TEXT NOT NULL DEFAULT '[]',
    resolved_at      TEXT
);
"""


@dataclass
class Outcome:
    """What one remediation attempt did. `acted` = it changed something; `give_up` = it cannot help
    (escalate to the operator now, with `summary` as the reason)."""
    summary: str
    acted: bool = False
    give_up: bool = False


@dataclass
class Ctx:
    key: str
    db_path: Optional[Path]
    now: datetime
    attempt: int
    config: object = None


@dataclass
class Remediation:
    name: str
    matches: Callable[[str], bool]
    run: Callable[[Ctx], Outcome]
    max_attempts: int = 2
    min_interval_s: int = 300


_REGISTRY: List[Remediation] = []
_LOADED = False


def register(rem: Remediation) -> Remediation:
    if all(r.name != rem.name for r in _REGISTRY):
        _REGISTRY.append(rem)
    return rem


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        import hypogeum.remediations  # noqa: F401,PLC0415  (registers itself)
    except Exception:  # noqa: BLE001 — no remediations must never stop the alerts
        pass


def find_remediation(key: str) -> Optional[Remediation]:
    _load()
    for r in _REGISTRY:
        try:
            if r.matches(key):
                return r
        except Exception:  # noqa: BLE001
            continue
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn(db_path):
    from hypogeum.db import get_connection  # noqa: PLC0415
    c = get_connection(Path(db_path) if db_path else None)
    c.execute("PRAGMA busy_timeout = 15000")
    c.executescript(_DDL)
    return c


def _parse(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _history_note(history: list) -> str:
    if not history:
        return ""
    lines = [f"• {h.get('summary', '')}" for h in history[-4:]]
    return "\nالمصنع حاول يحلها بنفسه:\n" + "\n".join(lines)


def handle_detected(key: str, db_path=None, now: Optional[datetime] = None, config=None) -> Tuple[str, str]:
    """A problem is present this tick. Returns ("defer", "") while a remediation still has attempts left,
    otherwise ("alert", note) where `note` lists what was tried. Never raises."""
    now = now or _now()
    if not getattr(config, "SELF_HEAL_ENABLED", False):
        return "alert", ""          # off (or no real config, e.g. a unit test): plain alerts only
    try:
        rem = find_remediation(key)
        if rem is None:
            return "alert", ""
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT * FROM self_heal_incidents WHERE incident_key = ?", (key,)).fetchone()
            if row is None or row["status"] == "resolved":
                conn.execute(
                    "INSERT OR REPLACE INTO self_heal_incidents (incident_key, remediation, status, detected_at, attempts, acted, "
                    "history_json) VALUES (?,?,?,?,0,0,'[]')", (key, rem.name, "healing", now.isoformat()))
                conn.commit()
                row = conn.execute("SELECT * FROM self_heal_incidents WHERE incident_key = ?", (key,)).fetchone()
            history = json.loads(row["history_json"] or "[]")
            if row["status"] == "escalated" or row["attempts"] >= rem.max_attempts:
                _set(conn, key, status="escalated")
                return "alert", _history_note(history)
            last = _parse(row["last_attempt_at"])
            if last and (now - last).total_seconds() < rem.min_interval_s:
                return "defer", ""                              # an attempt is still settling
            try:
                out = rem.run(Ctx(key=key, db_path=db_path, now=now, attempt=row["attempts"] + 1, config=config))
            except Exception as exc:  # noqa: BLE001
                out = Outcome(summary=f"{rem.name} فشلت: {str(exc)[:120]}", acted=False)
            history.append({"at": now.isoformat(), "remediation": rem.name, "summary": out.summary, "acted": out.acted})
            _set(conn, key, attempts=row["attempts"] + 1, last_attempt_at=now.isoformat(),
                 acted=1 if (out.acted or row["acted"]) else 0, history_json=json.dumps(history, ensure_ascii=False),
                 status="escalated" if out.give_up else "healing")
            if out.give_up:
                return "alert", _history_note(history)
            return "defer", ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — healing is best-effort: an engine problem means a plain alert
        return "alert", ""


def _set(conn, key: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE self_heal_incidents SET {sets} WHERE incident_key = ?", (*fields.values(), key))
    conn.commit()


def handle_cleared(key: str, db_path=None, now: Optional[datetime] = None) -> Optional[str]:
    """The problem is gone. Closes the incident; returns the "fixed by itself" message when a remediation
    actually changed something while the incident was healing, else None."""
    now = now or _now()
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT * FROM self_heal_incidents WHERE incident_key = ?", (key,)).fetchone()
            if row is None or row["status"] == "resolved":
                return None
            was_healing = row["status"] == "healing"
            _set(conn, key, status="resolved", resolved_at=now.isoformat())
            if was_healing and row["acted"]:
                history = [h for h in json.loads(row["history_json"] or "[]") if h.get("acted")]
                what = "\n".join(f"• {h['summary']}" for h in history[-3:])
                return f"🛠 المصنع حل المشكلة بنفسه ({key})\n{what}"
            return None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def healing_keys(prefix: str = "", db_path=None) -> set:
    try:
        conn = _conn(db_path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT incident_key FROM self_heal_incidents WHERE status = 'healing' AND incident_key LIKE ?",
                (prefix + "%",))}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return set()


def recent_incidents(db_path=None, since: Optional[datetime] = None) -> List[dict]:
    """Incidents that were detected (or resolved) since `since`, newest first."""
    try:
        conn = _conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM self_heal_incidents ORDER BY detected_at DESC")]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if since and (_parse(r["resolved_at"] or r["last_attempt_at"] or r["detected_at"]) or since) < since:
            continue
        r["history"] = json.loads(r.pop("history_json") or "[]")
        out.append(r)
    return out
