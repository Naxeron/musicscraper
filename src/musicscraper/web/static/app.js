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
  libraryReleases: [],
  selectedReleaseId: null,
  selectedReleaseData: null,
  releaseFilter: 'all',
  releaseSearchQuery: '',
  releaseSortBy: 'artist',
};

// ==============================================================================
// Initialization & Navigation
// ==============================================================================

document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  setupForms();
  setupLibraryReleases();
  refreshSystemStatus();
  loadConfig();
  refreshTaskList();
  refreshTransfers();
  loadLibraryReleases();

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

  // If opening releases tab and not yet loaded, load releases
  if (tabId === 'releases' && AppState.libraryReleases.length === 0) {
    loadLibraryReleases();
  }

  // Update title
  const titles = {
    dashboard: 'System Dashboard',
    releases: 'Library Releases & Missing Track Downloader',
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
      navUrlEl.textContent = nav?.error || nav?.url || 'Configure in Settings';
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
    document.getElementById('cfg-nav-user').value = cfg.NAVIDROME_USER || cfg.NAVIDROME_USERNAME || '';
    document.getElementById('cfg-lastfm-key').value = cfg.LASTFM_API_KEY || '';
    document.getElementById('cfg-bc-email').value = cfg.BANDCAMP_EMAIL || '';

    if (cfg.has_slskd_password) {
      document.getElementById('cfg-slskd-pass').placeholder = '•••••••• (configured in .env)';
    } else {
      document.getElementById('cfg-slskd-pass').placeholder = '';
    }

    if (cfg.has_navidrome_token || cfg.has_navidrome_password) {
      document.getElementById('cfg-nav-token').placeholder = '•••••••• (configured in .env)';
    } else {
      document.getElementById('cfg-nav-token').placeholder = '';
    }

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

    // Live update visual scan progress in Library Releases panel
    const activeScanTask = AppState.tasks.find(
      (t) => (t.type === 'library_scan' || t.type === 'library_audit_all') && (t.status === 'running' || t.status === 'pending')
    );
    updateLibraryScanProgress(activeScanTask);

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
  } else if (task.type === 'library_scan' || task.type === 'release_missing_download' || task.type === 'track_soulseek_download') {
    loadLibraryReleases(false);
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
    const slskdPass = document.getElementById('cfg-slskd-pass').value.trim();
    const navToken = document.getElementById('cfg-nav-token').value.trim();

    const updates = {
      DEFAULT_LIBRARY_DIR: document.getElementById('cfg-lib-dir').value.trim(),
      DEFAULT_OUTPUT_DIR: document.getElementById('cfg-out-dir').value.trim(),
      SLSKD_URL: document.getElementById('cfg-slskd-url').value.trim(),
      SLSKD_USERNAME: document.getElementById('cfg-slskd-user').value.trim(),
      NAVIDROME_URL: document.getElementById('cfg-nav-url').value.trim(),
      NAVIDROME_USER: document.getElementById('cfg-nav-user').value.trim(),
      NAVIDROME_USERNAME: document.getElementById('cfg-nav-user').value.trim(),
      LASTFM_API_KEY: document.getElementById('cfg-lastfm-key').value.trim(),
      BANDCAMP_EMAIL: document.getElementById('cfg-bc-email').value.trim(),
    };

    if (slskdPass) {
      updates.SLSKD_PASSWORD = slskdPass;
    }
    if (navToken) {
      updates.NAVIDROME_TOKEN = navToken;
      updates.NAVIDROME_PASSWORD = navToken;
    }

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
// Library Releases & Missing Track Downloader Controller
// ==============================================================================

let scanCompleteTimeout = null;

function updateLibraryScanProgress(task) {
  const card = document.getElementById('lib-scan-progress-card');
  const btnRescan = document.getElementById('btn-rescan-releases');
  const btnAuditAll = document.getElementById('btn-audit-all-releases');

  if (!card) return;

  if (task) {
    if (scanCompleteTimeout) {
      clearTimeout(scanCompleteTimeout);
      scanCompleteTimeout = null;
    }

    card.classList.remove('hidden');
    card.classList.remove('scan-complete');

    const titleEl = document.getElementById('lib-scan-progress-title');
    const pctEl = document.getElementById('lib-scan-progress-pct');
    const fillEl = document.getElementById('lib-scan-progress-fill');
    const subEl = document.getElementById('lib-scan-progress-sub');

    const pct = task.progress || 0;
    if (titleEl) {
      titleEl.innerHTML = `<span class="spinner-inline"></span> ${escapeHtml(task.name || 'Scanning library...')}`;
    }
    if (pctEl) pctEl.textContent = `${pct}%`;
    if (fillEl) fillEl.style.width = `${pct}%`;
    if (subEl) subEl.textContent = task.stage || 'Scanning audio files from disk...';

    if (btnRescan) btnRescan.classList.add('btn-spinning');
    if (btnAuditAll) btnAuditAll.classList.add('btn-spinning');
  } else {
    if (btnRescan) btnRescan.classList.remove('btn-spinning');
    if (btnAuditAll) btnAuditAll.classList.remove('btn-spinning');

    // If card was showing active progress, show completion state before hiding
    if (!card.classList.contains('hidden') && !card.classList.contains('scan-complete')) {
      card.classList.add('scan-complete');
      const titleEl = document.getElementById('lib-scan-progress-title');
      const pctEl = document.getElementById('lib-scan-progress-pct');
      const fillEl = document.getElementById('lib-scan-progress-fill');
      const subEl = document.getElementById('lib-scan-progress-sub');

      if (titleEl) titleEl.innerHTML = '✔ Scan Complete';
      if (pctEl) pctEl.textContent = '100%';
      if (fillEl) fillEl.style.width = '100%';
      if (subEl) subEl.textContent = 'Library releases up to date.';

      // Reload releases smoothly without flickering
      loadLibraryReleases(false);

      scanCompleteTimeout = setTimeout(() => {
        card.classList.add('hidden');
      }, 3000);
    }
  }
}

function setupLibraryReleases() {
  // Rescan button
  const btnRescan = document.getElementById('btn-rescan-releases');
  if (btnRescan) {
    btnRescan.addEventListener('click', async () => {
      const task = await startTask('library_scan', { force_rescan: true }, 'Rescan Music Library');
      if (task) {
        updateLibraryScanProgress(task);
      }
    });
  }

  // Audit All MB button
  const btnAuditAll = document.getElementById('btn-audit-all-releases');
  if (btnAuditAll) {
    btnAuditAll.addEventListener('click', async () => {
      const task = await startTask('library_audit_all', { force_refresh: true }, 'Audit All Releases (MusicBrainz)');
      if (task) {
        updateLibraryScanProgress(task);
      }
    });
  }



  // Filter buttons
  document.querySelectorAll('[data-filter-release]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-filter-release]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      AppState.releaseFilter = btn.getAttribute('data-filter-release');
      loadLibraryReleases(false);
    });
  });

  // Search input
  const searchInput = document.getElementById('lib-release-search');
  const clearBtn = document.getElementById('btn-clear-release-search');

  if (searchInput) {
    let searchDebounce = null;
    searchInput.addEventListener('input', (e) => {
      AppState.releaseSearchQuery = e.target.value;
      if (clearBtn) {
        clearBtn.classList.toggle('hidden', !e.target.value);
      }
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        loadLibraryReleases(false);
      }, 250);
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (searchInput) searchInput.value = '';
      clearBtn.classList.add('hidden');
      AppState.releaseSearchQuery = '';
      loadLibraryReleases(false);
    });
  }

  // Sort dropdown
  const sortSelect = document.getElementById('lib-release-sort');
  if (sortSelect) {
    sortSelect.addEventListener('change', (e) => {
      AppState.releaseSortBy = e.target.value;
      renderReleasesList();
    });
  }

  // Release action buttons
  const btnDownloadMissing = document.getElementById('btn-rel-download-missing');
  if (btnDownloadMissing) {
    btnDownloadMissing.addEventListener('click', () => {
      if (AppState.selectedReleaseData) {
        downloadMissingForRelease(AppState.selectedReleaseData);
      }
    });
  }

  const btnAuditMB = document.getElementById('btn-rel-audit-mb');
  if (btnAuditMB) {
    btnAuditMB.addEventListener('click', () => {
      auditSelectedRelease();
    });
  }

  const btnSearchSlsk = document.getElementById('btn-rel-search-soulseek');
  if (btnSearchSlsk) {
    btnSearchSlsk.addEventListener('click', () => {
      searchSoulseekForSelectedRelease();
    });
  }
}

