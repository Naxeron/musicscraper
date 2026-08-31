/**
 * MusicScraper Web GUI Application JavaScript
 * Zero external frameworks, fast vanilla SPA controller.
 */

// Application State
const AppState = {
  currentTab: 'dashboard',
  selectedTaskId: null,
  activeEventSource: null,
  tasks: [],
  auditResult: null,
  qualityCandidates: [],
};

// ==============================================================================
// Initialization & Navigation
// ==============================================================================

document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  setupForms();
  refreshSystemStatus();
  loadConfig();
  refreshTaskList();
  refreshTransfers();

  // Periodic poll for status & transfers every 15s
  setInterval(refreshSystemStatus, 15000);
  setInterval(refreshTransfers, 15000);
  setInterval(refreshTaskList, 8000);
});

function setupNavigation() {
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tabId = btn.getAttribute('data-tab');
      switchTab(tabId);
    });
  });

  document.getElementById('btn-refresh-status').addEventListener('click', () => {
    refreshSystemStatus();
    refreshTransfers();
  });

  document.getElementById('btn-refresh-transfers').addEventListener('click', refreshTransfers);
  document.getElementById('btn-refresh-task-list').addEventListener('click', refreshTaskList);
  document.getElementById('btn-view-global-task').addEventListener('click', () => {
    switchTab('tasks');
  });

  document.getElementById('btn-cancel-task').addEventListener('click', cancelSelectedTask);
}

function switchTab(tabId) {
  AppState.currentTab = tabId;

  // Update nav buttons
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
  });

  // Update tab panes
  document.querySelectorAll('.tab-pane').forEach((pane) => {
    pane.classList.remove('active');
  });
  const targetPane = document.getElementById(`view-${tabId}`);
  if (targetPane) {
    targetPane.classList.add('active');
  }

  // Update title
  const titles = {
    dashboard: 'System Dashboard',
    auditor: 'Discography Auditor',
    soulseek: 'Soulseek Peer Discovery & Transfers',
    artist: 'Multi-Source Artist Downloader',
    quality: 'Audio Quality Scanner & Upgrader',
    tagger: 'Last.fm Automated Genre Tagger',
    scrapers: 'Bandcamp & Web Release Crawlers',
    cleaner: 'Empty & Non-Music Folder Cleaner',
    tasks: 'Live Task Monitor & Console',
    settings: 'Settings & Integrations',
  };
  document.getElementById('current-page-title').textContent = titles[tabId] || 'MusicScraper';
}

// ==============================================================================
// System Status & Configuration
// ==============================================================================

