"""Immutable artifact persistence, versioning, dependency tracking, and cascading invalidation."""
from __future__ import annotations

import collections
import pathlib
import re
from typing import Any, Iterable

from labeeb.errors import ControllerError
from labeeb.models import (
    ArtifactMeta,
    ArtifactStatus,
    ArtifactValidity,
    canonical_json,
    sha256_text,
    utc_now,
)
from labeeb.storage.goal_store import (
    GoalPaths,
    GoalStore,
    atomic_json_write,
    file_ref,
    read_ref_json,
)

# Standard Artifact Type Identifiers
AUTHORITY_CONTEXT = "authority_context"
GOAL_CONTRACT = "goal_contract"
PRODUCT_CONTRACT = "product_contract"
PROOF_CONTRACT = "proof_contract"
REALITY_AUDIT = "reality_audit"
MUTATION_PREFLIGHT = "mutation_preflight"
BASELINE_RESULT = "baseline_result"
DIAGNOSIS = "diagnosis"
SOLUTION_CANDIDATES = "solution_candidates"
SECOND_AUDIT = "second_audit"
DELIVERY_REVIEW = "delivery_review"
CRITIC_REVIEW = "critic_review"
CONVERGENCE = "convergence"
CHANGE_AUTHORITY = "change_authority"
IMPLEMENTATION_READINESS = "implementation_readiness"
EXECUTION_CONTRACT = "execution_contract"
IMPLEMENTATION_RESULT = "implementation_result"
VALIDATION_RESULT = "validation_result"
GOAL_PROOF = "goal_proof"
FINAL_REPORT = "final_report"

# Direct dependency relationships (Artifact -> direct prerequisites)
ARTIFACT_DIRECT_DEPENDENCIES: dict[str, list[str]] = {
    AUTHORITY_CONTEXT: [],
    GOAL_CONTRACT: [AUTHORITY_CONTEXT],
    PRODUCT_CONTRACT: [GOAL_CONTRACT],
    PROOF_CONTRACT: [GOAL_CONTRACT],
    REALITY_AUDIT: [GOAL_CONTRACT, PROOF_CONTRACT],
    MUTATION_PREFLIGHT: [PROOF_CONTRACT, REALITY_AUDIT],
    BASELINE_RESULT: [PROOF_CONTRACT, MUTATION_PREFLIGHT],
    DIAGNOSIS: [BASELINE_RESULT, REALITY_AUDIT],
    SOLUTION_CANDIDATES: [REALITY_AUDIT, DIAGNOSIS],
    SECOND_AUDIT: [SOLUTION_CANDIDATES, REALITY_AUDIT],
    DELIVERY_REVIEW: [PRODUCT_CONTRACT, SOLUTION_CANDIDATES, SECOND_AUDIT],
    CRITIC_REVIEW: [SOLUTION_CANDIDATES, SECOND_AUDIT],
    CONVERGENCE: [SOLUTION_CANDIDATES, SECOND_AUDIT, CRITIC_REVIEW],
    CHANGE_AUTHORITY: [DIAGNOSIS, SOLUTION_CANDIDATES, CONVERGENCE],
    IMPLEMENTATION_READINESS: [
        AUTHORITY_CONTEXT,
        GOAL_CONTRACT,
        PROOF_CONTRACT,
        REALITY_AUDIT,
        SOLUTION_CANDIDATES,
        SECOND_AUDIT,
        CONVERGENCE,
        CHANGE_AUTHORITY,
    ],
    EXECUTION_CONTRACT: [
        IMPLEMENTATION_READINESS,
        SOLUTION_CANDIDATES,
        CONVERGENCE,
        CHANGE_AUTHORITY,
    ],
    IMPLEMENTATION_RESULT: [EXECUTION_CONTRACT],
    VALIDATION_RESULT: [IMPLEMENTATION_RESULT],
    GOAL_PROOF: [PROOF_CONTRACT, IMPLEMENTATION_RESULT, VALIDATION_RESULT],
    FINAL_REPORT: [GOAL_CONTRACT, PROOF_CONTRACT, IMPLEMENTATION_READINESS, VALIDATION_RESULT],
}


def _build_downstream_dependency_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for downstream, prerequisites in ARTIFACT_DIRECT_DEPENDENCIES.items():
        for prereq in prerequisites:
            graph[prereq].add(downstream)
    return dict(graph)


ARTIFACT_DOWNSTREAM_DEPENDENCIES: dict[str, set[str]] = _build_downstream_dependency_graph()


def compute_cascading_invalidation(
    invalidate_roots: Iterable[str],
    current_artifact_types: Iterable[str] | None = None,
) -> set[str]:
    """Compute transitive set of downstream artifact types invalidated by invalidate_roots."""
    visited: set[str] = set()
    queue = list(invalidate_roots)
    while queue:
        curr = queue.pop(0)
        for downstream in ARTIFACT_DOWNSTREAM_DEPENDENCIES.get(curr, set()):
            if downstream not in visited:
                visited.add(downstream)
                queue.append(downstream)
    result = set(invalidate_roots) | visited
    if current_artifact_types is not None:
        return result.intersection(set(current_artifact_types))
    return result


