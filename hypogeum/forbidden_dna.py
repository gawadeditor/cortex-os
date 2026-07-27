"""
hypogeum/forbidden_dna.py — Hypogeum Forbidden DNA v1 (2026-07-17, Phase B2.2)

The single canonical aggregation/deduplication point for every other DNA
module's negative items, per the frozen contract in
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md
(Deliverable 1, "Forbidden DNA" section). Extracted from the inline dedup
loop that previously lived in hypogeum/video_prompt_writer.py's Section 9
assembly — same algorithm, same ordering, relocated so it has one named,
independently-testable home instead of being reimplemented wherever a
forbidden section is assembled.

Not a content source. This module has no rules of its own, no positive
directive, and no forbidden-item list of its own — unlike every other DNA
module (video_dna.py, composition_dna.py, brand/visual_identity.py), it owns
zero creative/policy content. Its only job is combining lists that other
modules have already computed.

Ownership boundary — what this module does NOT own (per the frozen contract):
    - deciding what is forbidden                    -> each individual DNA module
    - provider-specific negative syntax ([no X] tags,
      descriptor-positive phrasing, or any other
      provider-preferred rendering)                  -> Provider Adapters (future, B3)
    - provider vocabulary translation                -> Provider Adapters (future, B3)
    - the "FORBIDDEN:" label / "; "-joined string
      rendering of the collected list                -> hypogeum.video_prompt_writer
      today (Section 9), and the future Prompt Compiler's Stage 3/4 — this
      module returns a plain list[str], never a formatted string
    - camera, motion, lighting, historical,
      composition, or brand policy content            -> the respective DNA modules
    - validation decisions (pass/fail gating)         -> future B5 Validation Layer

This module never imports the DNA modules whose output it aggregates —
callers (hypogeum.video_prompt_writer today; the Prompt Compiler once B4
exists) compute each source list themselves and pass it in. This keeps
Forbidden DNA a generic, zero-knowledge collector with no dependency on any
content decision, and trivially testable in isolation with synthetic lists.

Public API (exact signature, per B1_3_ARCHITECTURE_CONTRACT.md):
    collect_forbidden_items(*sources: list) -> list[str]
"""
from __future__ import annotations


def collect_forbidden_items(*sources: list) -> list:
    """
    Collect and deduplicate forbidden items from any number of
    already-computed DNA-module forbidden-item lists.

    Args:
        *sources: any number of list[str] forbidden-item collections, e.g.
            video_dna.get_video_dna_forbidden_items(),
            composition_dna.get_composition_forbidden_items(),
            visual_identity.get_adaptive_video_forbidden_items(). A future
            DNA module's forbidden-item getter is supported the same way —
            simply pass its result as one more positional argument; this
            function's signature never needs to change. Order of arguments
            determines final ordering. A None or empty source is skipped
            without error.

    Returns:
        A new list[str] — always a fresh list, never a reference into any
        source list (so mutating the source afterward, or mutating the
        returned list, cannot affect a previous or future call) — with
        duplicates removed (first occurrence wins, stable order preserved)
        and no other item reordered.
    """
    collected: list = []
    for source in sources:
        if not source:
            continue
        for item in source:
            if item not in collected:
                collected.append(item)
    return collected