async function refreshSystemStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();

    // Dashboard Cards
    const slsk = data.services?.slskd;
    const nav = data.services?.navidrome;
    const lfm = data.services?.lastfm;
    const paths = data.paths;

    // slskd
    const slskEl = document.getElementById('dash-slskd-status');
    const slskUserEl = document.getElementById('dash-slskd-user');
    const pillSlsk = document.getElementById('pill-slskd');
    if (slsk?.connected) {
      slskEl.textContent = 'Connected';
      slskEl.className = 'stat-value text-green';
      slskUserEl.textContent = `User: ${slsk.username || 'active'}`;
      pillSlsk.querySelector('.status-dot').className = 'status-dot online';
    } else {
      slskEl.textContent = slsk?.configured ? 'Offline' : 'Not Configured';
      slskEl.className = 'stat-value text-muted';
      slskUserEl.textContent = slsk?.error ? slsk.error.slice(0, 30) : 'Check SLSKD_URL';
      pillSlsk.querySelector('.status-dot').className = 'status-dot offline';
    }

    // Navidrome
    const navEl = document.getElementById('dash-nav-status');
    const navUrlEl = document.getElementById('dash-nav-url');
    const pillNav = document.getElementById('pill-navidrome');
    if (nav?.connected) {
      navEl.textContent = 'Connected';
      navEl.className = 'stat-value text-green';
      navUrlEl.textContent = nav.url || 'Online';
      pillNav.querySelector('.status-dot').className = 'status-dot online';
    } else {
      navEl.textContent = nav?.configured ? 'Offline' : 'Not Configured';
      navEl.className = 'stat-value text-muted';
      navUrlEl.textContent = nav?.url || 'Configure in Settings';
      pillNav.querySelector('.status-dot').className = 'status-dot offline';
    }

    // Last.fm
    const lfmEl = document.getElementById('dash-lastfm-status');
    const pillLfm = document.getElementById('pill-lastfm');
    if (lfm?.connected) {
      lfmEl.textContent = 'Last.fm: API Ready';
      pillLfm.querySelector('.status-dot').className = 'status-dot online';
    } else {
      lfmEl.textContent = lfm?.configured ? 'Last.fm: Unreachable' : 'Last.fm: No API Key';
      pillLfm.querySelector('.status-dot').className = 'status-dot warning';
    }

    // Library Path
    const libEl = document.getElementById('dash-lib-status');
    const libSubEl = document.getElementById('dash-lib-exists');
    libEl.textContent = paths?.library_dir || '-';
    libSubEl.textContent = paths?.library_exists ? '✔ Directory Verified' : '⚠ Directory not found';
    libSubEl.className = paths?.library_exists ? 'stat-sub text-green' : 'stat-sub text-yellow';
  } catch (err) {
    console.error('Error fetching system status:', err);
  }
}

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const cfg = await res.json();

    document.getElementById('cfg-lib-dir').value = cfg.DEFAULT_LIBRARY_DIR || '';
    document.getElementById('cfg-out-dir').value = cfg.DEFAULT_OUTPUT_DIR || '';
    document.getElementById('cfg-slskd-url').value = cfg.SLSKD_URL || '';
    document.getElementById('cfg-slskd-user').value = cfg.SLSKD_USERNAME || '';
    document.getElementById('cfg-nav-url').value = cfg.NAVIDROME_URL || '';
    document.getElementById('cfg-nav-user').value = cfg.NAVIDROME_USER || '';
    document.getElementById('cfg-lastfm-key').value = cfg.LASTFM_API_KEY || '';
    document.getElementById('cfg-bc-email').value = cfg.BANDCAMP_EMAIL || '';

    // Also populate default fields in other tabs
    if (!document.getElementById('audit-dir').value) {
      document.getElementById('audit-dir').value = cfg.DEFAULT_LIBRARY_DIR || '';
    }
    if (!document.getElementById('artist-dl-lib').value) {
      document.getElementById('artist-dl-lib').value = cfg.DEFAULT_LIBRARY_DIR || '';
    }
    if (!document.getElementById('artist-dl-output').value) {
      document.getElementById('artist-dl-output').value = cfg.DEFAULT_OUTPUT_DIR || '';
    }
    if (!document.getElementById('quality-lib-dir').value) {
      document.getElementById('quality-lib-dir').value = cfg.DEFAULT_LIBRARY_DIR || '';
    }
    if (!document.getElementById('cleaner-path').value) {
      document.getElementById('cleaner-path').value = cfg.DEFAULT_OUTPUT_DIR || '';
    }
    if (!document.getElementById('bc-output').value) {
      document.getElementById('bc-output').value = cfg.DEFAULT_OUTPUT_DIR || '';
    }
    if (!document.getElementById('crawl-output').value) {
      document.getElementById('crawl-output').value = cfg.DEFAULT_OUTPUT_DIR || '';
    }
  } catch (err) {
    console.error('Error loading config:', err);
  }
}

