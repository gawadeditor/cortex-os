"""
hypogeum/postiz_webhook.py — receives Postiz webhooks so a post that fails reaches the operator at once.

Postiz can call a URL when something happens to a post. It signs nothing, so the secret is part of the path:

    POST /hooks/postiz/<POSTIZ_WEBHOOK_SECRET>

Every delivery is stored raw in `postiz_webhook_events` (that table is the source of truth for what Postiz
really sends -- its docs do not list the payload). A delivery that looks like a failure opens a Telegram alert
(`pzhook:<post id>`); a later delivery that looks like a success for the same post resolves it.

This is a SEPARATE tiny app (run_webhook_receiver.py), so only this one route is ever exposed to the internet
-- never the dashboard. It answers 200 quickly and never raises: Postiz must not retry because of our bug.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from hypogeum.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS postiz_webhook_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    post_id      TEXT,
    kind         TEXT NOT NULL,          -- failed | published | other
    payload_json TEXT NOT NULL
);
"""
_MAX_BODY = 64 * 1024
_FAIL_WORDS = ("fail", "error", "reject", "refresh_needed", "expired")
_OK_WORDS = ("publish", "success", "posted")


def _walk(obj, depth=0):
    """Yield (key, value) pairs of a nested payload, shallowly bounded."""
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k).lower(), v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:20]:
            yield from _walk(v, depth + 1)


def classify(payload) -> Tuple[str, Optional[str]]:
    """(kind, post_id) from whatever Postiz sent. Failure wins over success words (a 'publish failed' event
    contains both)."""
    post_id = None
    words = []
    for k, v in _walk(payload):
        if post_id is None and k in ("postid", "post_id", "id") and isinstance(v, (str, int)) and str(v):
            post_id = str(v)
        if k in ("event", "type", "state", "status", "error", "errormessage", "message") and v:
            words.append(str(v).lower())
    text = " ".join(words)
    if any(w in text for w in _FAIL_WORDS):
        return "failed", post_id
    if any(w in text for w in _OK_WORDS):
        return "published", post_id
    return "other", post_id


def record_event(payload, db_path=None, config=None, now: Optional[datetime] = None) -> str:
    """Store one delivery and raise/resolve the alert. Returns the kind. Never raises."""
    now = now or datetime.now(timezone.utc)
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
        kind, post_id = classify(payload)
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            conn.executescript(_DDL)
            conn.execute("INSERT INTO postiz_webhook_events (received_at, post_id, kind, payload_json) VALUES (?,?,?,?)",
                         (now.isoformat(), post_id, kind, json.dumps(payload, ensure_ascii=False)[:20000]))
            conn.commit()
        finally:
            conn.close()
        from hypogeum import operator_alerts as oa  # noqa: PLC0415
        key = f"pzhook:{post_id or now.isoformat()}"
        if kind == "failed":
            detail = json.dumps(payload, ensure_ascii=False)[:600]
            oa.alert(key, "Postiz بلّغ بفشل بوست", f"Postiz نفسه بعتلنا إن بوست فشل.\nالتفاصيل: {detail}",
                     db_path=db_path, config=config)
        elif kind == "published" and post_id:
            oa.resolve(key, "البوست اتنشر بعد كده", db_path=db_path, config=config)
        return kind
    except Exception as exc:  # noqa: BLE001
        log.warn("postiz_webhook_record_failed", error=str(exc)[:200])
        return "error"


def create_app(db_path=None, config=None):
    from flask import Flask, request  # noqa: PLC0415
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_BODY

    @app.post("/hooks/postiz/<secret>")
    def _hook(secret: str):
        expected = str(getattr(config, "POSTIZ_WEBHOOK_SECRET", "") or "")
        if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
            return ("", 404)                                 # a wrong path looks like no route at all
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {"raw": request.get_data(as_text=True)[:2000]}
        kind = record_event(payload, db_path=db_path, config=config)
        return ({"ok": True, "kind": kind}, 200)

    return app