class GoalArtifactStore:
    def __init__(self, paths: GoalPaths, store: GoalStore):
        self.paths = paths
        self.store = store

    def _next_version(self, artifact_type: str, state: dict[str, Any]) -> int:
        """Find the next incremental version for this artifact type."""
        existing_version = int((state.get("artifacts", {}).get(artifact_type) or {}).get("version", 0))
        if self.paths.artifacts.exists():
            for p in self.paths.artifacts.glob(f"{artifact_type}.v*.json"):
                m = re.match(rf"^{re.escape(artifact_type)}\.v(\d+)\.json$", p.name)
                if m:
                    existing_version = max(existing_version, int(m.group(1)))
        return existing_version + 1

    def write_artifact(
        self,
        state: dict[str, Any],
        artifact_type: str,
        data: dict[str, Any],
        producer: str,
        *,
        dependencies: dict[str, int] | None = None,
        evidence_refs: list[str] | None = None,
        activity_status: str = ArtifactStatus.SATISFIED,
        not_applicable_reason: str | None = None,
        allow_stale_dependency: bool = False,
    ) -> tuple[str, int]:
        """Write an immutable versioned artifact and update the goal artifact index."""
        self.paths.artifacts.mkdir(parents=True, exist_ok=True)
        version = self._next_version(artifact_type, state)
        artifacts_index = state.setdefault("artifacts", {})

        # Automatically record current versions of direct prerequisites if not explicitly given
        resolved_dependencies: dict[str, int] = {}
        if dependencies is not None:
            resolved_dependencies = dict(dependencies)
        else:
            for dep in ARTIFACT_DIRECT_DEPENDENCIES.get(artifact_type, []):
                dep_info = artifacts_index.get(dep)
                if isinstance(dep_info, dict) and dep_info.get("version"):
                    resolved_dependencies[dep] = int(dep_info["version"])

        # Enforce that no dependency is STALE unless explicitly allowed (safety invariant)
        if not allow_stale_dependency:
            for dep in resolved_dependencies:
                dep_info = artifacts_index.get(dep)
                if isinstance(dep_info, dict) and dep_info.get("validity") == ArtifactValidity.STALE:
                    raise ControllerError(
                        f"Cannot write artifact '{artifact_type}' because dependency '{dep}' is STALE"
                    )

        payload = ArtifactMeta(
            artifact_type=artifact_type,
            version=version,
            validity=ArtifactValidity.VALID,
            activity_status=activity_status,
            producer=producer,
            created_at=utc_now(),
            dependencies=resolved_dependencies,
            evidence_refs=list(evidence_refs or []),
            not_applicable_reason=not_applicable_reason,
            data=data,
            sha256=sha256_text(canonical_json(data)),
        )

        target_file = self.paths.artifacts / f"{artifact_type}.v{version}.json"
        atomic_json_write(target_file, payload.to_dict())
        ref = file_ref(target_file)

        # Update index in state
        artifacts_index[artifact_type] = {
            "ref": ref,
            "version": version,
            "validity": ArtifactValidity.VALID,
            "activity_status": activity_status,
            "not_applicable_reason": not_applicable_reason,
            "sha256": payload.sha256,
            "updated_at": utc_now(),
        }

        return ref, version

    def invalidate_artifacts(
        self,
        state: dict[str, Any],
        invalidate_roots: Iterable[str],
        *,
        exclude: Iterable[str] | None = None,
    ) -> set[str]:
        """Mark invalidate_roots and all cascading downstream artifacts as STALE in the index.

        Files on disk are immutable and remain untouched for auditability.
        """
        artifacts_index = state.setdefault("artifacts", {})
        to_invalidate = compute_cascading_invalidation(
            invalidate_roots, current_artifact_types=artifacts_index.keys()
        )
        if exclude:
            to_invalidate = to_invalidate - set(exclude)
        affected: set[str] = set()
        for art_type in to_invalidate:
            entry = artifacts_index.get(art_type)
            if isinstance(entry, dict) and entry.get("validity") != ArtifactValidity.STALE:
                entry["validity"] = ArtifactValidity.STALE
                entry["updated_at"] = utc_now()
                affected.add(art_type)
        return affected

    def get_latest_artifact(self, state: dict[str, Any], artifact_type: str) -> dict[str, Any] | None:
        """Read the latest artifact payload recorded in the goal index."""
        entry = (state.get("artifacts") or {}).get(artifact_type)
        if not isinstance(entry, dict) or not entry.get("ref"):
            return None
        return read_ref_json(entry["ref"])

    def get_artifact_version(self, artifact_type: str, version: int) -> dict[str, Any] | None:
        """Read a specific immutable version directly from disk."""
        target_file = self.paths.artifacts / f"{artifact_type}.v{version}.json"
        if not target_file.exists():
            return None
        import json

        return json.loads(target_file.read_text(encoding="utf-8"))

    def list_artifact_versions(self, artifact_type: str) -> list[dict[str, Any]]:
        """List all persisted versions of an artifact type ordered by version."""
        if not self.paths.artifacts.exists():
            return []
        versions = []
        for p in sorted(self.paths.artifacts.glob(f"{artifact_type}.v*.json")):
            m = re.match(rf"^{re.escape(artifact_type)}\.v(\d+)\.json$", p.name)
            if m:
                import json

                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    versions.append(data)
                except Exception:
                    continue
        return sorted(versions, key=lambda x: int(x.get("version", 0)))

    def validate_artifact_dependencies(
        self,
        state: dict[str, Any],
        artifact_type: str,
    ) -> tuple[bool, list[str]]:
        """Verify whether all required dependencies of an artifact are currently VALID."""
        artifacts_index = state.get("artifacts") or {}
        stale_or_missing: list[str] = []
        for dep in ARTIFACT_DIRECT_DEPENDENCIES.get(artifact_type, []):
            entry = artifacts_index.get(dep)
            if not isinstance(entry, dict):
                stale_or_missing.append(f"{dep}:missing")
            elif entry.get("validity") == ArtifactValidity.STALE:
                stale_or_missing.append(f"{dep}:stale")
        return len(stale_or_missing) == 0, stale_or_missing
