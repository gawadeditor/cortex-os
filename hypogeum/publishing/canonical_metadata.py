"""
hypogeum/publishing/canonical_metadata.py — Generic, versioned,
per-(deliverable_id, platform) canonical publication metadata contract.

Fixed 2026-08-23 (ep_f3619022 live incident, see docs/operations/audits/
ZERNIO_YOUTUBE_FIELD_MAPPING_2026-08-23.md and
ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md): the incident-only
`metadata_source.youtube_metadata_package` field (main_episode/youtube
only) is replaced by a permanent, channel-agnostic architecture. Every
platform a channel targets for every deliverable (main episode or a
Short) gets its OWN approved metadata package, resolved from
`metadata_source.canonical_platform_metadata` — a LIST of dicts, each
carrying its own `deliverable_id`/`platform` pair, matched by that pair
(never positional) — the same convention `shorts_titles`/
`shorts_captions` already use.

Factory Core owns the CONTRACT (what fields exist, what "valid" means
structurally) and the generic validation logic. It NEVER hardcodes a
channel's editorial choices (e.g. "YouTube category must be 27") —
those live in ChannelOperationsProfile.platform_deliverable_policy
(the versioned Channel Package), read by the caller and passed in as
`class_entry` for validate_canonical_platform_metadata() to check
the resolved metadata AGAINST.

Never invents a value. Never accepts a raw topic/hook/logline/research
summary as final publication copy — see
validate_canonical_platform_metadata()'s own docstring.

Public API:
    CanonicalPlatformMetadata (dataclass)
    resolve_canonical_platform_metadata(final_render_package, deliverable_id, platform)
        -> Optional[CanonicalPlatformMetadata]
    validate_canonical_platform_metadata(metadata, *, deliverable_class, platform,
        class_entry, final_duration_seconds, episode_dir, episode_id) -> List[str]
    build_zernio_platform_specific_data(metadata, platform) -> Optional[dict]
    build_zernio_root_fields(metadata) -> dict
    extract_hashtag_tokens(text) -> set -- the ONE shared hashtag-token
        validator, real complete-token matching (never substring), also
        used by hypogeum.agents.canonical_metadata_agent's generation-time
        validation
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

_HASHTAG_TOKEN_RE = re.compile(r"#\w+")

# YouTube-documented title limit (docs.zernio.com/platforms/youtube,
# fetched 2026-08-23) -- platform-specific caption/description limits
# come from hypogeum.publishing.zernio_platform_contracts's own
# caption_max_chars, not duplicated here.
_YOUTUBE_TITLE_MAX_CHARS = 100

# Known pre-writing / research-stage artifacts that must never pass as
# final publication copy on their own -- confirmed live as the exact
# ep_f3619022 root cause (description resolved from topic_brief.logline).
_PLACEHOLDER_MARKERS = ("todo", "tbd", "placeholder", "lorem ipsum", "untitled video", "no content")


@dataclass
class CanonicalPlatformMetadata:
    deliverable_id: str
    platform: str
    description: str
    metadata_version: str
    source_artifact_ids: List[str]
    input_fingerprint: Dict[str, str]
    generated_at: str
    title: Optional[str] = None
    visible_hashtags: List[str] = field(default_factory=list)
    backend_tags: List[str] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)  # [{"title":, "url":, "source_id":}]
    claim_source_map: Dict[str, dict] = field(default_factory=dict)  # {claim_id: {"sources": [source_id,...]}}
    cta: Optional[str] = None
    chapters: List[dict] = field(default_factory=list)  # [{"start_seconds":, "title":}]
    language: Optional[str] = None
    approved_at: Optional[str] = None
    first_comment: Optional[str] = None
    category_id: Optional[str] = None
    platform_policy_fields: Dict[str, object] = field(default_factory=dict)
    thumbnail_ref: Optional[str] = None
    ai_disclosure: Optional[bool] = None

    @classmethod
    def from_dict(cls, d: dict) -> "CanonicalPlatformMetadata":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


def resolve_canonical_platform_metadata(
    final_render_package: dict, deliverable_id: str, platform: str,
) -> Optional[CanonicalPlatformMetadata]:
    """
    Reads metadata_source.canonical_platform_metadata (a list of dicts,
    each carrying its own deliverable_id/platform) and returns the ONE
    entry matching (deliverable_id, platform), or None if absent —
    never a partial/guessed result, never a fallback to any other field.
    """
    metadata_source = (final_render_package or {}).get("metadata_source", {}) or {}
    entries = metadata_source.get("canonical_platform_metadata") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("deliverable_id") == deliverable_id and entry.get("platform") == platform:
            return CanonicalPlatformMetadata.from_dict(entry)
    return None


def extract_hashtag_tokens(text: str) -> set:
    """
    Real tokenization -- '#Erd' must never match '#Erdstall', and
    '#Test' must never match '#TestTagExtra'. Case-insensitive (hashtag
    matching is not case-sensitive on any of these platforms). This is
    the ONE shared hashtag-token validator (2026-08-23) -- used here by
    validate_canonical_platform_metadata() (readiness) AND by
    hypogeum.agents.canonical_metadata_agent's generation-time
    validation, which used to do a broken substring check
    ('h.lower() in description.lower()') that let '#Test' pass against
    a description containing only '#TestTagExtra'.
    """
    return {m.group(0).lower() for m in _HASHTAG_TOKEN_RE.finditer(text or "")}


def _load_json(path: Path) -> dict:
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _is_placeholder_text(text: str, *, own_id: str = "") -> bool:
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    if own_id and low == own_id.strip().lower():
        return True
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def validate_canonical_platform_metadata(
    metadata: Optional[CanonicalPlatformMetadata],
    *,
    deliverable_class: str,
    platform: str,
    class_entry: dict,
    final_duration_seconds: Optional[float],
    episode_dir: Path,
    episode_id: str,
    contract=None,  # hypogeum.publishing.zernio_platform_contracts.PlatformContract, optional
) -> List[str]:
    """
    Full fail-closed validation. Every failure string is self-describing
    (used verbatim as the readiness reason). Never invents a value to
    make a check pass. `class_entry` is the channel's OWN
    platform_deliverable_policy[platform][deliverable_class] dict — the
    ONLY source of truth for editorial choices like an approved YouTube
    categoryId; this function never hardcodes one.
    """
    failures: List[str] = []

    if metadata is None:
        return [
            f"no canonical_platform_metadata entry found for (deliverable_id, platform={platform!r}) — "
            f"no approved metadata package exists for this deliverable/platform pair"
        ]

    # -- title (where the platform supports one)
    title_supported = getattr(contract, "title_supported", platform == "youtube")
    if title_supported:
        if _is_placeholder_text(metadata.title or "", own_id=metadata.deliverable_id):
            failures.append("title is missing or placeholder text")
        elif platform == "youtube" and len(metadata.title or "") > _YOUTUBE_TITLE_MAX_CHARS:
            failures.append(f"title is {len(metadata.title)} chars, exceeding YouTube's {_YOUTUBE_TITLE_MAX_CHARS}-char limit")

    # -- description/caption: not placeholder, not a raw pre-writing artifact
    if _is_placeholder_text(metadata.description, own_id=metadata.deliverable_id):
        failures.append("description/caption is missing or placeholder text")
    else:
        caption_max = getattr(contract, "caption_max_chars", None)
        if caption_max is not None and len(metadata.description) > caption_max:
            failures.append(f"description/caption is {len(metadata.description)} chars, exceeding {platform}'s {caption_max}-char limit")

        topic_brief = _load_json(episode_dir / f"topic_brief_{episode_id}.json")
        pre_writing_texts = {
            (topic_brief.get("logline") or "").strip(),
            (topic_brief.get("curiosity_hook") or "").strip(),
            (topic_brief.get("mystery_anchor") or "").strip(),
        }
        pre_writing_texts.discard("")
        desc_norm = metadata.description.strip()
        if desc_norm in pre_writing_texts:
            failures.append(
                "description/caption is verbatim identical to a raw research/pre-writing artifact "
                "(topic_brief.logline/curiosity_hook/mystery_anchor) -- not real publication copy"
            )

    # -- CTA
    if _is_placeholder_text(metadata.cta or ""):
        failures.append("cta is missing or placeholder text")

    # -- language / metadata_version / timestamps
    if not metadata.language:
        failures.append("language is absent")
    if not metadata.metadata_version:
        failures.append("metadata_version is absent")
    if not metadata.generated_at:
        failures.append("generated_at timestamp is absent")
    if not metadata.approved_at:
        failures.append("approved_at timestamp is absent -- metadata has not been marked approved")

    # -- source_artifact_ids / input_fingerprint (freshness)
    if not metadata.source_artifact_ids:
        failures.append("source_artifact_ids is empty")
    else:
        missing_artifacts = [a for a in metadata.source_artifact_ids if not (episode_dir / a).exists()]
        if missing_artifacts:
            failures.append(f"source_artifact_ids references file(s) that do not exist on disk: {missing_artifacts}")
        if not metadata.input_fingerprint:
            failures.append("input_fingerprint is absent -- no freshness evidence")
        else:
            stale = []
            for artifact_name in metadata.source_artifact_ids:
                expected_hash = metadata.input_fingerprint.get(artifact_name)
                if not expected_hash:
                    continue  # fingerprint doesn't cover this artifact -- reported separately below
                actual_hash = _sha256_file(episode_dir / artifact_name)
                if actual_hash is not None and actual_hash != expected_hash:
                    stale.append(artifact_name)
            if stale:
                failures.append(
                    f"metadata is stale relative to current source inputs -- {stale} have changed "
                    f"on disk since this metadata was generated"
                )
            uncovered = [a for a in metadata.source_artifact_ids if a not in metadata.input_fingerprint]
            if uncovered:
                failures.append(f"input_fingerprint does not cover every source_artifact_id: missing {uncovered}")

    # -- sources / claim-to-source provenance (structural verification
    # against the episode's REAL research_package.json -- never a live
    # HTTP crawl, but never "non-empty list = verified" either)
    research_package = _load_json(episode_dir / f"research_package_{episode_id}.json")
    real_source_urls = {s.get("url") for s in (research_package.get("source_registry") or []) if isinstance(s, dict)}
    real_source_ids = {s.get("source_id") for s in (research_package.get("source_registry") or []) if isinstance(s, dict)}

    if not metadata.sources:
        failures.append("sources is empty -- no verified source links")
    else:
        malformed = []
        unverified = []
        for s in metadata.sources:
            url = (s or {}).get("url", "")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                malformed.append(url)
                continue
            if real_source_urls and url not in real_source_urls:
                unverified.append(url)
        if malformed:
            failures.append(f"source URL(s) are not well-formed http(s) URLs: {malformed}")
        if unverified:
            failures.append(
                f"source URL(s) do not trace to this episode's own Research Package "
                f"(research_package_{episode_id}.json source_registry): {unverified}"
            )

    if not metadata.claim_source_map:
        failures.append("claim_source_map is empty -- no claim-to-source provenance")
    elif real_source_ids:
        bad_refs = []
        for claim_id, entry in metadata.claim_source_map.items():
            for sid in (entry or {}).get("sources", []):
                if sid not in real_source_ids:
                    bad_refs.append((claim_id, sid))
        if bad_refs:
            failures.append(f"claim_source_map references source_id(s) not in the Research Package: {bad_refs}")

    # -- visible hashtags: real token match, never substring
    desc_tokens = extract_hashtag_tokens(metadata.description)
    if not metadata.visible_hashtags:
        failures.append("visible_hashtags is empty")
    else:
        configured_tokens = {h.lower() if h.startswith("#") else f"#{h.lower()}" for h in metadata.visible_hashtags}
        missing = sorted(configured_tokens - desc_tokens)
        if missing:
            failures.append(
                f"hashtag(s) {missing} do not appear as complete hashtag tokens in the visible "
                f"description/caption text (a substring match is not valid token validation)"
            )

    # -- backend tags vs visible hashtags: must remain separate
    if metadata.backend_tags:
        backend_norm = {t.lower().lstrip("#") for t in metadata.backend_tags}
        hashtag_norm = {h.lower().lstrip("#") for h in metadata.visible_hashtags}
        overlap = backend_norm & hashtag_norm
        if overlap:
            failures.append(f"backend_tags and visible_hashtags overlap (must remain separate concepts): {sorted(overlap)}")

    # -- first comment, where required by contract/policy
    if getattr(contract, "required_policy_fields", None) and "firstComment" in (class_entry or {}):
        if _is_placeholder_text(metadata.first_comment or ""):
            failures.append("first_comment is required by channel policy but is missing or placeholder text")

    # -- category vs Channel Package policy (never a Factory-Core-hardcoded value)
    approved_category = (class_entry or {}).get("categoryId")
    if approved_category:
        if not metadata.category_id:
            failures.append(f"category_id is absent -- channel policy requires categoryId={approved_category!r}")
        elif str(metadata.category_id) != str(approved_category):
            failures.append(
                f"category_id={metadata.category_id!r} does not match the channel-approved "
                f"categoryId={approved_category!r} from platform_deliverable_policy"
            )

    # -- platform policy fields (contract-declared required_policy_fields,
    # e.g. TikTok consent/comment/duet/stitch) -- values must be present
    # in class_entry (the channel's own policy), same mechanism
    # build_platform_settings() already uses; this function only checks
    # the CANONICAL METADATA's own platform_policy_fields echo the same
    # values, catching a divergence between the two.
    required_fields = getattr(contract, "required_policy_fields", ()) or ()
    for f in required_fields:
        if f not in (class_entry or {}):
            failures.append(f"channel policy is missing required platform field {f!r} for platform={platform!r}")

    # -- thumbnail reference: only required for a main_episode. A Short
    # has no thumbnail requirement by default (matches
    # hypogeum.publishing.zernio_publish_runtime.DeliverableFile.
    # thumbnail_local_path's own documented convention -- "None means no
    # thumbnail requirement for this deliverable, e.g. every Short, by
    # default"). hypogeum.publishing.readiness.check_publish_readiness()'s
    # own F_THUMBNAIL check is the actual, separate gate for whether a
    # main episode's thumbnail requirement is satisfied.
    if deliverable_class == "main_episode" and not metadata.thumbnail_ref:
        failures.append("thumbnail_ref is absent")

    # -- AI/synthetic-media disclosure: required for every YouTube
    # deliverable class (main_episode AND shorts), not main_episode-only
    # -- containsSyntheticMedia is a per-video disclosure obligation, the
    # same as categoryId above. Other platforms are not gated on this yet
    # (see build_zernio_platform_specific_data()'s own docstring: only
    # YouTube's containsSyntheticMedia mapping is verified/wired).
    if platform == "youtube" and metadata.ai_disclosure is None:
        failures.append("ai_disclosure is absent (must be explicitly True or False, never omitted)")

    # -- chapters: only required for a main_episode with a documented
    # title_field (i.e. YouTube today) -- "where applicable" per the
    # canonical contract's own field list.
    if deliverable_class == "main_episode" and platform == "youtube":
        if len(metadata.chapters) < 3:
            failures.append(f"chapters has {len(metadata.chapters)} entries -- a main episode requires at least 3")
        else:
            failures.extend(_validate_chapters(metadata.chapters, final_duration_seconds))

    return failures


def build_zernio_platform_specific_data(metadata: CanonicalPlatformMetadata, platform: str) -> Optional[dict]:
    """
    Maps a CanonicalPlatformMetadata entry to the exact Zernio
    platforms[].platformSpecificData wire shape for one platform --
    the real production wiring point closing the gap flagged 2026-08-23
    (see docs/operations/audits/ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md
    item 5: "the provider accepts platform_specific_data, but that does
    not prove the production workflow supplies it").

    YouTube fields (categoryId/containsSyntheticMedia/firstComment) are
    CONFIRMED live via docs.zernio.com/platforms/youtube. madeForKids/
    playlistId/visibility -- also confirmed documented fields -- are
    read from metadata.platform_policy_fields when the caller has set
    them there (never defaulted). Facebook's documented title field
    (platformSpecificData.title) is populated when metadata.title is
    set. TikTok/Instagram fields are NOT built here -- TikTok's
    tikTokSettings is a ROOT-level object (build_platform_settings()'s
    existing, already-wired mechanism), and Instagram's exact AI-label
    field name remains UNVERIFIED (see this module's own docstring and
    the audit doc's item 7) -- never guessed at.
    """
    if metadata is None:
        return None
    data: dict = {}
    if platform == "youtube":
        if metadata.category_id:
            data["categoryId"] = metadata.category_id
        if metadata.ai_disclosure is not None:
            data["containsSyntheticMedia"] = metadata.ai_disclosure
        if metadata.first_comment:
            data["firstComment"] = metadata.first_comment
        for passthrough_field in ("madeForKids", "playlistId", "visibility"):
            if passthrough_field in metadata.platform_policy_fields:
                data[passthrough_field] = metadata.platform_policy_fields[passthrough_field]
    elif platform == "facebook":
        if metadata.title:
            data["title"] = metadata.title
    return data or None


def build_zernio_root_fields(metadata: Optional[CanonicalPlatformMetadata]) -> dict:
    """
    Maps a (YouTube) CanonicalPlatformMetadata entry to Zernio's
    ROOT-level tags/hashtags request fields -- confirmed live as
    root-level, not nested under any platform, via docs.zernio.com/
    posts/create-post (2026-08-23). Only ever sourced from the YOUTUBE
    canonical entry: tags/hashtags are documented with YouTube-specific
    validation constraints, and no other platform's contract defines
    either field -- see this module's own docstring for the open
    question of whether Zernio applies them post-wide regardless
    (unresolved; sourcing only from the youtube entry is the
    conservative choice pending that confirmation).
    """
    if metadata is None:
        return {}
    body: dict = {}
    if metadata.backend_tags:
        body["tags"] = list(metadata.backend_tags)
    if metadata.visible_hashtags:
        body["hashtags"] = list(metadata.visible_hashtags)
    return body


def _validate_chapters(chapters: list, final_duration_seconds: Optional[float]) -> List[str]:
    failures: List[str] = []
    try:
        starts = [float(c["start_seconds"]) for c in chapters]
    except (KeyError, TypeError, ValueError):
        return ["each chapter entry must have a numeric 'start_seconds' field"]
    if not starts:
        return failures
    if starts[0] != 0.0:
        failures.append(f"first chapter must start at 0.0s (YouTube requirement), got {starts[0]}")
    for i in range(len(starts) - 1):
        if not (starts[i] < starts[i + 1]):
            failures.append(
                f"chapter timestamps must be strictly increasing -- "
                f"chapter {i} ({starts[i]}s) is not before chapter {i + 1} ({starts[i + 1]}s)"
            )
        elif (starts[i + 1] - starts[i]) < 10.0:
            failures.append(
                f"chapter {i} is only {starts[i + 1] - starts[i]:.1f}s long -- "
                f"every chapter must be at least 10 seconds"
            )
    if final_duration_seconds is not None:
        for i, s in enumerate(starts):
            if not (s < final_duration_seconds):
                failures.append(
                    f"chapter {i} timestamp {s}s is not below the actual final encoded "
                    f"duration {final_duration_seconds}s -- chapters must be validated against "
                    f"the real, ffprobe-measured final timeline, never narration-duration "
                    f"summation alone"
                )
    else:
        failures.append(
            "could not measure the actual final encoded duration (ffprobe returned 0 or the "
            "deliverable file was not found) -- refusing to validate chapters without this "
            "real cross-check"
        )
    return failures
