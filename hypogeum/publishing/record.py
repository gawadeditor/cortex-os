"""
hypogeum/publishing/record.py — PublishingRecord builder

Constructs the publishing_record incrementally.
Writes to disk after each platform_results[] entry is appended.

Write path: .hypogeum-assets/episodes/{episode_id}/publishing_record_{episode_id}.json

overall_status logic:
  "published"          if youtube_main_published
  "partially_published" if any platform_results has status == "published"
  "failed"             otherwise

production_ready = (overall_status == "published")
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "publishing_agent"


# ---------------------------------------------------------------------------
# PublishingRecord
# ---------------------------------------------------------------------------

class PublishingRecord:
    """
    Incremental publishing record builder.

    Initialize at run start, append results after each deliverable,
    then seal at the end. Writes to disk after every mutation.

    Usage:
        record = PublishingRecord(episode_id, output_dir)
        record.initialize()
        record.append_result({...})
        record.seal()
        final = record.to_dict()

    expected_deliverables (optional): the run's readiness contract — a list
    of dicts, each describing one required class of deliverable this run
    must produce to be considered production_ready:

        {
            "platform": "youtube",
            "deliverable_type": "main_episode" | "short",
            "count": 1,               # minimum number of matching results required
            "status": "published",   # required status
            "visibility": "private", # required visibility (None to not constrain it)
            "skipped_reason_prefix": "instagram_container_validated",  # optional
        }

    Omitted (None, the default), production_ready keeps its original
    meaning: True iff the YouTube main episode published, exactly as before
    this parameter existed — every existing caller that doesn't pass it
    (including hypogeum.agents.publishing_agent) is unaffected. When
    provided, production_ready instead requires that every entry's count is
    met by platform_results matching its platform/deliverable_type/status/
    visibility (and skipped_reason_prefix, when given) — a deliverable class
    outside this list (disabled/not required for this run) never affects
    readiness, whether it's absent, skipped, or failed.

    The contract is persisted into package_metadata.expected_deliverables at
    initialize() time (and re-read from there at seal() time, not kept only
    as a transient constructor argument) so a sealed record on disk is
    self-describing — an operator or auditor can see exactly what this run
    required and verify production_ready against it without needing the
    original Python call that created the record.
    """

    def __init__(
        self,
        episode_id: str,
        output_dir: Optional[Path] = None,
        expected_deliverables: Optional[list[dict]] = None,
    ) -> None:
        self.episode_id = episode_id
        self._expected_deliverables = expected_deliverables
        self._log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

        # Determine write path
        if output_dir is not None:
            self._write_path = Path(output_dir) / f"publishing_record_{episode_id}.json"
        else:
            _project_root = Path(__file__).resolve().parent.parent.parent
            assets_root = _project_root / ".hypogeum-assets"
            episode_dir = assets_root / "episodes" / episode_id
            self._write_path = episode_dir / f"publishing_record_{episode_id}.json"

        self._record: dict = {}

    # ------------------------------------------------------------------
    # Initialize
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """
        Initialize the record at run start with in_progress status and empty results.
        Writes to disk immediately.
        """
        self._record = {
            "package_metadata": {
                "publishing_record_id": f"pub_{self.episode_id}_{uuid.uuid4().hex[:8]}",
                "episode_id": self.episode_id,
                "overall_status": "in_progress",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "sealed_at": None,
                "publishing_agent_version": "1.0",
                "expected_deliverables": self._expected_deliverables,
            },
            "platform_results": [],
            "failures": [],
            "summary": {
                "youtube_main_published": False,
                "total_deliverables_attempted": 0,
                "total_published": 0,
                "total_failed": 0,
                "total_skipped": 0,
            },
            "package_integrity": {
                "production_ready": False,
            },
        }
        self._flush()
        self._log.info(
            "publishing_record_initialized",
            episode_id=self.episode_id,
            record_id=self._record["package_metadata"]["publishing_record_id"],
            write_path=str(self._write_path),
        )

    # ------------------------------------------------------------------
    # Append result
    # ------------------------------------------------------------------

    def append_result(self, result: dict) -> None:
        """
        Append one platform_results[] entry and update summary counts.
        Writes to disk immediately after appending.

        Args:
            result: A platform result dict conforming to the publishing_record schema.
                    Must include: result_id, platform, status, metadata_applied.
        """
        # Ensure result_id is set
        if not result.get("result_id"):
            result["result_id"] = f"res_{uuid.uuid4().hex[:12]}"

        self._record["platform_results"].append(result)
        self._update_summary()
        self._flush()

        self._log.info(
            "publishing_result_appended",
            episode_id=self.episode_id,
            result_id=result.get("result_id"),
            platform=result.get("platform"),
            status=result.get("status"),
            deliverable_type=result.get("deliverable_type"),
        )

    # ------------------------------------------------------------------
    # Add failure reference
    # ------------------------------------------------------------------

    def add_failure(self, failure: dict) -> None:
        """
        Add a PDR/failure reference to the failures[] list.
        Writes to disk immediately.
        """
        self._record["failures"].append(failure)
        self._flush()

    # ------------------------------------------------------------------
    # Seal
    # ------------------------------------------------------------------

    def seal(self) -> dict:
        """
        Seal the record: compute overall_status, set production_ready, set sealed_at.
        Writes the final state to disk.

        Returns:
            The sealed publishing record dict.
        """
        overall_status = self._compute_overall_status()
        production_ready = self._compute_production_ready(overall_status)

        self._record["package_metadata"]["overall_status"] = overall_status
        self._record["package_metadata"]["sealed_at"] = datetime.now(timezone.utc).isoformat()
        self._record["package_integrity"]["production_ready"] = production_ready

        self._flush()

        self._log.info(
            "publishing_record_sealed",
            episode_id=self.episode_id,
            overall_status=overall_status,
            production_ready=production_ready,
            total_published=self._record["summary"]["total_published"],
            total_failed=self._record["summary"]["total_failed"],
            total_skipped=self._record["summary"]["total_skipped"],
        )

        return self._record

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return the current state of the record dict."""
        return self._record

    @property
    def youtube_main_published(self) -> bool:
        """True if the YouTube main episode was successfully published."""
        return self._record.get("summary", {}).get("youtube_main_published", False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_overall_status(self) -> str:
        """
        Compute overall_status from platform_results.

        "published"           if youtube_main_published == True
        "partially_published" if any result has status == "published"
        "failed"              otherwise
        """
        summary = self._record.get("summary", {})
        if summary.get("youtube_main_published", False):
            return "published"

        results = self._record.get("platform_results", [])
        if any(r.get("status") == "published" for r in results):
            return "partially_published"

        return "failed"

    def _compute_production_ready(self, overall_status: str) -> bool:
        """
        Legacy behavior (no expected_deliverables given at construction):
        True iff overall_status == "published" — unchanged from before this
        parameter existed.

        Contract-aware behavior (expected_deliverables given, read back from
        package_metadata rather than the constructor argument directly — see
        the class docstring): True iff every entry's required count is met
        by platform_results matching its platform, deliverable_type, status,
        and visibility (and skipped_reason_prefix, when given). A required
        count of N that is only partially met (failed, pending/absent, or
        matching count < N) makes the whole run not ready. A deliverable
        class absent from expected_deliverables never affects readiness —
        disabled/not-required platforms don't block it whether they're
        absent, skipped, or failed.
        """
        expected_deliverables = self._record.get("package_metadata", {}).get("expected_deliverables")
        if not expected_deliverables:
            return overall_status == "published"

        results = self._record.get("platform_results", [])
        for spec in expected_deliverables:
            if self._matching_deliverable_count(results, spec) < spec.get("count", 1):
                return False
        return True

    @staticmethod
    def _matching_deliverable_count(results: list[dict], spec: dict) -> int:
        """Count distinct deliverables in `results` satisfying `spec` (see
        the class docstring for the expected_deliverables entry shape)."""
        main_matched = False
        matching_short_ids: set = set()

        for r in results:
            if r.get("platform") != spec.get("platform"):
                continue
            if r.get("deliverable_type") != spec.get("deliverable_type"):
                continue
            if r.get("status") != spec.get("status"):
                continue
            if "visibility" in spec and r.get("visibility") != spec.get("visibility"):
                continue
            prefix = spec.get("skipped_reason_prefix")
            if prefix and not (r.get("skipped_reason") or "").startswith(prefix):
                continue

            if spec.get("deliverable_type") == "main_episode":
                main_matched = True
            else:
                matching_short_ids.add(r.get("short_id"))

        if spec.get("deliverable_type") == "main_episode":
            return 1 if main_matched else 0
        return len(matching_short_ids)

    def _update_summary(self) -> None:
        """Recount summary fields from platform_results."""
        results = self._record.get("platform_results", [])
        total_attempted = 0
        total_published = 0
        total_failed = 0
        total_skipped = 0
        youtube_main_published = False
        youtube_main_video_id: Optional[str] = None

        for r in results:
            status = r.get("status", "")
            platform = r.get("platform", "")
            deliverable_type = r.get("deliverable_type", "")

            if status == "skipped":
                total_skipped += 1
            else:
                total_attempted += 1

            if status == "published":
                total_published += 1
                if platform == "youtube" and deliverable_type == "main_episode":
                    youtube_main_published = True
                    youtube_main_video_id = r.get("platform_video_id")
            elif status == "failed":
                total_failed += 1

        self._record["summary"] = {
            "youtube_main_published": youtube_main_published,
            "youtube_main_video_id": youtube_main_video_id,
            "total_deliverables_attempted": total_attempted,
            "total_published": total_published,
            "total_failed": total_failed,
            "total_skipped": total_skipped,
        }

    def _flush(self) -> None:
        """Write the current record to disk."""
        self._write_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_path.write_text(
            json.dumps(self._record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Result entry factory
# ---------------------------------------------------------------------------

def make_result_entry(
    platform: str,
    deliverable_type: str,
    status: str,
    short_id: Optional[str] = None,
    platform_video_id: Optional[str] = None,
    published_url: Optional[str] = None,
    published_at: Optional[str] = None,
    attempts: int = 1,
    skipped_reason: Optional[str] = None,
    title: str = "",
    description_length_chars: int = 0,
    tags_count: int = 0,
    thumbnail_applied: bool = False,
    captions_submitted: bool = False,
    captions_skipped_reason: Optional[str] = None,
    ai_disclosure_included: bool = True,
    end_screen_applied: Optional[bool] = None,
    end_screen_status: Optional[str] = None,
    visibility: Optional[str] = None,
) -> dict:
    """
    Build a platform_results[] entry dict conforming to the publishing_record schema.

    Args:
        platform:                  "youtube" | "facebook" | "instagram" | "tiktok"
        deliverable_type:          "main_episode" | "short"
        status:                    "published" | "failed" | "skipped" |
                                    "uploaded_to_inbox" — whether the platform
                                    operation itself succeeded, failed, was
                                    skipped, or (TikTok Upload-to-Inbox only)
                                    was delivered to the creator's platform
                                    inbox awaiting a human to complete
                                    posting in-app. "uploaded_to_inbox" is
                                    never counted as "published" by
                                    PublishingRecord's summary/readiness
                                    computation — it is real progress, not
                                    full autonomous publishing.
        short_id:                  Short identifier (for short deliverables)
        platform_video_id:         Platform-assigned video ID
        published_url:             Public URL of published content
        published_at:              ISO8601 timestamp of publication
        attempts:                  Number of upload attempts (1 or 2)
        skipped_reason:            Reason string when status == "skipped"
        title:                     Title applied to this deliverable
        description_length_chars:  Character count of the description
        tags_count:                Number of tags applied
        thumbnail_applied:         Whether thumbnail was successfully applied
        captions_submitted:        Whether captions were submitted
        captions_skipped_reason:   Reason captions were skipped (if applicable)
        ai_disclosure_included:    Whether ai_disclosure_text was included
        end_screen_applied:        Legacy bool, derived from end_screen_status when the
                                    latter is given (True iff "applied") — never pass
                                    both. Kept for existing readers of this field;
                                    end_screen_status is the source of truth.
        end_screen_status:         "applied" | "unsupported" | "failed" | "not_attempted" |
                                    None (platform doesn't have an End Screen concept —
                                    non-YouTube deliverables). Ground truth for whether
                                    the interactive YouTube End Screen was verified
                                    applied. Must never be omitted for a YouTube
                                    main_episode deliverable that reached upload —
                                    an omitted/None status on that deliverable is exactly
                                    the ep_f3619022 defect (end_screen_applied silently
                                    persisted as null with no way to tell "not attempted"
                                    apart from "verified applied").
        visibility:                Optional. The actual delivery visibility/state on
                                    the platform (e.g. "private", "unpublished",
                                    "self_only", "container_only") — distinct from
                                    `status`. None/omitted for existing production
                                    callers that don't track this.

    Returns:
        platform_results[] entry dict.
    """
    if end_screen_status is not None:
        end_screen_applied = (end_screen_status == "applied")
    return {
        "result_id": f"res_{uuid.uuid4().hex[:12]}",
        "platform": platform,
        "deliverable_type": deliverable_type,
        "short_id": short_id,
        "status": status,
        "skipped_reason": skipped_reason,
        "platform_video_id": platform_video_id,
        "published_url": published_url,
        "published_at": published_at,
        "attempts": attempts,
        "visibility": visibility,
        "metadata_applied": {
            "title": title,
            "description_length_chars": description_length_chars,
            "tags_count": tags_count,
            "thumbnail_applied": thumbnail_applied,
            "captions_submitted": captions_submitted,
            "captions_skipped_reason": captions_skipped_reason,
            "ai_disclosure_included": ai_disclosure_included,
            "end_screen_applied": end_screen_applied,
            "end_screen_status": end_screen_status,
        },
    }
