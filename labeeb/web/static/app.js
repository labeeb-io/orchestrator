/**
 * Labeeb Orchestrator UI Client
 * Handles Tabs, SSE live updates, clipboard copying, and dynamic form builders.
 */

// Tab Switching with Event Delegation & State Preservation across HTMX swaps
function getGoalId() {
  const goalEl = document.getElementById('goal-meta');
  return goalEl ? goalEl.getAttribute('data-goal-id') : 'default';
}

function restoreActiveTab() {
  const goalId = getGoalId();
  const savedTab = sessionStorage.getItem('activeGoalTab_' + goalId);
  if (!savedTab) return;

  const btn = document.querySelector(`.tab-btn[data-tab="${savedTab}"]`);
  const pane = document.querySelector(`#${savedTab}`);
  if (btn && pane && !btn.disabled && !btn.hasAttribute('disabled')) {
    const parent = btn.closest('.tabs-container') || document;
    parent.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.remove('active');
      if (b.hasAttribute('active')) b.removeAttribute('active');
    });
    parent.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    btn.setAttribute('active', '');
    pane.classList.add('active');

    const mdTabs = btn.closest('md-tabs');
    if (mdTabs) {
      const tabs = Array.from(mdTabs.querySelectorAll('md-primary-tab'));
      const idx = tabs.indexOf(btn);
      if (idx !== -1) mdTabs.activeTabIndex = idx;
    }
  }
}

// Global click delegation for tab switching
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  if (btn.disabled || btn.hasAttribute('disabled')) return;
  const parent = btn.closest('.tabs-container') || document;
  const targetId = btn.getAttribute('data-tab');
  if (!targetId) return;

  parent.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('active');
    if (b.hasAttribute('active')) b.removeAttribute('active');
  });
  parent.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

  btn.classList.add('active');
  btn.setAttribute('active', '');
  const pane = parent.querySelector(`#${targetId}`);
  if (pane) pane.classList.add('active');

  const mdTabs = btn.closest('md-tabs');
  if (mdTabs) {
    const tabs = Array.from(mdTabs.querySelectorAll('md-primary-tab'));
    const idx = tabs.indexOf(btn);
    if (idx !== -1) mdTabs.activeTabIndex = idx;
  }

  // Persist chosen tab across live HTMX fragment reloads
  const goalId = getGoalId();
  sessionStorage.setItem('activeGoalTab_' + goalId, targetId);
});

// Support md-tabs change event for keyboard arrow navigation & indicator sync
document.addEventListener('change', (e) => {
  const mdTabs = e.target.closest('md-tabs');
  if (!mdTabs) return;
  const tabs = Array.from(mdTabs.querySelectorAll('md-primary-tab'));
  const activeTab = tabs[mdTabs.activeTabIndex];
  if (!activeTab) return;
  const targetId = activeTab.getAttribute('data-tab');
  if (!targetId) return;

  const parent = mdTabs.closest('.tabs-container') || document;
  parent.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('active');
    if (b.hasAttribute('active')) b.removeAttribute('active');
  });
  parent.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

  activeTab.classList.add('active');
  activeTab.setAttribute('active', '');
  const pane = parent.querySelector(`#${targetId}`);
  if (pane) pane.classList.add('active');

  const goalId = getGoalId();
  sessionStorage.setItem('activeGoalTab_' + goalId, targetId);
});

// Re-apply active tab whenever HTMX updates or settles
document.addEventListener('htmx:afterSettle', () => {
  restoreActiveTab();
});

document.addEventListener('htmx:afterSwap', () => {
  restoreActiveTab();
});

// Clipboard Helper
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const original = btn.innerText;
    btn.innerText = 'Copied!';
    setTimeout(() => {
      btn.innerText = original;
    }, 1500);
  });
}

// Dynamic input lists for New Goal page
function addListItem(containerId, placeholder, inputName) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const row = document.createElement('div');
  row.className = 'list-item-row';
  row.style.display = 'flex';
  row.style.gap = '8px';
  row.style.marginBottom = '8px';
  row.innerHTML = `
    <input type="text" name="${inputName}" class="form-input" placeholder="${placeholder}" style="flex: 1; font-family: var(--font-mono); font-size: 12.5px;" />
    <button type="button" class="btn btn-secondary btn-sm" onclick="this.parentElement.remove()">×</button>
  `;
  container.appendChild(row);
  row.querySelector('input').focus();
}

