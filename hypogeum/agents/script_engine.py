"""
hypogeum/agents/script_engine.py — Script Engine agent (M1.5)

The Script Engine is the fourth real agent in the pipeline. It consumes the
validated Narrative Package and produces the episode's complete script_package —
narration-ready beats, CTAs, chapter markers, Shorts scripts, text overlays,
and shorts extraction markers.

Execution sequence:
  1. Log se_script_started with narrative quality indicators
  2. Call run_agent() with Claude Sonnet → script_package
  3. Log se_script_completed with section count, CTA count, Shorts count
  4. Return script_package

This is a single-call agent. All script writing decisions (beat composition,
CTA placement, chapter title generation, Shorts narration) are delegated to
Claude Sonnet via the system prompt. The SE module enforces no governance rules
beyond what run_agent() validates through the schema.

Humanizer skill: the system prompt composes hypogeum/prompts/script_engine.txt
(this agent's own instructions, including a HUMANIZATION section bridging the
two) with the pinned upstream Humanizer skill
(third_party/skills/humanizer/<PINNED_HUMANIZER_SHA>/SKILL.md), loaded
verbatim via hypogeum.prompt_loader.compose_system_prompt() — the same
base+skill composition pattern every other skill-attached agent already uses
(hypogeum/skill_registry.py). Humanization happens inside this single
generation call, not as a separate stage, adapter, or pipeline pass: no
second LLM call, no separate checkpoint layer, no merge step. The pinned
skill file is never edited, paraphrased, or copied into this prompt file —
it is composed alongside it, unmodified, exactly like every other agent's
skill injection.

Model: Claude Sonnet 4.6 (frozen — T0.1)
Max tokens: 8,192

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt loaded from disk — never hardcoded inline
  - Output validated by run_agent() via schemas.py
  - No environment access — all settings via Config
  - All logging via get_logger() — never print() or stdlib logging

Logging events emitted:
  se_script_started     — info  — before run_agent() call
  se_script_completed   — info  — after script_package returned
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.channel_creative_brief import channel_brief_for
from hypogeum.config import Config
from hypogeum.humanizer import humanize_beats
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import compose_system_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_SONNET  = "anthropic/claude-sonnet-4.6"
_SE_MAX_TOKENS  = 16_384
_AGENT_NAME     = "script_engine"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "script_engine.txt"

# Pinned upstream Humanizer skill (docs/architecture/UPSTREAM_SKILL_MANIFEST.md,
# config/upstream_skills_manifest.json) — loaded verbatim, never edited.
PINNED_HUMANIZER_SHA = "1b48564898e999219882660237fde01bf4843a0f"
_THIRD_PARTY_ROOT = Path(__file__).resolve().parent.parent.parent / "third_party"
_HUMANIZER_SKILL_PATH = (
    _THIRD_PARTY_ROOT / "skills" / "humanizer" / PINNED_HUMANIZER_SHA / "SKILL.md"
)


# ---------------------------------------------------------------------------
# System prompt — composed once at import time from this agent's own prompt
# plus the pinned Humanizer skill
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _HUMANIZER_SKILL_PATH)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_script_engine(
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Execute the Script Engine agent end-to-end.

    Args:
        narrative_package: Validated narrative_package dict from the Narrative Architect.
        config:            Loaded Config instance. Passed through to run_agent().
        episode_id:        Episode identifier for log correlation and file naming.
        output_dir:        Optional output directory override (passed to run_agent()).

    Returns:
        script_package — validated dict from run_agent(). Contains narration beats
        for all sections, exactly 3 Shorts scripts with text overlays, chapter markers,
        shorts extraction markers, traceability manifest, and script integrity audit.

    Raises:
        ResponseParseError — Claude returned non-dict JSON.
        SchemaError        — Claude output failed script_package schema validation.
        Any exception from run_agent() / hypogeum.openrouter that is not caught here.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    archetype     = narrative_package.get("episode_metadata", {}).get("archetype", "unknown")
    sections_count = len(
        narrative_package.get("episode_structure", {}).get("sections", [])
    )
    shorts_count  = len(narrative_package.get("shorts_selection", []))

    log.info(
        "se_script_started",
        archetype=archetype,
        sections_count=sections_count,
        shorts_count=shorts_count,
    )

    _input_package: dict = {
        "task": "write_script",
        "narrative_package": narrative_package,
        "channel_brief": channel_brief_for(channel_id),
    }
    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        _max_words = int(getattr(config, "SHORT_NATIVE_MAX_NARRATION_WORDS", 150) or 150)
        # Model-aware + slow-voice-safe per-beat budget: the default 8.5s ceiling
        # assumes an 8s clip. A channel on a shorter-clip model (e.g. seedance
        # ~5s, adopted via the bake-off) must cap each beat's narration to what
        # ONE of its clips covers — even read by a slow voice — or every beat
        # needs a second contingency clip and Assembly's coverage-fill runs on
        # every short, the exact failure "one clip per beat" exists to avoid.
        _beat_secs, _per_beat_words, _wps = _short_native_beat_budget(config, channel_id)
        # One beat == one scene == one clip. The scene cap
        # (EPISODE_ZERO_MAX_SCENES, scaled up by apply_short_native_preset for
        # a shorter-clip model, else SHORT_NATIVE_MAX_SCENES) is the ceiling —
        # asking for more beats than there are scenes cuts the ending off.
        _scene_cap = (getattr(config, "EPISODE_ZERO_MAX_SCENES", 0)
                      or getattr(config, "SHORT_NATIVE_MAX_SCENES", 8) or 8)
        _beat_lo = max(3, min(_scene_cap, _max_words // _per_beat_words))
        _beat_hi = max(_beat_lo, min(_scene_cap, _max_words // max(1, int(6.0 * _wps))))
        _input_package["production_mode"] = "short_native"
        _input_package["short_native_directive"] = {
            "total_narration_words_max": _max_words,
            "sections": 1,
            "narration_beats": (
                f"{_beat_lo} to {_beat_hi} beats. EACH beat becomes exactly one "
                f"{_beat_secs:.0f}-second vertical video clip, so each beat's spoken "
                f"narration MUST be sayable in {_beat_secs:.0f} seconds or less — hard "
                f"cap {_per_beat_words} words per beat. A beat that runs long will be "
                f"truncated automatically; write within the budget."
            ),
            "per_beat_words_max": _per_beat_words,
            "shorts_scripts": "empty list — do not write derived Shorts",
            "chapter_markers": "none",
            "cta": "at most one short spoken line",
            "pacing": "open on the hook in the first sentence; no throat-clearing",
        }
        _outro_pol = _generated_outro_policy(channel_id, config)
        if _outro_pol is not None:
            _tie = _outro_pol.get(
                "tie_directive",
                "one gentle sentence showing the story's main character now peacefully asleep",
            )
            _input_package["short_native_directive"]["ending"] = (
                f"Your final beat is the story's close: {_tie}. Then STOP — do not "
                "add any farewell, sign-off, or call to follow/subscribe. The "
                "channel appends its own closing line after your script."
            )

    # Channel Learning: hook / pacing directives from this channel's own
    # retention analytics (hypogeum.channel_learning). Advisory to the model.
    try:
        from hypogeum.channel_learning import learning_input_block  # noqa: PLC0415
        _input_package.update(learning_input_block(channel_id, "hook", "pacing"))
    except Exception:  # noqa: BLE001
        pass

    script_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=_CLAUDE_SONNET,
        system_prompt=SYSTEM_PROMPT,
        input_package=_input_package,
        output_schema="script_package",
        episode_id=episode_id,
        max_tokens=_SE_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    # Humanize beat text fields (deterministic — no schema changes)
    for section in script_package.get("sections", []):
        humanize_beats(section.get("narration_beats", []))
    for short in script_package.get("shorts_scripts", []):
        humanize_beats(short.get("narration_beats", []))

    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        _enforce_short_native_budget(script_package, config, log, channel_id=channel_id)

    # Channel generated-outro: appended AFTER the budget trim so it is never
    # dropped. No-op unless the channel ships outro_policy.json {mode:generated}.
    _append_generated_outro(script_package, config, log, channel_id=channel_id)

    # Re-persist the checkpoint so it reflects the FINAL package (humanized
    # beats + short-native trim + generated outro), not just run_agent's raw
    # LLM output. Without this, a pipeline resume reloads the stale on-disk
    # copy and silently drops the outro / humanization.
    try:
        import json as _json  # noqa: PLC0415
        from hypogeum.agent import _resolve_output_path  # noqa: PLC0415
        _ckpt = _resolve_output_path(episode_id, "script_package", output_dir)
        _ckpt.write_text(
            _json.dumps(script_package, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        log.info("se_script_checkpoint_repersisted", path=str(_ckpt))
    except Exception as exc:  # noqa: BLE001
        log.warn("se_script_checkpoint_repersist_failed", error=str(exc))

    output_sections = len(script_package.get("sections", []))
    cta_count       = script_package.get("script_integrity", {}).get("cta_count", 0)
    output_shorts   = len(script_package.get("shorts_scripts", []))
    chapter_count   = len(script_package.get("chapter_markers", []))

    log.info(
        "se_script_completed",
        sections_count=output_sections,
        cta_count=cta_count,
        shorts_count=output_shorts,
        chapter_count=chapter_count,
    )

    return script_package


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Slowest sustained narration cadence observed across ElevenLabs voices: a calm
# bedtime read runs ~2.0 words/sec, a documentary narrator ~2.9.
# SHORT_NATIVE_WORDS_PER_SECOND assumes the fast end, so the per-beat word cap
# is additionally clamped against this floor at the editorial-speed ceiling — a
# beat must stay sayable inside ONE clip even when stretched to
# MAX_EDITORIAL_SPEED_FACTOR by the slowest plausible voice. Without this a
# slow-voiced channel (e.g. twinkle's bedtime narrator on ~5s seedance clips)
# over-runs every clip and forces Assembly's coverage-fill — the exact path
# short-native exists to avoid.
_SLOW_VOICE_WPS_FLOOR = 1.9


def _short_native_beat_budget(
    config, channel_id: Optional[str] = None,
) -> tuple[float, int, float]:
    """(_beat_secs, max_beat_words, wps) for one short-native narration beat.

    _beat_secs is min(SHORT_NATIVE_MAX_BEAT_SECONDS, the channel's real adopted
    clip duration). max_beat_words is the SHORT_NATIVE_WORDS_PER_SECOND estimate
    clamped so even _SLOW_VOICE_WPS_FLOOR fits one clip at the editorial ceiling.
    Never raises — falls back to the 8.5s / 2.9 wps defaults.
    """
    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415

    beat_secs = float(getattr(config, "SHORT_NATIVE_MAX_BEAT_SECONDS", 8.5) or 8.5)
    try:
        from hypogeum.muapi import get_requested_clip_duration_seconds  # noqa: PLC0415
        clip_secs = get_requested_clip_duration_seconds(config, channel_id)
        if clip_secs and clip_secs > 0:
            beat_secs = min(beat_secs, float(clip_secs))
    except Exception:  # noqa: BLE001
        pass
    wps = float(getattr(config, "SHORT_NATIVE_WORDS_PER_SECOND", 2.9) or 2.9)
    hard_cap = int(beat_secs * MAX_EDITORIAL_SPEED_FACTOR * _SLOW_VOICE_WPS_FLOOR)
    max_beat_words = max(8, min(int(beat_secs * wps), hard_cap))
    return beat_secs, max_beat_words, wps


def _enforce_short_native_budget(
    script_package: dict, config, log, *, channel_id: Optional[str] = None,
) -> None:
    """Deterministically make a short-native script fit its clip budget.

    SHORT_NATIVE gives every narration beat its own single vertical clip
    (see scene_planner.build_scene_plan_input). If a beat's narration runs
    longer than one clip can cover, Assembly is forced onto the coverage-fill
    path -- the exact thing short-native exists to avoid. The LLM directive
    asks for a compliant script; this pass guarantees one, with no extra
    model call:

      1. per beat: keep only the leading whole sentences that fit the
         per-beat second ceiling (always keep at least one sentence).
      2. across beats, in order: once the running total passes
         SHORT_NATIVE_MAX_NARRATION_WORDS, drop the remaining beats
         (never below 3 beats / 1 beat minimum).
      3. drop any text_overlay whose beat_reference no longer exists.

    The per-beat ceiling is min(SHORT_NATIVE_MAX_BEAT_SECONDS, the channel's
    real clip duration) -- the 8.5s default assumes an 8s clip; a channel on
    a shorter-clip model (seedance ~5s) must trim tighter or every beat
    over-runs one clip.
    """
    _beat_secs, max_beat_words, wps = _short_native_beat_budget(config, channel_id)
    max_total_words = int(getattr(config, "SHORT_NATIVE_MAX_NARRATION_WORDS", 150) or 150)

    sections = script_package.get("sections", []) or []
    if not sections:
        return

    trimmed_beats = 0
    dropped_beats = 0
    running_total = 0
    kept_beat_ids: set = set()

    for section in sections:
        beats = section.get("narration_beats", []) or []
        new_beats: list = []
        for beat in beats:
            text = (beat.get("text") or "").strip()
            words = text.split()
            # (1) per-beat sentence-boundary trim
            if len(words) > max_beat_words:
                sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
                kept, count = [], 0
                for sent in sentences:
                    sw = len(sent.split())
                    if kept and count + sw > max_beat_words:
                        break
                    kept.append(sent)
                    count += sw
                if not kept:  # single sentence already over budget — hard word cut
                    kept = [" ".join(words[:max_beat_words])]
                    count = max_beat_words
                new_text = " ".join(kept).strip()
                if new_text != text:
                    beat["text"] = new_text
                    trimmed_beats += 1
                    words = new_text.split()
            # (2) total-budget beat drop (keep >=3 where possible, >=1 always)
            if new_beats and running_total >= max_total_words and len(new_beats) >= 3:
                dropped_beats += 1
                continue
            running_total += len(words)
            new_beats.append(beat)
            if beat.get("beat_id"):
                kept_beat_ids.add(beat["beat_id"])
        section["narration_beats"] = new_beats or beats[:1]
        for b in section["narration_beats"]:
            if b.get("beat_id"):
                kept_beat_ids.add(b["beat_id"])

    # (3) prune now-dangling overlays
    dropped_overlays = 0
    for short in script_package.get("shorts_scripts", []) or []:
        ov = short.get("text_overlays", []) or []
        keep = [o for o in ov if o.get("beat_reference") in kept_beat_ids]
        dropped_overlays += len(ov) - len(keep)
        short["text_overlays"] = keep

    if trimmed_beats or dropped_beats or dropped_overlays:
        log.info(
            "se_short_native_budget_enforced",
            trimmed_beats=trimmed_beats,
            dropped_beats=dropped_beats,
            dropped_overlays=dropped_overlays,
            per_beat_words_max=max_beat_words,
            total_words_max=max_total_words,
            final_total_words=running_total,
        )


OUTRO_BEAT_TYPE = "outro"

# A generated closing beat is a GENERAL RULE for every short-native channel
# (2026-09-02) — every shorts channel, hypogeum included, ends on a short
# spoken follow/like line that gets its own generated shots. A channel can
# override the wording with channel_packages/<id>/outro_policy.json, or opt
# out entirely with {"mode": "none"} there.
_OUTRO_OPT_OUT_MODES = {"none", "off", "static", "disabled"}

_DEFAULT_OUTRO = {
    "follow_line": (
        "If you got something out of this, follow for more — see you in the next one."
    ),
    "tie_directive": (
        "one calm closing shot that settles back on the story's final place"
    ),
}
_GENTLE_OUTRO = {
    "follow_line": (
        "If you liked tonight's story, tap the little heart and follow for "
        "another one tomorrow night. Sleep tight."
    ),
    "tie_directive": (
        "one gentle sentence showing the story's main character now settled "
        "and peacefully asleep, in the same place the story ended"
    ),
}
_INVESTIGATIVE_OUTRO = {
    "follow_line": (
        "If this changed how you see it, subscribe — there's a new "
        "investigation every week."
    ),
    "tie_directive": (
        "one quiet closing shot that returns to the central artefact or "
        "place and holds still"
    ),
}


def _default_outro_for_channel(channel_id: Optional[str]) -> dict:
    """The channel-appropriate default follow line + story-tie directive,
    picked from the channel's tone. Never raises."""
    try:
        from hypogeum.channel_creative_brief import channel_brief_for  # noqa: PLC0415
        tone = (channel_brief_for(channel_id) or {}).get("tone", "") or ""
        from hypogeum.channel_creative_brief import _gentle, _investigative  # noqa: PLC0415
        if _gentle(tone):
            return dict(_GENTLE_OUTRO)
        if _investigative(tone):
            return dict(_INVESTIGATIVE_OUTRO)
    except Exception:  # noqa: BLE001
        pass
    return dict(_DEFAULT_OUTRO)


