"""
hypogeum/episode_channel_resolver.py — the one canonical answer to "which
channel does this episode (or analytics row) belong to?"

`platform_analytics.episode_id` (and, by extension, a `learning_actions`
row's `source_episode`) is USUALLY a real `ep_xxxx` id, but a handful of
rows predate the Zernio-driven publishing pipeline and were imported keyed
by the episode's raw title/caption text instead (no `late_post_id` existed
yet to match against `publishing_receipts`). Four call sites each grew
their own copy of the same id-then-title-then-fuzzy resolution heuristic
(dashboard_analytics.py, dashboard_rollup.py, learning_engine.py,
monetization_eligibility.py) with different coverage — meaning the same
row could be attributed to a channel on the Analytics page but silently
invisible to Learning or Monetization. This module is the single
implementation all four now call, so they can never disagree again.

Public API:
    build_episode_channel_map(conn, extra_keys=None) -> dict[str, str]
"""
from __future__ import annotations

from typing import Optional


def build_episode_channel_map(conn, extra_keys: Optional[set] = None) -> dict[str, str]:
    """{episode_id_or_legacy_title_key: channel_id}, covering every real
    episode_id known via `runs`/`episode_topics`, every distinct
    `platform_analytics.episode_id` key, and any caller-supplied
    `extra_keys` (e.g. a `learning_actions.source_episode` value, which
    isn't necessarily ever written to `platform_analytics` at all) —
    resolved via (in order): exact id match, exact title match, fuzzy
    title-prefix match, then — only when exactly one channel is bound to
    every platform that key has analytics data on — a platform-owner
    fallback. A key that resolves to nothing under all of these is simply
    absent, never guessed. Never raises."""
    # id -> channel (the common case: covers every episode with a run,
    # whether or not it has any analytics yet — dashboard_rollup's spend/
    # live-count filtering needs this even for analytics-less episodes).
    id_chan: dict[str, str] = {}
    for sql in ("SELECT episode_id, channel_id FROM runs "
                "WHERE episode_id IS NOT NULL AND channel_id IS NOT NULL",
                "SELECT episode_id, channel_id FROM episode_topics "
                "WHERE episode_id IS NOT NULL AND channel_id IS NOT NULL"):
        try:
            for r in conn.execute(sql):
                id_chan.setdefault(r[0], r[1])
        except Exception:  # noqa: BLE001
            pass

    keys = set(id_chan) | set(extra_keys or ())
    try:
        for r in conn.execute("SELECT DISTINCT episode_id FROM platform_analytics"):
            if r[0]:
                keys.add(r[0])
    except Exception:  # noqa: BLE001
        pass

    # title -> channel  (title -> episode_id -> channel; and topic_text direct)
    # Joined on episode_id, not `episodes.run_id = runs.run_id`: an
    # episode's own `run_id` column is often never backfilled (e.g. a
    # freshly-inserted test/legacy row), which would silently drop every
    # title for such an episode even though `runs` correctly links its
    # episode_id to a channel.
    title_chan: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT e.title AS t, r.channel_id AS c FROM episodes e "
            "JOIN runs r ON r.episode_id = e.episode_id "
            "WHERE e.title IS NOT NULL AND r.channel_id IS NOT NULL"):
            title_chan.setdefault(r["t"], r["c"])
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in conn.execute(
            "SELECT t.title_text AS t, r.channel_id AS c FROM titles t "
            "JOIN runs r ON r.run_id = t.run_id "
            "WHERE t.title_text IS NOT NULL AND r.channel_id IS NOT NULL"):
            title_chan.setdefault(r["t"], r["c"])
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in conn.execute(
            "SELECT topic_text AS t, channel_id AS c FROM episode_topics "
            "WHERE topic_text IS NOT NULL AND channel_id IS NOT NULL"):
            title_chan.setdefault(r["t"], r["c"])
    except Exception:  # noqa: BLE001
        pass

    # platform -> the single channel bound to it (fallback for keys that
    # match no episode/title — e.g. after old episodes were cleaned out).
    plat_owner: dict[str, str] = {}
    try:
        seen: dict[str, set] = {}
        for r in conn.execute(
            "SELECT platform, channel_id FROM channel_provider_bindings "
            "WHERE enabled = 1 AND capability = 'publishing'"):
            seen.setdefault(r[0], set()).add(r[1])
        plat_owner = {p: next(iter(cs)) for p, cs in seen.items() if len(cs) == 1}
    except Exception:  # noqa: BLE001
        pass
    key_plats: dict[str, set] = {}
    try:
        for r in conn.execute("SELECT DISTINCT episode_id, platform FROM platform_analytics"):
            key_plats.setdefault(r[0], set()).add(r[1])
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, str] = dict(id_chan)
    for k in keys:
        if k in out:
            continue
        if k in title_chan:
            out[k] = title_chan[k]
            continue
        hit = None
        for t, c in title_chan.items():
            if t and (t.startswith(k[:40]) or k.startswith(t[:40])):
                hit = c
                break
        if hit:
            out[k] = hit
            continue
        owners = {plat_owner[p] for p in key_plats.get(k, set()) if p in plat_owner}
        if len(owners) == 1:
            out[k] = next(iter(owners))
    return out
