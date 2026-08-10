"""
hypogeum/providers/enhancement.py — Source Enhancement provider abstraction.

This is the ONLY module hypogeum/source_enhancement.py (the pipeline stage)
is allowed to import to reach a concrete enhancement vendor. It must never
import a concrete provider module (e.g. muapi_video_upscaler) directly, and
a concrete provider module must never be imported anywhere the pipeline
stage itself lives — that separation is the whole point: swapping vendors
(MUAPI's AI Video Upscaler today; Topaz, native Veo 4K, or anything else
tomorrow) must never require touching hypogeum/source_enhancement.py or
hypogeum/pipeline.py.

A provider's only contract is EnhancementProvider.enhance(): given a real
source clip and a target resolution, return an EnhancementResult describing
exactly what was produced (or why it failed). The provider never decides
WHETHER enhancement should run — hypogeum/source_enhancement.py owns that
policy — it only performs the enhancement when asked.

Public API:
    EnhancementResult
    EnhancementProvider (ABC)
    get_enhancement_provider(config) -> EnhancementProvider
    register_provider(key, factory)          — used by built-ins and tests
    UnknownEnhancementProviderError
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional


@dataclass
class EnhancementResult:
    """
    Everything Source Enhancement (and, downstream, the episode manifest)
    needs to know about one enhancement attempt — success or failure.

    output_path is set only on success and points at a real file the
    provider has already downloaded to local disk; the caller (source_
    enhancement.py) is responsible for re-validating it via ffprobe before
    trusting it — a provider claiming success is not itself proof.
    """
    success: bool
    output_path: Optional[Path] = None
    width: int = 0
    height: int = 0
    codec: Optional[str] = None
    fps: Optional[str] = None
    duration_seconds: float = 0.0
    provider: str = ""
    model: str = ""
    provider_version: str = ""
    cost_usd: Optional[float] = None
    generation_time_s: float = 0.0
    failure_reason: Optional[str] = None
    # The provider's own job/request identifier for this enhancement
    # attempt, when one was obtained (None if the attempt failed before a
    # request_id was ever issued). Kept distinct from any generation-side
    # request_id — an upscale is a separate, separately-billed provider
    # call from the source generation it enhances, and callers that
    # persist provenance need to tell the two apart.
    request_id: Optional[str] = None
    # Some providers report an internal "auto-upgrade" step as part of
    # their own response (observed on the generation side of a real
    # recovered artifact: a completed job whose response included
    # upgrade_submitted=true and a separate upgrade_base_video_url,
    # while the actually-served output measured no higher than the
    # un-upgraded source). These are the provider's own CLAIMS about that
    # internal step and must never substitute for source_enhancement.py's
    # independent re-probe of output_path — see qualify_asset()'s
    # docstring. Default False/None: most providers never report this.
    upgrade_submitted: bool = False
    upgrade_base_video_url: Optional[str] = None


class EnhancementProvider(ABC):
    """
    A Source Enhancement provider raises an already-generated source clip's
    resolution. It is never a generation provider — it takes real footage
    in and returns real footage out, never a text prompt.

    Concrete providers set `name`, `model`, and `version` as class
    attributes (or instance attributes set in __init__) — these three
    values are what hypogeum/artifact_compatibility.py compares against a
    previously-recorded manifest entry to decide whether an existing
    enhancement is still valid or must be redone (e.g. after a provider
    swap or a provider-internal model/version bump).
    """

    name: str
    model: str
    version: str

    @abstractmethod
    def enhance(
        self,
        source_path: Path,
        dest_path: Path,
        target_width: int,
        target_height: int,
        target_resolution_label: str,
        config,
        episode_id: str,
        scene_id: str,
        log,
    ) -> EnhancementResult:
        """
        Enhance source_path to at least target_width x target_height and
        write the result to dest_path.

        The provider only produces a CANDIDATE file at dest_path — it does
        not decide whether that candidate is acceptable. hypogeum/source_
        enhancement.py always re-probes dest_path itself (width/height/
        codec/duration/fps) before committing it as the new canonical
        source clip; a provider reporting success=True is a claim, not
        proof. This keeps fail-closed validation in exactly one place
        regardless of how many providers exist.

        Must not raise for ordinary provider-side failures (HTTP errors,
        timeouts, rejected jobs) — those are reported via
        EnhancementResult(success=False, failure_reason=...) so
        hypogeum/source_enhancement.py's fail-closed policy can handle
        every provider uniformly. Raising is reserved for programming
        errors (e.g. missing config), matching the rest of this codebase's
        provider-client conventions (see hypogeum/muapi.py's exception
        classes for the equivalent generation-side contract).
        """
        raise NotImplementedError


class UnknownEnhancementProviderError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"No Source Enhancement provider registered for {key!r}")


_REGISTRY: Dict[str, Callable[[], EnhancementProvider]] = {}
_BUILTINS_REGISTERED = False


def register_provider(key: str, factory: Callable[[], EnhancementProvider]) -> None:
    """
    Register a zero-argument factory under `key`. Config.ENHANCEMENT_PROVIDER
    selects which registered key get_enhancement_provider() resolves.
    Tests use this to register a fake/deterministic provider without
    touching any real vendor code.
    """
    _REGISTRY[key] = factory


def _register_builtin_providers() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    # Lazy import: importing this abstraction module must never pull in a
    # concrete vendor's dependencies (requests calls, etc.) — only resolved
    # the first time a built-in key is actually requested.
    from hypogeum.providers.muapi_video_upscaler import (  # noqa: PLC0415
        MuapiVideoUpscalerProvider,
    )
    register_provider("muapi_ai_video_upscaler", MuapiVideoUpscalerProvider)
    _BUILTINS_REGISTERED = True


def get_enhancement_provider(config) -> EnhancementProvider:
    """
    Resolve config.ENHANCEMENT_PROVIDER to a concrete EnhancementProvider
    instance. This is the single seam hypogeum/source_enhancement.py uses
    to reach a vendor — it never imports a provider module by name itself.
    """
    key = config.ENHANCEMENT_PROVIDER
    if key not in _REGISTRY:
        _register_builtin_providers()
    if key not in _REGISTRY:
        raise UnknownEnhancementProviderError(key)
    return _REGISTRY[key]()
