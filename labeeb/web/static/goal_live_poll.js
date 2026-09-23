/**
 * goal_live_poll.js — Lightweight live-update poller for goal detail page.
 *
 * Polls /api/goals/{id}/poll every 5s while the goal is active.
 * On state change, dispatches "goalUpdated" so HTMX swaps the container.
 * Stops automatically when the goal reaches a terminal phase.
 */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const TERMINAL_PHASES = new Set(["PASS", "FAIL", "BLOCKED"]);

  const metaEl = document.getElementById("goal-meta");
  if (!metaEl) return;

  const goalId = metaEl.dataset.goalId;
  if (!goalId) return;

  let lastSnapshot = null;
  let timerId = null;

  function snapshotKey(data) {
    return [data.phase, data.current_activity, data.updated_at, data.reasoning_steps].join("|");
  }

  async function poll() {
    try {
      const resp = await fetch(`/api/goals/${goalId}/poll`);
      if (!resp.ok) return;
      const data = await resp.json();
      const key = snapshotKey(data);

      if (lastSnapshot !== null && key !== lastSnapshot) {
        // State changed — tell HTMX to re-fetch the container
        document.body.dispatchEvent(new CustomEvent("goalUpdated"));
      }
      lastSnapshot = key;

      // Stop polling when goal is terminal
      if (TERMINAL_PHASES.has(data.phase)) {
        // One final update after terminal state
        document.body.dispatchEvent(new CustomEvent("goalUpdated"));
        clearInterval(timerId);
        timerId = null;
      }
    } catch (_) {
      // Network hiccup — silently skip this cycle
    }
  }

  // Initial snapshot from current page state (avoid first-cycle false trigger)
  const phaseEl = document.getElementById("goal-phase-text");
  if (phaseEl) {
    const currentPhase = phaseEl.textContent.trim();
    if (TERMINAL_PHASES.has(currentPhase)) return; // Already terminal, no polling needed
  }

  // Start polling
  timerId = setInterval(poll, POLL_INTERVAL_MS);

  // Clean up on navigation
  window.addEventListener("beforeunload", function () {
    if (timerId) clearInterval(timerId);
  });
})();
