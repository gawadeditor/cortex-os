"""
hypogeum/episode_state.py — Four-way episode state model.

An episode has four independent status dimensions that the dashboard must surface
separately. A single `status` field collapses all of them into one value, which
produces contradictions like "needs_thumbnail" when the thumbnail file EXISTS but
was rejected by the TIG.

Dimensions
----------
  production_status   COMPLETE | PARTIAL | FAILED
      Has the pipeline produced a render file and all core packages?

  release_status      READY | BLOCKED | PENDING
      Is the episode cleared for publishing? Blocked by QA failures,
      script BGA violations, or TIG-blocked thumbnails.

  release_blocker     None | THUMBNAIL_TIG_BLOCKED | QA_FAILED |
                      SCRIPT_BGA_BLOCKED | MISSING_RENDER | QA_WARNING
      The primary reason release is blocked or pending.

  publish_status      NOT_READY | READY | PUBLISHED | PARTIAL
      Has the episode been published to at least one platform?

  analytics_status    NO_DATA | MANUAL_IMPORT | API_CONNECTED
      Does the dashboard have real performance data?

Public API
----------
  build_episode_state(
      qa_pkg, bga_pkgs, thumbnail_pkg, artifacts,
      publishing_outcomes=None,
      has_real_analytics=False,
  ) -> dict
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# production_status values
PROD_COMPLETE = "COMPLETE"
PROD_PARTIAL  = "PARTIAL"
PROD_FAILED   = "FAILED"

# release_status values
REL_READY   = "READY"
REL_BLOCKED = "BLOCKED"
REL_PENDING = "PENDING"

# release_blocker codes
BLOCKER_MISSING_RENDER       = "MISSING_RENDER"
BLOCKER_QA_FAILED            = "QA_FAILED"
BLOCKER_SCRIPT_BGA_BLOCKED   = "SCRIPT_BGA_BLOCKED"
BLOCKER_THUMBNAIL_TIG_BLOCKED = "THUMBNAIL_TIG_BLOCKED"
BLOCKER_QA_WARNING           = "QA_WARNING"

_BLOCKER_LABELS: dict[str, str] = {
    BLOCKER_MISSING_RENDER:        "Render missing — assembly not complete",
    BLOCKER_QA_FAILED:             "QA gate failed",
    BLOCKER_SCRIPT_BGA_BLOCKED:    "Script brand compliance blocked",
    BLOCKER_THUMBNAIL_TIG_BLOCKED: "Thumbnail rejected by brand identity check (TIG)",
    BLOCKER_QA_WARNING:            "QA approved with warnings — review before publishing",
}

# publish_status values
PUB_NOT_READY = "NOT_READY"
PUB_READY     = "READY"
PUB_PUBLISHED = "PUBLISHED"
PUB_PARTIAL   = "PARTIAL"

# analytics_status values
ANL_NO_DATA       = "NO_DATA"
ANL_MANUAL_IMPORT = "MANUAL_IMPORT"
ANL_API_CONNECTED = "API_CONNECTED"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_episode_state(
    qa_pkg:              dict,
    bga_pkgs:            list[dict],
    thumbnail_pkg:       Optional[dict],
    artifacts:           dict,
    publishing_outcomes: Optional[list[dict]] = None,
    has_real_analytics:  bool = False,
    has_api_analytics:   bool = False,
) -> dict:
    """
    Compute the four-way episode state from package/artifact data.

    Parameters
    ----------
    qa_pkg              qa_approval_report package dict (may be empty)
    bga_pkgs            list of bga_compliance_report_p* dicts
    thumbnail_pkg       thumbnail_package dict (may be None)
    artifacts           {has_render, has_thumbnail, has_narration, ...}
    publishing_outcomes list of {platform, publishing_status, ...} dicts
    has_real_analytics  True if platform_analytics has source != 'mock' rows
    has_api_analytics   True if a live platform API connection is verified

    Returns
    -------
    dict with keys:
        production_status, release_status, release_blocker,
        release_blocker_label, publish_status, analytics_status
    """
    has_render = bool(artifacts.get("has_render"))

    # ── Production status ────────────────────────────────────────────────────
    production_status = PROD_COMPLETE if has_render else PROD_PARTIAL

    # ── Release status & blocker ─────────────────────────────────────────────
    release_blocker: Optional[str] = None

    if not has_render:
        release_status  = REL_BLOCKED
        release_blocker = BLOCKER_MISSING_RENDER

    else:
        # Check script BGA (content brand check)
        _script_bga_blocked = False
        for bga in bga_pkgs:
            outcome    = (bga.get("outcome") or "").upper()
            blocking   = (bga.get("summary") or {}).get("blocking_count", 0)
            violations = bga.get("violations") or []
            has_blocking = any(
                (v.get("severity") or "").upper() == "BLOCKING"
                for v in violations
            )
            if outcome == "BLOCKED" or blocking > 0 or has_blocking:
                _script_bga_blocked = True
                break

        if _script_bga_blocked:
            release_status  = REL_BLOCKED
            release_blocker = BLOCKER_SCRIPT_BGA_BLOCKED

        elif not qa_pkg:
            release_status  = REL_BLOCKED
            release_blocker = BLOCKER_QA_FAILED

        elif not qa_pkg.get("approved"):
            release_status  = REL_BLOCKED
            release_blocker = BLOCKER_QA_FAILED

        else:
            # Check TIG thumbnail (image brand check)
            _tig_blocked = False
            if thumbnail_pkg:
                tig = thumbnail_pkg.get("thumbnail_identity_report") or {}
                tig_verdict  = (tig.get("verdict") or "").upper()
                tig_blocking = tig.get("blocking_count", 0)
                if tig_verdict == "BLOCKED" or tig_blocking > 0:
                    _tig_blocked = True

            if _tig_blocked:
                release_status  = REL_BLOCKED
                release_blocker = BLOCKER_THUMBNAIL_TIG_BLOCKED

            elif (qa_pkg.get("verdict") or "").upper() == "WARNING":
                release_status  = REL_PENDING
                release_blocker = BLOCKER_QA_WARNING

            else:
                release_status  = REL_READY
                release_blocker = None

    release_blocker_label = _BLOCKER_LABELS.get(release_blocker) if release_blocker else None

    # ── Publish status ───────────────────────────────────────────────────────
    outcomes = publishing_outcomes or []
    published  = [o for o in outcomes if (o.get("publishing_status") or "") == "success"]
    failed     = [o for o in outcomes if (o.get("publishing_status") or "") == "failed"]

    if len(published) == len(outcomes) > 0:
        publish_status = PUB_PUBLISHED
    elif published:
        publish_status = PUB_PARTIAL
    elif release_status == REL_READY:
        publish_status = PUB_READY
    else:
        publish_status = PUB_NOT_READY

    # ── Analytics status ─────────────────────────────────────────────────────
    if has_api_analytics:
        analytics_status = ANL_API_CONNECTED
    elif has_real_analytics:
        analytics_status = ANL_MANUAL_IMPORT
    else:
        analytics_status = ANL_NO_DATA

    return {
        "production_status":    production_status,
        "release_status":       release_status,
        "release_blocker":      release_blocker,
        "release_blocker_label": release_blocker_label,
        "publish_status":       publish_status,
        "analytics_status":     analytics_status,
    }


def thumbnail_status_label(thumbnail_pkg: Optional[dict], has_file: bool) -> str:
    """
    Returns a single-line wording describing the thumbnail's state.

    Never says "present" when the thumbnail is rejected.
    """
    if not has_file:
        return "No thumbnail file on disk"

    if not thumbnail_pkg:
        return "Thumbnail generated — no brand check recorded"

    tig = thumbnail_pkg.get("thumbnail_identity_report") or {}
    verdict = (tig.get("verdict") or "").upper()
    if verdict == "BLOCKED":
        return "Thumbnail generated — rejected by brand identity check"
    if verdict == "WARNING":
        return "Thumbnail generated — warnings from brand identity check"
    if verdict == "APPROVED":
        return "Thumbnail generated — brand approved"
    return "Thumbnail generated"