async function refreshTransfers() {
  try {
    const res = await fetch('/api/slskd/transfers');
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.querySelector('#table-slskd-transfers tbody');

    if (!data.connected || !data.downloads || data.downloads.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No active transfers or slskd not connected.</td></tr>';
      return;
    }

    let rows = '';
    for (const dl of data.downloads) {
      const sizeMb = dl.size ? (dl.size / (1024 * 1024)).toFixed(1) + ' MB' : '-';
      const speedKb = dl.speed ? (dl.speed / 1024).toFixed(0) + ' KB/s' : '0 KB/s';
      const pct = dl.percent ? `${dl.percent.toFixed(0)}%` : '0%';
      rows += `
        <tr>
          <td><span class="peer-user">${escapeHtml(dl.username || dl.remoteUser || '-')}</span></td>
          <td class="text-truncate" style="max-width: 300px;">${escapeHtml(dl.filename || dl.file || '-')}</td>
          <td>${sizeMb}</td>
          <td>${speedKb}</td>
          <td><span class="badge badge-running">${escapeHtml(dl.state || 'active')}</span></td>
          <td>
            <div style="width: 100px; background: #222; height: 6px; border-radius: 3px; overflow: hidden;">
              <div style="width: ${pct}; background: var(--accent-blue); height: 100%;"></div>
            </div>
          </td>
        </tr>
      `;
    }
    tbody.innerHTML = rows;
  } catch (err) {
    console.error('Error fetching transfers:', err);
  }
}

// ==============================================================================
// Task Execution & Streaming
// ==============================================================================

async function startTask(taskType, params, name = null) {
  try {
    const res = await fetch('/api/tasks/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: taskType, params, name })
    });

    if (!res.ok) {
      const err = await res.json();
      alert(`Error starting task: ${err.error || res.statusText}`);
      return null;
    }

    const data = await res.json();
    const task = data.task;

    // Show in global banner
    showGlobalTaskBanner(task);

    // Refresh task list & select it
    await refreshTaskList();
    selectTask(task.id);

    return task;
  } catch (err) {
    alert(`Failed to trigger task: ${err.message}`);
    return null;
  }
}

function showGlobalTaskBanner(task) {
  const banner = document.getElementById('global-task-banner');
  const nameEl = document.getElementById('global-task-name');
  banner.classList.remove('hidden');
  nameEl.textContent = `${task.name}: ${task.stage || 'Running'}`;
}

function hideGlobalTaskBanner() {
  document.getElementById('global-task-banner').classList.add('hidden');
}

async function refreshTaskList() {
  try {
    const res = await fetch('/api/tasks?limit=30');
    if (!res.ok) return;
    const data = await res.json();
    AppState.tasks = data.tasks || [];

    const activeCount = AppState.tasks.filter((t) => t.status === 'running' || t.status === 'pending').length;
    const badge = document.getElementById('active-tasks-badge');
    if (activeCount > 0) {
      badge.textContent = activeCount;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
      hideGlobalTaskBanner();
    }

    // Render Dashboard Compact List
    renderDashTaskList();

    // Render Task Console Sidebar
    renderTaskSidebar();
  } catch (err) {
    console.error('Error refreshing task list:', err);
  }
}

function renderDashTaskList() {
  const container = document.getElementById('dash-recent-tasks');
  if (AppState.tasks.length === 0) {
    container.innerHTML = '<div class="text-muted">No recent tasks executed.</div>';
    return;
  }

  container.innerHTML = AppState.tasks.slice(0, 5).map((t) => `
    <div class="task-nav-item" onclick="switchTab('tasks'); selectTask('${t.id}')">
      <div class="task-nav-title">${escapeHtml(t.name)}</div>
      <div class="task-nav-meta">
        <span class="badge badge-${t.status}">${t.status}</span>
        <span>${t.created_at ? t.created_at.slice(11, 19) : ''}</span>
      </div>
    </div>
  `).join('');
}

