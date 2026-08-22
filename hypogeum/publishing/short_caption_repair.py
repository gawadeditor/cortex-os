"""
Local repair primitives for an over-length Short: choose a coherent,
sentence-complete cutoff (never a blind hard cut at the duration limit),
truncate its already-timed .ass caption track to match, and build the
ffmpeg command that trims the original render and burns the truncated
captions in a single pass.

Pure, network-free, provider-free. No file is written or ffmpeg process
started by this module itself -- callers own execution (see
scripts/repair_ep_f3619022_shorts.py for the one-off ep_f3619022 run) so
every step stays independently testable without a real ffmpeg binary.

Companion to hypogeum.publishing.zernio_short_repair, which builds the
proposed Zernio update body once a caller has produced a repaired local
file using this module's outputs and independently verified it (burned
captions, duration, visual coverage -- via the SAME pixel-check helpers
hypogeum.agents.production_output_gate already uses).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from hypogeum.agents.production_output_gate import _ass_time_to_seconds
from hypogeum.agents.video_renderer import _escape_ffmpeg_filter_path, _parse_srt_cues


class NoCoherentCutoffError(ValueError):
    """No caption cue both fits within max_duration_s and completes a
    sentence -- refuses to guess a mid-sentence hard cut."""


@dataclass(frozen=True)
class CutoffPlan:
    cutoff_cue_index: int       # 0-based index of the last KEPT cue
    kept_cue_count: int         # cutoff_cue_index + 1 -- how many leading cues survive
    total_cue_count: int
    last_kept_cue_end_s: float  # end time of the last kept cue -- a sentence-complete boundary
    trim_seconds: float         # last_kept_cue_end_s + trailing_buffer_s -- the ffmpeg -t value


def select_coherent_cutoff(
    srt_text: str, *, max_duration_s: float, trailing_buffer_s: float = 0.6,
) -> CutoffPlan:
    """
    Choose the LAST caption cue that (a) ends at or before
    max_duration_s - trailing_buffer_s, and (b) closes a complete
    sentence/clause (terminal punctuation, never an em dash, which these
    SRTs use exclusively to signal the sentence continues into the next
    cue). The video is later trimmed to last_kept_cue_end_s +
    trailing_buffer_s -- always <= max_duration_s -- so narration always
    finishes speaking a full sentence before the picture cuts, instead of
    a blind cut at the duration ceiling itself.

    Raises NoCoherentCutoffError when no cue qualifies -- callers must
    not fall back to a mid-sentence cut.
    """
    cues = _parse_srt_cues(srt_text)
    if not cues:
        raise NoCoherentCutoffError("caption track has no cues to select a cutoff from")

    best_index = None
    for i, (_start, end, text) in enumerate(cues):
        end_s = _ass_time_to_seconds(end)
        if end_s + trailing_buffer_s > max_duration_s:
            break  # cues are chronological -- nothing later can qualify either
        if _ends_a_complete_sentence(text):
            best_index = i
    if best_index is None:
        raise NoCoherentCutoffError(
            f"no cue completes a sentence within max_duration_s={max_duration_s}s "
            f"(trailing_buffer_s={trailing_buffer_s}s) -- refusing a blind hard cut"
        )
    end_s = _ass_time_to_seconds(cues[best_index][1])
    return CutoffPlan(
        cutoff_cue_index=best_index,
        kept_cue_count=best_index + 1,
        total_cue_count=len(cues),
        last_kept_cue_end_s=round(end_s, 3),
        trim_seconds=round(end_s + trailing_buffer_s, 3),
    )


def _ends_a_complete_sentence(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith("--") or stripped.endswith("-"):
        return False
    return stripped.endswith((".", "!", "?", '."', '!"', '?"'))


def build_trimmed_ass_text(ass_text: str, keep_cue_count: int) -> str:
    """
    Return `ass_text` with only the first `keep_cue_count` [Events]
    Dialogue lines kept -- the [Script Info]/[V4+ Styles] header
    (including the already-baked-in burn-in style) is untouched.
    `keep_cue_count` must be a CutoffPlan.kept_cue_count from
    select_coherent_cutoff() run against the SAME source .srt, so the
    burned captions end exactly where the trimmed video does.
    """
    if keep_cue_count < 1:
        raise ValueError(f"keep_cue_count must be >= 1, got {keep_cue_count}")
    out: List[str] = []
    dialogue_seen = 0
    for line in ass_text.splitlines():
        if line.startswith("Dialogue:"):
            if dialogue_seen >= keep_cue_count:
                continue
            dialogue_seen += 1
        out.append(line)
    if dialogue_seen < keep_cue_count:
        raise ValueError(
            f"ass_text has only {dialogue_seen} Dialogue line(s), fewer than "
            f"requested keep_cue_count={keep_cue_count}"
        )
    return "\n".join(out) + "\n"


def build_repair_ffmpeg_command(
    input_path: Path, trimmed_ass_path: Path, output_path: Path, trim_seconds: float,
    *, ffmpeg_binary: str,
) -> List[str]:
    """
    Trim `input_path` to `trim_seconds` and burn `trimmed_ass_path` in,
    in a single re-encode pass (frame-accurate trim requires a re-encode
    regardless, so burning captions in the same pass costs nothing
    extra). `ffmpeg_binary` must be a libass-enabled build -- this
    function never assumes or defaults to one; the caller is responsible
    for resolving it (see hypogeum.agents.video_renderer.
    assert_subtitle_filter_available()).
    """
    escaped = _escape_ffmpeg_filter_path(trimmed_ass_path)
    return [
        ffmpeg_binary, "-y",
        "-i", str(input_path),
        "-t", f"{trim_seconds:.3f}",
        "-vf", f"subtitles=filename='{escaped}'",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
