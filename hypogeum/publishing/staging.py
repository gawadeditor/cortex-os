"""
hypogeum/publishing/staging.py — Storage provider abstraction for Instagram staging

Instagram requires a publicly accessible HTTPS URL for video uploads.
V1 ships two providers:
  - UrlStagingProvider: HTTP POST to META_STAGING_BASE_URL; receives public URL
  - NullStagingProvider: raises immediately; used when META_STAGING_BASE_URL unset

The StagingProvider Protocol defines the interface — cloud bucket (S3/R2)
can be added later as a deployment-time configuration change, not a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import requests

from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StagingError(RuntimeError):
    """Raised when a staging operation fails and cannot be retried."""


class StagingNotConfiguredError(StagingError):
    """Raised by NullStagingProvider when no staging URL is configured."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StagingProvider(Protocol):
    """
    Protocol for public-URL staging providers.
    All implementations must support stage_file() and delete_staged_file().
    """

    def stage_file(self, local_path: Path, filename: str) -> str:
        """
        Upload a local file to temporary public storage.

        Args:
            local_path: Absolute path to the local file.
            filename:   Suggested filename for the remote object.

        Returns:
            Public HTTPS URL where the file is accessible.

        Raises:
            StagingError — on upload failure.
        """
        ...

    def delete_staged_file(self, url: str) -> None:
        """
        Delete a previously staged file.

        Args:
            url: The public URL returned by stage_file().

        Raises:
            StagingError — on deletion failure (caller should log and continue).
        """
        ...


# ---------------------------------------------------------------------------
# UrlStagingProvider
# ---------------------------------------------------------------------------

class UrlStagingProvider:
    """
    HTTP POST staging provider.

    POSTs the file to META_STAGING_BASE_URL and expects the response body
    to contain the public URL (as plain text or in a JSON "url" field).
    Uses HTTP DELETE to clean up staged files.
    """

    def __init__(self, base_url: str, episode_id: str | None = None) -> None:
        """
        Args:
            base_url:   The staging endpoint base URL (from META_STAGING_BASE_URL).
            episode_id: For log correlation.
        """
        self._base_url = base_url.rstrip("/")
        self._log = get_logger(episode_id=episode_id, agent="staging")

    def stage_file(self, local_path: Path, filename: str) -> str:
        """
        POST the file to the staging endpoint and return the public URL.

        Expected response formats (either accepted):
          - Plain text body containing the URL
          - JSON body with a "url" field: {"url": "https://..."}

        Args:
            local_path: Absolute path to the local file.
            filename:   Suggested filename.

        Returns:
            Public HTTPS URL.

        Raises:
            StagingError — on HTTP error or unexpected response format.
        """
        upload_url = f"{self._base_url}/upload"

        self._log.info(
            "staging_upload_start",
            filename=filename,
            local_path=str(local_path),
            upload_url=upload_url,
        )

        try:
            with open(local_path, "rb") as fh:
                response = requests.post(
                    upload_url,
                    files={"file": (filename, fh)},
                    timeout=120,
                )
        except requests.RequestException as exc:
            raise StagingError(f"staging POST failed: {exc}") from exc

        if not response.ok:
            raise StagingError(
                f"staging POST returned HTTP {response.status_code}: {response.text[:200]}"
            )

        # Try JSON response first, then plain text
        public_url = None
        try:
            data = response.json()
            public_url = data.get("url", "")
        except ValueError:
            public_url = response.text.strip()

        if not public_url or not public_url.startswith("http"):
            raise StagingError(
                f"staging POST did not return a valid URL: {response.text[:200]}"
            )

        self._log.info(
            "staging_upload_complete",
            filename=filename,
            public_url=public_url,
        )

        return public_url

    def delete_staged_file(self, url: str) -> None:
        """
        DELETE a previously staged file.
        Logs failures but does not raise — callers should continue on cleanup failure.

        Args:
            url: The public URL returned by stage_file().
        """
        self._log.info("staging_delete_start", url=url)

        try:
            response = requests.delete(url, timeout=30)
            if response.ok:
                self._log.info("staging_delete_complete", url=url)
            else:
                self._log.warn(
                    "staging_delete_failed",
                    url=url,
                    status_code=response.status_code,
                )
        except requests.RequestException as exc:
            self._log.warn(
                "staging_delete_error",
                url=url,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# NullStagingProvider
# ---------------------------------------------------------------------------

class NullStagingProvider:
    """
    Null staging provider — raises immediately on any operation.
    Used when META_STAGING_BASE_URL is not configured.
    Instagram uploads will fail immediately with a clear error message.
    """

    def stage_file(self, local_path: Path, filename: str) -> str:
        """Always raises StagingNotConfiguredError."""
        raise StagingNotConfiguredError(
            "META_STAGING_BASE_URL is not configured. "
            "Instagram Reels upload requires a public staging URL. "
            "Set META_STAGING_BASE_URL in your .env file to enable Instagram publishing."
        )

    def delete_staged_file(self, url: str) -> None:
        """Always raises StagingNotConfiguredError."""
        raise StagingNotConfiguredError(
            "META_STAGING_BASE_URL is not configured. Cannot delete staged file."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_staging_provider(
    meta_staging_base_url: str | None,
    episode_id: str | None = None,
) -> StagingProvider:
    """
    Return the appropriate StagingProvider based on configuration.

    Args:
        meta_staging_base_url: Value of META_STAGING_BASE_URL config (may be None).
        episode_id:            For log correlation.

    Returns:
        UrlStagingProvider if META_STAGING_BASE_URL is set.
        NullStagingProvider if META_STAGING_BASE_URL is unset.
    """
    if meta_staging_base_url:
        return UrlStagingProvider(
            base_url=meta_staging_base_url,
            episode_id=episode_id,
        )
    return NullStagingProvider()
