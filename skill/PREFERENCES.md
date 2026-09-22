# Labeeb Orchestrator Preferences

The preferences in this document establish operational routing policies, role assignments, resource budgets, and safety boundaries for the Labeeb Orchestrator V2 control plane.

## 1. User Preferences & Role Specialization

### Primary Reasoning & Orchestration Brain
- **Provider / Runtime**: Codex (via `orchestrator launch codex` / `codex-app-server`).
- **Responsibility**: Owns intent comprehension, repository auditing, Goal Contract compilation, trade-off evaluation, proof synthesis, and final `PASS | FAIL | BLOCKED` determinations.
- **Guidance**: Must work read-only during reasoning turns. Must adhere to the 16-activity reasoning graph and emit structured JSON decision envelopes.

### Repository Implementation Worker
- **Provider / Runtime**: Jules (via `cjules` or `orchestrator launch jules`).
- **Responsibility**: Performs bounded repository-backed implementation, refactoring, and patch generation in isolated worktrees.
- **Guidance**: Jules completion is evidence, never verified proof. Jules cannot approve its own work or declare a goal completed.

### Independent Adversarial Critic
- **Provider / Runtime**: Claude Code (using Opus via direct command or Bifrost gateway).
- **Responsibility**: Provides independent, read-only criticism of candidate plans and post-implementation diffs.
- **Focus Areas**: Missed reuse, hidden coupling, concurrency/state race conditions, security vulnerabilities, public contract breaks, and missing test coverage.
- **Guidance**: A fresh critic session is used for each independent review. Claude never owns the final decision and never speaks directly to Jules.

---

## 2. Resource Budgets & Repair Governance

### Reasoning Loop Budget
- **Turn Cap**: Maximum 25 reasoning steps within the `THINK / CONVERGE` macro phase.
- **Activity Repeat Cap**: Maximum 3 visits to any single reasoning activity.
- **Progress Delta**: Each repeat must yield new verified evidence or changed artifact signatures (`sha256`). Zero-progress loops are blocked.

### Implementation & Repair Budgets
- **Execution Rounds**: Maximum 2 implementation attempts with Jules.
- **Targeted Repairs**: Exactly 1 targeted repair round for implementation/patch defects. If validation or proof fails a second time, the goal terminates in `FAIL`.
- **Assumption Invalidation vs Implementation Defect**:
  - If a test fails because of a code/syntax bug in Jules's patch, emit `TARGETED_REPAIR`.
  - If a test fails because a fundamental planning assumption was disproven by repository evidence, emit `RETURN_TO_THINKING` with `invalidate_roots`.
  - **Crucial**: `RETURN_TO_THINKING` resets reasoning artifacts but **does not consume the single targeted repair budget**.

---

## 3. Waiting & Event Synchronization

- **Deterministic Waiting**: Always use deterministic process waiting (`orchestrator read <id> --wait`) and Controller event polling instead of active LLM polling loops.
- **Controller Wakeups**: The Controller resumes the Brain only upon meaningful asynchronous events (Jules session completion, critic findings ready, baseline finished, validation failed).

---

## 4. Authority Boundaries & Human Checkpoints

- **Autonomous Pre-Authorization**:
  - When `preauthorize_plan = true` and the change is classified as `LOCAL_ENGINEERING` within configured `--allowed-path` scopes, proceed autonomously to `EXECUTE` without stopping at `PLAN_GATE`.
- **Mandatory Escalation to `PLAN_GATE`**:
  - Immediately pause and request human approval (`NEEDS_HUMAN`) when:
    1. A planned edit falls outside the user's allowed path whitelist.
    2. Change touches core database migrations, authentication, public API contracts, or production secrets (`PRODUCTION_MUTATION`).
    3. An unresolved product decision or conflicting requirement arises.
- **Strict Remote Mutation Boundary**:
  - Under no circumstances may an agent execute `git push`, create a GitHub Pull Request, merge branches, or alter remote production state without explicit, out-of-band user authorization.

---

## 5. Fallback & Degradation Policies

- **Provider Unavailability**:
  - When a configured provider (Codex, Claude, or Jules) is unavailable, follow current user instructions first, then the fallback configured in `config.toml`.
  - Never silently widen authority or switch to unvetted models when high-risk tags are present.
- **Critic Failure**:
  - If the Claude Opus critic fails or times out on a high-risk goal, the Controller enforces `critic_failure_blocks_high_risk = true` and halts at `BLOCKED`.
