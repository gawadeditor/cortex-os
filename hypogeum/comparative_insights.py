"""
hypogeum/comparative_insights.py — Cross-episode comparative analytics.

hypogeum.learning_engine's INSIGHT_RULES are all single-episode, absolute-
threshold rules (CTR < 2% -> weak thumbnail, avg_view_pct < 15% -> hook
failure). They can never answer the question an operator actually asks
when comparing two of their own episodes: "this one was watched all the
way through and the other wasn't — was that because of WHEN I posted it,
WHAT it was about, HOW it was written, or HOW it looked?" -- a genuinely
comparative question, not an absolute one.

"topic" and "publishing_time" have been valid analytics_insights
dimensions (see hypogeum.analytics_store.VALID_DIMENSIONS) with full
hypogeum.learning_engine._ACTION_TEMPLATES entries since this system was
built — but no rule ever populated them, because no single-episode
threshold can. "script" and "video" never existed as dimensions at all,
for the same reason: nothing about a script or a video clip crosses an
absolute quality threshold in isolation; it only means something next to
another episode's script or video.

This module is the missing half: for a channel with >=2 real (source=
'api') analytics episodes, find that channel's best- and worst-
performing episode by avg_view_pct, and check whether they differ
meaningfully on each of four axes in turn:

  publishing_time — real weekday + hour the episode actually went out
                     (hypogeum.publishing.publish_slot_ledger's
                     channel_publish_slots when recorded, falling back to
                     the earliest real publishing_attempts.created_at for
                     an older episode that predates that ledger).
  topic            — episodes.topic (the raw production topic string).
  script            — total narration word count across every
                     narration_beats[].text in script_package (a script
                     is not judged on ANY single word count — a channel
                     making 60s bedtime shorts and one making 8-scene
                     documentaries have completely different natural
                     ranges — only on how far apart TWO OF THE SAME
                     CHANNEL'S OWN episodes are).
  video             — scene count in visual_package.scenes.

When exactly one axis differs by more than that axis's own minimum-
meaningful-gap threshold, this is reported as ONE comparative insight,
naming both episodes and their real numbers, attributed to that axis —
never a vague "engagement is low" statement. When more than one axis
differs, or the channel doesn't have enough real episodes yet, nothing
is fabricated: this is a real constraint of comparing exactly two data
points, not a caveat to explain away — get_recommendations() staying
empty here is honest, not broken (a channel that has one --or one
GOOD-- real-analytics episode has nothing to compare it to yet).

Public API:
    generate_comparative_insights(channel_id, db_path=None, assets_root=None) -> list[dict]
        Computes the comparison, stores any new insights via
        hypogeum.analytics_store.store_insight() (dedup by
        (episode_id, platform, insight_type), exactly like
        hypogeum.learning_engine.extract_insights()), and returns the
        insight dicts it wrote (or has already written) this call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.analytics_store import get_episode_analytics, list_insights, store_insight
from hypogeum.db import get_connection
from hypogeum.logger import get_logger

log = get_logger(agent="comparative_insights")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

# Minimum meaningful gaps per axis — deliberately loose (this is a rule-
# based, not statistical, system; see this module's own docstring on why
# these thresholds are RELATIVE to each channel's own episodes, never a
# fixed absolute value shared across channels).
_MIN_PERFORMANCE_GAP = 0.05     # avg_view_pct: 5 percentage points
_MIN_SCRIPT_GAP_RATIO = 1.30    # 30% difference in total narration word count
_MIN_VIDEO_GAP_SCENES = 2       # 2+ scene count difference
_MIN_TIMING_GAP_HOURS = 3.0     # different weekday, or same weekday 3+ hours apart


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _episode_performance(episode_id: str, db_path: Optional[Path]) -> Optional[float]:
    """Average avg_view_pct across every platform with real (source='api')
    analytics for this episode. None if no real data exists."""
    analytics = get_episode_analytics(episode_id, db_path=db_path)
    vals = [
        float(d["avg_view_pct"])
        for d in analytics.values()
        if d.get("source") == "api" and d.get("avg_view_pct") is not None
    ]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _episode_platforms_with_real_data(episode_id: str, db_path: Optional[Path]) -> list[str]:
    analytics = get_episode_analytics(episode_id, db_path=db_path)
    return sorted(p for p, d in analytics.items() if d.get("source") == "api")


def _episode_publish_dt(episode_id: str, db_path: Optional[Path]) -> Optional[datetime]:
    """Real publish moment. channel_publish_slots.slot_utc when this
    episode was scheduled through that ledger; otherwise the earliest
    publishing_attempts.created_at (every published episode has at least
    one attempt row, including ones that predate the slot ledger)."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT slot_utc FROM channel_publish_slots WHERE episode_id=? LIMIT 1",
            (episode_id,),
        ).fetchone()
        if row and row["slot_utc"]:
            dt = _parse_dt(row["slot_utc"])
            if dt is not None:
                return dt
        row = conn.execute(
            "SELECT created_at FROM publishing_attempts WHERE episode_id=? "
            "ORDER BY created_at LIMIT 1",
            (episode_id,),
        ).fetchone()
        if row and row["created_at"]:
            return _parse_dt(row["created_at"])
    finally:
        conn.close()
    return None