function renderTaskSidebar() {
  const container = document.getElementById('task-list-sidebar');
  if (AppState.tasks.length === 0) {
    container.innerHTML = '<div class="p-3 text-muted">No tasks available.</div>';
    return;
  }

  container.innerHTML = AppState.tasks.map((t) => `
    <div class="task-nav-item ${AppState.selectedTaskId === t.id ? 'active' : ''}" onclick="selectTask('${t.id}')">
      <div class="task-nav-title">${escapeHtml(t.name)}</div>
      <div class="task-nav-meta">
        <span class="badge badge-${t.status}">${t.status}</span>
        <span>${t.created_at ? t.created_at.slice(11, 19) : ''}</span>
      </div>
    </div>
  `).join('');
}

let activePollTimer = null;

function selectTask(taskId) {
  AppState.selectedTaskId = taskId;
  renderTaskSidebar();

  if (activePollTimer) {
    clearTimeout(activePollTimer);
    activePollTimer = null;
  }

  const logViewer = document.getElementById('terminal-log-viewer');
  logViewer.innerHTML = '';

  const task = AppState.tasks.find((t) => t.id === taskId);
  if (task) {
    document.getElementById('current-task-title').textContent = `${task.name} (${task.type})`;
    updateTaskStatusBadge(task.status);
    document.getElementById('current-task-progress-bar').style.width = `${task.progress || 0}%`;
    document.getElementById('btn-cancel-task').disabled = (task.status !== 'running' && task.status !== 'pending');
  }

  pollTaskLogs(taskId);
}

function updateTaskStatusBadge(status) {
  const badge = document.getElementById('current-task-status-badge');
  badge.className = `badge badge-${status}`;
  badge.textContent = status;
}

let lastRenderedLogCount = 0;

async function pollTaskLogs(taskId) {
  if (AppState.selectedTaskId !== taskId) return;
  try {
    const res = await fetch(`/api/tasks/${taskId}?logs=true&log_limit=1000`);
    if (!res.ok) return;
    const task = await res.json();

    const logViewer = document.getElementById('terminal-log-viewer');
    const autoscroll = document.getElementById('chk-autoscroll');

    // Render logs
    if (task.logs) {
      logViewer.innerHTML = '';
      task.logs.forEach(appendLogLine);
      if (autoscroll.checked) {
        logViewer.scrollTop = logViewer.scrollHeight;
      }
    }

    document.getElementById('current-task-progress-bar').style.width = `${task.progress || 0}%`;
    updateTaskStatusBadge(task.status);
    document.getElementById('btn-cancel-task').disabled = (task.status !== 'running' && task.status !== 'pending');

    if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
      handleTaskResultUpdate(task);
      refreshTaskList();
    } else {
      activePollTimer = setTimeout(() => pollTaskLogs(taskId), 800);
    }
  } catch (err) {
    console.error('Error polling task logs:', err);
  }
}

function appendLogLine(entry) {
  const logViewer = document.getElementById('terminal-log-viewer');
  const div = document.createElement('div');
  div.className = 'log-line';

  const timeSpan = document.createElement('span');
  timeSpan.className = 'log-time';
  timeSpan.textContent = `[${entry.time || ''}]`;

  const msgSpan = document.createElement('span');
  if (entry.level === 'ERROR') {
    msgSpan.className = 'log-error';
  } else if (entry.level === 'WARNING') {
    msgSpan.className = 'log-warn';
  } else {
    msgSpan.className = 'log-info';
  }
  msgSpan.textContent = ` ${entry.message}`;

  div.appendChild(timeSpan);
  div.appendChild(msgSpan);
  logViewer.appendChild(div);
}

async function cancelSelectedTask() {
  if (!AppState.selectedTaskId) return;
  try {
    await fetch(`/api/tasks/${AppState.selectedTaskId}/cancel`, { method: 'POST' });
    refreshTaskList();
  } catch (err) {
    alert(`Failed to cancel task: ${err.message}`);
  }
}