def _generated_outro_policy(
    channel_id: Optional[str], config=None,
) -> Optional[dict]:
    """The resolved generated-outro policy for this channel, or None if the
    channel gets no generated outro.

    Resolution order:
      1. channel_packages/<id>/outro_policy.json — {"mode": "none"} opts out
         entirely; any recognised "follow_line" / "tie_directive" there
         override the default wording.
      2. Otherwise every SHORT_NATIVE channel gets a generated outro with a
         tone-appropriate default line (the general rule).
      3. A non-short-native channel gets one only via an explicit
         {"mode": "generated"} file.
    """
    if not channel_id:
        return None

    file_pol: dict = {}
    try:
        from hypogeum.channel_package_scaffold import channel_outro_policy  # noqa: PLC0415
        _p = channel_outro_policy(channel_id)
        if isinstance(_p, dict):
            file_pol = _p
    except Exception:  # noqa: BLE001
        file_pol = {}

    mode = str(file_pol.get("mode", "") or "").lower()
    if mode in _OUTRO_OPT_OUT_MODES:
        return None

    short_native = getattr(config, "SHORT_NATIVE_MODE", False) is True
    if not short_native and mode != "generated":
        return None

    default = _default_outro_for_channel(channel_id)
    return {
        "mode": "generated",
        "follow_line": (file_pol.get("follow_line") or default["follow_line"]).strip(),
        "tie_directive": (file_pol.get("tie_directive") or default["tie_directive"]).strip(),
    }