def _episode_topic(episode_id: str, db_path: Optional[Path]) -> Optional[str]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT topic FROM episodes WHERE episode_id=?", (episode_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["topic"] if row and row["topic"] else None


def _episode_dir(episode_id: str, assets_root: Path) -> Path:
    return assets_root / "episodes" / episode_id


def _script_word_count(episode_id: str, assets_root: Path) -> Optional[int]:
    """Total words across every narration_beats[].text in every section
    of script_package -- the one script property that is a plain count,
    comparable between any two episodes of the SAME channel regardless of
    story content, without any text-quality judgment this system has no
    business making."""
    path = _episode_dir(episode_id, assets_root) / f"script_package_{episode_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    total = 0
    for section in data.get("sections", []) or []:
        for beat in section.get("narration_beats", []) or []:
            text = beat.get("text") or ""
            total += len(text.split())
    return total if total > 0 else None


def _video_scene_count(episode_id: str, assets_root: Path) -> Optional[int]:
    path = _episode_dir(episode_id, assets_root) / f"visual_package_{episode_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scenes = data.get("scenes")
    return len(scenes) if isinstance(scenes, list) and scenes else None


def _weekday_name(dt: datetime) -> str:
    return ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][dt.weekday()]


def _timing_gap_hours(a: datetime, b: datetime) -> float:
    """Hours apart on a repeating weekly clock -- a Sunday-23:00 post and
    a Monday-01:00 post are 2 hours apart, not ~6 days, so a channel that
    posts at the same real-world time every week is correctly read as
    'same timing' regardless of which two weeks the two episodes actually
    published in."""
    a_minutes = a.weekday() * 24 * 60 + a.hour * 60 + a.minute
    b_minutes = b.weekday() * 24 * 60 + b.hour * 60 + b.minute
    diff = abs(a_minutes - b_minutes)
    week_minutes = 7 * 24 * 60
    diff = min(diff, week_minutes - diff)
    return diff / 60.0