function handleTaskResultUpdate(task) {
  if (!task || !task.result) return;

  if (task.type === 'audit') {
    renderAuditResults(task.result);
  } else if (task.type === 'soulseek_search') {
    renderSoulseekResults(task.result);
  } else if (task.type === 'quality_scan') {
    renderQualityCandidates(task.result);
  } else if (task.type === 'clean_folders') {
    renderCleanerResults(task.result);
  }
}

// ==============================================================================
// Feature Specific Controllers
// ==============================================================================

function setupForms() {
  // 1. Audit Form
  document.getElementById('form-audit').addEventListener('submit', async (e) => {
    e.preventDefault();
    const artist = document.getElementById('audit-artist').value.trim();
    const musicDir = document.getElementById('audit-dir').value.trim();
    const fullScan = document.getElementById('audit-full-scan').checked;
    const forceRefresh = document.getElementById('audit-force-refresh').checked;

    switchTab('tasks');
    await startTask('audit', {
      artist,
      music_dir: musicDir,
      full_scan: fullScan,
      force_refresh: forceRefresh,
    }, `Audit: ${artist}`);
  });

  // Audit filter buttons
  document.querySelectorAll('[data-filter-audit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-filter-audit]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      filterAuditReleases(btn.getAttribute('data-filter-audit'));
    });
  });

  // Audit Export Buttons
  document.getElementById('btn-export-audit-json').addEventListener('click', () => {
    if (!AppState.auditResult) return;
    downloadJson(AppState.auditResult, `${AppState.auditResult.artist}_audit.json`);
  });

  document.getElementById('btn-export-audit-txt').addEventListener('click', () => {
    if (!AppState.auditResult || !AppState.auditResult.missing_items) return;
    const lines = AppState.auditResult.missing_items.map((m) => `${m.release || 'Unknown Release'} - ${m.title}`);
    downloadText(lines.join('\n'), `${AppState.auditResult.artist}_missing_tracks.txt`);
  });

  document.getElementById('btn-audit-send-soulseek').addEventListener('click', () => {
    if (!AppState.auditResult) return;
    switchTab('soulseek');
    document.getElementById('slsk-query').value = AppState.auditResult.artist;
  });

  // 2. Soulseek Form
  document.getElementById('form-soulseek-search').addEventListener('submit', async (e) => {
    e.preventDefault();
    const query = document.getElementById('slsk-query').value.trim();
    const format = document.getElementById('slsk-format').value;
    const timeout = parseFloat(document.getElementById('slsk-timeout').value) || 15;

    switchTab('tasks');
    await startTask('soulseek_search', { query, format, timeout }, `Soulseek Search: ${query}`);
  });

  // 3. Artist Downloader Form
  document.getElementById('form-artist-dl').addEventListener('submit', async (e) => {
    e.preventDefault();
    const artist = document.getElementById('artist-dl-name').value.trim();
    const format = document.getElementById('artist-dl-format').value;
    const outputDir = document.getElementById('artist-dl-output').value.trim();
    const libraryDir = document.getElementById('artist-dl-lib').value.trim();
    const useBandcamp = document.getElementById('artist-dl-use-bc').checked;
    const useSoulseek = document.getElementById('artist-dl-use-slsk').checked;
    const dryRun = document.getElementById('artist-dl-dry-run').checked;

    switchTab('tasks');
    await startTask('artist_download', {
      artist,
      format,
      output_dir: outputDir,
      library_dir: libraryDir,
      use_bandcamp: useBandcamp,
      use_soulseek: useSoulseek,
      dry_run: dryRun,
    }, `Artist Downloader: ${artist}`);
  });

  // 4. Quality Scanner & Upgrader
  document.getElementById('btn-scan-quality').addEventListener('click', async () => {
    const libraryDir = document.getElementById('quality-lib-dir').value.trim();
    const artistFilter = document.getElementById('quality-artist-filter').value.trim();
    const format = document.getElementById('quality-target-fmt').value;

    switchTab('tasks');
    await startTask('quality_scan', {
      library_dir: libraryDir,
      artist_filter: artistFilter,
      format,
    }, `Quality Scan: ${artistFilter || 'Full Library'}`);
  });

  document.getElementById('btn-upgrade-quality').addEventListener('click', async () => {
    const libraryDir = document.getElementById('quality-lib-dir').value.trim();
    const artistFilter = document.getElementById('quality-artist-filter').value.trim();
    const format = document.getElementById('quality-target-fmt').value;

    switchTab('tasks');
    await startTask('quality_upgrade', {
      library_dir: libraryDir,
      artist_filter: artistFilter,
      format,
      dry_run: false,
    }, `Quality Upgrade: ${artistFilter || 'Full Library'}`);
  });

  // 5. Genre Tagger Form
  document.getElementById('form-tagger').addEventListener('submit', async (e) => {
    e.preventDefault();
    const path = document.getElementById('tagger-path').value.trim();
    const strategy = document.getElementById('tagger-strategy').value;
    const limit = parseInt(document.getElementById('tagger-limit').value, 10) || 3;
    const mode = document.getElementById('tagger-mode').value;
    const dryRun = document.getElementById('tagger-dry-run').checked;

    switchTab('tasks');
    await startTask('genre_tag', {
      path,
      strategy,
      limit,
      mode,
      dry_run: dryRun,
    }, `Genre Tagging: ${path}`);
  });

  // 6. Bandcamp Downloader
  document.getElementById('form-bc-dl').addEventListener('submit', async (e) => {
    e.preventDefault();
    const targets = document.getElementById('bc-targets').value.trim();
    const format = document.getElementById('bc-format').value;
    const outputDir = document.getElementById('bc-output').value.trim();
    const fallback = document.getElementById('bc-fallback').checked;

    switchTab('tasks');
    await startTask('bandcamp_download', {
      targets,
      format,
      output_dir: outputDir,
      fallback,
    }, 'Bandcamp Downloads');
  });

  // 7. Universal Web Crawler
  document.getElementById('form-crawl-dl').addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = document.getElementById('crawl-url').value.trim();
    const workers = parseInt(document.getElementById('crawl-workers').value, 10) || 4;
    const outputDir = document.getElementById('crawl-output').value.trim();
    const overwrite = document.getElementById('crawl-overwrite').checked;

    switchTab('tasks');
    await startTask('universal_scrape', {
      url,
      max_workers: workers,
      output_dir: outputDir,
      overwrite,
    }, `Web Scraper: ${url}`);
  });

  // 8. Folder Cleaner Form
  document.getElementById('form-cleaner').addEventListener('submit', async (e) => {
    e.preventDefault();
    const path = document.getElementById('cleaner-path').value.trim();
    const execute = document.getElementById('cleaner-execute').checked;

    switchTab('tasks');
    await startTask('clean_folders', {
      path,
      execute,
    }, `Folder Cleaner (${execute ? 'Execute' : 'Preview'}): ${path}`);
  });

  // 9. Settings Form
  document.getElementById('form-settings').addEventListener('submit', async (e) => {
    e.preventDefault();
    const updates = {
      DEFAULT_LIBRARY_DIR: document.getElementById('cfg-lib-dir').value.trim(),
      DEFAULT_OUTPUT_DIR: document.getElementById('cfg-out-dir').value.trim(),
      SLSKD_URL: document.getElementById('cfg-slskd-url').value.trim(),
      SLSKD_USERNAME: document.getElementById('cfg-slskd-user').value.trim(),
      SLSKD_PASSWORD: document.getElementById('cfg-slskd-pass').value.trim(),
      NAVIDROME_URL: document.getElementById('cfg-nav-url').value.trim(),
      NAVIDROME_USER: document.getElementById('cfg-nav-user').value.trim(),
      NAVIDROME_TOKEN: document.getElementById('cfg-nav-token').value.trim(),
      LASTFM_API_KEY: document.getElementById('cfg-lastfm-key').value.trim(),
      BANDCAMP_EMAIL: document.getElementById('cfg-bc-email').value.trim(),
    };

    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      });
      if (res.ok) {
        alert('Configuration saved successfully!');
        refreshSystemStatus();
      } else {
        alert('Failed to save settings.');
      }
    } catch (err) {
      alert(`Settings save error: ${err.message}`);
    }
  });
}

