# Changelog

## 1.2.0 (Developer Web UI & HTMX Frontend)

- Fast server-rendered Web UI powered by FastAPI and Jinja2 templates.
- Developer-tailored dark theme (`app.css`) with high-contrast status colors, JetBrains Mono font stack, and responsive layout.
- Visual 8-step pipeline progress tracker (Contract -> Audit -> Plan -> Critique -> Gate -> Jules -> Validation -> Result).
- Interactive Dashboard (`/dashboard`) with active/recent goal cards and live HTMX sync.
- Goal Creation Wizard (`/goals/new`) with dynamic path & validation command list builders and risk classification.
- Comprehensive Goal Detail view (`/goals/{id}`) with real-time SSE streaming, Human Action Gate banner (`PLAN_GATE` / `BLOCKED`), patch diff viewer, and tabbed timeline.
- Configuration Manager (`/config`) with role breakdown and validated Raw TOML editor.
- System Diagnostics page (`/doctor`) with preflight checks and one-click re-test.
- Comprehensive test suite in `tests/test_web.py`.

## 1.1.0 (Backend Modularization & Headless API)

- Modularized `labeeb/` package with clear separation of storage, providers, core, cli, and api.
- Storage layer with `GoalLock` (`flock`) and atomic `GoalStore` (`fsync` + `os.replace`).
- Clean provider abstractions for Orchestrator, Jules, Claude Critic, and Git.
- Added `FakeOrchestratorProvider`, `FakeJulesProvider`, and `FakeCriticProvider` for UI development and unit testing without live LLMs.
- Headless FastAPI REST API (`/api/goals`, `/api/config`, `/api/system/doctor`).
- Server-Sent Events (SSE) streaming endpoint (`/api/goals/{id}/events`) with domain event broker.
- Added `labeeb web` CLI command to launch the local API server.
- Preserved 100% backward compatibility via `labeeb_controller.py` facade.

## 1.0.0

- Python stdlib controller with TOML configuration.
- Configurable brain/critic/implementer roles.
- Configurable Jules state actions and critic routing.
- Persistent per-goal JSON state with flock + atomic fsync writes.
- PREPARED/IN_FLIGHT effect protocol and conservative crash reconciliation.
- Orchestrator launch/resume integration.
- Structured cjules get/logs/new/msg/approve integration.
- One-repair protocol with activity snapshots and stale-COMPLETED rejection.
- Isolated git-worktree validation tied to patch hash/base commit.
- Optional fresh read-only Claude critic.
- Optional brain-thread rollover.
- Detached background runner and manual restart recovery.

- Event reservation is persisted atomically with the transition/effect to avoid lost wakeups after crashes.
- Ambiguous Jules message/approval reconciliation uses bounded read retries before BLOCKED.
