# Zernio Canonical Metadata Contract — Permanent Workflow (2026-08-23)

Companion to `ZERNIO_YOUTUBE_FIELD_MAPPING_2026-08-23.md` (which documents
the real Zernio wire-shape findings). This doc records the eight permanent
rules the canonical-metadata architecture implements, so a missing or
generic metadata package can never reach Zernio again for any future
episode — `ep_f3619022` is the acceptance test, not the reason the
architecture exists.

## 1. Auto-generation, no human repair artifact

`hypogeum.publishing.zernio_channel_release_trigger._backfill_canonical_platform_metadata()`
generates and persists a `canonical_platform_metadata` entry for every
ENABLED `(deliverable_id, platform)` pair still missing one, immediately
before `build_publish_package_from_episode_dir()` builds any
`DeliverableFile`. Mirrors the existing `_backfill_shorts_metadata()`
pattern. A generation failure for one deliverable/platform is caught and
leaves that pair unresolved — never fabricated — so
`cross_platform_readiness` is always the real, final gate.

## 2. No research-text fallback

`hypogeum.publishing.readiness.resolve_canonical_metadata()` no longer
accepts `topic_brief.logline` as a description candidate. Internal
research/pre-writing text may never become public-facing copy by
fallback — this was the exact live root cause for `ep_f3619022`'s Shorts
publishing with `content: ""`.

## 3. Production path consumes the canonical package directly

`_pick_root_title_description()` sources the shared root Zernio
`title`/`content` fields from the real `CanonicalPlatformMetadata` entry
(youtube preferred, else first available platform) whenever one exists,
never from the older resolver. Per-platform variance lives in
`platforms[].platformSpecificData`, not a second full alternate
description.

## 4. Readiness fails closed

`hypogeum.publishing.canonical_metadata.validate_canonical_platform_metadata()`
fails closed on missing/blank/stale/generic-only/unresolved fields:
placeholder text, verbatim research-text reuse, missing sources,
unverifiable claim-to-source references, invalid hashtag tokens, a
`categoryId` that disagrees with channel policy, a stale input
fingerprint. See that module's own docstring for the full field list.

## 5. No transport call bypasses the gate

Every real submission path — `zernio_publish_runtime.py`'s
`publish_multi_platform()` call site, `zernio_channel_release_trigger.py`'s
aggregated multi-platform publish, and any repair/retry flow — receives
`platform_specific_data`/`root_fields` built from the same
`CanonicalPlatformMetadata` entries via
`build_zernio_platform_specific_data()`/`build_zernio_root_fields()`.
Nothing downstream re-derives metadata independently.

## 6. PUT whitelist, never byte-for-byte GET echo

`hypogeum.publishing.zernio._whitelist_platform_entry_for_put()` builds a
PUT body from an explicit editable-field whitelist (`platform`,
`accountId`, `customMedia`, `scheduledFor`, `platformSpecificData`) —
confirmed necessary by a real diagnostic PUT: Zernio's update endpoint
performs strict schema validation and rejects an echoed GET-only/
provider-internal field shape (e.g. `accountId` returned as an expanded
object by GET, but required as a plain id string by PUT).

## 7. Deterministic end-to-end tests

Tests assert the FINAL serialized Zernio request body starting from a
real canonical artifact — not `publish_multi_platform()` called directly
with manually supplied kwargs. See
`tests/test_zernio_channel_release_trigger.py::TestCanonicalMetadataReachesTheRealSerializedRequestBody`
and `::TestCanonicalMetadataAutoGeneration`, plus
`tests/test_publishing_zernio.py::TestPublishMultiPlatformYoutubeFieldsReachTheRealRequestBody`.

## 8. No hardcoded episode values in production logic

`ep_f3619022`'s episode id, post ids, and specific field values appear
only in fixtures, migration/repair data, comments/docstrings explaining
the historical incident, and the final live acceptance run itself —
never as a literal compared against in production control flow.