// ==============================================================================
// Result Renderers
// ==============================================================================

function renderAuditResults(result) {
  AppState.auditResult = result;
  const panel = document.getElementById('audit-results-panel');
  panel.classList.remove('hidden');

  document.getElementById('audit-res-artist').textContent = result.artist || '-';
  document.getElementById('audit-res-mbid').textContent = `MBID: ${result.mbid || '-'}`;
  document.getElementById('audit-res-pct').textContent = `${result.completion_pct || 0}%`;
  document.getElementById('audit-res-ratio').textContent = `${result.found_count || 0} / ${result.total_tracks || 0} tracks`;
  document.getElementById('audit-res-found').textContent = result.found_count || 0;
  document.getElementById('audit-res-missing').textContent = result.missing_count || 0;

  filterAuditReleases('all');
}

function filterAuditReleases(filterMode) {
  if (!AppState.auditResult || !AppState.auditResult.releases) return;
  const tbody = document.getElementById('tbody-audit-releases');

  let releases = AppState.auditResult.releases;
  if (filterMode === 'missing') {
    releases = releases.filter((r) => r.found_count < r.total_tracks);
  } else if (filterMode === 'complete') {
    releases = releases.filter((r) => r.found_count >= r.total_tracks && r.total_tracks > 0);
  }

  if (releases.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No releases matching filter.</td></tr>';
    return;
  }

  tbody.innerHTML = releases.map((r) => {
    const isComplete = r.total_tracks > 0 ? (r.found_count >= r.total_tracks) : true;
    const missingCount = r.missing_count > 0 ? r.missing_count : Math.max(0, r.total_tracks - r.found_count);
    const pct = r.total_tracks > 0 ? ((r.found_count / r.total_tracks) * 100).toFixed(0) : (isComplete ? 100 : 0);
    const badgeClass = isComplete ? 'badge-found' : 'badge-missing';
    const badgeText = isComplete ? '100% Complete' : `${missingCount} Missing`;

    return `
      <tr>
        <td>${escapeHtml(r.year || '-')}</td>
        <td><span class="badge">${escapeHtml(r.type || 'Album')}</span></td>
        <td><strong>${escapeHtml(r.title)}</strong></td>
        <td>${r.found_count} / ${r.total_tracks}</td>
        <td>
          <span class="badge ${badgeClass}">${badgeText} (${pct}%)</span>
        </td>
        <td>
          ${!isComplete ? `
            <button class="btn btn-xs btn-outline" onclick="queueReleaseSoulseek('${escapeHtml(AppState.auditResult.artist)}', '${escapeHtml(r.title)}')">
              ⚡ Soulseek
            </button>
          ` : '<span class="text-muted text-xs">Complete</span>'}
        </td>
      </tr>
    `;
  }).join('');
}