def _append_generated_outro(
    script_package: dict, config, log, *, channel_id: Optional[str] = None,
) -> None:
    """Append the channel's closing outro to the script as its own beats.

    The story's own final beat (written by the LLM) leaves the protagonist
    settled to sleep; this adds the channel's fixed follow/like line on top,
    split into per-clip-sized beats so each gets its own generated shot
    through the normal pipeline (scene planner, cinematic director, VPD,
    assembly) — no static outro.mp4, no coverage-fill. Runs AFTER
    _enforce_short_native_budget so the outro is never trimmed. General rule
    for every short-native channel; a channel opts out with
    channel_packages/<id>/outro_policy.json {"mode": "none"}.
    """
    pol = _generated_outro_policy(channel_id, config)
    if pol is None:
        return
    follow_line = (pol.get("follow_line") or "").strip()
    if not follow_line:
        return

    sections = script_package.get("sections", []) or []
    if not sections:
        return
    last = sections[-1]
    beats = last.setdefault("narration_beats", [])

    # already appended (idempotent resume)?
    if any((b.get("beat_type") == OUTRO_BEAT_TYPE) for b in beats):
        return

    _beat_secs, per_beat_words, _wps = _short_native_beat_budget(config, channel_id)

    # Split the follow line into clip-sized chunks, breaking on sentence then
    # comma boundaries, so each outro beat is sayable in one clip.
    chunks: list[str] = []
    for sentence in (s for s in _SENTENCE_SPLIT.split(follow_line) if s.strip()):
        words = sentence.split()
        if len(words) <= per_beat_words:
            chunks.append(sentence.strip())
            continue
        cur: list[str] = []
        for part in sentence.replace(", ", ",\x00").split("\x00"):
            pw = part.split()
            if cur and len(" ".join(cur).split()) + len(pw) > per_beat_words:
                chunks.append(" ".join(cur).strip())
                cur = []
            cur.append(part.strip())
        if cur:
            chunks.append(" ".join(cur).strip())
    # any chunk still over the cap (a long comma-free clause) → split into
    # ceil(words/cap) roughly-equal parts (avoids a 1-word orphan tail)
    import math as _math  # noqa: PLC0415
    _bounded: list[str] = []
    for c in (x for x in chunks if x):
        w = c.split()
        if len(w) <= per_beat_words:
            _bounded.append(c)
            continue
        n = _math.ceil(len(w) / per_beat_words)
        size = _math.ceil(len(w) / n)
        for j in range(0, len(w), size):
            _bounded.append(" ".join(w[j:j + size]))
    # greedily merge adjacent chunks that still fit — fewest outro clips
    merged: list[str] = []
    for c in (x for x in _bounded if x):
        if merged and len((merged[-1] + " " + c).split()) <= per_beat_words:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    chunks = merged or [follow_line]

    for i, text in enumerate(chunks, start=1):
        beats.append({
            "beat_id": f"b_outro_{i}",
            "text": text,
            "beat_type": OUTRO_BEAT_TYPE,
        })
    log.info(
        "se_generated_outro_appended",
        outro_beats=len(chunks),
        per_beat_words_max=per_beat_words,
        channel_id=channel_id,
    )
