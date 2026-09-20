"""
hypogeum/production_preflight.py — hard pre-spend production gates (2026-09-04).

Two failures on 2026-09-01 motivated this module:

  1. twinkle produced ep_twinkle_sep08 / ep_twinkle_sep10 on
     seedance-pro-t2v-fast, whose native output is 496x864 — below half the
     1080x1920 a Shorts deliverable requires, so no amount of upscaling
     makes it compliant. Both episodes are unpublishable (~$4 spent),
     caught only by the publish-time A_VIDEO gate. The bake-off that led to
     the adoption surfaced price/latency/motion but not resolution.

  2. That same batch fed the identical topic ("The Little Hedgehog Who
     Couldn't Find His Way Home") to two runs. Nothing checked whether a
     topic had already been produced for the channel —
     hypogeum.artifact_compatibility.evaluate_subject_production_decision()
     existed but was never wired into the pipeline, and only covered
     *published* subjects anyway.

Both gates run in hypogeum.pipeline BEFORE run registration and any paid
agent. They FAIL CLOSED with a typed error — no override, no warn-and-
continue (operator decision, 2026-09-04). A denied run costs $0.

  assert_video_model_qualified(channel_id, *, db_path)
  assert_topic_not_already_produced(channel_id, topic_text, *, episode_id, db_path)

Neither talks to the network. Both are no-ops when channel_id is None
(the pipeline's existing "bypass channel loading" contract).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from hypogeum.logger import get_logger

__all__ = [
    "ProductionPreflightError",
    "VideoModelNotQualifiedError",
    "DuplicateTopicError",
    "channel_deliverable_resolution",
    "evaluate_candidate_video_model",
    "evaluate_channel_video_model",
    "assert_video_model_qualified",
    "assert_topic_not_already_produced",
]

log = get_logger(agent="production_preflight")


class ProductionPreflightError(RuntimeError):
    """Base for a fail-closed pre-spend gate denial."""


class VideoModelNotQualifiedError(ProductionPreflightError):
    """The channel's adopted video model cannot meet the deliverable's
    required source resolution / orientation via one upscale pass."""


class DuplicateTopicError(ProductionPreflightError):
    """A non-superseded episode for this channel already exists under the
    same canonical subject."""


# ---------------------------------------------------------------------------
# Deliverable resolution for a channel
# ---------------------------------------------------------------------------

def _is_short_native_channel(channel_id: str, db_path: Optional[Path]) -> bool:
    """A shorts-only channel (videos_per_week == 0 and shorts_per_week >= 1)
    produces one vertical deliverable per run. Mirrors the derivation in
    hypogeum.pipeline (which sets config.SHORT_NATIVE_MODE the same way)."""
    try:
        from hypogeum.channel_config import get_channel_operations_profile
        ops = get_channel_operations_profile(channel_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        return False
    cadence = (getattr(ops, "production_cadence", {}) or {}) if ops is not None else {}
    vpw = cadence.get("videos_per_week")
    spw = cadence.get("shorts_per_week")
    return vpw == 0 and isinstance(spw, int) and spw >= 1


def channel_deliverable_resolution(
    channel_id: str, *, db_path: Optional[Path] = None,
) -> Tuple[int, int]:
    """The (width, height) every source clip for this channel's primary
    deliverable must reach — portrait 1080x1920 for a shorts-only channel,
    the landscape main_episode policy otherwise. Delegates the actual
    numbers to hypogeum.artifact_compatibility so there is one source of
    truth."""
    from hypogeum.artifact_compatibility import required_source_resolution
    return required_source_resolution(
        "main_episode", short_native=_is_short_native_channel(channel_id, db_path)
    )


# ---------------------------------------------------------------------------
# Gate 1 — adopted video model must be able to meet the deliverable
# ---------------------------------------------------------------------------

def evaluate_candidate_video_model(
    channel_id: str, model: str, *, db_path: Optional[Path] = None,
) -> Tuple[bool, str, dict]:
    """Would `model` be a valid adoption for `channel_id`? Same rule as
    evaluate_channel_video_model but for a model that is not adopted yet —
    used by the dashboard adoption route to refuse a sub-resolution /
    wrong-orientation pick before it is written. Resolution evidence comes
    from this channel's bake-off manifests.

    (native, w, h) is returned in detail so the route can persist it.
    """
    from hypogeum.artifact_compatibility import qualifies_for_deliverable_after_upscale
    from hypogeum.video_bakeoff import latest_model_resolution

    req_w, req_h = channel_deliverable_resolution(channel_id, db_path=db_path)
    native = latest_model_resolution(model, channel_id)
    detail = {
        "candidate_model": model,
        "required_resolution": f"{req_w}x{req_h}",
        "native_resolution": f"{native[0]}x{native[1]}" if native else None,
        "native_width": native[0] if native else None,
        "native_height": native[1] if native else None,
    }
    if native is None:
        return False, (
            f"no bake-off resolution evidence for '{model}' on {channel_id} — "
            f"run it through the Video Model bake-off first so its real output "
            f"resolution is measured"
        ), detail
    ok, why = qualifies_for_deliverable_after_upscale(native[0], native[1], req_w, req_h)
    return (ok, "" if ok else f"'{model}': {why}", detail)


def evaluate_channel_video_model(
    channel_id: str, *, db_path: Optional[Path] = None,
) -> Tuple[bool, str, dict]:
    """Shared check used by both the dashboard adoption route and the
    pipeline preflight. Returns (ok, reason, detail).

    ok=True with reason="" when the channel has no adopted model (the
    global MUAPI tier config — veo3.1-lite, 1080p — is trusted) OR the
    adopted model has bake-off resolution evidence that
    qualifies_for_deliverable_after_upscale() accepts for this channel's
    deliverable.

    ok=False when a model IS adopted but has no resolution evidence, or its
    native resolution / orientation cannot be recovered by one upscale pass.
    """
    from hypogeum.channel_config import resolve_channel_video_model
    from hypogeum.artifact_compatibility import qualifies_for_deliverable_after_upscale
    from hypogeum.video_bakeoff import latest_model_resolution

    model, _price = resolve_channel_video_model(channel_id, db_path=db_path)
    if not model:
        return True, "", {"adopted_model": None}

    req_w, req_h = channel_deliverable_resolution(channel_id, db_path=db_path)
    required_aspect = "9:16" if req_h > req_w else "16:9"

    # Prefer the resolution captured on the model profile at adoption time —
    # but ONLY when its orientation actually matches what THIS run needs.
    # A model is adopted once, at whatever aspect ratio the channel's mode
    # was at adoption time (e.g. twinkle adopted wan2.7 while shorts-only,
    # caching its 720x1280 portrait clip). That cached value says nothing
    # about the model's landscape output — the same model can render a
    # different real orientation for a different requested aspect_ratio
    # (confirmed 2026-09-19: wan2.7-text-to-video is genuinely 1280x720 at
    # "16:9", not just upscaled/cropped portrait). So an orientation
    # mismatch against the cached value falls through to a fresh,
    # aspect-scoped bake-off scan rather than failing immediately — this is
    # what lets one adopted model serve a channel that runs both shorts and
    # long-form, as long as each aspect ratio has its own bake-off evidence.
    native: Optional[Tuple[int, int]] = None
    try:
        from hypogeum.channel_config import get_channel_profile, get_model_profile
        ch = get_channel_profile(channel_id, db_path=db_path)
        mp = get_model_profile(ch.model_profile_id, db_path=db_path) if ch and ch.model_profile_id else None
        if mp and mp.video_model_native_width and mp.video_model_native_height:
            cached_portrait = mp.video_model_native_height > mp.video_model_native_width
            if cached_portrait == (required_aspect == "9:16"):
                native = (mp.video_model_native_width, mp.video_model_native_height)
    except Exception:  # noqa: BLE001
        native = None
    if native is None:
        native = latest_model_resolution(model, channel_id, aspect_ratio=required_aspect)

    detail = {
        "adopted_model": model,
        "required_resolution": f"{req_w}x{req_h}",
        "native_resolution": f"{native[0]}x{native[1]}" if native else None,
    }

    if native is None:
        return False, (
            f"video model '{model}' is adopted for {channel_id} but has no "
            f"bake-off resolution evidence — re-run the Video Model bake-off "
            f"so its real output resolution is measured"
        ), detail

    ok, why = qualifies_for_deliverable_after_upscale(native[0], native[1], req_w, req_h)
    if ok:
        return True, "", detail
    return False, f"video model '{model}': {why}", detail


def assert_video_model_qualified(
    channel_id: Optional[str], *, db_path: Optional[Path] = None,
) -> None:
    """Raise VideoModelNotQualifiedError when the channel's adopted video
    model cannot produce clips that meet its deliverable. No-op when
    channel_id is None."""
    if channel_id is None:
        return
    ok, reason, detail = evaluate_channel_video_model(channel_id, db_path=db_path)
    if ok:
        log.info("preflight_video_model_ok", channel_id=channel_id, **detail)
        return
    log.error("preflight_video_model_denied", channel_id=channel_id, reason=reason, **detail)
    raise VideoModelNotQualifiedError(
        f"{reason}. Production is blocked until the channel adopts a model "
        f"that can deliver {detail.get('required_resolution')} (Settings → "
        f"Video Model bake-off)."
    )


# ---------------------------------------------------------------------------
# Gate 2 — the topic must not already be produced for this channel
# ---------------------------------------------------------------------------

_LIVE_EPISODE_STATUSES_EXCLUDED = ("superseded",)


def assert_topic_not_already_produced(
    channel_id: Optional[str],
    topic_text: Optional[str],
    *,
    episode_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Raise DuplicateTopicError when a DIFFERENT episode for this channel
    already exists under the same canonical subject and is not superseded.

    A rerun/retry of the same episode keeps its episode_id and is never
    blocked. No-op when channel_id or topic_text is missing.
    """
    if not channel_id or not (topic_text or "").strip():
        return

    from hypogeum.db import get_connection
    from hypogeum.artifact_compatibility import canonical_subject

    subject = canonical_subject(topic_text)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT e.episode_id, e.topic, e.status
            FROM episodes e
            JOIN runs r ON r.episode_id = e.episode_id
            WHERE r.channel_id = ?
              AND e.topic IS NOT NULL AND e.topic != ''
              AND e.status NOT IN ('superseded')
            """,
            (channel_id,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if episode_id and row["episode_id"] == episode_id:
            continue
        if canonical_subject(row["topic"]) == subject:
            log.error(
                "preflight_duplicate_topic_denied",
                channel_id=channel_id, canonical_subject=subject,
                blocking_episode_id=row["episode_id"], blocking_status=row["status"],
            )
            raise DuplicateTopicError(
                f"channel {channel_id} already has episode {row['episode_id']} "
                f"(status={row['status']}) under the same subject "
                f"\"{subject}\". Producing it again would ship a duplicate — "
                f"supersede or archive the existing episode first, or choose a "
                f"different topic."
            )
    log.info("preflight_topic_unique_ok", channel_id=channel_id, canonical_subject=subject)