function queueReleaseSoulseek(artist, release) {
  switchTab('soulseek');
  document.getElementById('slsk-query').value = `${artist} ${release}`;
}

function renderSoulseekResults(result) {
  const countEl = document.getElementById('slsk-results-count');
  const container = document.getElementById('slsk-results-container');
  const dirs = result.directories || [];

  countEl.textContent = `${dirs.length} directories found`;
  if (dirs.length === 0) {
    container.innerHTML = '<div class="text-center text-muted py-4">No peer directories found for this search.</div>';
    return;
  }

  container.innerHTML = dirs.map((d, dIdx) => `
    <div class="peer-folder-card">
      <div class="peer-folder-header">
        <div>
          <span class="peer-user">${escapeHtml(d.user)}</span>
          <span class="text-muted"> (${d.file_count} audio files, ${(d.total_size / (1024 * 1024)).toFixed(1)} MB)</span>
          <div class="peer-folder-path">${escapeHtml(d.dir_name)}</div>
        </div>
        <button class="btn btn-sm btn-primary" onclick="queuePeerFolder(${dIdx})">📥 Queue Folder</button>
      </div>
      <div class="peer-files-list">
        ${d.files.slice(0, 8).map((f) => `
          <div class="peer-file-row">
            <span>${escapeHtml(f.base_filename)}</span>
            <span>
              <span class="badge">${escapeHtml(f.fmt_label || 'Audio')}</span>
              <span class="text-muted">${(f.size / (1024 * 1024)).toFixed(1)} MB</span>
            </span>
          </div>
        `).join('')}
        ${d.files.length > 8 ? `<div class="text-muted text-xs">+ ${d.files.length - 8} more tracks...</div>` : ''}
      </div>
    </div>
  `).join('');

  // Store globally on window for button click handler
  window._lastSoulseekDirs = dirs;
}