// Server-Sent Events (SSE) Live Connection
function connectGoalSSE(goalId) {
  if (!goalId) return;
  const evtSource = new EventSource(`/api/goals/${goalId}/events`);

  evtSource.onmessage = (e) => {
    // heartbeats or default messages
  };

  const reloadFragments = () => {
    if (window.htmx) {
      htmx.trigger('body', 'goalUpdated');
    } else {
      // Light fallback refresh if HTMX is absent
      const badge = document.getElementById('stage-badge-container');
      if (badge) {
        fetch(`/api/goals/${goalId}`)
          .then(r => r.json())
          .then(data => {
            if (data.phase) {
              const statusEl = document.getElementById('goal-phase-text');
              if (statusEl) statusEl.innerText = data.phase;
            }
          });
      }
    }
  };

  evtSource.addEventListener('goal.synced', (e) => {
    console.log('[SSE] Goal synced:', e.data);
  });

  evtSource.addEventListener('goal.blocked', (e) => {
    reloadFragments();
    console.warn('[SSE] Goal blocked:', e.data);
  });

  evtSource.addEventListener('goal.passed', (e) => {
    reloadFragments();
    console.log('[SSE] Goal passed:', e.data);
  });

  evtSource.addEventListener('goal.failed', (e) => {
    reloadFragments();
    console.error('[SSE] Goal failed:', e.data);
  });

  evtSource.addEventListener('plan.ready', (e) => {
    reloadFragments();
  });

  evtSource.addEventListener('jules.dispatched', (e) => {
    reloadFragments();
  });

  evtSource.addEventListener('repair.dispatched', (e) => {
    reloadFragments();
  });

  evtSource.onerror = (err) => {
    console.log('[SSE] Connection reconnecting...');
  };
}

// Log filtering on Goal Summary tab
function filterLogRows(query) {
  const q = (query || '').toLowerCase().trim();
  const rows = document.querySelectorAll('.log-row');
  rows.forEach(row => {
    const text = row.getAttribute('data-log-text') || '';
    if (!q || text.includes(q)) {
      row.style.display = 'flex';
    } else {
      row.style.display = 'none';
    }
  });
}

// Toggle between structured and raw logs
function toggleRawLogs(btn) {
  const structured = document.getElementById('structured-logs-container');
  const raw = document.getElementById('raw-logs-container');
  if (!structured || !raw) return;

  if (raw.style.display === 'none' || !raw.style.display) {
    raw.style.display = 'block';
    structured.style.display = 'none';
    btn.innerText = 'Show Structured';
  } else {
    raw.style.display = 'none';
    structured.style.display = 'block';
    btn.innerText = 'Show Raw';
  }
}

// Known Project Presets
const WORKSPACE_PRESETS = {
  'labeeb2025': {
    workspace: '/home/hany/webserver/server/www/labeeb2025',
    repo: 'labeeb-io/labeeb',
    branch: 'master'
  },
  'labeeb-orchestrator': {
    workspace: '/home/hany/webserver/server/www/labeeb-orchestrator',
    repo: 'labeeb-io/labeeb-orchestrator',
    branch: 'master'
  }
};

// Select workspace preset on New Goal page
function selectWorkspacePreset(presetId) {
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-preset') === presetId);
  });

  const workspaceInput = document.getElementById('workspace');
  const repoInput = document.getElementById('repo');
  const branchSelect = document.getElementById('branch');
  const hintEl = document.getElementById('workspace-status-hint');

  if (presetId === 'custom') {
    if (workspaceInput) {
      workspaceInput.value = '';
      workspaceInput.placeholder = '/path/to/local/git/repo';
      workspaceInput.focus();
    }
    if (hintEl) hintEl.innerText = 'Enter absolute path to local git repository.';
    return;
  }

  const preset = WORKSPACE_PRESETS[presetId];
  if (!preset) return;

  if (workspaceInput) workspaceInput.value = preset.workspace;
  if (repoInput) repoInput.value = preset.repo;
  if (branchSelect && preset.branch) branchSelect.value = preset.branch;

  // Inspect the path to confirm real branches from disk
  onWorkspacePathChange(preset.workspace);
}

