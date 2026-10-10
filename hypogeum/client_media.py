"""
hypogeum/client_media.py — the material (image / video) the operator supplies for a client's drafted post.

Instagram cannot publish text alone, and the material is the operator's own work, so it arrives as an upload
from the plan page. Everything is validated before it is kept: extension AND the file's own signature must
agree, size is capped, and the stored name is generated (the client's filename is never used as a path).

    save_post_media(client_id, post_date, filename, stream, *, db_path=None, root=None) -> dict   {path, kind, name, size, mime}
    media_issue(media) -> str | None      why a stored media can no longer be used (missing, changed size)
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

MAX_IMAGE = 8 * 1024 * 1024
MAX_VIDEO = 100 * 1024 * 1024            # the dashboard's tunnel body limit; Instagram itself allows more
_KINDS = {".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"), ".png": ("image", "image/png"),
          ".mp4": ("video", "video/mp4"), ".mov": ("video", "video/quicktime")}


class MediaRejected(ValueError):
    """The upload is not usable; the message is plain language for the operator."""


def _root(root=None) -> Path:
    if root is not None:
        return Path(root)
    from hypogeum.db import _DEFAULT_DB_PATH  # noqa: PLC0415
    return Path(_DEFAULT_DB_PATH).parent / "client_media"


def _signature_ok(ext: str, head: bytes) -> bool:
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".mp4", ".mov"):
        return head[4:8] == b"ftyp"
    return False


def save_post_media(client_id: str, post_date: str, filename: str, stream, *, db_path=None, root=None) -> dict:
    ext = Path(filename or "").suffix.lower()
    if ext not in _KINDS:
        raise MediaRejected("Use a JPG, PNG, MP4 or MOV file.")
    kind, mime = _KINDS[ext]
    limit = MAX_IMAGE if kind == "image" else MAX_VIDEO
    if not client_id.replace("_", "").isalnum() or len(post_date) != 10:
        raise MediaRejected("Unknown post.")
    folder = _root(root) / client_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{post_date}_{secrets.token_hex(6)}{ext}"
    size, head = 0, b""
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > limit:
                    raise MediaRejected(f"That file is too large (limit {limit // (1024 * 1024)} MB for {kind}s).")
                out.write(chunk)
        if size == 0:
            raise MediaRejected("The file is empty.")
        if not _signature_ok(ext, head):
            raise MediaRejected("The file's contents do not match its extension.")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return {"path": str(dest), "kind": kind, "name": Path(filename).name[:120], "size": size, "mime": mime}


def media_issue(media: Optional[dict]) -> Optional[str]:
    if not media or not media.get("path"):
        return "no material attached"
    p = Path(media["path"])
    if not p.is_file():
        return "the attached file is missing"
    if media.get("size") and p.stat().st_size != media["size"]:
        return "the attached file changed after upload"
    return None