function queuePeerFolder(dirIndex) {
  const dirs = window._lastSoulseekDirs;
  if (!dirs || !dirs[dirIndex]) return;
  const d = dirs[dirIndex];
  const filesToQueue = d.files.map((f) => ({
    filename: f.full_filename || f.filename,
    size: f.size
  }));

  startTask('soulseek_download', {
    username: d.user,
    files: filesToQueue,
    dir_name: d.dir_name
  }, `Download Folder: ${d.user}`);
}

function renderQualityCandidates(result) {
  const candidates = result.candidates || [];
  AppState.qualityCandidates = candidates;
  document.getElementById('quality-candidate-count').textContent = `${candidates.length} tracks`;
  const tbody = document.getElementById('tbody-quality-candidates');

  if (candidates.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No low-bitrate candidates found in library!</td></tr>';
    return;
  }

  tbody.innerHTML = candidates.slice(0, 100).map((c) => `
    <tr>
      <td>${escapeHtml(c.artist || '-')}</td>
      <td>${escapeHtml(c.album || '-')}</td>
      <td><strong>${escapeHtml(c.title || '-')}</strong></td>
      <td><span class="badge badge-missing">${escapeHtml(c.current_label || c.format)}</span></td>
      <td><span class="badge badge-found">${escapeHtml(c.target_quality)}</span></td>
      <td class="text-truncate" style="max-width: 200px;">${escapeHtml(c.filename)}</td>
    </tr>
  `).join('');
}

function renderCleanerResults(result) {
  const folders = result.deleted_folders || [];
  document.getElementById('cleaner-count').textContent = `${folders.length} folders (${result.dry_run ? 'Preview' : 'Deleted'})`;
  const list = document.getElementById('list-cleaner-folders');

  if (folders.length === 0) {
    list.innerHTML = '<li class="text-muted">No empty or non-music folders detected.</li>';
    return;
  }

  list.innerHTML = folders.map((f) => `
    <li>${escapeHtml(f)}</li>
  `).join('');
}

// ==============================================================================
// Utilities
// ==============================================================================

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function downloadJson(obj, filename) {
  const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(obj, null, 2));
  const el = document.createElement('a');
  el.setAttribute('href', dataStr);
  el.setAttribute('download', filename);
  document.body.appendChild(el);
  el.click();
  el.remove();
}

function downloadText(text, filename) {
  const dataStr = 'data:text/plain;charset=utf-8,' + encodeURIComponent(text);
  const el = document.createElement('a');
  el.setAttribute('href', dataStr);
  el.setAttribute('download', filename);
  document.body.appendChild(el);
  el.click();
  el.remove();
}
