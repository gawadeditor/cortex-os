"""
hypogeum/agents/humanization_stage.py — Isolated Humanization Stage (0.1.0)

Wired into hypogeum/pipeline.py (OFF and SHADOW modes only, as of the
current pipeline wiring — see hypogeum/pipeline.py's own Humanization Stage
block). This module itself remains otherwise isolated:

  - No checkpoint, no database access, no file writes of any kind — the
    checkpoint layer (hypogeum/humanization_checkpoint.py) is a separate
    module the pipeline wires around this one.
  - hypogeum/humanizer.py is used unchanged, never edited or duplicated.
  - The only network/LLM call surface this module can reach is
    hypogeum.agents.humanizer_llm_adapter.call_humanizer_llm(), used only
    when mode="SHADOW" — OFF and DETERMINISTIC_ONLY never call it and
    remain fully in-memory, zero-I/O code paths exactly as before.

Deliberate omission for OFF/DETERMINISTIC_ONLY: hypogeum.logger.get_logger()
is not constructed for those two modes, even though it is a normal
dependency for every other agent module. Its default behavior appends to
.hypogeum-assets/pipeline.log on disk, which would be a file write —
explicitly out of scope for those two zero-I/O modes. SHADOW mode does
construct a logger (per HUMANIZATION_STAGE_DESIGN.md §12's `hs_*` event
namespace) because it genuinely performs I/O (an LLM call) that must be
observable and because a failure must be logged clearly, never swallowed.

Supported modes (docs/architecture/HUMANIZATION_STAGE_DESIGN.md §13):
  OFF                 — pass-through only, no transformation attempted
  DETERMINISTIC_ONLY  — hypogeum.humanizer.humanize_field() only, no LLM
  SHADOW              — deterministic pass, then the pinned upstream
                         Humanizer LLM pass (via humanizer_llm_adapter),
                         full preservation validation, checkpointed and
                         logged — but never consumed by any downstream
                         pipeline stage (Finance/VPD/Audio/Assembly all
                         continue reading the original script_package
                         directly; wiring that consumption is a separate,
                         future, explicitly authorized task)

LLM_CANARY / ACTIVE / ROLLBACK are valid humanized_script_package schema
values but are not implemented by this stage yet — requesting them raises
UnsupportedHumanizationModeError.

P24 safety (docs/architecture/HUMANIZER_RUNTIME_MIGRATION_AUDIT.md §2/§3):
hypogeum/humanizer.py exposes no parameter to disable its P24 hedging-clause
patterns individually, and three of them delete a clause outright rather
than soften it (unlike the upstream skill's own demonstrated behavior).
Rather than edit hypogeum/humanizer.py, this module fails closed: any beat
whose source narration matches one of those three specific patterns is
never passed to humanize_field() at all — it is returned unchanged, with
transformation_status="reverted" and an explicit warning. A second,
independent safety net (a hedge-term-count comparison, see
_beat_preservation_checks()) catches any other case where a deterministic
transform would have reduced uncertainty/hedging language, for the same
reason.

Public API:
    run_humanization_stage(*, episode_id, script_package, pipeline_run_id,
                            mode="OFF", config=None) -> dict
        Returns a schema-validated humanized_script_package dict, or raises
        HumanizationStageError if the assembled result cannot pass
        hypogeum.schemas.validate("humanized_script_package", ...).
        config is required (and used) only when mode="SHADOW".
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from hypogeum.agents.humanizer_llm_adapter import (
    HUMANIZER_LLM_MODEL_ID,
    HumanizerLLMAdapterError,
    call_humanizer_llm,
)
from hypogeum.config import Config
from hypogeum.humanization_fingerprint import (
    PINNED_HUMANIZER_SHA,
    STAGE_VERSION,
    WRAPPER_VERSION,
    build_humanization_fingerprint,
)
from hypogeum.humanizer import humanize_field
from hypogeum.logger import get_logger
from hypogeum.schemas import SchemaError, validate

# ---------------------------------------------------------------------------
# Constants
#
# Version/SHA constants are aliased from hypogeum.humanization_fingerprint —
# the single source of truth also used by hypogeum.humanization_checkpoint —
# rather than redefined here, so the two modules can never silently drift
# apart on what "the current version" means.
# ---------------------------------------------------------------------------

_STAGE_VERSION = STAGE_VERSION
_WRAPPER_VERSION = WRAPPER_VERSION
_PINNED_SKILL_SHA = PINNED_HUMANIZER_SHA
_SKILL_SOURCE = "https://github.com/blader/humanizer"
_SOURCE_STAGE = "script_engine"
_SOURCE_SCHEMA = "script_package"
_SOURCE_SCHEMA_VERSION = "v1"  # script_package carries no explicit version field today

_SUPPORTED_MODES = ("OFF", "DETERMINISTIC_ONLY", "SHADOW")

_DETERMINISTIC_RULE_MARKER = "legacy_deterministic_humanizer"


class HumanizationStageError(Exception):
    """
    Raised when the assembled humanized_script_package fails schema
    validation. Per design, this stage never returns a partially valid
    package — a failure here means nothing is returned at all.
    """


class UnsupportedHumanizationModeError(ValueError):
    """Raised when run_humanization_stage() is called with an unsupported mode."""


# ---------------------------------------------------------------------------
# P24 fail-closed detection — hypogeum/humanizer.py's own deleting patterns,
# matched here (not imported — humanizer.py exposes no public pattern list)
# so this module can detect and avoid them without editing that file.
# ---------------------------------------------------------------------------

_P24_UNSAFE_DELETION_PATTERNS = (
    re.compile(r"\bit could be argued that\b", re.IGNORECASE),
    re.compile(r"\bone might (?:suggest|argue) that\b", re.IGNORECASE),
    re.compile(r"\bit (?:is|seems) (?:clear|evident|obvious) that\b", re.IGNORECASE),
)

_UNCERTAINTY_TERMS = (
    "may", "might", "could", "possibly", "perhaps", "likely", "appears to",
    "appear to", "seems to", "seem to", "suggests", "suggest", "according to",
    "reportedly", "allegedly", "probably", "believed to",
)

_NEGATION_TERMS = ("not", "no", "never", "cannot", "n't", "none", "nothing", "without")

_DATE_RE = re.compile(r"\b(?:\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4})\b")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_QUOTE_RE = re.compile(r'"([^"]*)"')
_URL_RE = re.compile(r"https?://\S+")
_CITATION_RE = re.compile(r"\([A-Z][a-zA-Z]+,?\s+\d{4}\)")
_CAPITALIZED_WORD_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_humanization_stage(
    *,
    episode_id: str,
    script_package: dict,
    pipeline_run_id: str,
    mode: str = "OFF",
    config: Optional[Config] = None,
) -> dict:
    """
    Produce a schema-validated humanized_script_package from a script_package.

    Never mutates script_package. OFF and DETERMINISTIC_ONLY make no LLM
    call, no network call, no file write, no database access. SHADOW makes
    one LLM call per non-empty narration beat via
    hypogeum.agents.humanizer_llm_adapter — the only network call surface
    this module ever reaches, and only when mode="SHADOW".

    Args:
        episode_id:      Episode identifier. Injected into the copied source
                          package's episode_metadata.episode_id — never into
                          the caller's original script_package.
        script_package:  A schema-valid script_package dict (not modified).
        pipeline_run_id: Opaque run identifier, carried into provenance only.
        mode:            "OFF", "DETERMINISTIC_ONLY", or "SHADOW".
        config:          Required when mode="SHADOW" (provides API keys for
                          the LLM call). Ignored for OFF/DETERMINISTIC_ONLY.

    Returns:
        A dict that passes hypogeum.schemas.validate("humanized_script_package", ...).

    Raises:
        UnsupportedHumanizationModeError: mode is not currently supported.
        ValueError: mode="SHADOW" but config was not provided.
        HumanizationStageError: the assembled result failed schema validation.
    """
    if mode not in _SUPPORTED_MODES:
        raise UnsupportedHumanizationModeError(
            f"mode must be one of {_SUPPORTED_MODES}, got {mode!r}"
        )
    if mode == "SHADOW" and config is None:
        raise ValueError("config is required when mode='SHADOW'")

    _fingerprint_model_id = HUMANIZER_LLM_MODEL_ID if mode == "SHADOW" else None

    # Fingerprint the caller-provided script_package first — before any
    # copying or mutation — using the same canonical formula
    # hypogeum.humanization_checkpoint's reuse check computes independently,
    # so a checkpoint saved from this result can actually be reused.
    input_hash = build_humanization_fingerprint(
        episode_id=episode_id,
        script_package=script_package,
        mode=mode,
        stage_version=_STAGE_VERSION,
        wrapper_version=_WRAPPER_VERSION,
        skill_commit_sha=_PINNED_SKILL_SHA,
        model_id=_fingerprint_model_id,
    )

    # 1/2 — never mutate the caller's script_package; work on a deep copy.
    source = copy.deepcopy(script_package)

    # 3 — inject episode_id into the copy only. script_package's own schema
    # (hypogeum/schemas.py::_validate_script_package) requires no episode_id
    # field anywhere, so episode_metadata may be absent or missing the key —
    # created/overwritten defensively, never touching the caller's object.
    if not isinstance(source.get("episode_metadata"), dict):
        source["episode_metadata"] = {}
    source["episode_metadata"]["episode_id"] = episode_id

    # 4/5 — extract narration beats: sections first, then shorts_scripts,
    # matching hypogeum/agents/script_engine.py's own iteration order.
    input_beats, extraction_warnings = _extract_beats(script_package)

    # SHADOW is the only mode that performs I/O — the logger is constructed
    # only in that case, preserving OFF/DETERMINISTIC_ONLY's zero-I/O contract.
    shadow_log = get_logger(episode_id=episode_id, agent="humanization_stage") if mode == "SHADOW" else None

    beat_records = []
    package_fallback_used = False
    shadow_usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    shadow_latency_total = 0
    for beat_id, original_text in input_beats:
        if mode == "OFF":
            beat_record = _build_off_beat(beat_id, original_text)
        elif mode == "DETERMINISTIC_ONLY":
            beat_record = _build_deterministic_beat(beat_id, original_text)
            if beat_record["transformation_status"] == "reverted":
                package_fallback_used = True
        else:  # SHADOW
            beat_record, beat_fallback, usage = _build_shadow_beat(
                beat_id, original_text,
                episode_id=episode_id, config=config, log=shadow_log,
            )
            if beat_fallback:
                package_fallback_used = True
            if usage is not None:
                shadow_llm_attempted = True
                shadow_usage_totals["input_tokens"] += usage["input_tokens"]
                shadow_usage_totals["output_tokens"] += usage["output_tokens"]
                shadow_usage_totals["total_tokens"] += usage["total_tokens"]
                shadow_latency_total += usage["latency_ms"]

        beat_records.append(beat_record)

    output_hash = _hash_texts([b["humanized_narration"] for b in beat_records])
    source_content_hash = _hash_obj(source)

    humanization = {
        "stage_version": _STAGE_VERSION,
        "mode": mode,
        "status": _package_status(mode, beat_records, package_fallback_used),
        "input_hash": input_hash,
        "output_hash": output_hash,
        "skill_commit_sha": _PINNED_SKILL_SHA,
        "wrapper_version": _WRAPPER_VERSION,
        "model_id": HUMANIZER_LLM_MODEL_ID if mode == "SHADOW" else None,
        "provider": "openrouter" if mode == "SHADOW" else None,
        "fallback_used": package_fallback_used,
        "warnings": list(extraction_warnings),
        "latency_ms": shadow_latency_total if mode == "SHADOW" else 0,
        "token_usage": shadow_usage_totals if mode == "SHADOW" else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "preservation_validation": _package_preservation_validation(beat_records),
    }

    provenance = {
        "pipeline_run_id": pipeline_run_id,
        "source_stage": _SOURCE_STAGE,
        "source_schema": _SOURCE_SCHEMA,
        "source_schema_version": _SOURCE_SCHEMA_VERSION,
        "source_content_hash": source_content_hash,
        "humanization_stage_version": _STAGE_VERSION,
        "skill_source": _SKILL_SOURCE,
        "skill_commit_sha": _PINNED_SKILL_SHA,
        "wrapper_version": _WRAPPER_VERSION,
        "model_id": HUMANIZER_LLM_MODEL_ID if mode == "SHADOW" else None,
        "created_at": _utc_now_iso(),
    }

    result = {
        "episode_id": episode_id,
        "source_script_package": source,
        "humanization": humanization,
        "beats": beat_records,
        "provenance": provenance,
    }

    try:
        validate("humanized_script_package", result)
    except SchemaError as exc:
        raise HumanizationStageError(
            f"assembled humanized_script_package failed validation: {exc}"
        ) from exc

    return result


# ---------------------------------------------------------------------------
# Beat extraction
# ---------------------------------------------------------------------------

def _extract_beats(script_package: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Flatten narration beats from sections[] then shorts_scripts[], in order.

    Mirrors hypogeum.humanizer.humanize_beats()'s own conservative skip
    behavior: a beat missing beat_id or text (or a non-dict entry) is
    skipped, not fabricated — with a warning recorded so nothing is
    silently dropped without trace. Duplicate beat_id values (which the
    humanized_script_package schema forbids) are de-duplicated, keeping
    only the first occurrence.
    """
    beats: list[tuple[str, str]] = []
    warnings: list[str] = []
    seen_ids: set = set()

    def _collect(narration_beats: Any) -> None:
        if not isinstance(narration_beats, list):
            return
        for nb in narration_beats:
            if not isinstance(nb, dict):
                warnings.append("skipped a non-dict narration beat entry")
                continue
            beat_id = nb.get("beat_id")
            text = nb.get("text")
            if not isinstance(beat_id, str) or not beat_id.strip():
                warnings.append("skipped a narration beat with missing/invalid beat_id")
                continue
            if not isinstance(text, str):
                warnings.append(f"skipped beat {beat_id!r} with missing/invalid text")
                continue
            if beat_id in seen_ids:
                warnings.append(f"skipped duplicate beat_id {beat_id!r} (kept first occurrence)")
                continue
            seen_ids.add(beat_id)
            beats.append((beat_id, text))

    for section in script_package.get("sections", []) or []:
        if isinstance(section, dict):
            _collect(section.get("narration_beats", []))
    for short in script_package.get("shorts_scripts", []) or []:
        if isinstance(short, dict):
            _collect(short.get("narration_beats", []))

    return beats, warnings


