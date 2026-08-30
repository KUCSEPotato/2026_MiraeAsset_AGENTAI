"""Explicit, coherent runtime data-bundle selection.

The application intentionally selects a bundle, rather than independently
selecting RDB, graph, and semantic stores.  This prevents a partially set
environment from combining canonical_v2 with a legacy derived store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


RuntimeDataVersion = Literal["v1", "v2"]


@dataclass(frozen=True)
class RuntimeBundle:
    """The selected store family; compatibility is checked at startup."""

    version: RuntimeDataVersion

    @property
    def name(self) -> str:
        return f"canonical_{self.version}"

    @property
    def uses_canonical_v2(self) -> bool:
        return self.version == "v2"


def select_runtime_bundle_from_env() -> RuntimeBundle:
    """Select one bundle and reject contradictory legacy store switches.

    ``RUNTIME_DATA_VERSION`` is the deployment-facing selection.  The older
    v2 variables remain supported only as a complete, mutually consistent
    legacy selection so an existing deployment cannot accidentally become a
    mixed bundle during an upgrade.
    """

    configured = os.getenv("RUNTIME_DATA_VERSION")
    legacy_repository = os.getenv("RDB_REPOSITORY_VERSION")
    legacy_multi_store = os.getenv("CANONICAL_V2_MULTI_STORE_ENABLED")
    legacy_enabled = _as_bool(legacy_multi_store) if legacy_multi_store is not None else None

    if configured is not None:
        normalized = configured.strip().lower()
        if normalized not in {"v1", "v2"}:
            raise ValueError("RUNTIME_DATA_VERSION must be v1 or v2")
        if legacy_repository is not None and legacy_repository.strip().lower() != normalized:
            raise ValueError(
                "RUNTIME_DATA_VERSION conflicts with RDB_REPOSITORY_VERSION"
            )
        if normalized == "v1" and legacy_enabled is True:
            raise ValueError(
                "RUNTIME_DATA_VERSION=v1 cannot enable CANONICAL_V2_MULTI_STORE_ENABLED"
            )
        if normalized == "v2" and legacy_enabled is False:
            raise ValueError(
                "RUNTIME_DATA_VERSION=v2 cannot disable CANONICAL_V2_MULTI_STORE_ENABLED"
            )
        return RuntimeBundle(version=normalized)  # type: ignore[arg-type]

    # Backward compatibility requires both legacy values.  One v2 switch is
    # never enough to infer a multi-store v2 request.
    repository = (legacy_repository or "v1").strip().lower()
    if repository not in {"v1", "v2"}:
        raise ValueError("RDB_REPOSITORY_VERSION must be v1 or v2")
    if repository == "v2" and legacy_enabled is not True:
        raise ValueError(
            "v2 requires RUNTIME_DATA_VERSION=v2 or the complete legacy v2 configuration"
        )
    if repository == "v1" and legacy_enabled is True:
        raise ValueError(
            "CANONICAL_V2_MULTI_STORE_ENABLED requires RDB_REPOSITORY_VERSION=v2"
        )
    return RuntimeBundle(version=repository)  # type: ignore[arg-type]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}