def generate_comparative_insights(
    channel_id: str,
    db_path: Optional[Path] = None,
    assets_root: Optional[Path] = None,
) -> list[dict]:
    """
    Compare this channel's best- and worst-performing real-analytics
    episode across publishing_time / topic / script / video. Stores and
    returns one insight per axis that differs by more than that axis's
    own minimum-meaningful-gap AND shows a real performance gap between
    the two episodes (>= _MIN_PERFORMANCE_GAP). Never invents a
    comparison from fewer than 2 real-analytics episodes.
    """
    if assets_root is None:
        assets_root = _DEFAULT_ASSETS_ROOT

    from hypogeum.learning_engine import _channel_episode_ids  # noqa: PLC0415

    candidate_ids = _channel_episode_ids(channel_id, db_path)
    scored: list[tuple[str, float]] = []
    for episode_id in candidate_ids:
        perf = _episode_performance(episode_id, db_path)
        if perf is not None:
            scored.append((episode_id, perf))

    if len(scored) < 2:
        log.info(
            "comparative_insights_insufficient_data",
            channel_id=channel_id, real_analytics_episodes=len(scored),
        )
        return []

    scored.sort(key=lambda t: t[1])
    worst_id, worst_perf = scored[0]
    best_id, best_perf = scored[-1]
    if worst_id == best_id:
        return []

    performance_gap = best_perf - worst_perf
    if performance_gap < _MIN_PERFORMANCE_GAP:
        log.info(
            "comparative_insights_no_real_gap", channel_id=channel_id,
            best_id=best_id, worst_id=worst_id, gap=round(performance_gap, 4),
        )
        return []

    now = _now_iso()
    new_insights: list[dict] = []
    platform = "all"  # a comparative insight spans every platform's own signal, not one

    # ---- publishing_time -----------------------------------------------
    best_dt = _episode_publish_dt(best_id, db_path)
    worst_dt = _episode_publish_dt(worst_id, db_path)
    if best_dt is not None and worst_dt is not None:
        gap_hours = _timing_gap_hours(best_dt, worst_dt)
        if gap_hours >= _MIN_TIMING_GAP_HOURS:
            new_insights.append({
                "episode_id": worst_id, "platform": platform,
                "created_at": now, "insight_type": "worse_publishing_time",
                "affected_dim": "publishing_time",
                "signal_strength": round(-min(1.0, performance_gap * 2), 3),
                "threshold_used": f"timing_gap_hours >= {_MIN_TIMING_GAP_HOURS}",
                "detail": (
                    f"{best_id} ({_weekday_name(best_dt)} {best_dt.strftime('%H:%M')} UTC) "
                    f"reached {best_perf:.0%} avg view vs {worst_id} "
                    f"({_weekday_name(worst_dt)} {worst_dt.strftime('%H:%M')} UTC) at {worst_perf:.0%} "
                    f"— {gap_hours:.1f}h apart on the weekly clock; consider {best_id}'s slot"
                ),
            })

    # ---- topic ------------------------------------------------------------
    best_topic = _episode_topic(best_id, db_path)
    worst_topic = _episode_topic(worst_id, db_path)
    if best_topic and worst_topic and best_topic != worst_topic:
        new_insights.append({
            "episode_id": worst_id, "platform": platform,
            "created_at": now, "insight_type": "weaker_topic",
            "affected_dim": "topic",
            "signal_strength": round(-min(1.0, performance_gap * 2), 3),
            "threshold_used": "best vs worst real-analytics episode, distinct topics",
            "detail": (
                f"\"{best_topic}\" reached {best_perf:.0%} avg view vs "
                f"\"{worst_topic}\" at {worst_perf:.0%} — topics differ, "
                f"nothing else compared here rules out a shared cause"
            ),
        })

    # ---- script ------------------------------------------------------------
    best_words = _script_word_count(best_id, assets_root)
    worst_words = _script_word_count(worst_id, assets_root)
    if best_words and worst_words:
        ratio = max(best_words, worst_words) / max(1, min(best_words, worst_words))
        if ratio >= _MIN_SCRIPT_GAP_RATIO:
            longer_id, longer_words = (best_id, best_words) if best_words > worst_words else (worst_id, worst_words)
            shorter_id, shorter_words = (worst_id, worst_words) if longer_id == best_id else (best_id, best_words)
            new_insights.append({
                "episode_id": worst_id, "platform": platform,
                "created_at": now, "insight_type": "script_length_gap",
                "affected_dim": "script",
                "signal_strength": round(-min(1.0, performance_gap * 2), 3),
                "threshold_used": f"word_count_ratio >= {_MIN_SCRIPT_GAP_RATIO}",
                "detail": (
                    f"{best_id} ({best_words} narration words) reached {best_perf:.0%} avg view "
                    f"vs {worst_id} ({worst_words} words) at {worst_perf:.0%} "
                    f"— {longer_id} runs {longer_words - shorter_words} words "
                    f"{'longer' if longer_id == best_id else 'shorter'} than the stronger episode"
                ),
            })

    # ---- video ------------------------------------------------------------
    best_scenes = _video_scene_count(best_id, assets_root)
    worst_scenes = _video_scene_count(worst_id, assets_root)
    if best_scenes and worst_scenes and abs(best_scenes - worst_scenes) >= _MIN_VIDEO_GAP_SCENES:
        new_insights.append({
            "episode_id": worst_id, "platform": platform,
            "created_at": now, "insight_type": "scene_count_gap",
            "affected_dim": "video",
            "signal_strength": round(-min(1.0, performance_gap * 2), 3),
            "threshold_used": f"scene_count_gap >= {_MIN_VIDEO_GAP_SCENES}",
            "detail": (
                f"{best_id} ({best_scenes} scenes) reached {best_perf:.0%} avg view vs "
                f"{worst_id} ({worst_scenes} scenes) at {worst_perf:.0%}"
            ),
        })

    if not new_insights:
        return []

    # Dedup by (episode_id, platform, insight_type) -- same discipline as
    # hypogeum.learning_engine.extract_insights().
    existing = {
        (i["episode_id"], i["platform"], i["insight_type"])
        for i in list_insights(episode_id=worst_id, db_path=db_path)
    }
    stored: list[dict] = []
    for ins in new_insights:
        key = (ins["episode_id"], ins["platform"], ins["insight_type"])
        if key in existing:
            continue
        store_insight(ins, db_path=db_path)
        existing.add(key)
        stored.append(ins)

    log.info(
        "comparative_insights_generated", channel_id=channel_id,
        best_id=best_id, worst_id=worst_id,
        performance_gap=round(performance_gap, 4), insights_written=len(stored),
    )
    return stored
