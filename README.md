# Labeeb Orchestrator V1

Small local controller for bounded engineering orchestration across:

- **Labeeb/Codex** — decision brain
- **Claude** — optional independent read-only critic
- **Jules** — implementation worker
- **Python controller** — state, side effects, wait/wake, reconciliation, repair budget
- **deterministic validation** — isolated git worktree + configured commands
- **Developer Web UI** — high-performance FastAPI, Jinja2, HTMX, and Server-Sent Events

The controller runs with Python 3.12+ (dedicated `.venv` provided) and integrates with the CLIs already present in the environment: `orchestrator`, `cjules`, `git`, and optionally `claude`.

## V1 safety invariants

These are code-owned and intentionally not configurable:

- one writer at a time per goal (`flock`)
- atomic state writes with `fsync` + `os.replace`
- no blind retry after an ambiguous write
- exactly one automatic repair round
- no push / PR / merge / production mutation
- `PASS` requires deterministic validation when enabled
- stale `COMPLETED` is not accepted as a repair result
- ambiguous recovery becomes `BLOCKED`

## Files

```text
labeeb_controller.py   backward-compatible CLI & entry point
labeeb/                modular controller application package
├── api/               FastAPI REST routes & Server-Sent Events
├── web/               HTML views, dark developer theme, HTMX templates
├── core/              effects engine, validation, events, state machine
├── providers/         Orchestrator, Jules, Claude, Git, and Fake adapters
└── storage/           single-writer flock locking & atomic GoalStore
config.toml            customizable roles + workflow policy
requirements.txt       fastapi, uvicorn, httpx, jinja2, python-multipart
install.sh             optional local install helper
examples/              start-command examples
tests/                 20 unit & integration tests (CLI, Core, API, Web)
```

## Quick Start: Developer Web UI

Start the local control plane server:

```bash
./labeeb_controller.py web --port 8765
```

Then open in your browser:
**`http://127.0.0.1:8765`**

Features:
- **Dashboard (`/dashboard`)**: Live summary metrics, active goal cards, auto-sync.
- **Goal Creator (`/goals/new`)**: Goal creation wizard with path and validation command builders.
- **Goal Workspace (`/goals/{id}`)**: Visual 8-step pipeline tracker, human decision gate (`Approve` / `Stop`), patch unified diff viewer, critic review, and raw state JSON.
- **Diagnostics (`/doctor`)**: Environment preflight checks with one-click re-test.
- **Config Editor (`/config`)**: Role policies breakdown and safe TOML editor.

## 1. Preflight

Edit `config.toml` first. If `orchestrator` is only visible through NVM, set an absolute path:

```toml
[executables]
orchestrator = "/home/hany/.nvm/versions/node/v24.21.0/bin/orchestrator"
```

Then run:

```bash
./labeeb_controller.py --config ./config.toml doctor
```

## 2. Start one goal

```bash
./labeeb_controller.py --config ./config.toml start \
  --intent-file ./examples/intent.md \
  --workspace /path/to/repo \
  --repo OWNER/REPO \
  --branch feature-branch \
  --risk architecture \
  --allow-path api/app \
  --allow-path api/tests \
  --validate 'docker compose exec -T api php artisan test tests/Feature/TargetTest.php' \
  --preauthorize-plan \
  --background
```

`--preauthorize-plan` means the initial intent authorizes one bounded implementation plan that stays inside the supplied authority and safety rules. Omit it if you want the controller to stop at `PLAN_GATE` for explicit approval.

## 3. Approve a plan manually

```bash
./labeeb_controller.py --config ./config.toml approve <goal-id>
```

## 4. Inspect status

```bash
./labeeb_controller.py --config ./config.toml status <goal-id>
```

Goal state is stored under:

```text
~/.local/state/labeeb-controller/goals/<goal-id>/
```

## 5. Recovery

Run the same goal again after a controller/WSL interruption:

```bash
./labeeb_controller.py --config ./config.toml run <goal-id>
```

The controller first reads persisted `pending_action`, Orchestrator task records, and Jules structured data before deciding whether it is safe to continue.

## Customizing roles

Roles live in `config.toml`.

### Brain

```toml
[roles.brain]
transport = "orchestrator"
runtime = "codex"
model = ""
```