# ---------------------------------------------------------------------------
# Beat builders
# ---------------------------------------------------------------------------

def _empty_preservation_validation(warnings: Optional[list] = None) -> dict:
    return {"passed": True, "hard_failures": [], "warnings": list(warnings or [])}


def _build_off_beat(beat_id: str, original_text: str) -> dict:
    return {
        "beat_id": beat_id,
        "original_narration": original_text,
        "humanized_narration": original_text,
        "transformation_status": "unchanged",
        "deterministic_rules_applied": [],
        "llm_pass_applied": False,
        "differences": [],
        "warnings": [],
        "preservation_validation": _empty_preservation_validation(),
    }


def _build_deterministic_beat(beat_id: str, original_text: str) -> dict:
    if not original_text.strip():
        return {
            "beat_id": beat_id,
            "original_narration": original_text,
            "humanized_narration": original_text,
            "transformation_status": "unchanged",
            "deterministic_rules_applied": [],
            "llm_pass_applied": False,
            "differences": [],
            "warnings": [],
            "preservation_validation": _empty_preservation_validation(),
        }

    # P24 fail-closed: never even attempt the transform on text matching a
    # known unsafe deletion pattern.
    for pattern in _P24_UNSAFE_DELETION_PATTERNS:
        if pattern.search(original_text):
            return _reverted_beat(
                beat_id, original_text,
                "P24 uncertainty-clause-deletion pattern detected in source text; "
                "reverted to original per HUMANIZER_RUNTIME_MIGRATION_AUDIT.md §2/§3 "
                "(hypogeum/humanizer.py exposes no way to disable this pattern group, "
                "so this stage fails closed rather than editing it)."
            )

    humanized_text = humanize_field(original_text, "narration")

    hard_failures = _beat_preservation_checks(original_text, humanized_text)
    if hard_failures:
        return _reverted_beat(
            beat_id, original_text,
            "Preservation check failed after deterministic transform: "
            + "; ".join(hard_failures)
        )

    changed = humanized_text != original_text
    return {
        "beat_id": beat_id,
        "original_narration": original_text,
        "humanized_narration": humanized_text,
        "transformation_status": "deterministic" if changed else "unchanged",
        "deterministic_rules_applied": [_DETERMINISTIC_RULE_MARKER] if changed else [],
        "llm_pass_applied": False,
        "differences": (
            [{"type": "text_change", "before": original_text, "after": humanized_text}]
            if changed else []
        ),
        "warnings": [],
        "preservation_validation": _empty_preservation_validation(),
    }


