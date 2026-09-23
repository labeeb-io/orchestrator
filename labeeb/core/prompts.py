"""Canonical prompt fragments and deterministic composition for Jules implementation workers."""
from __future__ import annotations

import re
import textwrap
from typing import Any

REMOTE_WRITE_BOUNDARY = textwrap.dedent(
    """\
    No push.
    No PR creation.
    No merge.
    No production mutation.
    No remote ref changes.
    Return control if scope must widen."""
).strip()


def build_workspace_implementation_contract() -> str:
    """Produce the canonical, non-overridable implementation-worker materialization contract."""
    return textwrap.dedent(
        """\
        WORKSPACE IMPLEMENTATION CONTRACT

        You are an implementation worker operating on the actual repository working tree.

        You MUST perform the requested implementation by creating/modifying the authorized files in the repository workspace using your repository editing tools.

        A response containing source code, a suggested patch, or a unified diff is NOT implementation.

        Do not merely:
        - describe the requested change;
        - print the desired source code;
        - print a hypothetical unified diff;
        - explain what should be changed.

        You must materialize the requested changes on disk.

        After editing, verify the actual repository state.

        At minimum:
        1. Verify every requested new file actually exists.
        2. Inspect `git status --short`.
        3. Inspect the resulting diff/change set.
        4. Verify no path outside the authorized scope was modified.
        5. Run the requested validation commands when provided.

        Do not claim implementation is complete unless these checks confirm that the working-tree changes exist.

        When the implementation is materialized and verification is complete:
        - stop making further changes;
        - return control to the orchestrator;
        - do not continue with unrelated improvements;
        - do not commit unless the execution contract explicitly authorizes it;
        - do not push;
        - do not create a pull request;
        - do not merge;
        - do not modify remote refs;
        - do not mutate production.

        Your final response should summarize the changes that actually exist in the workspace. Do not use the final response as a substitute for editing the repository."""
    ).strip()


def build_materialization_correction_message() -> str:
    """Bounded protocol correction sent to Jules when it provides text/diff without disk changes."""
    return textwrap.dedent(
        """\
        Your previous response described or printed the requested change, but the orchestrator cannot verify any materialized repository change.

        Do not return the code or patch as text.

        Create/modify the requested files in the actual repository working tree using your repository editing tools.

        Verify the resulting working-tree change with repository state/diff inspection.

        Do not commit or push.

        Stop after the actual change is materialized and return control."""
    ).strip()


def _sanitize_direct_prompt(prompt_str: str) -> str:
    """Sanitize phrases in Brain prompts that encourage textual/diff-only responses."""
    text = prompt_str.strip()
    replacements = [
        (re.compile(r"Produce a unified patch containing only", re.IGNORECASE), "Create or modify the following file(s) in the repository working tree:"),
        (re.compile(r"Produce a unified patch\b", re.IGNORECASE), "Create or modify the authorized file(s) in the repository working tree"),
        (re.compile(r"Produce a patch\b", re.IGNORECASE), "Create or modify the authorized file(s) in the repository working tree"),
        (re.compile(r"Return a patch\b", re.IGNORECASE), "Materialize the changes in the repository working tree"),
        (re.compile(r"Output the following file\b", re.IGNORECASE), "Create or modify the following file in the repository working tree"),
    ]
    for pattern, repl in replacements:
        text = pattern.sub(repl, text)
    return text


