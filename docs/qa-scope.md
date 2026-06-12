# QA Scope — Current vs. Future

## Current QA (M5.1 — QA Agent, R47)

The QA Agent (`hypogeum/agents/qa_agent.py`) is a **structural readiness gate**.

It checks:
- Required fields present in all pipeline packages
- Asset counts meet minimums
- No forbidden content markers
- Publishing prerequisites met

It does **not** check:
- Actual video quality or visual coherence
- Audio sync or narration quality
- Brand guideline compliance (that is BGA's role)
- Viewer experience or engagement signals

The QA Agent gates the pipeline: a FAIL verdict halts publishing.

## Future QA (M9.1 — deferred)

Full media QA is planned for M9.1 and will include:
- Frame-level video quality checks
- Narration clarity and sync validation
- End-to-end render review
- Brand Guardian integration (cross-check)

M9.1 is deferred until the channel has sufficient volume (20+ episodes)
to warrant the investment. See `docs/future-roadmap.md` for the full roadmap.

## Agent Naming

- **QA Agent** (R47, current) — structural gate
- **QA Agent v2** (M9.1, future) — full media quality review