async function loadLibraryReleases(refresh = false) {
  const masterList = document.getElementById('releases-master-list');
  if (!masterList) return;

  if (refresh) {
    masterList.innerHTML = '<div class="empty-state-card"><div class="spinner-inline"></div><div class="text-muted mt-2">Scanning library on disk...</div></div>';
  }

  try {
    const queryParams = new URLSearchParams({
      refresh: refresh ? 'true' : 'false',
      search: AppState.releaseSearchQuery || '',
      filter: AppState.releaseFilter || 'all',
    });
    const res = await fetch(`/api/library/releases?${queryParams.toString()}`);
    if (!res.ok) return;
    const data = await res.json();

    AppState.libraryReleases = data.releases || [];

    // Update Summary Header Cards
    if (data.summary) {
      const elTotal = document.getElementById('lib-rel-total-count');
      const elComp = document.getElementById('lib-rel-complete-count');
      const elMiss = document.getElementById('lib-rel-missing-count');
      const elTrkTotal = document.getElementById('lib-rel-tracks-total');
      const elMissSub = document.getElementById('lib-rel-missing-tracks-sub');

      if (elTotal) elTotal.textContent = data.summary.total_releases || 0;
      if (elComp) elComp.textContent = data.summary.complete_releases || 0;
      if (elMiss) elMiss.textContent = data.summary.has_missing_releases || 0;
      if (elTrkTotal) elTrkTotal.textContent = `${data.summary.total_local_tracks || 0} local tracks`;
      if (elMissSub) elMissSub.textContent = `${data.summary.total_missing_tracks || 0} missing tracks`;
    }

    renderReleasesList();

    // If an existing release was selected, ensure it remains rendered and updated with new data
    if (AppState.selectedReleaseId) {
      let updated = (data.releases || []).find((r) => r.id === AppState.selectedReleaseId);
      if (!updated && AppState.selectedReleaseData) {
        // Fallback match by title and artist in case ID shifted due to unification
        updated = (data.releases || []).find(
          (r) => r.title === AppState.selectedReleaseData.title && r.artist === AppState.selectedReleaseData.artist
        );
      }
      if (updated) {
        AppState.selectedReleaseId = updated.id;
        AppState.selectedReleaseData = updated;
        renderReleaseDetails(updated);
        // Refresh full audited details in background to ensure tracks match latest disk scan
        selectLibraryRelease(updated.id, true);
      } else if (AppState.selectedReleaseData) {
        renderReleaseDetails(AppState.selectedReleaseData);
      }
    }
  } catch (err) {
    console.error('Error loading library releases:', err);
    masterList.innerHTML = `<div class="empty-state-card text-red">Failed to load releases: ${escapeHtml(err.message)}</div>`;
  }
}

