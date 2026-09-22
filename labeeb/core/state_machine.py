"""Prompt formatting, structured decision parsing, and review transitions."""
from __future__ import annotations

import textwrap
from typing import Any

from labeeb.config import Config, critic_needed
from labeeb.errors import ControllerError
from labeeb.models import (
    CRITIC_END,
    CRITIC_START,
    DECISION_END,
    DECISION_START,
    format_json_for_prompt,
    new_operation_id,
    task_name,
    utc_now,
)
from labeeb.providers.base import extract_enveloped_json
from labeeb.providers.claude import ClaudeCriticProvider
from labeeb.providers.jules import path_allowed
from labeeb.storage.goal_store import GoalPaths, GoalStore, read_ref_json


def initial_brain_prompt(contract_seed: dict[str, Any], config: Config) -> str:
    custom = config.get("prompts.brain_initial", "")
    if custom:
        return str(custom).format(contract_json=format_json_for_prompt(contract_seed))
    return textwrap.dedent(
        f"""
        You are the Labeeb engineering decision brain. Work READ-ONLY in the repository during this turn.
        If the labeeb-orchestrator skill is available, use it. Do not implement, commit, push, create PRs, merge, or mutate production.

        Compile the user's intent into a bounded Goal Contract, inspect current repository reality, deliberately reuse existing mechanisms, evaluate alternatives, perform a second audit, and converge on a worker-ready plan.

        Initial immutable request:
        {format_json_for_prompt(contract_seed)}

        Return exactly one structured envelope:
        {DECISION_START}
        {{
          "action": "PLAN_READY" | "BLOCKED",
          "reason": "...",
          "goal_contract": {{
            "observable_outcome": "...",
            "current_behavior": "...",
            "expected_behavior": "...",
            "constraints": ["..."],
            "acceptance_criteria": ["..."],
            "non_goals": ["..."],
            "must_not_change": ["..."],
            "required_evidence": ["..."],
            "material_unknowns": ["..."]
          }},
          "execution": {{
            "jules_prompt": "bounded execution contract",
            "validation_commands": ["deterministic command"],
            "allowed_paths": ["path/or/prefix"],
            "risk_tags": ["architecture|concurrency|persistence|security|public_contract|large_blast_radius|other"],
            "needs_pre_critic": false,
            "needs_post_critic": false
          }},
          "plan_summary": "..."
        }}
        {DECISION_END}

        Do not add prose outside the envelope.
        """
    ).strip()


def convergence_prompt(contract: dict[str, Any], plan: dict[str, Any], critique: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        Continue the SAME Labeeb goal. An independent read-only critic reviewed the candidate plan.
        Resolve every material finding against repository evidence. Reject unsupported criticism explicitly; change the plan only when evidence requires it.

        Goal contract:
        {format_json_for_prompt(contract)}

        Candidate plan:
        {format_json_for_prompt(plan)}

        Critic result:
        {format_json_for_prompt(critique)}

        Return exactly:
        {DECISION_START}
        {{
          "action": "PLAN_READY" | "BLOCKED",
          "reason": "...",
          "goal_contract": {{...complete contract...}},
          "execution": {{
            "jules_prompt": "complete bounded execution contract",
            "validation_commands": ["..."],
            "allowed_paths": ["..."],
            "risk_tags": ["..."],
            "needs_pre_critic": false,
            "needs_post_critic": true|false
          }},
          "plan_summary": "...",
          "critic_resolution": [{{"finding":"...","resolution":"accepted|rejected|blocked","evidence":"..."}}]
        }}
        {DECISION_END}
        No prose outside the envelope.
        """
    ).strip()


def event_brain_prompt(
    event: dict[str, Any],
    evidence: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    config: Config,
) -> str:
    max_chars = int(config.get("controller.max_prompt_evidence_chars", 70000))
    return textwrap.dedent(
        f"""
        Continue the SAME Labeeb goal. A deterministic controller woke you for a meaningful event.
        Do not execute Jules write commands yourself. Do not push, create a PR, merge, or mutate production.

        Contract:
        {format_json_for_prompt(contract)}

        Approved plan/execution contract:
        {format_json_for_prompt(plan)}

        Event:
        {format_json_for_prompt(event)}

        Evidence packet:
        {format_json_for_prompt(evidence, max_chars=max_chars)}

        Return exactly one action envelope:
        {DECISION_START}
        {{
          "action": "PASS" | "REPAIR" | "APPROVE_JULES_PLAN" | "ANSWER_JULES" | "BLOCKED" | "FAIL",
          "reason": "...",
          "repair_message": "required only for REPAIR",
          "answer_message": "required only for ANSWER_JULES",
          "evidence_assessment": "..."
        }}
        {DECISION_END}

        Rules:
        - PASS only if the original Goal Contract is actually evidenced, not because Jules says completed.
        - REPAIR must be a targeted correction of a concrete mismatch; no redesign or scope widening.
        - If authority/scope/evidence is insufficient, BLOCKED.
        - No prose outside the envelope.
        """
    ).strip()


def critic_prompt(
    when: str,
    contract: dict[str, Any],
    plan: dict[str, Any],
    evidence: dict[str, Any] | None,
    config: Config,
) -> str:
    if when == "pre":
        task = "Try to disprove or simplify the proposed plan before implementation."
        focus = "missed reuse, hidden coupling, secondary callers, concurrency/state invariants, excessive scope, missing validation, architecture mismatch"
    else:
        task = "Try to find a material reason the actual implementation does not satisfy the original Goal Contract."
        focus = "requirement omissions, scope drift, hidden regressions, unplanned files, stale assumptions, false-positive tests, validation gaps"
    max_chars = int(config.get("controller.max_prompt_evidence_chars", 70000))
    return textwrap.dedent(
        f"""
        You are an independent READ-ONLY engineering critic. {task}
        Do not edit files. Do not use write-capable tools. Do not contact Jules. Do not redesign merely for preference.
        Focus on: {focus}.

        Goal contract:
        {format_json_for_prompt(contract)}

        Plan:
        {format_json_for_prompt(plan)}

        Evidence:
        {format_json_for_prompt(evidence or {}, max_chars=max_chars)}

        Return exactly:
        {CRITIC_START}
        {{
          "material_findings": [
            {{"finding":"...","location":"...","evidence":"...","impact":"...","test":"..."}}
          ],
          "notes": ""
        }}
        {CRITIC_END}
        No prose outside the envelope.
        """
    ).strip()
