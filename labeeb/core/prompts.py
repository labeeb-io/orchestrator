"""Canonical prompt fragments and deterministic composition for Jules implementation workers."""
from __future__ import annotations

import textwrap

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