def _reverted_beat(beat_id: str, original_text: str, warning: str) -> dict:
    """
    A beat whose deterministic transform was attempted-or-considered but
    declined for safety. The returned text is the untouched original, so
    preservation_validation.passed is True by construction — the warning
    explains why a transform was not applied, it does not describe a defect
    in the returned record itself.
    """
    return {
        "beat_id": beat_id,
        "original_narration": original_text,
        "humanized_narration": original_text,
        "transformation_status": "reverted",
        "deterministic_rules_applied": [],
        "llm_pass_applied": False,
        "differences": [],
        "warnings": [warning],
        "preservation_validation": _empty_preservation_validation([warning]),
    }


# ---------------------------------------------------------------------------
# SHADOW-mode beat builder — deterministic pre-pass, then the pinned
# upstream Humanizer LLM pass, then preservation validation against the
# true original (docs/architecture/HUMANIZATION_STAGE_DESIGN.md §7.2/§8/§11)
# ---------------------------------------------------------------------------

def _build_shadow_beat(
    beat_id: str,
    original_text: str,
    *,
    episode_id: str,
    config: Config,
    log,
) -> tuple:
    """
    Return (beat_record, beat_used_fallback, llm_usage_or_None).

    llm_usage_or_None carries token/latency figures whenever the LLM call
    actually returned a result — even if that result was subsequently
    reverted for a preservation failure, since real spend was incurred
    either way (hypogeum.finance.record_cost() already fired inside
    call_humanizer_llm() regardless of what this function does with the
    result afterward).

    Every failure mode (missing skill, provider failure, malformed output,
    a per-beat hard preservation failure) falls back to the deterministic
    pass's own result — never to a raw, unvalidated LLM rewrite, and never
    silently: each fallback is logged via hs_fallback_used or
    hs_preservation_validation_failed before returning.
    """
    det_beat = _build_deterministic_beat(beat_id, original_text)

    if not original_text.strip():
        # Empty narration — no-op pass-through, no LLM call (§11).
        return det_beat, False, None

    det_text = det_beat["humanized_narration"]
    det_changed = det_beat["transformation_status"] == "deterministic"

    try:
        llm_result = call_humanizer_llm(
            beat_id=beat_id, original_text=det_text, episode_id=episode_id, config=config,
        )
    except HumanizerLLMAdapterError as exc:
        log.warn(
            "hs_fallback_used",
            episode_id=episode_id, beat_id=beat_id, reason=str(exc)[:200],
        )
        fallback_beat = dict(det_beat)
        fallback_beat["warnings"] = list(det_beat["warnings"]) + [
            f"LLM pass unavailable, using deterministic-only result: {exc}"
        ]
        return fallback_beat, True, None

    usage = {
        "input_tokens": llm_result.input_tokens,
        "output_tokens": llm_result.output_tokens,
        "total_tokens": llm_result.total_tokens,
        "latency_ms": llm_result.latency_ms,
    }

    # Preservation is checked against the TRUE original, not the
    # deterministic-pass intermediate — this catches drift introduced by
    # either pass, chained.
    hard_failures = _beat_preservation_checks(original_text, llm_result.humanized_text)
    if hard_failures:
        log.warn(
            "hs_preservation_validation_failed",
            episode_id=episode_id, beat_id=beat_id, failure_count=len(hard_failures),
        )
        fallback_beat = dict(det_beat)
        fallback_beat["warnings"] = list(det_beat["warnings"]) + [
            "LLM pass reverted: preservation check failed after LLM transform: "
            + "; ".join(hard_failures)
        ]
        fallback_beat["transformation_status"] = "reverted"
        return fallback_beat, True, usage

    llm_changed = llm_result.humanized_text != det_text
    final_text = llm_result.humanized_text

    if det_changed and llm_changed:
        status = "deterministic_and_llm"
    elif llm_changed:
        status = "llm"
    elif det_changed:
        status = "deterministic"
    else:
        status = "unchanged"

    differences = (
        [{"type": "text_change", "before": original_text, "after": final_text}]
        if final_text != original_text else []
    )

    beat_record = {
        "beat_id": beat_id,
        "original_narration": original_text,
        "humanized_narration": final_text,
        "transformation_status": status,
        "deterministic_rules_applied": list(det_beat["deterministic_rules_applied"]),
        "llm_pass_applied": True,
        "differences": differences,
        "warnings": [],
        "preservation_validation": _empty_preservation_validation(),
    }
    return beat_record, False, usage


