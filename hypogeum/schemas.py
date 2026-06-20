"""
hypogeum/schemas.py — Schema validators for all pipeline output packages

Every agent calls validate(schema_name, package) before sealing its output.
Validation is fail-fast: the first structural problem raises SchemaError immediately.

Validated schemas (25 total):
  1.  validation_scorecard          — Executive Producer topic validation result
  2.  topic_brief                   — Executive Producer topic brief
  3.  package_deficiency_report     — PDR issued by any agent
  4.  research_package              — Research Director output
  5.  narrative_package             — Narrative Architect output
  6.  script_package                — Script Engine output
  7.  visual_package                — Visual Package Director output
  8.  thumbnail_handoff_brief       — Handoff Brief extracted from Visual Package; TD's sole input
  9.  thumbnail_generation_plan     — Thumbnail Director Phase 1: Claude's ranked candidate selection
  10. thumbnail_package             — Thumbnail Director output (final)
  11. narration_package             — Audio Agent output
  12. final_render_package          — Assembly Agent output
  13. publishing_record             — Publishing Agent output
  14. media_specs_report            — Media Specs QA Agent (Agent 36) per-file inspection result
  15. render_output_manifest         — Assembly Agent render facts (files, status, gaps)
  16. render_review_verdict          — Final Render Reviewer verdict; sole source of production_ready
  17. music_track_package            — Music Agent output; all sourced music tracks per section
  18. audio_qa_verdict               — Audio QA Agent output; sole source of narration production_ready
  19. caption_package                 — Caption Agent output; SRT entries and content checks per component
  20. qa_approval_report              — QA Agent (R47) pre-publish gate verdict; APPROVED/WARNING/REJECTED
  21. compliance_report               — Rights & Compliance Agent (R48) verdict; APPROVED/WARNING/REJECTED
  22. archive_record                  — Archive Agent (R49) post-publish record; ARCHIVED/WARNING/FAILED
  23. finance_record                  — Finance Agent (R50) budget gate + cost ledger; AUTHORIZED/WARNING/BLOCKED/RECORDED
  24. bga_compliance_report           — Brand Guardian Agent (v1A) Pass 1/2 audit report; APPROVED/APPROVED_WITH_FLAGS/REJECTED
  25. signal_record                   — Phase 5B normalized analytics signal; ingested from analytics_snapshot

Design principles:
  - Stdlib only; no external JSON Schema library
  - Checks presence and type of every required top-level section
  - Checks presence of key fields within critical subsections
  - Validates minimum / exact list length constraints where frozen governance specifies them
  - Does NOT validate every nested leaf field — agents ensure their own correctness;
    the validator guards against missing sections that would silently corrupt a downstream agent

Usage:
    from hypogeum.schemas import validate, SchemaError

    try:
        validate("script_package", my_package)
    except SchemaError as e:
        log.error("schema_validation_failed", schema=e.schema_name,
                  path=e.path, reason=e.reason)
        raise
"""

from __future__ import annotations