function renderReleasesList() {
  const masterList = document.getElementById('releases-master-list');
  if (!masterList) return;

  if (!AppState.libraryReleases || AppState.libraryReleases.length === 0) {
    masterList.innerHTML = '<div class="empty-state-card"><div class="text-muted">No releases matching current filters.</div></div>';
    return;
  }

  // Sort releases
  const sorted = [...AppState.libraryReleases].sort((a, b) => {
    const sortBy = AppState.releaseSortBy || 'artist';
    if (sortBy === 'title') {
      return (a.title || '').localeCompare(b.title || '');
    } else if (sortBy === 'year') {
      return (b.year || '').localeCompare(a.year || '');
    } else if (sortBy === 'missing') {
      return (b.missing_count || 0) - (a.missing_count || 0);
    } else if (sortBy === 'tracks') {
      return (b.found_count || 0) - (a.found_count || 0);
    }
    // Default: artist
    const artCmp = (a.artist || '').localeCompare(b.artist || '');
    if (artCmp !== 0) return artCmp;
    return (a.title || '').localeCompare(b.title || '');
  });

  masterList.innerHTML = sorted.map((r) => {
    const isComplete = r.status === 'complete' || (r.missing_count === 0 && r.found_count > 0);
    const badgeClass = isComplete ? 'badge-found' : 'badge-missing';
    const missingText = r.missing_count > 0 ? `${r.missing_count} missing` : (isComplete ? 'Complete' : `${r.found_count} tracks`);
    const isSelected = AppState.selectedReleaseId === r.id;

    return `
      <div class="release-list-item ${isSelected ? 'active' : ''}" onclick="selectLibraryRelease('${r.id}')">
        <div class="release-item-art">💿</div>
        <div class="release-item-details">
          <div class="release-item-title">${escapeHtml(r.title)}</div>
          <div class="release-item-artist">${escapeHtml(r.artist)} ${r.year ? `(${r.year})` : ''}</div>
          <div class="release-item-tags">
            <span class="badge ${badgeClass}">${missingText}</span>
            <span class="badge font-mono">${r.found_count}${r.total_tracks_expected > r.found_count ? ` / ${r.total_tracks_expected}` : ''} trks</span>
            ${(r.formats || []).map((f) => `<span class="badge">${escapeHtml(f)}</span>`).join('')}
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function selectLibraryRelease(releaseId, audit = true) {
  AppState.selectedReleaseId = releaseId;
  renderReleasesList();

  const placeholder = document.getElementById('release-empty-placeholder');
  const detailWrapper = document.getElementById('release-detail-wrapper');

  if (placeholder) placeholder.classList.add('hidden');
  if (detailWrapper) detailWrapper.classList.remove('hidden');

  // If we already have full audited data for this release in memory, render that immediately
  if (AppState.selectedReleaseData && AppState.selectedReleaseData.id === releaseId && AppState.selectedReleaseData.tracks && AppState.selectedReleaseData.tracks.length > 0) {
    renderReleaseDetails(AppState.selectedReleaseData);
  } else {
    // Otherwise find in master list for quick immediate display
    const localRel = AppState.libraryReleases.find((r) => r.id === releaseId);
    if (localRel) {
      renderReleaseDetails(localRel);
    }
  }

  // Fetch full audited details from server if requested
  try {
    const res = await fetch(`/api/library/releases/${releaseId}?audit=${audit ? 'true' : 'false'}`);
    if (!res.ok) return;
    const releaseData = await res.json();
    AppState.selectedReleaseData = releaseData;
    renderReleaseDetails(releaseData);

    // Sync master list release object with audited data
    const idx = AppState.libraryReleases.findIndex((r) => r.id === releaseId);
    if (idx !== -1) {
      AppState.libraryReleases[idx] = { ...AppState.libraryReleases[idx], ...releaseData };
      renderReleasesList();
    }
  } catch (err) {
    console.error('Error fetching release details:', err);
  }
}


function renderReleaseDetails(rel) {
  AppState.selectedReleaseData = rel;

  const elTitle = document.getElementById('rel-det-title');
  const elArtist = document.getElementById('rel-det-artist');
  const elYear = document.getElementById('rel-det-year');
  const elPath = document.getElementById('rel-det-path');
  const elMbid = document.getElementById('rel-det-mbid');

  if (elTitle) elTitle.textContent = rel.title || 'Unknown Title';
  if (elArtist) elArtist.textContent = rel.artist || 'Unknown Artist';
  if (elYear) elYear.textContent = rel.year ? `Year: ${rel.year}` : 'Year: -';
  if (elPath) elPath.textContent = `Folder: ${rel.folder_path || '-'}`;

  if (elMbid) {
    if (rel.mb_release_id) {
      elMbid.innerHTML = `<a href="https://musicbrainz.org/release/${rel.mb_release_id}" target="_blank" class="text-cyan">${rel.mb_release_id.slice(0, 8)}... ↗</a>`;
    } else {
      elMbid.textContent = 'Unlinked (Local match)';
    }
  }

  const isComplete = rel.status === 'complete' || (rel.missing_count === 0 && rel.found_count > 0);
  const statusBadge = document.getElementById('rel-det-badge-status');
  if (statusBadge) {
    statusBadge.className = `badge ${isComplete ? 'badge-found' : 'badge-missing'}`;
    statusBadge.textContent = isComplete ? '100% Complete' : `${rel.missing_count} Missing Tracks`;
  }

  const total = rel.total_tracks_expected || (rel.tracks ? rel.tracks.length : rel.found_count);
  const found = rel.found_count || (rel.tracks ? rel.tracks.filter((t) => t.status === 'found').length : 0);
  const pct = rel.completion_pct !== undefined ? rel.completion_pct : (total > 0 ? ((found / total) * 100).toFixed(0) : 100);

  const elTrackCounts = document.getElementById('rel-det-track-counts');
  const elMissHigh = document.getElementById('rel-det-missing-highlight');
  const elProgFill = document.getElementById('rel-det-progress-fill');

  if (elTrackCounts) elTrackCounts.textContent = `${found} / ${total} tracks (${pct}%)`;
  if (elMissHigh) {
    elMissHigh.textContent = rel.missing_count > 0 ? `${rel.missing_count} missing` : 'All tracks found';
    elMissHigh.className = rel.missing_count > 0 ? 'text-red' : 'text-green';
  }
  if (elProgFill) elProgFill.style.width = `${pct}%`;

  // Download All Missing Tracks button visibility/state
  const btnDownloadAll = document.getElementById('btn-rel-download-missing');
  const missingTracks = (rel.tracks || []).filter((t) => t.status === 'missing');
  if (btnDownloadAll) {
    btnDownloadAll.disabled = missingTracks.length === 0;
    btnDownloadAll.textContent = missingTracks.length > 0 ? `⚡ Download All Missing (${missingTracks.length})` : '✔ All Tracks Downloaded';
  }

  // Format pills
  const formatList = document.getElementById('rel-det-formats-list');
  if (formatList) {
    formatList.innerHTML = (rel.formats || []).map((f) => `<span class="badge font-mono">${escapeHtml(f)}</span>`).join('');
  }

  // Tracks Table
  const tbody = document.getElementById('tbody-release-tracks');
  if (!tbody) return;

  const tracks = rel.tracks || [];

  if (tracks.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No track data available. Click "Re-Audit with MusicBrainz" to fetch official tracklist.</td></tr>';
    return;
  }

  tbody.innerHTML = tracks.map((t, idx) => {
    const isFound = t.status === 'found';
    const trClass = isFound ? '' : 'track-row-missing';
    const titleClass = isFound ? 'track-title-found' : 'track-title-missing';
    const statusBadgeHtml = isFound
      ? `<span class="badge badge-found">✔ Found</span>`
      : `<span class="badge badge-missing">✖ Missing</span>`;

    const formatInfo = isFound
      ? `${escapeHtml(t.format || 'AUDIO')}${t.bitrate ? ` • ${t.bitrate} kbps` : ''}`
      : '<span class="text-muted">-</span>';

    const detailsInfo = isFound
      ? `<span class="text-truncate font-mono font-xs" style="max-width: 220px; display: inline-block;">${escapeHtml(t.filename || '-')}</span>`
      : '<span class="text-muted font-xs">Not in local library</span>';

    const trackArtist = t.artist || rel.artist;
    const showArtistInTitle = t.artist && (rel.artist.toLowerCase() === 'various artists' || t.artist.toLowerCase() !== rel.artist.toLowerCase());
    const displayTitle = showArtistInTitle
      ? `<span class="text-muted font-mono font-xs" style="margin-right: 4px;">${escapeHtml(t.artist)} -</span><strong class="${titleClass}">${escapeHtml(t.title)}</strong>`
      : `<strong class="${titleClass}">${escapeHtml(t.title)}</strong>`;

    const trkNum = t.track_number || t.track_num_int || (idx + 1);
    const actionButton = !isFound
      ? `<button class="btn btn-xs btn-primary" onclick="downloadSingleMissingTrack('${escapeHtml(rel.artist)}', '${escapeHtml(rel.title)}', '${escapeHtml(t.title)}', '${escapeHtml(t.artist || '')}', '${escapeHtml(trkNum)}')">⚡ Download</button>`
      : `<button class="btn btn-xs btn-outline" onclick="searchSoulseekForTrack('${escapeHtml(trackArtist)}', '${escapeHtml(t.title)}')">🔍 Search</button>`;

    return `
      <tr class="${trClass}">
        <td><span class="text-muted font-mono">${t.track_number || (idx + 1)}</span></td>
        <td>${displayTitle}</td>
        <td>${statusBadgeHtml}</td>
        <td>${formatInfo}</td>
        <td>${detailsInfo}</td>
        <td style="text-align: right;">${actionButton}</td>
      </tr>
    `;
  }).join('');
}

async function downloadMissingForRelease(rel) {
  if (!rel) return;
  // Use audited details from AppState if available to ensure accurate track titles
  let currentRel = rel;
  if (AppState.selectedReleaseData && AppState.selectedReleaseData.id === rel.id && AppState.selectedReleaseData.tracks) {
    currentRel = AppState.selectedReleaseData;
  }
  const missingTracks = (currentRel.tracks || []).filter((t) => t.status === 'missing');
  if (missingTracks.length === 0) {
    alert('All tracks for this release are already present in the library!');
    return;
  }

  switchTab('tasks');
  await startTask('release_missing_download', {
    artist: currentRel.artist,
    release_title: currentRel.title,
    missing_tracks: missingTracks,
    format: 'flac',
  }, `Download Missing: ${currentRel.artist} - ${currentRel.title}`);
}

async function downloadSingleMissingTrack(artist, releaseTitle, trackTitle, trackArtist = '', trackNumber = null) {
  // If trackTitle is still a generic placeholder, check if audited data is available
  if (/^Track\s+\d+\s*\(Missing\)$/i.test(trackTitle) && AppState.selectedReleaseData && AppState.selectedReleaseData.tracks) {
    const match = AppState.selectedReleaseData.tracks.find(
      (t) => String(t.track_number) === String(trackNumber) || String(t.track_num_int) === String(trackNumber)
    );
    if (match && match.title && !/^Track\s+\d+\s*\(Missing\)$/i.test(match.title)) {
      trackTitle = match.title;
      if (!trackArtist && match.artist) trackArtist = match.artist;
    }
  }

  switchTab('tasks');
  const displayName = trackArtist ? `${trackArtist} - ${trackTitle}` : `${artist} - ${trackTitle}`;
  await startTask('track_soulseek_download', {
    artist,
    release_title: releaseTitle,
    track_title: trackTitle,
    track_artist: trackArtist,
    track_number: trackNumber,
    format: 'flac',
  }, `Download Track: ${displayName}`);
}

function searchSoulseekForTrack(artist, trackTitle) {
  switchTab('soulseek');
  const queryEl = document.getElementById('slsk-query');
  if (queryEl) {
    if (artist && artist.toLowerCase() !== 'various artists') {
      queryEl.value = `${artist} ${trackTitle}`;
    } else {
      queryEl.value = `Various Artists ${trackTitle}`;
    }
  }
}

function searchSoulseekForSelectedRelease() {
  if (!AppState.selectedReleaseData) return;
  const rel = AppState.selectedReleaseData;
  switchTab('soulseek');
  const queryEl = document.getElementById('slsk-query');
  if (queryEl) {
    if (rel.artist && (rel.artist.toLowerCase() === 'various artists' || rel.is_va)) {
      queryEl.value = `Various Artists ${rel.title}`;
    } else {
      queryEl.value = `${rel.artist} ${rel.title}`;
    }
  }
}

async function auditSelectedRelease() {
  if (!AppState.selectedReleaseId) return;
  const relId = AppState.selectedReleaseId;
  const btn = document.getElementById('btn-rel-audit-mb');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Auditing...';
  }

  try {
    const res = await fetch(`/api/library/releases/${relId}/audit`, { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      if (data.release) {
        AppState.selectedReleaseId = data.release.id;
        AppState.selectedReleaseData = data.release;
        renderReleaseDetails(data.release);
        // Refresh master list item
        const idx = AppState.libraryReleases.findIndex((r) => r.id === data.release.id || r.id === relId);
        if (idx !== -1) {
          AppState.libraryReleases[idx] = data.release;
        } else {
          AppState.libraryReleases.push(data.release);
        }
        renderReleasesList();
      }
    } else {
      const errData = await res.json().catch(() => ({}));
      alert(`Audit error: ${errData.error || res.statusText}`);
    }
  } catch (err) {
    alert(`Audit error: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔍 Re-Audit with MusicBrainz';
    }
  }
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

// Global aliases for legacy / inline event handlers
window.selectRelease = selectLibraryRelease;
window.selectLibraryRelease = selectLibraryRelease;
