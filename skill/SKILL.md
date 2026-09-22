---
name: labeeb-orchestrator
description: Autonomous, evidence-driven engineering orchestration across THINK/CONVERGE, EXECUTE, PROVE, and REPORTING for Labeeb goals. Coordinates Codex brain, Claude critic, and Jules implementer with deterministic Python Controller state and immutable versioned artifacts.
---

# Labeeb Orchestrator V2 — Agent Skill

The `labeeb-orchestrator` skill defines the autonomous, evidence-driven orchestration contract for long-running software engineering tasks in the Labeeb ecosystem.

## 1. Core Operating Invariants

1. **Deterministic Controller Ownership**:
   - The Python Controller is the **sole owner** of durable workflow state, process locking (`flock`), atomic disk writes (`fsync`), and orchestration side effects.
   - The Brain (Codex) reasons, audits, and decides. Claude criticizes independently. Jules implements. Automated tests prove.
   - **Never attempt to execute write side effects directly** (do not push, merge, create PRs, mutate remote production, or bypass the Controller).

2. **Autonomy by Default**:
   - The workflow proceeds unattended from intent through reasoning, implementation, proof, and reporting.
   - Escalation to human review (`PLAN_GATE`) occurs **only** when crossing legitimate authority boundaries (e.g. out-of-scope paths, production mutations, or unresolvable product decisions).

3. **Separated Resource Budgets**:
   - **Reasoning Loop Budget**: Maximum 25 reasoning steps; maximum 3 visits per activity.
   - **Execution Budget**: Maximum 2 implementation rounds with Jules.
   - **Repair Budget**: Exactly 1 targeted repair round for implementation/patch defects.
   - **Backtracking Semantics**: If post-implementation proof reveals an invalidated planning assumption, emit `RETURN_TO_THINKING`. The Controller will invalidate affected artifacts while **preserving the single targeted repair budget**.

---

## 2. The 4 Macro Phases

```text
THINK / CONVERGE
      |
      v
   EXECUTE
      |
      v
    PROVE  ---- (Planning Assumption Invalidated) ----> RETURN TO THINK / CONVERGE
      |                                                 (Preserves Repair Budget)
      v
  REPORTING
      |
      v
PASS | FAIL | BLOCKED
```

- **`THINK / CONVERGE`**: The Brain autonomously traverses up to 16 fine-grained reasoning activities to audit reality, formulate baseline tests, evaluate candidate architectures, integrate critique, and compile an implementation contract.
- **`EXECUTE`**: The Controller isolates the target repository in an isolated worktree and dispatches the bounded execution contract to Jules.
- **`PROVE`**: The Controller applies Jules's patch and executes the locked original proof entrypoint and completion probe.
- **`REPORTING`**: Mandatory phase before terminal completion. Generates both structured `final_report.json` and human-readable `final_report.md` via a 100% deterministic fallback generator.
- **Terminal States**:
  - `PASS`: Objective, deterministic proof that the original Goal Contract is satisfied.
  - `FAIL`: Unrecoverable defect or exhaustion of execution/repair budgets.
  - `BLOCKED`: Authority boundary, safety violation, or ambiguous reconciliation requiring human intervention.

---

## 3. The 16 Reasoning Activities

Every turn within `THINK / CONVERGE` executes one specific reasoning activity from the deterministic DAG:

| # | Activity Identifier | Output Artifact | Responsibility |
|---|---|---|---|
| 1 | `authority_context` | `authority_context.v<n>.json` | Compiles repository policies, environment bounds, and user constraints from `AGENTS.md`. Secrets and tokens are strictly redacted. |
| 2 | `goal_contract` | `goal_contract.v<n>.json` | Compiles user intent into observable outcomes, current vs expected behavior, constraints, non-goals, and required evidence. |
| 3 | `product_validation` | `product_contract.v<n>.json` | Validates product standards, user journey integrity, and bilingual (Arabic/English) criteria. |
| 4 | `proof_contract` | `proof_contract.v<n>.json` | Defines the observable entrypoint, completion probe, and deterministic test commands. Locks the proof path. |
| 5 | `reality_audit` | `reality_audit.v<n>.json` | Inspects live repository code, analogous implementations, existing abstractions, callers, and dead code to avoid redundant mechanisms. |
| 6 | `mutation_preflight` | `mutation_preflight.v<n>.json` | Evaluates safety of baseline commands. Blocks unbounded fan-out, missing cleanup, or uncontained mutations. |
| 7 | `baseline` | `baseline_result.v<n>.json` | Runs baseline commands before any code edits. **Shortcut:** If baseline already passes, goal passes without mutations. |
| 8 | `diagnosis` | `diagnosis.v<n>.json` | Classifies root causes, failure mechanisms, and existing invariant violations. |
| 9 | `solution_exploration` | `solution_candidates.v<n>.json` | Evaluates alternatives: deliberate reuse vs extension vs composition vs new component. Prioritizes minimal blast radius. |
| 10 | `second_reality_audit` | `second_audit.v<n>.json` | Actively stress-tests preferred candidate against live code, secondary callers, and edge cases. Invalidate roots if disproven. |
| 11 | `delivery_readiness` | `delivery_review.v<n>.json` | Evaluates blast radius, file modifications, migration reversibility, and rollback strategy. |
| 12 | `independent_critique` | `critic_review.v<n>.json` | Read-only adversarial review by Claude looking for missed reuse, hidden coupling, and concurrency/persistence risks. |
| 13 | `convergence` | `convergence.v<n>.json` | Resolves all material critique findings against repository evidence. |
| 14 | `change_authority` | `change_authority.v<n>.json` | Classifies change: `LOCAL_ENGINEERING`, `GATED_ENGINEERING`, `INCIDENTAL_SCOPE`, `PRODUCTION_MUTATION`. |
| 15 | `implementation_readiness` | `implementation_readiness.v<n>.json` | Controller verification gate verifying all 10 readiness invariants. |
| 16 | `execution_dispatch` | `execution_contract.v<n>.json` | Formulates bounded Jules prompt with explicit path limits and validation commands. |