from typing import Any, Tuple, Type, Union


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class SchemaError(ValueError):
    """
    Raised when a package fails schema validation.

    Attributes:
        schema_name — the schema being validated (e.g., "script_package")
        path        — dot-notation path to the failing field (e.g., "shorts_scripts[0].text_overlays")
        reason      — human-readable description of the failure
    """
    def __init__(self, schema_name: str, path: str, reason: str) -> None:
        self.schema_name = schema_name
        self.path = path
        self.reason = reason
        super().__init__(f"[{schema_name}] {path}: {reason}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_VALIDATORS = {}  # populated at module bottom after all _validate_* are defined


def validate(schema_name: str, package: dict) -> None:
    """
    Validate a package dict against the named frozen schema.

    Args:
        schema_name: One of the 11 known schema names (see module docstring).
        package:     The package dict produced by the agent.

    Returns:
        None — validation passed.

    Raises:
        ValueError    — unknown schema_name.
        SchemaError   — a required field is missing or has the wrong type.
        TypeError     — package is not a dict.
    """
    if not isinstance(package, dict):
        raise TypeError(f"package must be a dict, got {type(package).__name__}")
    if schema_name not in _VALIDATORS:
        raise ValueError(
            f"Unknown schema: {schema_name!r}. "
            f"Known schemas: {sorted(_VALIDATORS)}"
        )
    _VALIDATORS[schema_name](package)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_Types = Union[type, Tuple[type, ...]]


def _type_label(types: _Types) -> str:
    if isinstance(types, type):
        return types.__name__
    return " | ".join(t.__name__ for t in types)


def _req(d: dict, schema: str, key: str, types: _Types, path: str = "") -> Any:
    """
    Assert d[key] exists and isinstance(d[key], types).
    Returns the value on success; raises SchemaError on failure.
    """
    full = f"{path}.{key}".lstrip(".")
    if key not in d:
        raise SchemaError(schema, full, "missing required field")
    val = d[key]
    if not isinstance(val, types):
        raise SchemaError(
            schema, full,
            f"expected {_type_label(types)}, got {type(val).__name__}"
        )
    return val


def _req_str(d: dict, schema: str, key: str, path: str = "") -> str:
    return _req(d, schema, key, str, path)


def _req_dict(d: dict, schema: str, key: str, path: str = "") -> dict:
    return _req(d, schema, key, dict, path)


def _req_list(d: dict, schema: str, key: str, path: str = "", min_length: int = 0) -> list:
    """Assert d[key] is a list with at least min_length items."""
    val = _req(d, schema, key, list, path)
    full = f"{path}.{key}".lstrip(".")
    if len(val) < min_length:
        raise SchemaError(
            schema, full,
            f"must have at least {min_length} item(s), got {len(val)}"
        )
    return val


def _req_exact(
    d: dict, schema: str, key: str, length: int, path: str = "",
    *, allow_zero: bool = False,
) -> list:
    """Assert d[key] is a list with exactly length items.

    When allow_zero=True, an empty list is also accepted (Episode Zero /
    no-shorts mode produces 0 shorts; production always produces exactly
    length items).
    """
    val = _req(d, schema, key, list, path)
    full = f"{path}.{key}".lstrip(".")
    if len(val) != length and not (allow_zero and len(val) == 0):
        allowed = f"exactly {length} item(s)" + (" or 0" if allow_zero else "")
        raise SchemaError(schema, full, f"must have {allowed}, got {len(val)}")
    return val


def _req_in(d: dict, schema: str, key: str, allowed: tuple, path: str = "") -> str:
    """Assert d[key] is a str and its value is in allowed."""
    val = _req_str(d, schema, key, path)
    full = f"{path}.{key}".lstrip(".")
    if val not in allowed:
        raise SchemaError(
            schema, full,
            f"must be one of {allowed}, got {val!r}"
        )
    return val


def _req_numeric(d: dict, schema: str, key: str, path: str = "") -> Any:
    """Assert d[key] is int or float."""
    return _req(d, schema, key, (int, float), path)


# ---------------------------------------------------------------------------
# Schema validators — one per frozen schema
# ---------------------------------------------------------------------------

def _validate_validation_scorecard(p: dict) -> None:
    s = "validation_scorecard"
    _req_str(p, s, "scorecard_id")
    _req_str(p, s, "topic_id")
    _req_dict(p, s, "dimensions")
    dims = p["dimensions"]
    for dim in (
        "audience_demand", "curiosity_potential", "mystery_potential",
        "visual_potential", "differentiation_opportunity", "shorts_potential",
    ):
        _req(dims, s, dim, (int, float), path="dimensions")
    _req_numeric(p, s, "overall_score")
    _req_in(p, s, "decision", ("PASS", "WARN", "REJECT"))
    _req_str(p, s, "issued_at")
    _req_str(p, s, "issued_by")


def _validate_topic_brief(p: dict) -> None:
    s = "topic_brief"
    _req_str(p, s, "topic_id")
    _req_str(p, s, "episode_title")
    _req_str(p, s, "topic_category")
    _req_str(p, s, "logline")
    _req_str(p, s, "audience_demand_signal")
    _req_str(p, s, "curiosity_hook")
    _req_str(p, s, "mystery_anchor")
    _req_str(p, s, "differentiation_hypothesis")
    _req(p, s, "research_depth_flags", (dict, list))
    _req_list(p, s, "shorts_directions", min_length=2)
    _req_str(p, s, "issued_at")
    _req_str(p, s, "issued_by")


def _validate_package_deficiency_report(p: dict) -> None:
    s = "package_deficiency_report"
    _req_str(p, s, "report_id")
    _req_str(p, s, "issuing_agent")
    _req_str(p, s, "target_package")
    _req_str(p, s, "issued_at")
    _req_in(p, s, "severity", ("BLOCKING", "MAJOR", "MINOR"))
    items = _req_list(p, s, "deficiency_items", min_length=1)
    for i, item in enumerate(items):
        path = f"deficiency_items[{i}]"
        if not isinstance(item, dict):
            raise SchemaError(s, path, "each deficiency item must be a dict")
        _req_str(item, s, "item_id", path=path)
        _req_str(item, s, "description", path=path)
    _req_str(p, s, "routing_target")


def _validate_research_package(p: dict) -> None:
    s = "research_package"
    _req_dict(p, s, "research_run_metadata")
    _req_dict(p, s, "research_strategy")
    claims = _req_list(p, s, "claims", min_length=1)
    for i, claim in enumerate(claims):
        path = f"claims[{i}]"
        if not isinstance(claim, dict):
            raise SchemaError(s, path, "each claim must be a dict")
        _req_str(claim, s, "claim_id", path=path)
        _req_str(claim, s, "text", path=path)
        _req(claim, s, "confidence", (int, float), path=path)
    _req_list(p, s, "contradictions")
    _req_list(p, s, "open_questions")
    _req_list(p, s, "source_registry")
    _req_dict(p, s, "competitive_landscape")
    _req_dict(p, s, "research_run_summary")
    _req_dict(p, s, "package_integrity")


def _validate_narrative_package(p: dict) -> None:
    s = "narrative_package"
    _req_dict(p, s, "episode_metadata")
    _req_dict(p, s, "episode_structure")
    ep_struct = p["episode_structure"]
    _req_str(ep_struct, s, "archetype", path="episode_structure")
    _req_list(ep_struct, s, "sections", path="episode_structure", min_length=1)
    _req_dict(p, s, "narrative_arc")
    arc = p["narrative_arc"]
    for field in ("setup", "escalation", "revelation", "resolution"):
        _req(arc, s, field, (str, dict), path="narrative_arc")
    _req_dict(p, s, "reveal_architecture")
    shorts = _req_exact(p, s, "shorts_selection", 3, allow_zero=True)
    for i, short in enumerate(shorts):
        path = f"shorts_selection[{i}]"
        if not isinstance(short, dict):
            raise SchemaError(s, path, "each shorts_selection entry must be a dict")
        _req_str(short, s, "short_id", path=path)
        _req(short, s, "text_overlay_plan", dict, path=path)
    _req_list(p, s, "narrative_signals")
    _req_dict(p, s, "differentiation_record")
    _req_list(p, s, "narrative_integrity_log")
    _req_dict(p, s, "traceability")


def _validate_script_package(p: dict) -> None:
    s = "script_package"
    _req_dict(p, s, "episode_metadata")
    sections = _req_list(p, s, "sections", min_length=1)
    for i, sec in enumerate(sections):
        path = f"sections[{i}]"
        if not isinstance(sec, dict):
            raise SchemaError(s, path, "each section must be a dict")
        _req_str(sec, s, "section_id", path=path)
        _req_str(sec, s, "title", path=path)
        _req_list(sec, s, "narration_beats", path=path, min_length=1)
    shorts = _req_exact(p, s, "shorts_scripts", 3)
    for i, short in enumerate(shorts):
        path = f"shorts_scripts[{i}]"
        if not isinstance(short, dict):
            raise SchemaError(s, path, "each short must be a dict")
        _req_str(short, s, "short_id", path=path)
        _req_list(short, s, "narration_beats", path=path, min_length=1)
        _req_list(short, s, "text_overlays", path=path)
        for j, overlay in enumerate(short["text_overlays"]):
            opath = f"{path}.text_overlays[{j}]"
            if not isinstance(overlay, dict):
                raise SchemaError(s, opath, "each text overlay must be a dict")
            _req_str(overlay, s, "overlay_id", path=opath)
            # overlay_type must be a non-empty string (open enum per V1.2 constraints)
            ot = _req_str(overlay, s, "overlay_type", path=opath)
            if not ot.strip():
                raise SchemaError(s, f"{opath}.overlay_type", "must be a non-empty string")
            _req_str(overlay, s, "text", path=opath)
            _req_str(overlay, s, "beat_reference", path=opath)
            _req_numeric(overlay, s, "display_duration_seconds", path=opath)
    _req_dict(p, s, "traceability_manifest")
    _req_dict(p, s, "script_integrity")


def _validate_visual_package(p: dict) -> None:
    s = "visual_package"
    _req_dict(p, s, "package_metadata")
    _req_dict(p, s, "episode_metadata")
    _req_dict(p, s, "visual_continuity_record")
    vcr = p["visual_continuity_record"]
    _req_list(vcr, s, "locked_entities", path="visual_continuity_record")
    for i, ent in enumerate(vcr.get("locked_entities", [])):
        path = f"visual_continuity_record.locked_entities[{i}]"
        if not isinstance(ent, dict):
            raise SchemaError(s, path, "each locked entity must be a dict")
        _req_str(ent, s, "entity_id", path=path)
        _req_in(ent, s, "lock_state", ("provisional", "promoted", "final"), path=path)
        # reference_image_asset_id: str or null (V1.3 amendment)
        if "reference_image_asset_id" in ent and ent["reference_image_asset_id"] is not None:
            if not isinstance(ent["reference_image_asset_id"], str):
                raise SchemaError(s, f"{path}.reference_image_asset_id",
                                  "must be str or null")
    _req_list(p, s, "scenes", min_length=1)
    for i, scene in enumerate(p["scenes"]):
        path = f"scenes[{i}]"
        if not isinstance(scene, dict):
            raise SchemaError(s, path, "each scene must be a dict")
        _req_str(scene, s, "scene_id", path=path)
        _req_in(scene, s, "source_type", ("main_episode", "short"), path=path)
        _req_str(scene, s, "derivation_source", path=path)
    _req_dict(p, s, "asset_manifest")
    _req_dict(p, s, "provider_routing_manifest")
    _req_dict(p, s, "visual_integrity_log")
    thb = _req_dict(p, s, "thumbnail_handoff_brief")
    _req_str(thb, s, "central_curiosity_gap_statement", path="thumbnail_handoff_brief")
    _req_list(thb, s, "candidates", path="thumbnail_handoff_brief", min_length=2)
    _req_exact(thb, s, "short_candidates", 3, path="thumbnail_handoff_brief", allow_zero=True)
    _req_dict(p, s, "package_integrity")
    pi = p["package_integrity"]
    _req(pi, s, "production_ready", bool, path="package_integrity")


def _validate_thumbnail_handoff_brief(p: dict) -> None:
    s = "thumbnail_handoff_brief"
    _req_str(p, s, "thumbnail_handoff_brief_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "visual_package_id")
    _req_str(p, s, "central_curiosity_gap_statement")
    cands = _req_list(p, s, "candidates", min_length=2)
    for i, c in enumerate(cands):
        path = f"candidates[{i}]"
        if not isinstance(c, dict):
            raise SchemaError(s, path, "each candidate must be a dict")
        _req_str(c, s, "candidate_id", path=path)
        _req_in(c, s, "candidate_type",
                ("literal_scene", "inferred_moment", "symbolic_representation"),
                path=path)
    short_cands = _req_exact(p, s, "short_candidates", 3, allow_zero=True)
    for i, sc in enumerate(short_cands):
        path = f"short_candidates[{i}]"
        if not isinstance(sc, dict):
            raise SchemaError(s, path, "each short_candidates entry must be a dict")
        _req_str(sc, s, "short_id", path=path)
        _req_list(sc, s, "candidates", path=path, min_length=2)
    _req_dict(p, s, "primary_subject_identity")
    _req_str(p["primary_subject_identity"], s, "entity_id",
             path="primary_subject_identity")
    _req_list(p, s, "negative_constraints")


def _validate_text_overlay(overlay: object, schema: str, path: str) -> None:
    """Validate a non-null text_overlay dict has required text and placement fields."""
    if not isinstance(overlay, dict):
        raise SchemaError(schema, path, "text_overlay must be a dict when non-null")
    _req_str(overlay, schema, "text", path=path)
    _req_in(overlay, schema, "placement", ("top", "bottom"), path=path)


def _validate_thumbnail_generation_plan(p: dict) -> None:
    s = "thumbnail_generation_plan"
    _req_str(p, s, "thumbnail_generation_plan_id")
    _req_str(p, s, "episode_id")
    main = _req_dict(p, s, "main_episode")
    raw_main_cands = main.get("ranked_candidates")
    if not isinstance(raw_main_cands, list):
        raise SchemaError(s, "main_episode.ranked_candidates", "must be a list")
    if len(raw_main_cands) != 3:
        raise SchemaError(
            s, "main_episode.ranked_candidates",
            f"must have exactly 3 entries for A/B/C testing (got {len(raw_main_cands)})"
        )
    main_cands = raw_main_cands
    for i, c in enumerate(main_cands):
        path = f"main_episode.ranked_candidates[{i}]"
        if not isinstance(c, dict):
            raise SchemaError(s, path, "each ranked_candidate must be a dict")
        _req_str(c, s, "candidate_id", path=path)
        _req_str(c, s, "image_prompt",  path=path)
        _req(c, s, "rank", int, path=path)
        _req_in(c, s, "aspect_ratio", ("16:9",), path=path)
        # ALL three main episode candidates MUST have non-null text_overlay —
        # every candidate carries its own hook text so any of the three can be
        # published without missing the on-screen title band.
        overlay = c.get("text_overlay")
        if overlay is None:
            raise SchemaError(
                s, f"{path}.text_overlay",
                "all main_episode candidates must have non-null text_overlay "
                "(hook text required on every generated candidate)"
            )
        _validate_text_overlay(overlay, s, f"{path}.text_overlay")
    shorts = _req_exact(p, s, "shorts", 3, allow_zero=True)
    for i, sh in enumerate(shorts):
        path = f"shorts[{i}]"
        if not isinstance(sh, dict):
            raise SchemaError(s, path, "each shorts entry must be a dict")
        _req_str(sh, s, "short_id", path=path)
        sh_cands = _req_list(sh, s, "ranked_candidates", path=path, min_length=1)
        for j, c in enumerate(sh_cands):
            cpath = f"shorts[{i}].ranked_candidates[{j}]"
            if not isinstance(c, dict):
                raise SchemaError(s, cpath, "each ranked_candidate must be a dict")
            _req_str(c, s, "candidate_id", path=cpath)
            _req_str(c, s, "image_prompt",  path=cpath)
            _req(c, s, "rank", int, path=cpath)
            _req_in(c, s, "aspect_ratio", ("9:16",), path=cpath)
            if "text_overlay" in c and c["text_overlay"] is not None:
                _validate_text_overlay(c["text_overlay"], s, f"{cpath}.text_overlay")


def _validate_thumbnail_package(p: dict) -> None:
    s = "thumbnail_package"
    _req_str(p, s, "thumbnail_package_id")
    _req_str(p, s, "episode_id")
    main = _req_dict(p, s, "main_thumbnail")
    _req_str(main, s, "asset_id", path="main_thumbnail")
    shorts = _req_exact(p, s, "short_thumbnails", 3, allow_zero=True)
    for i, st in enumerate(shorts):
        path = f"short_thumbnails[{i}]"
        if not isinstance(st, dict):
            raise SchemaError(s, path, "each short_thumbnail must be a dict")
        _req_str(st, s, "short_id", path=path)
        _req_str(st, s, "asset_id", path=path)
    # Validate candidates[] — the A/B/C set generated for dashboard display and QA.
    # Must be present and non-empty; count may be less than 3 in smoke-test mode.
    cands = p.get("candidates")
    if cands is None:
        raise SchemaError(s, "candidates", "missing required field")
    if not isinstance(cands, list):
        raise SchemaError(s, "candidates", "must be a list")
    if len(cands) < 1:
        raise SchemaError(s, "candidates", "must have at least 1 entry")
    for i, c in enumerate(cands):
        cpath = f"candidates[{i}]"
        if not isinstance(c, dict):
            raise SchemaError(s, cpath, "each candidate must be a dict")
        _req_str(c, s, "candidate_id", path=cpath)
        _req(c, s, "rank", int, path=cpath)
        _req(c, s, "selected", bool, path=cpath)
        _req_str(c, s, "status", path=cpath)
    _req_dict(p, s, "package_integrity")
    pi = p["package_integrity"]
    _req(pi, s, "production_ready", bool, path="package_integrity")


# ---------------------------------------------------------------------------
# Thumbnail concept brief — produced by the Thumbnail Strategy Skill (Phase 1)
# ---------------------------------------------------------------------------

_PERMITTED_EMOTIONAL_TRIGGERS = frozenset({
    "awe", "dread", "impossibility", "betrayal", "discovery", "scale", "loss",
})


def _validate_thumbnail_concept_brief(p: dict) -> None:
    s = "thumbnail_concept_brief"
    _req_str(p, s, "thumbnail_concept_brief_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "central_viewer_question")

    raw = p.get("concepts")
    if not isinstance(raw, list):
        raise SchemaError(s, "concepts", "must be a list")
    if len(raw) != 3:
        raise SchemaError(
            s, "concepts",
            f"must have exactly 3 entries for A/B/C testing (got {len(raw)})"
        )

    seen_triggers: set[str] = set()
    seen_ids: set[str] = set()

    for i, c in enumerate(raw):
        path = f"concepts[{i}]"
        if not isinstance(c, dict):
            raise SchemaError(s, path, "each concept must be a dict")

        _req_str(c, s, "concept_id",        path=path)
        _req_str(c, s, "concept_name",       path=path)
        _req_str(c, s, "viewer_promise",     path=path)
        _req_str(c, s, "curiosity_gap",      path=path)
        _req_str(c, s, "visual_thesis",      path=path)

        # emotional_trigger: must be a permitted value, unique across concepts
        trigger = c.get("emotional_trigger")
        if not isinstance(trigger, str) or not trigger:
            raise SchemaError(s, f"{path}.emotional_trigger", "missing required field")
        if trigger not in _PERMITTED_EMOTIONAL_TRIGGERS:
            raise SchemaError(
                s, f"{path}.emotional_trigger",
                f"must be one of {sorted(_PERMITTED_EMOTIONAL_TRIGGERS)}, got {trigger!r}"
            )
        if trigger in seen_triggers:
            raise SchemaError(
                s, f"{path}.emotional_trigger",
                f"emotional_trigger '{trigger}' already used — each concept must use a "
                "distinct emotional register"
            )
        seen_triggers.add(trigger)

        # concept_id: must be unique
        cid = c["concept_id"]
        if cid in seen_ids:
            raise SchemaError(
                s, f"{path}.concept_id",
                f"concept_id '{cid}' is duplicated — all concept_ids must be unique"
            )
        seen_ids.add(cid)

        # suggested_hook_text: required, 3–5 words
        hook = c.get("suggested_hook_text")
        if not isinstance(hook, str) or not hook.strip():
            raise SchemaError(s, f"{path}.suggested_hook_text", "missing required field")
        word_count = len(hook.strip().split())
        if word_count < 3 or word_count > 5:
            raise SchemaError(
                s, f"{path}.suggested_hook_text",
                f"must be 3–5 words (got {word_count}): {hook!r}"
            )


def _validate_narration_package(p: dict) -> None:
    s = "narration_package"
    _req_dict(p, s, "package_metadata")
    meta = p["package_metadata"]
    _req_str(meta, s, "narration_package_id", path="package_metadata")
    _req_str(meta, s, "episode_id", path="package_metadata")
    vc = _req_dict(p, s, "voice_configuration")
    _req_str(vc, s, "voice_id", path="voice_configuration")
    _req_str(vc, s, "environment_source", path="voice_configuration")
    segs = _req_list(p, s, "narration_segments", min_length=1)
    for i, seg in enumerate(segs):
        path = f"narration_segments[{i}]"
        if not isinstance(seg, dict):
            raise SchemaError(s, path, "each segment must be a dict")
        _req_str(seg, s, "segment_id", path=path)
        _req_str(seg, s, "beat_reference", path=path)
        _req_in(seg, s, "source_type", ("main_episode", "short"), path=path)
        _req_in(seg, s, "generation_status",
                ("generated", "failed", "retry_succeeded"), path=path)
    # exactly 4 stitched tracks: 1 main + 3 Shorts
    tracks = _req_exact(p, s, "stitched_tracks", 4)
    for i, track in enumerate(tracks):
        path = f"stitched_tracks[{i}]"
        if not isinstance(track, dict):
            raise SchemaError(s, path, "each stitched track must be a dict")
        _req_str(track, s, "track_id", path=path)
        _req_str(track, s, "audio_asset_id", path=path)
        _req_in(track, s, "source_type", ("main_episode", "short"), path=path)
    # exactly 4 caption entries: 1 main + 3 Shorts
    caps = _req_exact(p, s, "captions", 4)
    for i, cap in enumerate(caps):
        path = f"captions[{i}]"
        if not isinstance(cap, dict):
            raise SchemaError(s, path, "each caption entry must be a dict")
        _req_str(cap, s, "caption_id", path=path)
        _req_str(cap, s, "srt_asset_id", path=path)
        _req_in(cap, s, "caption_method",
                ("elevenlabs_alignment", "whisper_fallback"), path=path)
    _req_dict(p, s, "audio_qa_report")
    qa = p["audio_qa_report"]
    _req_in(qa, s, "overall_status", ("pass", "warn", "fail"), path="audio_qa_report")
    _req_dict(p, s, "package_integrity")
    pi = p["package_integrity"]
    _req(pi, s, "production_ready", bool, path="package_integrity")


def _validate_final_render_package(p: dict) -> None:
    s = "final_render_package"
    _req_dict(p, s, "package_metadata")
    meta = p["package_metadata"]
    _req_str(meta, s, "render_package_id", path="package_metadata")
    _req_str(meta, s, "episode_id", path="package_metadata")
    ep = _req_dict(p, s, "episode_render")
    _req_str(ep, s, "video_asset_id", path="episode_render")
    _req_str(ep, s, "caption_asset_id", path="episode_render")
    shorts = _req_exact(p, s, "shorts_renders", 3, allow_zero=True)
    for i, sr in enumerate(shorts):
        path = f"shorts_renders[{i}]"
        if not isinstance(sr, dict):
            raise SchemaError(s, path, "each short render must be a dict")
        _req_str(sr, s, "short_id", path=path)
        _req_str(sr, s, "video_asset_id", path=path)
        _req_str(sr, s, "caption_asset_id", path=path)
    _req_list(p, s, "thumbnail_references", min_length=1)
    for i, tr in enumerate(p["thumbnail_references"]):
        path = f"thumbnail_references[{i}]"
        if not isinstance(tr, dict):
            raise SchemaError(s, path, "each thumbnail_reference must be a dict")
        _req_str(tr, s, "thumbnail_asset_id", path=path)
        _req_str(tr, s, "format", path=path)
    _req_list(p, s, "music_log")
    for i, entry in enumerate(p["music_log"]):
        path = f"music_log[{i}]"
        if not isinstance(entry, dict):
            raise SchemaError(s, path, "each music_log entry must be a dict")
        _req_str(entry, s, "log_id", path=path)
        _req_in(entry, s, "source", ("channel_library", "ai_generated"), path=path)
    ms = _req_dict(p, s, "metadata_source")
    _req_str(ms, s, "episode_title", path="metadata_source")
    _req_str(ms, s, "ai_disclosure_text", path="metadata_source")
    _req_list(ms, s, "section_titles", path="metadata_source")
    _req_dict(p, s, "assembly_qa_report")
    qa = p["assembly_qa_report"]
    _req_in(qa, s, "overall_status", ("pass", "warn", "fail"), path="assembly_qa_report")
    _req_dict(p, s, "package_integrity")
    pi = p["package_integrity"]
    _req(pi, s, "production_ready", bool, path="package_integrity")


def _validate_publishing_record(p: dict) -> None:
    s = "publishing_record"
    _req_dict(p, s, "package_metadata")
    meta = p["package_metadata"]
    _req_str(meta, s, "publishing_record_id", path="package_metadata")
    _req_str(meta, s, "episode_id", path="package_metadata")
    _req_in(meta, s, "overall_status",
            ("published", "partially_published", "failed"),
            path="package_metadata")
    results = _req_list(p, s, "platform_results", min_length=1)
    for i, res in enumerate(results):
        path = f"platform_results[{i}]"
        if not isinstance(res, dict):
            raise SchemaError(s, path, "each platform_result must be a dict")
        _req_str(res, s, "result_id", path=path)
        _req_str(res, s, "platform", path=path)
        _req_in(res, s, "status", ("published", "failed", "skipped"), path=path)
        applied = _req_dict(res, s, "metadata_applied", path=path)
        _req(applied, s, "ai_disclosure_included", bool,
             path=f"{path}.metadata_applied")
    _req_list(p, s, "failures")
    summary = _req_dict(p, s, "summary")
    _req(summary, s, "youtube_main_published", bool, path="summary")
    _req(summary, s, "total_deliverables_attempted", int, path="summary")
    _req(summary, s, "total_published", int, path="summary")
    _req(summary, s, "total_failed", int, path="summary")
    _req(summary, s, "total_skipped", int, path="summary")
    _req_dict(p, s, "package_integrity")
    pi = p["package_integrity"]
    _req(pi, s, "production_ready", bool, path="package_integrity")


def _validate_music_track_package(p: dict) -> None:
    s = "music_track_package"
    _req_str(p, s, "package_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "sourced_at")
    _req_in(p, s, "sourcing_status", ("complete", "partial", "failed"))
    tracks = _req_dict(p, s, "tracks")
    for k, v in tracks.items():
        if not isinstance(v, dict):
            raise SchemaError(s, f"tracks.{k}", "each track must be a dict")
        path = f"tracks.{k}"
        _req_str(v, s, "section_key", path=path)
        _req_str(v, s, "category", path=path)
        _req_str(v, s, "asset_id", path=path)
        _req_str(v, s, "local_path", path=path)
        _req_in(v, s, "source", ("channel_library", "ai_generated"), path=path)
    _req_list(p, s, "music_log")
    _req_list(p, s, "sections_without_music")


def _validate_render_output_manifest(p: dict) -> None:
    s = "render_output_manifest"
    _req_str(p, s, "manifest_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "generated_at")
    _req_in(p, s, "render_status", ("success", "partial", "failed"))
    mr = _req_dict(p, s, "main_render")
    _req_str(mr, s, "file_path", path="main_render")
    _req(mr, s, "file_exists", bool, path="main_render")
    shorts = _req_list(p, s, "shorts_renders")
    for i, sr in enumerate(shorts):
        path = f"shorts_renders[{i}]"
        if not isinstance(sr, dict):
            raise SchemaError(s, path, "each shorts_render must be a dict")
        _req_str(sr, s, "short_id", path=path)
        _req_str(sr, s, "file_path", path=path)
        _req(sr, s, "file_exists", bool, path=path)
    _req_list(p, s, "missing_clips")
    _req_list(p, s, "sections_without_music")
    _req_list(p, s, "ffmpeg_errors")


def _validate_render_review_verdict(p: dict) -> None:
    s = "render_review_verdict"
    _req_str(p, s, "verdict_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "manifest_id")
    _req_str(p, s, "reviewed_at")
    _req(p, s, "production_ready", bool)
    _req_in(p, s, "overall_verdict", ("APPROVED", "CONDITIONAL", "REJECTED"))
    _req_list(p, s, "checks")
    for i, ck in enumerate(p["checks"]):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")
    _req(p, s, "human_review_recommended", bool)
    _req_list(p, s, "human_review_reasons")


def _validate_media_specs_report(p: dict) -> None:
    s = "media_specs_report"
    _req_str(p, s, "report_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "file_path")
    _req_in(p, s, "file_type", ("video", "render", "short", "audio", "image"))
    _req(p, s, "file_valid", bool)
    _req(p, s, "publish_ready", bool)
    _req_dict(p, s, "specs_observed")
    _req_dict(p, s, "specs_expected")
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")
    _req_str(p, s, "checked_at")


def _validate_caption_package(p: dict) -> None:
    s = "caption_package"
    _req_dict(p, s, "package_metadata")
    meta = p["package_metadata"]
    _req_str(meta, s, "caption_package_id", path="package_metadata")
    _req_str(meta, s, "episode_id", path="package_metadata")
    _req_list(p, s, "captions", min_length=1)
    for i, cap in enumerate(p["captions"]):
        path = f"captions[{i}]"
        if not isinstance(cap, dict):
            raise SchemaError(s, path, "each caption entry must be a dict")
        _req_str(cap, s, "caption_id", path=path)
        _req_str(cap, s, "srt_asset_id", path=path)
        _req_in(cap, s, "caption_method",
                ("elevenlabs_alignment", "whisper_fallback"), path=path)
        _req(cap, s, "has_content", bool, path=path)
    _req_list(p, s, "caption_checks")
    for i, ck in enumerate(p["caption_checks"]):
        path = f"caption_checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_str(ck, s, "component", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)


def _validate_archive_record(p: dict) -> None:
    s = "archive_record"
    _req_str(p, s, "archive_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "archived_at")
    _req_in(p, s, "archive_status", ("archived", "warning", "failed"))
    _req_in(p, s, "verdict", ("ARCHIVED", "WARNING", "FAILED"))
    _req(p, s, "archived", bool)
    checks = _req_list(p, s, "checks")
    for i, ck in enumerate(checks):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")
    es = _req_dict(p, s, "episode_summary")
    _req_str(es, s, "episode_title", path="episode_summary")
    _req(es, s, "youtube_main_published", bool, path="episode_summary")
    _req(es, s, "total_published", int, path="episode_summary")


def _validate_compliance_report(p: dict) -> None:
    s = "compliance_report"
    _req_str(p, s, "report_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "evaluated_at")
    _req_in(p, s, "verdict", ("APPROVED", "WARNING", "REJECTED"))
    _req(p, s, "compliant", bool)
    checks = _req_list(p, s, "checks")
    for i, ck in enumerate(checks):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")


def _validate_qa_approval_report(p: dict) -> None:
    s = "qa_approval_report"
    _req_str(p, s, "report_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "evaluated_at")
    _req_in(p, s, "verdict", ("APPROVED", "WARNING", "REJECTED"))
    _req(p, s, "approved", bool)
    checks = _req_list(p, s, "checks")
    for i, ck in enumerate(checks):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")


def _validate_finance_record(p: dict) -> None:
    s = "finance_record"
    _req_str(p, s, "finance_id")
    _req_str(p, s, "episode_id")
    _req_in(p, s, "phase", ("authorization", "post_vpd_authorization", "ledger"))
    _req_str(p, s, "recorded_at")
    _req_in(p, s, "verdict", ("AUTHORIZED", "WARNING", "BLOCKED", "RECORDED"))
    _req(p, s, "authorized", bool)
    _req_numeric(p, s, "budget_ceiling_usd")
    _req_numeric(p, s, "estimated_spend_usd")
    checks = _req_list(p, s, "checks")
    for i, ck in enumerate(checks):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")
    _req_dict(p, s, "cost_breakdown")


def _validate_asset_archive_record(p: dict) -> None:
    s = "asset_archive_record"
    _req_str(p, s, "asset_archive_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "cataloged_at")
    _req_in(p, s, "verdict", ("CATALOGED", "WARNING", "FAILED"))
    _req(p, s, "cataloged", bool)
    checks = _req_list(p, s, "checks")
    for i, ck in enumerate(checks):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)
    _req_list(p, s, "failures")
    _req_list(p, s, "warnings")
    cs = _req_dict(p, s, "catalog_summary")
    _req(cs, s, "clips_indexed", int, path="catalog_summary")
    _req(cs, s, "thumbnails_indexed", int, path="catalog_summary")
    _req(cs, s, "music_tracks_indexed", int, path="catalog_summary")
    _req(cs, s, "entities_indexed", int, path="catalog_summary")
    _req(cs, s, "total_assets_indexed", int, path="catalog_summary")


def _validate_bga_compliance_report(p: dict) -> None:
    s = "bga_compliance_report"
    _req_str(p, s, "report_id")
    _req(p, s, "pass_number", int)
    _req_str(p, s, "episode_id")
    _req_str(p, s, "bible_version")
    _req_str(p, s, "bga_version")
    _req_str(p, s, "produced_at")
    _req_in(p, s, "outcome", ("APPROVED", "APPROVED_WITH_FLAGS", "REJECTED"))
    violations = _req_list(p, s, "violations")
    for i, v in enumerate(violations):
        path = f"violations[{i}]"
        if not isinstance(v, dict):
            raise SchemaError(s, path, "each violation must be a dict")
        _req_str(v, s, "item_id", path=path)
        _req_str(v, s, "rule_id", path=path)
        _req_in(v, s, "severity", ("BLOCKING", "FLAG"), path=path)
        _req_str(v, s, "description", path=path)
    summary = _req_dict(p, s, "summary")
    _req(summary, s, "total_items", int, path="summary")
    _req(summary, s, "blocking_count", int, path="summary")
    _req(summary, s, "flag_count", int, path="summary")


def _validate_signal_record(p: dict) -> None:
    s = "signal_record"
    _req_str(p, s, "signal_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "run_id")
    _req_str(p, s, "ingested_at")
    _req_str(p, s, "metadata_version")
    _req_str(p, s, "youtube_video_id")
    _req_str(p, s, "published_at")
    _req(p, s, "window_days", int)
    _req_in(p, s, "data_quality", ("captured", "error", "no_data"))
    _req_dict(p, s, "metrics")
    _req_dict(p, s, "normalized_metrics")
    summary = _req_dict(p, s, "signal_summary")
    _req(summary, s, "metric_count", int, path="signal_summary")
    _req_in(summary, s, "data_quality", ("captured", "error", "no_data"),
            path="signal_summary")
    _req(summary, s, "has_views", bool, path="signal_summary")


def _validate_audio_qa_verdict(p: dict) -> None:
    s = "audio_qa_verdict"
    _req_str(p, s, "verdict_id")
    _req_str(p, s, "episode_id")
    _req_str(p, s, "evaluated_at")
    _req_in(p, s, "overall_status", ("pass", "warn", "fail"))
    _req(p, s, "production_ready", bool)
    _req(p, s, "total_beats", int)
    _req(p, s, "beats_generated", int)
    _req(p, s, "beats_failed", int)
    _req(p, s, "failure_rate", (int, float))
    _req(p, s, "total_characters_billed", int)
    _req_list(p, s, "checks")
    for i, ck in enumerate(p["checks"]):
        path = f"checks[{i}]"
        if not isinstance(ck, dict):
            raise SchemaError(s, path, "each check must be a dict")
        _req_str(ck, s, "check_id", path=path)
        _req_in(ck, s, "status", ("pass", "warn", "fail"), path=path)
        _req_str(ck, s, "detail", path=path)


# ---------------------------------------------------------------------------
# Registry — populated after all validators are defined
# ---------------------------------------------------------------------------

_VALIDATORS = {
    "validation_scorecard":      _validate_validation_scorecard,
    "topic_brief":               _validate_topic_brief,
    "package_deficiency_report": _validate_package_deficiency_report,
    "research_package":          _validate_research_package,
    "narrative_package":         _validate_narrative_package,
    "script_package":            _validate_script_package,
    "visual_package":            _validate_visual_package,
    "thumbnail_handoff_brief":       _validate_thumbnail_handoff_brief,
    "thumbnail_concept_brief":       _validate_thumbnail_concept_brief,
    "thumbnail_generation_plan":     _validate_thumbnail_generation_plan,
    "thumbnail_package":             _validate_thumbnail_package,
    "narration_package":         _validate_narration_package,
    "final_render_package":      _validate_final_render_package,
    "publishing_record":         _validate_publishing_record,
    "media_specs_report":        _validate_media_specs_report,
    "render_output_manifest":    _validate_render_output_manifest,
    "render_review_verdict":     _validate_render_review_verdict,
    "music_track_package":       _validate_music_track_package,
    "audio_qa_verdict":          _validate_audio_qa_verdict,
    "caption_package":           _validate_caption_package,
    "qa_approval_report":        _validate_qa_approval_report,
    "compliance_report":         _validate_compliance_report,
    "archive_record":            _validate_archive_record,
    "finance_record":            _validate_finance_record,
    "bga_compliance_report":     _validate_bga_compliance_report,
    "asset_archive_record":      _validate_asset_archive_record,
    "signal_record":             _validate_signal_record,
}

# Expose schema names for discovery and testing
SCHEMA_NAMES: tuple = tuple(sorted(_VALIDATORS))
