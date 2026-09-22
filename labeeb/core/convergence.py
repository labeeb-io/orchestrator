"""Anti-loop protection, cycle detection, and reasoning progress tracking."""
from __future__ import annotations

from typing import Any

from labeeb.models import (
    ArtifactStatus,
    canonical_json,
    sha256_text,
)


class ReasoningProgressTracker:
    @staticmethod
    def init_tracker(state: dict[str, Any]) -> dict[str, Any]:
        return state.setdefault(
            "no_progress_tracker",
            {
                "total_reasoning_steps": 0,
                "activity_counts": {},
                "activity_signatures": {},
                "seen_evidence_refs": [],
                "recent_activity_sequence": [],
            },
        )

    @classmethod
    def record_step(
        cls,
        state: dict[str, Any],
        current_activity: str,
        produced_artifact_data: dict[str, Any] | None,
        evidence_refs: list[str] | None,
        *,
        activity_status: str = ArtifactStatus.SATISFIED,
        max_steps: int = 25,
        max_repeats: int = 3,
    ) -> tuple[bool, str]:
        """Evaluate if reasoning step made progress. Returns (progress_ok, reason)."""
        tracker = cls.init_tracker(state)
        tracker["total_reasoning_steps"] += 1
        total_steps = tracker["total_reasoning_steps"]

        if total_steps > max_steps:
            return False, f"Global reasoning step limit exceeded ({max_steps} steps max)"

        # Progress detection signals
        has_progress = False

        # 1. New evidence refs
        seen_refs = set(tracker.setdefault("seen_evidence_refs", []))
        incoming_refs = set(evidence_refs or [])
        new_refs = incoming_refs - seen_refs
        if new_refs:
            has_progress = True
            tracker["seen_evidence_refs"].extend(sorted(new_refs))

        # 2. Artifact content hash change
        signatures = tracker.setdefault("activity_signatures", {})
        prior_hashes = signatures.setdefault(current_activity, [])
        current_hash = (
            sha256_text(canonical_json(produced_artifact_data))
            if produced_artifact_data
            else ""
        )
        if current_hash and current_hash not in prior_hashes:
            has_progress = True
            prior_hashes.append(current_hash)

        # 3. Status advancement (e.g. NOT_APPLICABLE or SATISFIED)
        if activity_status in {ArtifactStatus.NOT_APPLICABLE, ArtifactStatus.SATISFIED}:
            if not prior_hashes:
                has_progress = True

        # Track repeat counts
        counts = tracker.setdefault("activity_counts", {})
        current_count = counts.get(current_activity, 0)

        if has_progress:
            counts[current_activity] = 1
        else:
            current_count += 1
            counts[current_activity] = current_count
            if current_count > max_repeats:
                return (
                    False,
                    f"Reasoning convergence stalled: activity '{current_activity}' repeated {current_count} times without new evidence or state change",
                )

        # 4. Cycle detection (e.g. ping-pong: A -> B -> A -> B)
        history = tracker.setdefault("recent_activity_sequence", [])
        history.append(current_activity)
        if len(history) >= 4:
            a, b, c, d = history[-4:]
            if a == c and b == d and a != b and not has_progress:
                return (
                    False,
                    f"Reasoning cycle detected between '{a}' and '{b}' without state progress",
                )

        return True, "OK"
