"""
hypogeum/hook_patterns.py — the Hook Pattern Library.

Ten opening-line moves distilled from a widely-shared hook breakdown, turned
into channel-agnostic writing rules. The Narrative Architect and Script Engine
read this (via channel_creative_brief._virality_directive) and open every
script's first spoken line with one of these patterns — best-effort, fail-open:
if none fits the topic without distorting the story or promising a payoff the
episode cannot deliver, the writer skips the library, writes the strongest
honest opening line, and records `hook_pattern: "none"` plus what was
considered.

This never relaxes the existing hard rules (no bare date/place opener, spine
must pay off, script must end on a stinger) — those stay fail-closed in
script_engine.ScriptViralityError. The library is a menu of ways to *satisfy*
the hook rule, never a licence to break the truth guardrail. Four high-clickbait
patterns from the source list (foreshadow-the-shock, finish-and-you'll-see,
exclusion "not for everyone", "this will disturb you") were deliberately left
out.

Public API:
    render_hook_library(kind: str) -> str        # 'investigative' | 'gentle' | 'neutral'
    PATTERN_IDS -> tuple[str, ...]                # valid hook_pattern values (+ 'none')
"""
from __future__ import annotations

# id, name, what it does, investigative form + example, gentle form (or None)
_PATTERNS = [
    (
        "withheld_knowledge",
        "The withheld-early-knowledge open",
        "Frame the episode as knowledge that reorders what the viewer thought they knew.",
        'Investigative: "Everyone who studied this site for 60 years walked past what was under the floor."',
        'Gentle: a character who did not yet know something small and cosy — "Pip didn\'t know the stars needed tucking in too."',
    ),
    (
        "nobody_told_us",
        "The under-reported angle",
        "Point at the part of a familiar story that never gets told.",
        'Investigative: "This is in every textbook. The part they leave out is the strange part."',
        'Gentle: a shared discovery, never a question — "Nobody had noticed the little cloud was tired, so the moon went to check."',
    ),
    (
        "impossible_number",
        "Lead with the exact, hard-to-believe number",
        "Open on a real, sourced figure that sounds impossible or unsettling. House style.",
        'Investigative: "1,800 scrolls. Still sealed. Still unread."',
        'Gentle: a small soothing count — "Seven little stars. One blanket. Not quite enough to go around."',
    ),
    (
        "before_after_knowing",
        "Before / after knowing",
        "Contrast the world before the finding with the world after it.",
        'Investigative: "For 200 years it was a Roman toy. Then someone weighed it."',
        'Gentle: "Before the walk, the room felt too big. After, it felt just right." (can double as the closing refrain.)',
    ),
    (
        "result_first",
        "Result first, method second",
        "State the finding in the first line; spend the episode on how it was reached.",
        'Investigative: "The body was never in the tomb. Here is how we know that."',
        'Gentle: open on the settled ending image — "By the end, every star was asleep. It started with one that wouldn\'t settle."',
    ),
    (
        "knowledge_gap",
        "The knowledge gap",
        "There is what most people believe and what the people closest to it believe.",
        'Investigative: "Tourists are told one date. The archaeologists argue about a different one."',
        'Gentle: two friends, one already sleepy and one not — the gap is cosy, never adversarial.',
    ),
    (
        "direct_question",
        "The sharp direct question",
        "A specific question the episode fully answers — sharper than a generic one.",
        'Investigative: "Why carve a room the size of a house and then seal it with nothing inside?"',
        None,  # gentle bans questions in the open
    ),
    (
        "accepted_fact_wrong",
        "Accepted fact, then the correction",
        "State the thing everyone believes, then the real scholarly position that overturns it. Strongest pattern for investigative.",
        'Investigative: "Farming came first, then cities and temples. Gobekli Tepe was built in the wrong order."',
        'Gentle: a soft surprise, no "wrong" — "Everyone thought the moon lit the sky. Turns out a very small cloud helped."',
    ),
    (
        "first_time_reveal",
        "The rarely-told reveal",
        "Frame the core as something that seldom makes it into the mainstream version.",
        'Investigative: "This rarely makes it into the documentaries."',
        'Gentle: "Here is a secret the stars don\'t usually tell." (warm, not suspenseful.)',
    ),
    (
        "contradicts_assumption",
        "The contradiction of assumption",
        "Lead with a true claim that directly contradicts what the viewer assumes. This is the spine of the whole library.",
        'Investigative: already the first instruction of the investigative directive.',
        None,  # tension-based; not for gentle
    ),
]

PATTERN_IDS = tuple(p[0] for p in _PATTERNS) + ("none",)


def render_hook_library(kind: str) -> str:
    """A text block for injection into the channel virality directive.
    kind: 'investigative' (all 10 patterns), 'gentle' (softened forms only),
    'neutral' (a short generic version)."""
    if kind == "neutral":
        return (
            "\n- HOOK PATTERNS: open the first line on the most specific, most "
            "interesting true thing in the story. If a stronger framing does not "
            "present itself, that is enough — never force a formula that distorts "
            "the story. Record `hook_pattern` in the script package (or \"none\")."
        )

    gentle = kind == "gentle"
    lines = [
        "",
        "- HOOK PATTERNS (first spoken line): choose ONE deliberate opening move "
        "from the list below and write beat 1 to it. Pick the pattern that best "
        "fits THIS topic. Record which one in the script package as "
        "`hook_pattern` (use the id in [brackets]).",
    ]
    if gentle:
        lines.append(
            "  Your existing warm opener rule still governs — no question, no "
            "tension, no promise to keep watching. These patterns are optional "
            "flavours of that opener, not replacements. Several have no gentle "
            "form; use them only where a soft version is shown.")
    for pid, name, does, inv, gen in _PATTERNS:
        form = gen if gentle else inv
        if gentle and form is None:
            continue
        lines.append(f"  [{pid}] {name} — {does}")
        lines.append(f"      {form}")
    lines.append(
        "  FAIL-OPEN: if no pattern fits without distorting the story, inventing "
        "beyond the research/story, or promising a payoff the episode cannot "
        "deliver — skip the library, write the strongest honest opening line, "
        "and set `hook_pattern` to \"none\" with a one-line note of what you "
        "considered. Skipping is allowed; forcing a weak or dishonest hook is not.")
    if not gentle:
        lines.append(
            "  The hook may dramatize a real, sourced fact or a real open "
            "question. It must never fabricate, overstate past the evidence, or "
            "foreshadow a payoff bigger than the one the episode delivers.")
    return "\n".join(lines)