def build_jules_prompt(
    execution: dict[str, Any],
    produced_data: dict[str, Any],
    plan_summary: str = "",
) -> str:
    """Extract or synthesize a complete Jules prompt from execution or artifact data, emphasizing working-tree mutation."""
    direct = (
        execution.get("jules_prompt")
        or execution.get("prompt")
        or produced_data.get("jules_prompt")
        or produced_data.get("prompt")
    )
    if direct and str(direct).strip():
        return _sanitize_direct_prompt(str(direct))

    # Synthesize from structured execution_contract artifact fields
    sections: list[str] = []
    goal = (
        produced_data.get("goal")
        or produced_data.get("approved_direction")
        or plan_summary
    )
    if goal:
        sections.append(f"Goal:\n{goal}")

    impl = produced_data.get("implementation") or produced_data.get("instructions")
    if impl:
        if isinstance(impl, list):
            sections.append("Implementation Steps:\n" + "\n".join(f"- {s}" for s in impl))
        elif isinstance(impl, str) and impl.strip():
            sections.append(f"Implementation:\n{impl.strip()}")

    new_files = produced_data.get("new_files") or []
    modified_files = produced_data.get("modified_files") or []
    expected = produced_data.get("expected_worker_output") or produced_data.get("expected_output")

    if new_files and isinstance(new_files, list):
        sections.append("Required Materialized File(s):\n" + "\n".join(f"- {f}" for f in new_files))
    if modified_files and isinstance(modified_files, list):
        sections.append("Required Working-Tree Modifications:\n" + "\n".join(f"- {f}" for f in modified_files))
    if expected and not (new_files or modified_files):
        sections.append(f"Required Working-Tree Changes:\n{expected}\nThe actual repository change is the required output.")
    elif not (new_files or modified_files):
        sections.append("Required Action:\nCreate or modify the authorized file(s) in the repository working tree.\nThe actual repository change is the required output.")

    criteria = produced_data.get("acceptance_criteria")
    if criteria and isinstance(criteria, list):
        sections.append("Acceptance Criteria:\n" + "\n".join(f"- {c}" for c in criteria))

    paths = execution.get("allowed_paths") or produced_data.get("allowed_paths")
    if paths and isinstance(paths, list):
        sections.append("Allowed Paths:\n" + "\n".join(f"- {p}" for p in paths))

    cmds = execution.get("validation_commands") or produced_data.get("validation_commands")
    if cmds and isinstance(cmds, list):
        sections.append("Validation Commands:\n" + "\n".join(f"- {c}" for c in cmds))

    stop_conds = produced_data.get("stop_conditions")
    if stop_conds and isinstance(stop_conds, list):
        sections.append("Stop Conditions:\n" + "\n".join(f"- {c}" for c in stop_conds))

    return "\n\n".join(sections).strip()


def compose_jules_implementation_prompt(
    execution_prompt: str,
    *,
    allowed_paths: list[str] | None = None,
    validation_commands: list[str] | None = None,
    expected_outputs: list[str] | None = None,
    remote_write_boundary: str | None = None,
) -> str:
    """Deterministically compose the final implementation prompt in strict section order:

    1. Brain / Execution Contract instructions
    2. === WORKSPACE IMPLEMENTATION CONTRACT ===
    3. === AUTHORIZED SCOPE ===
    4. === REMOTE-WRITE BOUNDARY ===
    """
    clean_exec = str(execution_prompt or "").strip()
    contract_section = build_workspace_implementation_contract()
    boundary_text = str(remote_write_boundary or REMOTE_WRITE_BOUNDARY).strip()

    # Scope section
    scope_lines = ["=== AUTHORIZED SCOPE ==="]
    paths = list(allowed_paths or [])
    if paths:
        scope_lines.append("Allowed paths:")
        for p in paths:
            scope_lines.append(f"- {p}")
    else:
        scope_lines.append("Allowed paths: (Standard repository bounds)")

    cmds = list(validation_commands or [])
    if cmds:
        scope_lines.append("\nValidation commands:")
        for c in cmds:
            scope_lines.append(f"- {c}")
    else:
        scope_lines.append("\nValidation commands: None specified")

    outs = list(expected_outputs or [])
    if outs:
        scope_lines.append("\nExpected working-tree modifications:")
        for o in outs:
            scope_lines.append(f"- {o}")

    scope_section = "\n".join(scope_lines).strip()

    parts = [
        clean_exec,
        f"=== WORKSPACE IMPLEMENTATION CONTRACT ===\n{contract_section}",
        scope_section,
        f"=== REMOTE-WRITE BOUNDARY ===\n{boundary_text}",
    ]
    return "\n\n".join(p for p in parts if p).strip()
