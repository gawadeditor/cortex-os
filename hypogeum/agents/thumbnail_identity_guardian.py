"""
hypogeum/agents/thumbnail_identity_guardian.py — Thumbnail Identity Guardian v1

Vision-based post-generation gate. Inspects the rendered thumbnail image for
compliance with The Hypogeum Chronicles visual identity before the thumbnail
is committed to the production pipeline.

Unlike the Brand Guardian Agent (BGA), which operates on text descriptions
before image generation, the TIG operates on the actual generated image using
Claude's vision capability.

Public API:
    run_thumbnail_identity_guardian(
        episode_id, image_path,
        tig_client=None, config=None, output_dir=None,
    ) -> dict

Output:
    thumbnail_identity_{episode_id}.json
    Written to .hypogeum-assets/tig/ (or output_dir if overridden).

Verdict outcomes:
    APPROVED       — no violations found
    WARNING        — advisory violations only; production can proceed
    BLOCKED        — one or more hard violations; thumbnail must be regenerated

BLOCKING violations (any one → BLOCKED):
    TIG-01  Human face as primary subject
    TIG-02  Bright or high-key background (light sky, bright backgrounds)
    TIG-03  Neon, cyberpunk, or sci-fi visual language
    TIG-04  Talking-head or interview framing
    TIG-05  Missing or illegible text overlay (wordmark or title)
    TIG-06  Wrong dimensions or aspect ratio (expected 1280×720 / 16:9)
    TIG-07  Wordmark absent from composition

WARNING violations (style advisory only):
    TIG-W01  Background brightness elevated (not yet high-key, but trending warm)
    TIG-W02  Subtitle text weak or low-contrast
    TIG-W03  Wordmark scale smaller than ideal
    TIG-W04  Image subject competes with wordmark placement

Injectable client:
    tig_client is a callable:
        tig_client(image_path: Path, episode_id: str) -> str
    It must return a JSON string: {"violations": [...]} where each violation is:
        {"rule_id": str, "severity": "BLOCKING"|"WARNING", "description": str}

    When tig_client=None, uses the built-in _default_tig_call() which
    encodes the image as base64 and calls Claude via call_claude().
    Tests should inject a mock callable to avoid live API calls.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.identifiers import METADATA_VERSION
from hypogeum.prompt_loader import load_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_identity_guardian"
_TIG_VERSION = "v1"
_TIG_MODEL = "anthropic/claude-sonnet-4.6"
_TIG_MAX_TOKENS = 2048

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_TIG_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "tig"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "thumbnail_identity_guardian.txt"

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

_BLOCKING_RULES = {
    "TIG-01": "Human face as primary subject",
    "TIG-02": "Bright or high-key background",
    "TIG-03": "Neon, cyberpunk, or sci-fi visual language",
    "TIG-04": "Talking-head or interview framing",
    "TIG-05": "Missing or illegible text overlay",
    "TIG-06": "Wrong dimensions or aspect ratio",
    "TIG-07": "Wordmark absent from composition",
}

_WARNING_RULES = {
    "TIG-W01": "Background brightness elevated",
    "TIG-W02": "Subtitle text weak or low-contrast",
    "TIG-W03": "Wordmark scale smaller than ideal",
    "TIG-W04": "Image subject competes with wordmark",
}

# System prompt loaded from disk — headers stripped by prompt_loader.
# Source: hypogeum/prompts/thumbnail_identity_guardian.txt
_SYSTEM_PROMPT: str = load_prompt(_PROMPT_FILE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_fence(content: str) -> str:
    stripped = content.strip()
    m = _FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def _parse_violations(raw: str) -> list[dict]:
    """Parse Claude response JSON into a list of violation dicts. Never raises."""
    content = _strip_fence(raw)
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            v = obj.get("violations")
            if isinstance(v, list):
                return v
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _normalize_violation(v: object) -> Optional[dict]:
    """Normalize a single violation. Returns None for malformed entries."""
    if not isinstance(v, dict):
        return None
    rule_id = str(v.get("rule_id", "")).strip()
    severity = v.get("severity", "")
    description = str(v.get("description", "")).strip()
    if not rule_id or severity not in ("BLOCKING", "WARNING") or not description:
        return None
    return {
        "rule_id": rule_id,
        "severity": severity,
        "description": description,
    }


def _clean_violations(raw_list: list) -> list[dict]:
    return [n for v in raw_list if (n := _normalize_violation(v)) is not None]


def _compute_verdict(violations: list[dict]) -> str:
    """Compute overall verdict from cleaned violations list."""
    if any(v["severity"] == "BLOCKING" for v in violations):
        return "BLOCKED"
    if violations:
        return "WARNING"
    return "APPROVED"


def _encode_image(image_path: Path) -> tuple[str, str]:
    """
    Base64-encode an image file. Returns (base64_string, media_type).
    Media type is inferred from suffix.
    """
    suffix = image_path.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")
    data = image_path.read_bytes()
    return base64.b64encode(data).decode("ascii"), media_type


def _default_tig_call(image_path: Path, episode_id: str, config) -> str:
    """
    Default TIG implementation: encodes image and calls Claude with vision.

    Returns raw JSON string from Claude (may be fenced).
    Raises on API failure (let the caller handle).
    """
    from hypogeum.openrouter import call_claude

    b64, media_type = _encode_image(image_path)
    data_url = f"data:{media_type};base64,{b64}"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
                {
                    "type": "text",
                    "text": (
                        f"Inspect this thumbnail for episode {episode_id!r}. "
                        "Return violations JSON only."
                    ),
                },
            ],
        },
    ]

    model_slug = getattr(config, "TIG_MODEL", _TIG_MODEL) if config is not None else _TIG_MODEL
    result = call_claude(
        model_slug=model_slug,
        messages=messages,
        max_tokens=_TIG_MAX_TOKENS,
        config=config,
        episode_id=episode_id,
        agent=_AGENT_NAME,
    )
    return result.content


def _write_report(report: dict, tig_dir: Path, episode_id: str) -> None:
    out_path = tig_dir / f"thumbnail_identity_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_thumbnail_identity_guardian(
    episode_id: str,
    image_path: Path,
    tig_client: Optional[Callable] = None,
    config=None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Inspect a rendered thumbnail image for channel identity compliance.

    Calls Claude (or the injected tig_client) with the thumbnail image and
    the channel identity rules. Parses violations, computes a verdict, and
    writes a durable thumbnail_identity_{episode_id}.json report.

    Args:
        episode_id:   Episode identifier.
        image_path:   Path to the rendered thumbnail image (JPG, PNG, or WebP).
        tig_client:   Optional injectable callable:
                        tig_client(image_path: Path, episode_id: str) -> str
                      Must return raw JSON string with {"violations": [...]}.
                      If None, uses the built-in Claude vision call.
        config:       Loaded Config instance. Required when tig_client is None.
        output_dir:   Override output directory.
                      Defaults to .hypogeum-assets/tig/.

    Returns:
        thumbnail_identity dict with keys:
            episode_id, image_path (str), verdict (APPROVED/WARNING/BLOCKED),
            violations (list), blocking_count (int), warning_count (int),
            checked_at (ISO 8601), tig_version, metadata_version.
    """
    image_path = Path(image_path)
    resolved_dir = Path(output_dir) if output_dir is not None else _DEFAULT_TIG_DIR

    report: dict = {
        "episode_id": episode_id,
        "image_path": str(image_path),
        "verdict": "error",
        "violations": [],
        "blocking_count": 0,
        "warning_count": 0,
        "checked_at": _now_iso(),
        "tig_version": _TIG_VERSION,
        "metadata_version": METADATA_VERSION,
    }

    # --- Image existence check ---
    if not image_path.exists():
        report["verdict"] = "BLOCKED"
        report["violations"] = [{
            "rule_id": "TIG-05",
            "severity": "BLOCKING",
            "description": f"Thumbnail image not found at path: {image_path}",
        }]
        report["blocking_count"] = 1
        _write_report(report, resolved_dir, episode_id)
        return report

    # --- Call vision client ---
    raw_response = ""
    try:
        if tig_client is not None:
            raw_response = tig_client(image_path, episode_id)
        else:
            raw_response = _default_tig_call(image_path, episode_id, config)
    except Exception as exc:
        report["verdict"] = "error"
        report["error"] = str(exc)[:500]
        try:
            _write_report(report, resolved_dir, episode_id)
        except Exception:
            pass
        return report

    # --- Parse and clean violations ---
    raw_violations = _parse_violations(raw_response)
    violations = _clean_violations(raw_violations)

    blocking = [v for v in violations if v["severity"] == "BLOCKING"]
    warnings = [v for v in violations if v["severity"] == "WARNING"]

    verdict = _compute_verdict(violations)

    report["verdict"] = verdict
    report["violations"] = violations
    report["blocking_count"] = len(blocking)
    report["warning_count"] = len(warnings)

    _write_report(report, resolved_dir, episode_id)
    return report