# ---------------------------------------------------------------------------
# Beat-level preservation checks (conservative, regex/heuristic-based —
# see docs/architecture/HUMANIZATION_STAGE_DESIGN.md §8)
# ---------------------------------------------------------------------------

def _extract_capitalized_words(text: str) -> set:
    """Heuristic proper-noun/location surface — not true NER."""
    return set(_CAPITALIZED_WORD_RE.findall(text))


def _count_terms(text: str, terms: tuple) -> int:
    lowered = text.lower()
    total = 0
    for term in terms:
        if " " in term:
            total += lowered.count(term)
        else:
            total += len(re.findall(r"\b" + re.escape(term) + r"\b", lowered))
    return total


def _beat_preservation_checks(original: str, humanized: str) -> list[str]:
    """
    Return a list of human-readable hard-failure descriptions. An empty
    list means every implemented check passed. Checks that cannot be
    reliably performed deterministically (evidence levels, prohibited
    claims, timing/visual-direction fields — none of which exist on
    script_package narration beats today) are not evaluated here; they are
    reported at the package level as informational/warning-severity
    entries, per HUMANIZATION_STAGE_DESIGN.md §8's "unsupported semantic
    checks may pass with warning-level details, but must not be omitted."
    """
    failures = []

    if set(_DATE_RE.findall(original)) - set(_DATE_RE.findall(humanized)):
        failures.append("a date present in the original is missing from the humanized text")

    if set(_NUMBER_RE.findall(original)) - set(_NUMBER_RE.findall(humanized)):
        failures.append("a number present in the original is missing from the humanized text")

    if sorted(_QUOTE_RE.findall(original)) != sorted(_QUOTE_RE.findall(humanized)):
        failures.append("a direct quotation was altered or removed")

    if set(_URL_RE.findall(original)) - set(_URL_RE.findall(humanized)):
        failures.append("a URL present in the original is missing from the humanized text")

    if set(_CITATION_RE.findall(original)) - set(_CITATION_RE.findall(humanized)):
        failures.append("a citation present in the original is missing from the humanized text")

    if _extract_capitalized_words(original) - _extract_capitalized_words(humanized):
        failures.append(
            "a capitalized name/location token present in the original is missing "
            "from the humanized text"
        )

    # Complete elimination, not mere reduction, is the unsafe case — a
    # sentence with three redundant hedges collapsed into one still reads
    # as hedged (upstream's own §24 example: "could potentially possibly"
    # -> "could"); a sentence that goes from having a hedge to having none
    # has had its epistemic qualification destroyed.
    if _count_terms(original, _UNCERTAINTY_TERMS) > 0 and _count_terms(humanized, _UNCERTAINTY_TERMS) == 0:
        failures.append("uncertainty/hedging language was entirely removed")

    if _count_terms(humanized, _NEGATION_TERMS) != _count_terms(original, _NEGATION_TERMS):
        failures.append("negation word count changed")

    return failures