// Dynamically inspect local workspace path for git repo and branches
async function onWorkspacePathChange(path) {
  const trimmed = (path || '').trim();
  if (!trimmed) return;

  const hintEl = document.getElementById('workspace-status-hint');
  const repoInput = document.getElementById('repo');
  const branchSelect = document.getElementById('branch');
  const branchBadge = document.getElementById('branch-count-badge');

  if (hintEl) {
    hintEl.innerHTML = '<span style="color: var(--text-muted);">🔍 Inspecting repository...</span>';
  }

  try {
    const res = await fetch(`/api/workspaces/inspect?path=${encodeURIComponent(trimmed)}`);
    const data = await res.json();

    if (data.valid) {
      if (repoInput && data.repo) {
        repoInput.value = data.repo;
      }
      if (branchSelect && Array.isArray(data.branches) && data.branches.length > 0) {
        const curVal = branchSelect.value || data.default_branch || 'master';
        branchSelect.innerHTML = data.branches.map(b => 
          `<option value="${b}" ${b === curVal ? 'selected' : ''}>${b}</option>`
        ).join('');
      }
      if (branchBadge && data.branches) {
        branchBadge.innerText = `${data.branches.length} branches`;
      }
      if (hintEl) {
        const branchCount = data.branches ? data.branches.length : 0;
        hintEl.innerHTML = `<span style="color: var(--color-pass-text);">✓ Git repository ready (${branchCount} branches detected)</span>`;
      }
    } else {
      if (hintEl) {
        hintEl.innerHTML = `<span style="color: var(--color-warn-text);">⚠️ ${data.error || 'Not a valid git repository.'}</span>`;
      }
    }
  } catch (err) {
    if (hintEl) {
      hintEl.innerHTML = `<span style="color: var(--text-muted);">Inspection offline. Default settings active.</span>`;
    }
  }
}

// Reactive Risk Impact Panel calculation
const RISK_TAG_NAMES = {
  'architecture': 'Architecture',
  'security': 'Security & Auth',
  'persistence': 'Database & Store',
  'concurrency': 'Concurrency',
  'public_contract': 'Public API',
  'large_blast_radius': 'High Blast Radius'
};

function updateRiskImpact(checkbox) {
  if (checkbox) {
    const card = checkbox.closest('.risk-card');
    if (card) {
      card.classList.toggle('selected', checkbox.checked);
    }
  }

  const checkedBoxes = Array.from(document.querySelectorAll('input[name="risk_tags"]:checked'));
  const panel = document.getElementById('risk-impact-panel');
  const badgeEl = document.getElementById('risk-impact-badge');
  const tagsEl = document.getElementById('risk-impact-tags');
  const detailEl = document.getElementById('risk-impact-detail');

  if (!panel || !badgeEl || !detailEl) return;

  if (checkedBoxes.length > 0) {
    panel.className = 'impact-panel high-risk';
    const tagNames = checkedBoxes.map(cb => RISK_TAG_NAMES[cb.value] || cb.value);
    badgeEl.innerText = `🛡️ High-Assurance Safety Pipeline Active (${checkedBoxes.length} high-risk area${checkedBoxes.length > 1 ? 's' : ''})`;
    detailEl.innerHTML = `Selected: <strong>${tagNames.join(', ')}</strong>. This goal will trigger independent <strong>Claude Critic (Opus)</strong> read-only reviews before plan approval and post-validation. If any critical finding is raised, the goal will pause at a human gate.`;
  } else {
    panel.className = 'impact-panel fast-path';
    badgeEl.innerText = '⚡ Streamlined Fast-Path Pipeline Active';
    detailEl.innerHTML = 'No high-risk tags selected. The goal will execute directly via <strong>Codex Brain → Jules Worker → Isolated Worktree Validation</strong> without Claude Critic overhead, optimizing execution speed and token usage.';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  restoreActiveTab();
  const goalEl = document.getElementById('goal-meta');
  if (goalEl) {
    const goalId = goalEl.getAttribute('data-goal-id');
    if (goalId) {
      connectGoalSSE(goalId);
    }
  }

  // Initialize dynamic git branches for default workspace on New Goal page
  const workspaceInput = document.getElementById('workspace');
  if (workspaceInput && workspaceInput.value) {
    onWorkspacePathChange(workspaceInput.value);
  }
});

// Instant client-side search filter for sidebar sessions
function filterSidebarSessions(query) {
  const q = (query || '').toLowerCase().trim();
  const items = document.querySelectorAll('.sidebar-session-item');
  let visibleCount = 0;

  items.forEach(item => {
    const text = (item.getAttribute('data-session-text') || '').toLowerCase();
    if (!q || text.includes(q)) {
      item.style.display = 'flex';
      visibleCount++;
    } else {
      item.style.display = 'none';
    }
  });

  const emptyHint = document.getElementById('sidebar-empty-sessions');
  if (emptyHint) {
    emptyHint.style.display = (visibleCount === 0 && q) ? 'block' : 'none';
  }
}

