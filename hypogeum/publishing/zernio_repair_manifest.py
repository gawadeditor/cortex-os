"""
Deterministic repair manifest + proposed Zernio update body construction
for an EXISTING scheduled post whose title/content/media/thumbnail needs
correction (never re-creation). Pure, network-free -- callers own
fetching the current post (ZernioProvider.get_post(), read-only) and
issuing the actual repair call (ZernioProvider.repair_existing_post());
this module only proves what that call would carry and gates whether it
should be attempted at all.

Companion to hypogeum.publishing.short_caption_repair (local video/
caption trimming) and hypogeum.publishing.zernio.ZernioProvider.
repair_existing_post() (the actual PUT, never invoked by this module).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from hypogeum.publishing.zernio import _whitelist_platform_entry_for_put


def validate_repair_preconditions(
    *,
    title: Optional[str],
    content: Optional[str],
    duration_s: Optional[float],
    max_duration_s: float,
    burned_caption_status: Optional[str],
    visual_coverage_status: Optional[str],
    thumbnail_status: Optional[str],
) -> List[str]:
    """
    Return every blocking reason a proposed repair mutation must not
    proceed for -- empty list means every precondition is satisfied.
    Never partial: a caller must treat ANY non-empty result as "make
    zero staging/intent/POST/PUT calls for this post", exactly like
    hypogeum.agents.production_output_gate already gates the autonomous
    write path.
    """
    reasons: List[str] = []
    if not title or not title.strip():
        reasons.append("title is empty")
    if not content or not content.strip():
        reasons.append("content/caption is empty")
    if duration_s is None:
        reasons.append("duration could not be measured")
    elif duration_s > max_duration_s:
        reasons.append(f"duration {duration_s:.2f}s exceeds max_duration_s={max_duration_s}s")
    if burned_caption_status != "pass":
        reasons.append(f"burned-caption verification did not pass (status={burned_caption_status!r})")
    if visual_coverage_status != "pass":
        reasons.append(f"visual coverage verification did not pass (status={visual_coverage_status!r})")
    if thumbnail_status != "pass":
        reasons.append(f"no thumbnail passes QA/identity gates (status={thumbnail_status!r})")
    return reasons


def build_repair_manifest_entry(
    *,
    deliverable_id: str,
    provider_post_id: str,
    existing_post: dict,
    repaired_local_media_path: Optional[str],
    repair_verification: Optional[dict],
    generated_title: str,
    generated_content: str,
    target_platforms: List[str],
    thumbnail_verdict: dict,
    blocking_reasons: List[str],
) -> dict:
    """
    One deterministic manifest row -- proves the repair targets the
    EXISTING post_id and preserves its EXISTING schedule, by echoing both
    directly from the GET result rather than restating them from any
    other source.
    """
    return {
        "deliverable_id": deliverable_id,
        "provider_post_id": provider_post_id,
        "preserved_schedule": {
            "scheduledFor": existing_post.get("scheduledFor"),
            "timezone": existing_post.get("timezone"),
        },
        "existing_platforms": sorted(
            e.get("platform") for e in (existing_post.get("platforms") or []) if e.get("platform")
        ),
        "target_platforms": sorted(target_platforms),
        "current_title": existing_post.get("title"),
        "current_content": existing_post.get("content"),
        "generated_title": generated_title,
        "generated_content": generated_content,
        "repaired_local_media_path": repaired_local_media_path,
        "media_staging_status": "not staged in this phase -- no live upload was performed",
        "repair_verification": repair_verification,
        "thumbnail_verdict": thumbnail_verdict,
        "blocking_reasons": blocking_reasons,
        "mutation_would_proceed": not blocking_reasons,
    }


def build_proposed_update_body(
    *,
    existing_post: dict,
    title: str,
    content: str,
    target_platforms: List[str],
    accounts: Dict[str, dict],
    media_url: Optional[str],
    thumbnail_url: Optional[str],
    tiktok_settings: Optional[dict] = None,
) -> dict:
    """
    Exact PUT /v1/posts/{postId} body this repair WOULD send -- never
    sent by this module. `media_url` is None when the repaired render has
    not been staged yet (this phase); in that case the mediaItems entry
    carries no `url`, and `media_staging_required` is emitted at the
    top level so a caller can never mistake this for a submittable body.

    scheduledFor/timezone are echoed verbatim from `existing_post` (never
    recomputed) -- PUT's own omission semantics are undocumented (see
    docs/operations/audits/
    ZERNIO_PUT_CONTRACT_AND_DURATION_VERIFICATION_2026-08-22.md), so the
    existing schedule is always explicitly restated, never left to an
    undocumented default.

    Every existing platforms[] entry -- whether or not its platform is in
    target_platforms -- is preserved into this body, reduced to its
    editable-field whitelist (see below); only a platform genuinely
    absent from the existing post gets a freshly built entry appended.
    Matches ZernioProvider.repair_existing_post()'s own merge logic
    exactly, so this preview body is an accurate prediction of what that
    call sends -- the whole point of a "mandatory preview before
    external creation."

    Editable-field whitelist (2026-08-23, see
    hypogeum.publishing.zernio._PLATFORM_ENTRY_PUT_WHITELIST's own
    docstring): a real diagnostic PUT against the live API proved
    Zernio's update endpoint performs strict schema validation --
    accountId's GET-expanded object shape ({_id, platform, profileId,
    displayName, ...}) is actively REJECTED ("Invalid input: expected
    string, received object", param="platforms.N.accountId"), not
    silently tolerated. Every existing entry is therefore reduced to
    only its documented-editable fields (platform, accountId normalized
    to a plain string, customMedia, scheduledFor, platformSpecificData)
    -- response-only/provider-internal fields (contentHash, provider
    _id, status, publishAttempts, ...) are dropped, never echoed back.
    """
    existing_entries = [
        _whitelist_platform_entry_for_put(e) for e in (existing_post.get("platforms") or [])
    ]
    existing_platform_names = {e.get("platform") for e in existing_entries}
    missing_platforms = [p for p in target_platforms if p not in existing_platform_names]
    merged_platforms = existing_entries + [
        {"platform": p, "accountId": accounts[p]["external_account_id"]}
        for p in sorted(missing_platforms)
    ]

    media_item: dict = {"type": "video", "title": title}
    if media_url:
        media_item["url"] = media_url
    if thumbnail_url:
        media_item["thumbnail"] = thumbnail_url

    body: dict = {
        "title": title,
        "content": content,
        "mediaItems": [media_item],
        "platforms": merged_platforms,
    }
    if existing_post.get("scheduledFor"):
        body["scheduledFor"] = existing_post["scheduledFor"]
    if existing_post.get("timezone"):
        body["timezone"] = existing_post["timezone"]
    if tiktok_settings and "tiktok" in target_platforms:
        body["tiktokSettings"] = tiktok_settings
    if not media_url:
        body["media_staging_required"] = True
    return body