---

## 4. Structured Decision Envelope

In every turn, the Brain MUST output its conclusion inside exactly one structured decision envelope. No commentary outside the envelope is evaluated.

```text
<<<DECISION_START>>>
{
  "decision": "CONTINUE_REASONING" | "IMPLEMENTATION_READY" | "NEEDS_HUMAN" | "RETURN_TO_THINKING" | "TARGETED_REPAIR" | "PASS" | "FAIL" | "BLOCKED",
  "current_activity": "second_reality_audit",
  "activity_status": "SATISFIED" | "NOT_APPLICABLE" | "NEEDS_WORK" | "BLOCKED",
  "not_applicable_reason": null,
  "next_activity": "delivery_readiness",
  "reason": "Detailed evidence-backed rationale for transition or status.",
  "produced_artifact": {
    "artifact_type": "second_audit",
    "data": {
      "survived": true,
      "audit_findings": []
    }
  },
  "invalidate_roots": ["solution_candidates"],
  "evidence_refs": ["file:app/Services/ClaimService.php#L45-L60"],
  "human_checkpoint": {
    "needed": false,
    "boundary_type": null,
    "question": null
  }
}
<<<DECISION_END>>>
```

### Action Semantics
- `CONTINUE_REASONING`: Completed current activity; advances to `next_activity` according to transition matrix.
- `IMPLEMENTATION_READY`: All 16 activities converged and verified; ready for autonomous dispatch or human gate.
- `NEEDS_HUMAN`: Encountered legitimate authority or safety boundary; pauses at `PLAN_GATE` with `human_checkpoint`.
- `RETURN_TO_THINKING`: Emitted during `PROVE` when an assumption is invalidated; backtracks to `THINK / CONVERGE` with `invalidate_roots`.
- `TARGETED_REPAIR`: Emitted during `PROVE` when implementation contains a bounded code/syntax bug; uses the 1 repair budget.
- `PASS`: Original Goal Contract independently proved.
- `BLOCKED`: Safety rule or invariant violated.

---

## 5. Immutable Artifacts & Cascading Invalidation

- **Immutable Disk Files**: Every activity's output is written atomically to `artifacts/<type>.v<n>.json`. Historical versions are **never overwritten or deleted**.
- **State Index**: The goal's `state.json` tracks pointers: `state["artifacts"][<type>] = {ref, version, validity, activity_status, sha256}`.
- **Dependency Checking**: The Controller enforces that all prerequisite artifacts are `VALID` before writing a new artifact.
- **Cascading Invalidation**:
  - When an assumption is disproven, specify `invalidate_roots: ["<root_type>"]`.
  - The Controller computes transitive downstream dependents using `compute_cascading_invalidation()`.
  - Stale artifacts are marked `validity = STALE` in the state index.
  - The newly produced audit artifact is automatically protected from immediate self-invalidation.

---

## 6. Proof-Path Integrity & Mutation Preflight

- **Proof-Path Locking**:
  - The proof entrypoint specified in `proof_contract` is locked (`proof_path_locked = true`) as soon as state-changing baseline commands begin.
  - Its status is tracked as `PathIntegrityStatus.ORIGINAL`.
  - Models cannot swap the test for an easier or synthetic path. Any diagnostic-only probe must be explicitly flagged with `is_diagnostic_only = true`.
- **Mutation Preflight**:
  - Any command proposed for baseline or verification is evaluated:
    - Must specify bounded mutation targets (never production DB or shared state).
    - Must include limit semantics and stable identifiers.
    - Must prohibit unbounded fan-out.
    - Must define automated cleanup.

---

## 7. Role Relationships & Precedence

```text
User Explicit Instruction
          |
          v
Controller Invariants (Single writer, atomic writes, flock, repair budget)
          |
          v
Labeeb Orchestrator Preferences (PREFERENCES.md)
          |
          v
Live Runtime & Provider Facts (orchestrator doctor, limits)
          |
          v
Agent Judgment
```

- **Brain (Codex)**: Synthesizes goal contracts, audits code, evaluates alternatives, and decides macro actions.
- **Critic (Claude Code)**: Adversarial read-only review of candidate plans and patches. Never decides `PASS`.
- **Implementer (Jules)**: Executes code modifications in git worktrees. Completion is evidence, never verified proof.
- **No Direct Loops**: Never establish direct Claude↔Jules repair loops. The Brain always synthesizes criticism and issues targeted instructions.