You can switch the runtime to `claude-code` if that provider is intended to become the decision brain for a run.

### Critic

Default V1 uses a direct Claude invocation to enforce read-only operation technically:

```toml
[roles.critic]
transport = "direct"
read_only = true
command = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--output-format", "json", "--model", "opus"]
```

It inherits the existing Claude/Bifrost environment.

### Implementer

```toml
[roles.implementer]
transport = "jules"
command = "cjules"
```

V1 intentionally requires a Jules transport for the implementation worker. The role name and executable are configurable; the safety protocol is not.

## Customizing behavior

### Critic policy

```toml
[workflow]
pre_critic = "high_risk"  # never | high_risk | always
post_critic = "high_risk"
high_risk_tags = ["architecture", "concurrency", "persistence", "security", "public_contract", "large_blast_radius"]
```

### Jules state actions

```toml
[jules_state_actions]
QUEUED = "wait"
PLANNING = "wait"
IN_PROGRESS = "wait"
AWAITING_PLAN_APPROVAL = "wake"
AWAITING_USER_FEEDBACK = "wake"
PAUSED = "wake"
COMPLETED = "review"
FAILED = "wake"
CANCELLED = "wake"
UNKNOWN = "block"
```

Allowed actions are `wait`, `wake`, `review`, `block`. V1 restricts `COMPLETED` to `review` or `block` so a configuration change cannot bypass evidence collection.

### Repair fallback

```toml
[workflow]
repair_fallback = "blocked" # blocked | new_session
```

Default is conservative. If a correction sent to a completed Jules session does not create provable new work within `repair_activation_seconds`, the goal becomes `BLOCKED`.

`new_session` is available as an explicit fallback. It creates a replacement Jules session from the original execution contract plus the targeted correction; continuity with the old Jules workspace is not claimed.

### Brain thread rollover

```toml
[workflow]
max_brain_resumes_per_thread = 0
```

`0` keeps the same provider thread. A positive value starts a fresh brain task after that many resumes. The event prompt is reconstructed from persisted contract, plan, and evidence, so continuity does not depend on an endless transcript.

## Repair protocol

V1 repair is exactly one bounded correction:

```text
review result #1
→ reserve repair budget
→ snapshot current Jules activity keys + patch hash
→ send one message with unique repair marker
→ prove matching user message exists
→ wait for new agent activity / new patch
→ reject stale COMPLETED
→ validate result #2
→ PASS / FAIL / BLOCKED
```

The controller never interprets opaque activity IDs as sortable sequence numbers.

## Validation

The controller never applies a Jules patch to the user's working tree.

For a result it:

1. extracts a structured `gitPatch` artifact from `cjules logs -f json`
2. records patch hash + changed paths + base commit
3. checks changed paths against the allowed path prefixes
4. creates a detached temporary `git worktree` at the patch base commit
5. applies the patch there
6. runs the configured validation commands
7. stores deterministic evidence with the review

## Process durability

`start --background` uses Python `start_new_session=True` and writes output to the goal directory. It is intended to survive terminal closure while the WSL instance remains alive.

V1 does **not** promise progress through:

- Windows sleep
- `wsl --shutdown`
- Windows reboot

After WSL/reboot, run `run <goal-id>` manually. Auto-start/systemd is intentionally outside V1.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The package includes 20 automated unit and integration tests across 4 modules:
- `tests/test_controller.py`: 10 legacy controller tests (backward-compatible facades, atomic refs, event reservation, stale completion rejection, 1-repair budget, and isolated git-worktree validation).
- `tests/test_backend_core.py`: Modular storage locking (`flock`), atomic `fsync` persistence, fake provider lifecycles, and code-enforced safety invariants.
- `tests/test_api.py`: FastAPI REST routes (`/api`, `/api/goals`, `/api/config`, `/api/system/doctor`).
- `tests/test_web.py`: Developer Web UI views (dashboard, doctor, config editor, goal creation form submission, and detail workspace).

## Live acceptance still required

The code is built and locally tested, but two capabilities depend on your real installed environment and must be proven there before calling the overall system production-ready:

1. `cjules msg <COMPLETED_SESSION>` actually causes a completed Jules session to perform new work.
2. The default read-only Claude command works with your exact Claude/Bifrost setup and still reaches the intended model.

The controller handles failure conservatively if either assumption is false.