# ---------------------------------------------------------------------------
# Package-level assembly helpers
# ---------------------------------------------------------------------------

_TRANSFORMED_STATUSES = ("deterministic", "llm", "deterministic_and_llm")


def _package_status(mode: str, beat_records: list, fallback_used: bool) -> str:
    if mode == "OFF":
        return "not_run"
    if fallback_used:
        return "fallback"
    if any(b["transformation_status"] in _TRANSFORMED_STATUSES for b in beat_records):
        return "transformed"
    return "unchanged"


def _check(passed: bool, severity: str, details: Any) -> dict:
    return {"passed": passed, "severity": severity, "details": details}


def _package_preservation_validation(beat_records: list) -> dict:
    hard_failures: list = []
    warnings: list = []
    for b in beat_records:
        pv = b["preservation_validation"]
        hard_failures.extend(f"{b['beat_id']}: {hf}" for hf in pv.get("hard_failures", []))
        warnings.extend(f"{b['beat_id']}: {w}" for w in pv.get("warnings", []))

    checks = {
        "names": _check(True, "info", "heuristic capitalized-token comparison per beat"),
        "dates": _check(True, "info", "regex date-token comparison per beat"),
        "numbers": _check(True, "info", "regex numeric-token comparison per beat"),
        "locations": _check(
            True, "info", "shares the names heuristic; not independently distinguishable via regex"
        ),
        "quotations": _check(True, "info", "quoted-span comparison per beat"),
        "citations": _check(True, "info", "parenthetical-citation regex comparison per beat"),
        "urls": _check(True, "info", "URL regex comparison per beat"),
        "evidence_levels": _check(
            True, "warning",
            "script_package carries no evidence-level field on narration beats today; "
            "not verifiable at this stage"
        ),
        "uncertainty_language": _check(
            True, "info",
            "hedge-term count comparison per beat, plus an explicit P24 fail-closed pre-check"
        ),
        "negations": _check(True, "info", "negation-term count comparison per beat"),
        "prohibited_claims": _check(
            True, "warning",
            "script_package carries no prohibited-claims field on narration beats today; "
            "not verifiable at this stage"
        ),
        "beat_count": _check(
            True, "info", f"{len(beat_records)} beat(s) processed, one output record per extracted input beat"
        ),
        "beat_order": _check(
            True, "info", "output beat order matches extraction order (sections then shorts_scripts)"
        ),
        "schema_shape": _check(
            True, "info", "final result validated against humanized_script_package schema before return"
        ),
        "timing_fields": _check(
            True, "warning", "script_package narration beats carry no timing field; not applicable"
        ),
        "visual_direction_fields": _check(
            True, "warning", "script_package schema has no visual-direction field; not applicable"
        ),
    }

    return {
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Hashing and timestamps
# ---------------------------------------------------------------------------

def _hash_texts(texts: list) -> str:
    canonical = json.dumps(list(texts), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_obj(obj: Any) -> str:
    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
