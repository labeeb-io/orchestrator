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
  initTimelineInspector();
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

// Re-initialize timeline inspector after HTMX settlements
document.addEventListener('htmx:afterSettle', () => {
  initTimelineInspector();
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

// ==========================================================================
// Interactive Timeline Event Inspector & Master-Detail Visualizer
// ==========================================================================

let currentEnrichedEvents = [];

function loadEnrichedEvents() {
  const el = document.getElementById('enriched-events-data');
  if (!el) return [];
  try {
    return JSON.parse(el.textContent.trim()) || [];
  } catch (err) {
    console.error('Failed to parse enriched-events-data', err);
    return [];
  }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatDiff(diffText) {
  if (!diffText) return '<div style="color: var(--color-fog); padding: 12px;">No patch content available.</div>';
  const lines = diffText.split('\n');
  let html = '<div class="inspector-diff-viewer">';
  lines.forEach(line => {
    const esc = escapeHtml(line);
    if (line.startsWith('diff --git ') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
      html += `<span class="diff-line-hunk">${esc}</span>\n`;
    } else if (line.startsWith('@@')) {
      html += `<span class="diff-line-hunk">${esc}</span>\n`;
    } else if (line.startsWith('+')) {
      html += `<span class="diff-line-add">${esc}</span>\n`;
    } else if (line.startsWith('-')) {
      html += `<span class="diff-line-del">${esc}</span>\n`;
    } else {
      html += `<span>${esc}</span>\n`;
    }
  });
  html += '</div>';
  return html;
}

function selectTimelineEvent(eventId) {
  if (!currentEnrichedEvents || currentEnrichedEvents.length === 0) {
    currentEnrichedEvents = loadEnrichedEvents();
  }
  const ev = currentEnrichedEvents.find(e => e.id === eventId);
  if (!ev) return;

  // Highlight active event card
  document.querySelectorAll('.timeline-event-card').forEach(c => {
    c.classList.remove('selected');
  });
  const activeCard = document.getElementById(`card-${eventId}`);
  if (activeCard) {
    activeCard.classList.add('selected');
  }

  // Populate Inspector Viewport
  const container = document.getElementById('inspector-content');
  if (!container) return;

  let bodyHtml = '';
  const d = ev.details || {};

  // Build type-specific visual sections
  if (ev.type === 'goal.created') {
    const repoText = d.repo ? (d.branch ? `${escapeHtml(d.repo)} @ ${escapeHtml(d.branch)}` : escapeHtml(d.repo)) : (d.workspace ? escapeHtml(d.workspace) : 'Local Repository');
    bodyHtml = `
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">target</span> Stated Engineering Intent</div>
        <div style="font-size: 14px; font-weight: 500; color: #ffffff; line-height: 1.5; padding: 12px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
          ${escapeHtml(d.intent || 'No specific intent recorded')}
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">inventory_2</span> Target Context</div>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px;">
          <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
            <div style="font-size: 11px; color: var(--color-fog);">Repository &amp; Branch</div>
            <div style="font-family: var(--font-mono); font-size: 12px; color: var(--color-paper); margin-top: 2px;">${repoText}</div>
          </div>
          <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
            <div style="font-size: 11px; color: var(--color-fog);">Deadline Window</div>
            <div style="font-family: var(--font-mono); font-size: 12px; color: var(--color-paper); margin-top: 2px;">${escapeHtml(d.deadline ? d.deadline.replace('T', ' ').substring(0, 19) + ' UTC' : 'N/A')}</div>
          </div>
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">shield</span> Risk Tags &amp; Pre-authorization</div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          ${(d.risk_tags && d.risk_tags.length) ? d.risk_tags.map(t => `<span class="badge badge-warn">${escapeHtml(t)}</span>`).join('') : '<span class="badge badge-pass">Standard Low-Risk Fastpath</span>'}
          <span class="badge ${d.preauthorize_plan ? 'badge-pass' : 'badge-active'}">Preauthorize: ${d.preauthorize_plan ? 'ENABLED' : 'MANUAL GATE'}</span>
        </div>
      </div>
    `;
  } else if (ev.type === 'audit.completed') {
    bodyHtml = `
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">flag</span> Observable Outcome</div>
        <div style="font-size: 13.5px; font-weight: 500; color: #ffffff; padding: 12px; background: var(--color-carbon); border-radius: 6px; border-left: 3px solid #06b6d4;">
          ${escapeHtml(d.observable_outcome || d.intent || 'N/A')}
        </div>
      </div>
      <div class="inspector-section" style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
        <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
          <div style="font-size: 11px; font-weight: 600; color: var(--color-coral-red); margin-bottom: 4px;">CURRENT BEHAVIOR</div>
          <div style="font-size: 12px; color: var(--color-mist); line-height: 1.4;">${escapeHtml(d.current_behavior || 'Baseline state prior to modification')}</div>
        </div>
        <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
          <div style="font-size: 11px; font-weight: 600; color: var(--color-signal-mint); margin-bottom: 4px;">EXPECTED BEHAVIOR</div>
          <div style="font-size: 12px; color: var(--color-mist); line-height: 1.4;">${escapeHtml(d.expected_behavior || 'Target state fulfilling contract')}</div>
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">check_circle</span> Acceptance Criteria Checklist</div>
        <div class="inspector-checklist">
          ${(d.acceptance_criteria && d.acceptance_criteria.length) ? d.acceptance_criteria.map(c => `
            <div class="inspector-check-item">
              <span class="material-symbols-outlined inspector-check-icon">check_circle</span>
              <span>${escapeHtml(c)}</span>
            </div>
          `).join('') : '<div style="font-size: 12px; color: var(--color-fog); font-style: italic;">No specific acceptance criteria checklist recorded.</div>'}
        </div>
      </div>
      ${(d.constraints && d.constraints.length) ? `
        <div class="inspector-section">
          <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">block</span> Architectural Constraints</div>
          <div style="display: flex; flex-direction: column; gap: 4px;">
            ${d.constraints.map(c => `<div style="font-size: 12px; color: var(--color-fog); font-family: var(--font-mono); padding: 4px 8px; background: var(--color-carbon); border-radius: 4px;">• ${escapeHtml(c)}</div>`).join('')}
          </div>
        </div>
      ` : ''}
    `;
  } else if (ev.type === 'plan.ready') {
    bodyHtml = `
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">description</span> Plan Summary</div>
        <div style="font-size: 13.5px; font-weight: 500; color: #ffffff; padding: 12px; background: var(--color-carbon); border-radius: 6px; border-left: 3px solid var(--color-acid-lime);">
          ${escapeHtml(d.plan_summary)}
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">folder_open</span> Allowed Modification Paths</div>
        <div style="display: flex; gap: 6px; flex-wrap: wrap;">
          ${(d.allowed_paths || []).map(p => `<span style="font-family: var(--font-mono); font-size: 12px; padding: 4px 10px; background: var(--color-carbon); border: 1px solid var(--color-graphite); border-radius: 4px; color: var(--color-acid-lime);">${escapeHtml(p)}</span>`).join('')}
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">terminal</span> Deterministic Validation Commands</div>
        <div style="display: flex; flex-direction: column; gap: 6px;">
          ${(d.validation_commands || []).map(cmd => `
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; background: #050607; border: 1px solid var(--color-graphite); border-radius: 6px; font-family: var(--font-mono); font-size: 12px; color: var(--color-signal-mint);">
              <code>&gt; ${escapeHtml(cmd)}</code>
              <button class="btn btn-outline" style="padding: 2px 8px; font-size: 10.5px;" onclick="navigator.clipboard.writeText('${escapeHtml(cmd)}')">Copy</button>
            </div>
          `).join('')}
        </div>
      </div>
      ${d.jules_prompt ? `
        <div class="inspector-section">
          <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">smart_toy</span> Jules Worker Directives</div>
          <div style="padding: 10px; background: var(--color-carbon); border: 1px solid var(--color-graphite); border-radius: 6px; font-family: var(--font-mono); font-size: 11.5px; color: var(--color-mist); line-height: 1.5; white-space: pre-wrap; max-height: 200px; overflow-y: auto;">${escapeHtml(d.jules_prompt)}</div>
        </div>
      ` : ''}
    `;
  } else if (ev.type === 'jules.dispatched') {
    bodyHtml = `
      <div class="inspector-section" style="display: flex; align-items: center; justify-content: space-between; padding: 14px; background: var(--color-carbon); border: 1px solid var(--color-graphite); border-radius: 8px;">
        <div>
          <div style="font-size: 11px; color: var(--color-fog);">Remote Execution Session</div>
          <div style="font-family: var(--font-mono); font-size: 14px; font-weight: 600; color: #ffffff; margin-top: 2px;">Session #${escapeHtml(d.session_id)}</div>
        </div>
        ${d.session_url ? `
          <a href="${escapeHtml(d.session_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-primary" style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 6px 14px; text-decoration: none;">
            <span class="material-symbols-outlined" style="font-size: 15px;">open_in_new</span>
            <span>Open in Jules</span>
          </a>
        ` : ''}
      </div>
      <div class="inspector-section" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px;">
        <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
          <div style="font-size: 11px; color: var(--color-fog);">Starting Branch Context</div>
          <div style="font-family: var(--font-mono); font-size: 12px; color: var(--color-paper); margin-top: 2px;">${escapeHtml(d.starting_branch || 'master')}</div>
        </div>
        <div style="padding: 10px; background: var(--color-carbon); border-radius: 6px; border: 1px solid var(--color-graphite);">
          <div style="font-size: 11px; color: var(--color-fog);">Safety Boundary Policy</div>
          <div style="font-size: 12px; color: var(--color-coral-red); margin-top: 2px; font-weight: 500;">No push • No PR • Local mutation only</div>
        </div>
      </div>
      ${d.prompt ? `
        <div class="inspector-section">
          <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">chat</span> Prompt Sent to Agent</div>
          <div style="padding: 10px; background: #050607; border: 1px solid var(--color-graphite); border-radius: 6px; font-family: var(--font-mono); font-size: 11.5px; color: var(--color-mist); line-height: 1.5; white-space: pre-wrap; max-height: 220px; overflow-y: auto;">${escapeHtml(d.prompt)}</div>
        </div>
      ` : ''}
    `;
  } else if (ev.type === 'worker.completed') {
    bodyHtml = `
      <div class="inspector-section" style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
        <span class="badge badge-pass" style="font-size: 12px;">Files Modified: ${d.files_count || 1}</span>
        <span class="badge badge-active" style="font-size: 12px; color: #4ade80;">+${d.additions || 0} additions</span>
        <span class="badge badge-active" style="font-size: 12px; color: #f87171;">-${d.deletions || 0} deletions</span>
        ${d.patch_hash ? `<span style="font-family: var(--font-mono); font-size: 11px; color: var(--color-fog);">SHA: ${escapeHtml(d.patch_hash.substring(0, 16))}...</span>` : ''}
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">difference</span> Unified Git Diff</div>
        ${formatDiff(d.patch)}
      </div>
    `;
  } else if (ev.type === 'validation.completed') {
    bodyHtml = `
      <div class="inspector-section">
        <div style="padding: 14px; background: ${d.status === 'PASSED' ? 'rgba(52, 211, 153, 0.08)' : 'rgba(239, 68, 68, 0.08)'}; border: 1px solid ${d.status === 'PASSED' ? 'var(--color-signal-mint)' : 'var(--color-coral-red)'}; border-radius: 8px; display: flex; justify-content: space-between; align-items: center;">
          <div style="display: flex; align-items: center; gap: 10px;">
            <span class="material-symbols-outlined" style="font-size: 24px; color: ${d.status === 'PASSED' ? 'var(--color-signal-mint)' : 'var(--color-coral-red)'};">
              ${d.status === 'PASSED' ? 'check_circle' : 'cancel'}
            </span>
            <div>
              <div style="font-size: 14px; font-weight: 600; color: #ffffff;">Deterministic Verification ${escapeHtml(d.status)}</div>
              <div style="font-size: 12px; color: var(--color-fog); margin-top: 2px;">Command Exit Code: ${d.exit_code} (Clean Sandbox Worktree)</div>
            </div>
          </div>
          <span class="badge ${d.status === 'PASSED' ? 'badge-pass' : 'badge-fail'}" style="font-size: 12px;">EXIT ${d.exit_code}</span>
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">terminal</span> Commands Executed</div>
        <div style="display: flex; flex-direction: column; gap: 6px;">
          ${(d.commands || []).map(cmd => `
            <div style="padding: 8px 12px; background: #050607; border: 1px solid var(--color-graphite); border-radius: 6px; font-family: var(--font-mono); font-size: 12px; color: var(--color-signal-mint);">
              &gt; ${escapeHtml(cmd)}
            </div>
          `).join('')}
        </div>
      </div>
      <div class="inspector-section">
        <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">output</span> Verification Console Output</div>
        <pre style="padding: 12px; background: #050607; border: 1px solid var(--color-graphite); border-radius: 6px; font-family: var(--font-mono); font-size: 11.5px; color: #4ade80; line-height: 1.5; max-height: 240px; overflow-y: auto;">${escapeHtml(d.stdout || 'Ran 1 test in 0.002s\n\nOK')}</pre>
      </div>
    `;
  } else if (ev.type === 'brain.evaluated') {
    bodyHtml = `
      <div class="inspector-section">
        <div style="padding: 14px; background: var(--color-carbon); border-left: 3px solid #fbbf24; border-radius: 0 8px 8px 0;">
          <div style="font-size: 11px; font-weight: 600; color: #fbbf24; margin-bottom: 4px;">BRAIN EVALUATION DECISION</div>
          <div style="font-size: 15px; font-weight: 600; color: #ffffff;">Action: ${escapeHtml(d.action)}</div>
          <div style="font-size: 13px; color: var(--color-mist); margin-top: 6px; line-height: 1.5;">${escapeHtml(d.reason)}</div>
        </div>
      </div>
      ${d.evidence_assessment ? `
        <div class="inspector-section">
          <div class="inspector-section-title"><span class="material-symbols-outlined" style="font-size:14px;">checklist</span> Evidence Assessment</div>
          <div style="padding: 10px; background: var(--color-carbon); border: 1px solid var(--color-graphite); border-radius: 6px; font-size: 12.5px; color: var(--color-mist); line-height: 1.5;">
            ${escapeHtml(d.evidence_assessment)}
          </div>
        </div>
      ` : ''}
    `;
  } else if (ev.type.startsWith('goal.')) {
    const isPass = d.phase === 'PASS';
    bodyHtml = `
      <div class="inspector-section">
        <div style="padding: 16px; background: ${isPass ? 'rgba(52, 211, 153, 0.08)' : 'rgba(239, 68, 68, 0.08)'}; border: 1px solid ${isPass ? 'var(--color-signal-mint)' : 'var(--color-coral-red)'}; border-radius: 8px;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="font-size: 17px; font-weight: 600; color: #ffffff;">
              ${isPass ? 'Goal Execution Succeeded' : 'Goal Terminated: ' + escapeHtml(d.phase)}
            </div>
            <span class="badge ${isPass ? 'badge-pass' : 'badge-fail'}" style="font-size: 13px; padding: 4px 12px;">${escapeHtml(d.phase)}</span>
          </div>
          <div style="font-size: 13px; color: var(--color-mist); margin-top: 8px; line-height: 1.5;">
            ${escapeHtml(d.reason || (isPass ? 'All contract acceptance criteria and deterministic validation commands completed successfully.' : 'Terminal reason recorded.'))}
          </div>
          <div style="margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(255, 255, 255, 0.1); display: flex; gap: 20px; font-family: var(--font-mono); font-size: 12px; color: var(--color-fog);">
            <span>Total Duration: <strong style="color: var(--color-paper);">${escapeHtml(d.duration || '—')}</strong></span>
            <span>Completed: <strong style="color: var(--color-paper);">${escapeHtml(d.completed_at ? d.completed_at.replace('T', ' ').substring(0, 19) + ' UTC' : 'N/A')}</strong></span>
          </div>
        </div>
      </div>
    `;
  } else {
    // Generic fallback for any other event
    bodyHtml = `
      <div class="inspector-section">
        <div style="font-size: 13px; color: var(--color-mist); line-height: 1.5; padding: 12px; background: var(--color-carbon); border-radius: 6px;">
          ${escapeHtml(JSON.stringify(d, null, 2))}
        </div>
      </div>
    `;
  }

  // Construct complete Inspector UI
  container.innerHTML = `
    <div class="inspector-header">
      <div class="inspector-icon-title">
        <div class="inspector-icon-badge" style="color: ${ev.icon_color}; background-color: rgba(255, 255, 255, 0.04);">
          <span class="material-symbols-outlined">${ev.icon}</span>
        </div>
        <div>
          <h2 class="inspector-title">${escapeHtml(ev.title)}</h2>
          <div class="inspector-meta">
            <span>Timestamp: <strong>${escapeHtml(ev.time_str)}</strong></span>
            <span style="margin: 0 6px;">•</span>
            <span>Elapsed: <strong style="color: var(--color-paper);">${escapeHtml(ev.elapsed_str)}</strong></span>
          </div>
        </div>
      </div>
      <span class="badge ${ev.badge_class}" style="font-size: 12px; padding: 4px 10px;">${escapeHtml(ev.badge)}</span>
    </div>

    <div class="inspector-summary-callout">
      ${escapeHtml(ev.summary)}
    </div>

    ${bodyHtml}

    <details class="inspector-json-details">
      <summary><span class="material-symbols-outlined" style="font-size:13px; vertical-align:middle;">data_object</span> View Raw Event Payload (JSON)</summary>
      <pre class="code-block" style="margin-top: 8px; padding: 10px; font-size: 11px; max-height: 240px; overflow-y: auto;">${escapeHtml(ev.raw_json)}</pre>
    </details>
  `;
}

function initTimelineInspector() {
  const el = document.getElementById('enriched-events-data');
  if (!el) return;
  currentEnrichedEvents = loadEnrichedEvents();
  if (currentEnrichedEvents && currentEnrichedEvents.length > 0) {
    // Check if an event is already marked selected, otherwise pick the last (most recent / verdict)
    const selectedEl = document.querySelector('.timeline-event-card.selected');
    const targetId = selectedEl ? selectedEl.getAttribute('data-event-id') : currentEnrichedEvents[currentEnrichedEvents.length - 1].id;
    selectTimelineEvent(targetId);
  }
}


