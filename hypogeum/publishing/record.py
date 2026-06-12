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
    """

    def __init__(
        self,
        episode_id: str,
        output_dir: Optional[Path] = None,
    ) -> None:
        self.episode_id = episode_id
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
        production_ready = overall_status == "published"

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

    def _update_summary(self) -> None:
        """Recount summary fields from platform_results."""
        results = self._record.get("platform_results", [])
        total_attempted = 0
        total_published = 0
        total_failed = 0
        total_skipped = 0
        youtube_main_published = False

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
            elif status == "failed":
                total_failed += 1

        self._record["summary"] = {
            "youtube_main_published": youtube_main_published,
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
) -> dict:
    """
    Build a platform_results[] entry dict conforming to the publishing_record schema.

    Args:
        platform:                  "youtube" | "facebook" | "instagram" | "tiktok"
        deliverable_type:          "main_episode" | "short"
        status:                    "published" | "failed" | "skipped"
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
        end_screen_applied:        Whether end screen was applied (YouTube only)

    Returns:
        platform_results[] entry dict.
    """
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
        "metadata_applied": {
            "title": title,
            "description_length_chars": description_length_chars,
            "tags_count": tags_count,
            "thumbnail_applied": thumbnail_applied,
            "captions_submitted": captions_submitted,
            "captions_skipped_reason": captions_skipped_reason,
            "ai_disclosure_included": ai_disclosure_included,
            "end_screen_applied": end_screen_applied,
        },
    }
