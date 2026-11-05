/* site_group_monitor 仪表盘交互脚本（原生 JS，无构建链）。
 * 依赖：本地 vendor/echarts.min.js（缺失时自动降级为表格 + 提示）。
 */
'use strict';

const state = {
  page: 1,
  size: 50,
  sort: 'domain',
  desc: false,
  group: '',
  keyword: '',
  indexStatus: '',
  health: '',
  total: 0,
  trendChart: null,
  diffChart: null,
  siteChart: null,
  hasEcharts: typeof window.echarts !== 'undefined',
  drawerId: '',
  checking: [],
  checkingKey: '',
  selected: new Set(),
  failedTotal: 0,
  checkLabel: '',
  statusTimer: null,
};

const el = (id) => document.getElementById(id);

/** 统一封装 fetch JSON 请求，自动剥离统一信封。 */
async function api(path, options) {
  const resp = await fetch(path, options || {});
  const payload = await resp.json().catch(() => ({ code: -1, message: '响应解析失败' }));
  if (payload.code !== 0) {
    throw new Error(payload.message || ('接口错误 code=' + payload.code));
  }
  return payload.data;
}

function buildQuery(params) {
  const search = new URLSearchParams();
  Object.keys(params).forEach((key) => {
    const value = params[key];
    if (value !== null && value !== undefined && value !== '') {
      search.append(key, value);
    }
  });
  return search.toString();
}

function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function statusTag(status) {
  const map = {
    indexed: ['ok', '已收录'],
    not_indexed: ['bad', '未收录'],
    unknown_need_manual_check: ['warn', '查询失败'],
  };
  const hit = map[status] || ['gray', status || '-'];
  return `<span class="tag ${hit[0]}">${hit[1]}</span>`;
}

function statusLabel(status) {
  const map = {
    indexed: '已收录',
    not_indexed: '未收录',
    unknown_need_manual_check: '查询失败',
  };
  return map[status] || status || '-';
}

function healthClass(health, httpStatus) {
  if (health) return 'row-' + health;
  if (httpStatus !== null && httpStatus >= 400) return 'row-abnormal';
  return '';
}

/* ------------------------------------------------------------------ 数据加载 */
async function loadOverview() {
  try {
    const data = await api('/api/overview');
    el('cTotal').textContent = data.total_sites;
    el('cIndexed').textContent = data.indexed_count ?? 0;
    el('cNotIndexed').textContent = data.not_indexed_count ?? 0;
    el('cUnknown').textContent = data.unknown_count ?? 0;
    state.failedTotal = Number(data.unknown_count || 0);
    updateSelectedButton();
    const unchecked = data.unchecked_count ?? 0;
    el('cTotalHint').textContent = unchecked
      ? `未检测 ${unchecked} · 点击查看全部`
      : '点击查看全部';
    el('cRate').textContent = (data.index_rate * 100).toFixed(2) + '%';
    el('cRateHint').textContent = '不含查询失败';
    el('cAnomaly').textContent = data.anomalous_count;
    el('cSslHint').textContent = '网站打不开或状态码异常';
    const delta = Number(data.delta_vs_yesterday || 0);
    const deltaText = (delta >= 0 ? '+' : '') + (delta * 100).toFixed(2) + ' pt';
    el('cDelta').textContent = deltaText;
    el('cDelta').className = 'value ' + (delta >= 0 ? 'ok' : 'bad');
    const rate = data.proxy_total ? data.proxy_health_rate : 0;
    el('cProxy').textContent = data.proxy_total
      ? (rate * 100).toFixed(0) + '%'
      : '未配置代理';
    el('cProxyHint').textContent = data.proxy_total
      ? `可用 ${data.proxy_enabled}/${data.proxy_total}`
      : '代理池为空，已降级直连（速率受限）';
    markOverviewFilter();
    const run = data.last_run || {};
    el('cRun').textContent = run.started_at
      ? `${String(run.mode || '')} · ${escapeHtml(String(run.started_at).replace('T', ' ').replace('Z', ''))}`
      : '暂无记录';
  } catch (error) {
    console.warn('overview failed', error);
  }
}

function filterByStatus(status) {
  state.indexStatus = status || '';
  state.page = 1;
  el('statusFilter').value = state.indexStatus;
  markOverviewFilter();
  loadSites();
}

function markOverviewFilter() {
  document.querySelectorAll('.card-filter').forEach((card) => {
    card.classList.toggle('active', (card.getAttribute('data-status') || '') === (state.indexStatus || ''));
  });
}

function updateCheckProgress(progress, running) {
  const banner = el('checkBanner');
  const currentList = (progress.current || []).filter(Boolean);
  const current = currentList.join('、');
  const msg = progress.message || '';
  const url = progress.browser_url || '';
  state.checking = currentList;
  if (running) {
    el('cProgress').textContent = progress.total
      ? `进度 ${progress.done || 0}/${progress.total} · 正在查 ${current || '准备中'} · 未知 ${progress.unknown || 0} · 异常 ${progress.failed || 0}`
      : (msg || '检测准备中…');
    banner.classList.remove('hidden');
    banner.textContent = [
      msg || '正在查询收录、TDK 和网站能否打开',
      current ? `当前站点：${current}` : '',
      progress.network ? `出口：${progress.network}` : '',
      url ? `页面：${url}` : '',
    ].filter(Boolean).join('  ·  ');
  } else {
    el('cProgress').textContent = progress.total
      ? `进度 ${progress.done}/${progress.total} · 未知 ${progress.unknown || 0} · 异常 ${progress.failed || 0}`
      : '空闲';
    banner.classList.add('hidden');
    banner.textContent = '';
    state.checking = [];
  }
  markCheckingRows();
}

function markCheckingRows() {
  const set = new Set((state.checking || []).map((item) => String(item).toLowerCase()));
  const key = Array.from(set).sort().join(',');
  let first = null;
  document.querySelectorAll('#siteRows tr[data-domain]').forEach((row) => {
    const domain = (row.getAttribute('data-domain') || '').toLowerCase();
    const checking = set.has(domain);
    row.classList.toggle('row-checking', checking);
    if (checking && !first) first = row;
  });
  if (first && key && key !== state.checkingKey) {
    first.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  state.checkingKey = key;
}

async function loadStatus() {
  let nextDelay = 15000;
  try {
    const data = await api('/api/system/status');
    const progress = data.progress || {};
    const version = String(data.version || '').replace(/^v/i, '');
    if (version) {
      const versionEl = el('appVersion');
      if (versionEl) versionEl.textContent = `v${version}`;
      document.title = `站群监控 v${version}`;
    }
    const running = progress.phase === 'running';
    nextDelay = running ? 2000 : 15000;
    updateCheckProgress(progress, running);
    if (running) {
      el('btnCheck').disabled = true;
      el('btnCheck').textContent = '检测中…';
      el('btnCheckSelected').disabled = true;
      el('btnCancelCheck').classList.remove('hidden');
    } else if (!_checkPolling) {
      resetCheckButton();
    }
    const mail = data.mail || {};
    if (!mail.enabled) {
      el('cRun').title = '邮件推送未启用：' + (mail.reason || '');
    }
  } catch (error) {
    console.warn('status failed', error);
    nextDelay = 30000;
  } finally {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = window.setTimeout(loadStatus, nextDelay);
  }
}

function renderTrend(data) {
  if (!state.hasEcharts) return;
  const series = (data.series || []);
  const dates = series.map((item) => item.stat_date);
  const rates = series.map((item) => Number(((item.rate || 0) * 100).toFixed(2)));
  if (!state.trendChart) {
    state.trendChart = echarts.init(el('trendChart'));
  }
  state.trendChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 45, right: 20, top: 30, bottom: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' } },
    series: [
      {
        name: '收录率',
        type: 'line',
        smooth: true,
        areaStyle: { opacity: 0.15 },
        data: rates,
        itemStyle: { color: '#36a2eb' },
      },
    ],
  });
}

function renderDiff(data) {
  if (!state.hasEcharts) return;
  const diffs = (data.diffs || []);
  const dates = diffs.map((item) => item.stat_date);
  const added = diffs.map((item) => item.new_indexed || 0);
  const lost = diffs.map((item) => item.lost_indexed || 0);
  if (!state.diffChart) {
    state.diffChart = echarts.init(el('diffChart'));
  }
  state.diffChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['新增收录', '掉收录'], top: 4 },
    grid: { left: 45, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      { name: '新增收录', type: 'bar', data: added, itemStyle: { color: '#2e7d32' } },
      { name: '掉收录', type: 'bar', data: lost, itemStyle: { color: '#c62828' } },
    ],
  });
}

async function loadTrend() {
  try {
    const days = Number(el('trendDays').textContent || 30);
    const data = await api('/api/overview/trend?days=' + days);
    renderTrend(data);
    renderDiff(data);
  } catch (error) {
    console.warn('trend failed', error);
  }
}

async function loadSites() {
  const params = {
    page: state.page,
    size: state.size,
    sort: state.sort,
    desc: state.desc,
    group: state.group,
    keyword: state.keyword,
    index_status: state.indexStatus,
    health: state.health,
  };
  try {
    const data = await api('/api/sites?' + buildQuery(params));
    state.total = data.total;
    el('pageInfo').textContent = `第 ${data.page} 页 / 共 ${Math.max(1, Math.ceil(data.total / data.size))} 页（${data.total} 条）`;
    el('tableHint').textContent = `共 ${data.total} 个站点${state.selected.size ? ` · 已选 ${state.selected.size}` : ''}`;
    const rows = (data.items || []).map((item) => `
      <tr class="${healthClass(item.health, item.http_status)}" data-id="${item.id}" data-domain="${escapeHtml(item.domain)}">
        <td class="check"><input type="checkbox" class="site-pick" ${state.selected.has(item.domain) ? 'checked' : ''}/></td>
        <td class="domain">${escapeHtml(item.domain)}<span class="checking-badge">查询中</span></td>
        <td>${escapeHtml(item.group_name || '')}</td>
        <td>${statusTag(item.index_status)}</td>
        <td class="ellipsis" title="${escapeHtml(item.title || '')}">${escapeHtml((item.title || '-').slice(0, 36))}</td>
        <td>${item.http_status === null ? '-' : item.http_status}</td>
        <td>${item.response_ms === null ? '-' : item.response_ms}</td>
        <td class="hint">${escapeHtml(item.checked_at || item.last_checked_at || '-')}</td>
        <td class="site-actions">
          <button type="button" class="btn small site-check-one">检测</button>
          <button type="button" class="btn small danger site-del">删除</button>
        </td>
      </tr>`);
    el('siteRows').innerHTML = rows.join('') || '<tr><td colspan="9" class="empty">没有匹配的站点</td></tr>';
    Array.from(document.querySelectorAll('#siteRows tr[data-id]')).forEach((row) => {
      const siteId = row.getAttribute('data-id');
      const domain = row.getAttribute('data-domain') || '';
      row.addEventListener('click', () => openDrawer(siteId));
      const pick = row.querySelector('.site-pick');
      if (pick) {
        pick.addEventListener('click', (event) => event.stopPropagation());
        pick.addEventListener('change', (event) => {
          event.stopPropagation();
          toggleSelected(domain, pick.checked);
        });
      }
      const oneBtn = row.querySelector('.site-check-one');
      if (oneBtn) {
        oneBtn.addEventListener('click', (event) => {
          event.stopPropagation();
          triggerCheck([domain]);
        });
      }
      const delBtn = row.querySelector('.site-del');
      if (delBtn) {
        delBtn.addEventListener('click', (event) => {
          event.stopPropagation();
          deleteSite(siteId, domain);
        });
      }
    });
    syncSelectAllBox();
    updateSelectedButton();
  } catch (error) {
    el('siteRows').innerHTML = `<tr><td colspan="9" class="empty">加载失败：${escapeHtml(error.message)}</td></tr>`;
  }
  markCheckingRows();
}

function toggleSelected(domain, checked) {
  if (!domain) return;
  if (checked) state.selected.add(domain);
  else state.selected.delete(domain);
  updateSelectedButton();
  syncSelectAllBox();
  el('tableHint').textContent = `共 ${state.total} 个站点${state.selected.size ? ` · 已选 ${state.selected.size}` : ''}`;
}

function selectedDomains() {
  return Array.from(state.selected);
}

function updateSelectedButton() {
  const btn = el('btnCheckSelected');
  if (!btn) return;
  const count = state.selected.size;
  btn.disabled = count === 0 || _checkPolling;
  btn.textContent = count ? `检测选中 (${count})` : '检测选中';
  const copyBtn = el('btnCopySelected');
  if (copyBtn) {
    copyBtn.disabled = count === 0;
    copyBtn.textContent = count ? `复制选中 (${count})` : '复制选中';
  }
  const retryBtn = el('btnRetryFailed');
  if (retryBtn) {
    retryBtn.disabled = state.failedTotal === 0 || _checkPolling;
    retryBtn.textContent = state.failedTotal ? `重查失败 (${state.failedTotal})` : '重查失败';
  }
}

function syncSelectAllBox() {
  const box = el('checkAllPage');
  if (!box) return;
  const picks = Array.from(document.querySelectorAll('#siteRows .site-pick'));
  box.checked = picks.length > 0 && picks.every((item) => item.checked);
}

async function loadGroups() {
  try {
    const groups = await api('/api/sites/groups');
    const select = el('groupFilter');
    // 保留默认「全部分组」选项，清空后重新渲染，保证可重复刷新不重复追加
    select.innerHTML = '<option value="">全部分组</option>';
    groups.forEach((item) => {
      const option = document.createElement('option');
      option.value = item.group_name;
      option.textContent = `${item.group_name} (${item.count})`;
      select.appendChild(option);
    });
  } catch (error) {
    console.warn('groups failed', error);
  }
}

/* ------------------------------------------------------------------ 详情抽屉 */
function kv(pairs) {
  return pairs
    .map((item) => {
      const span = item[2] ? ' kv-span' : '';
      return `<div class="kv-item${span}"><div class="kv-key">${escapeHtml(item[0])}</div><div class="kv-val">${escapeHtml(item[1] ?? '-')}</div></div>`;
    })
    .join('');
}

function closeDrawer() {
  el('drawer').classList.add('hidden');
  el('mask').classList.add('hidden');
  state.drawerId = '';
}

async function deleteSite(siteId, domain) {
  const label = domain || siteId;
  if (!confirm(`确认删除站点 ${label}？检测记录会一并清除。`)) return;
  try {
    await api(`/api/sites/${encodeURIComponent(siteId)}`, { method: 'DELETE' });
    if (String(state.drawerId) === String(siteId)) closeDrawer();
    await Promise.all([loadSites(), loadOverview(), loadGroups()]);
  } catch (error) {
    alert('删除失败：' + error.message);
  }
}

async function openDrawer(siteId) {
  try {
    const data = await api(`/api/sites/${siteId}?days=60`);
    state.drawerId = String(siteId);
    el('drawerDomain').textContent = data.site.domain;
    el('drawerMeta').textContent = `${data.site.group_name || 'default'} · ${(data.site.tags || []).join(' / ') || '无标签'}`;
    el('drawerDelete').setAttribute('data-id', String(data.site.id || siteId));
    el('drawerDelete').setAttribute('data-domain', data.site.domain || '');
    el('drawerSummary').innerHTML = kv([
      ['收录状态', statusLabel(data.latest.index_status)],
      ['原因码', data.latest.index_reason],
      ['HTTP 状态', data.latest.http_status],
      ['响应时间(ms)', data.latest.response_ms],
      ['检测时间', data.latest.checked_at],
      ['本次出口', data.latest.network || (data.latest.anomalies && data.latest.anomalies.network) || '未记录'],
      ['最终 URL', data.latest.final_url, true],
    ]);
    const evidenceBox = el('drawerEvidence');
    const kind = data.latest.evidence_kind || '';
    if (kind === 'screenshot') {
      evidenceBox.innerHTML =
        `<div class="evidence-note">百度检索结果截图</div>` +
        `<img class="evidence-img" alt="收录证据截图" src="/api/sites/${encodeURIComponent(siteId)}/evidence?t=${Date.now()}"/>`;
    } else if (data.latest.evidence_path) {
      evidenceBox.innerHTML =
        `<div class="evidence-note">百度检索页 HTML 快照（当时未能截图）</div>` +
        `<a class="hint" href="/api/sites/${encodeURIComponent(siteId)}/evidence" target="_blank">下载证据文件</a>`;
    } else {
      evidenceBox.innerHTML = '<div class="empty">暂无收录证据</div>';
    }
    const tdk = data.latest.tdk || {};
    el('drawerTdk').innerHTML = kv([
      ['Title', tdk.title, true],
      ['Description', tdk.description, true],
      ['Keywords', tdk.keywords, true],
      ['页面编码', tdk.encoding],
    ]);
    const chain = data.latest.redirect_chain || [];
    el('drawerChain').innerHTML = chain.length
      ? chain
          .map((hop) => `<li>${hop.status || '-'} → ${escapeHtml(hop.url || '')} ${hop.location ? ' ⇒ ' + escapeHtml(hop.location) : ''}</li>`)
          .join('')
      : '<li class="empty">无重定向</li>';
    const events = data.events || [];
    el('drawerEvents').innerHTML = events.length
      ? events
          .map((event) => `<li><span class="tag gray">${escapeHtml(event.event_type)}</span> ${escapeHtml(event.stat_date)} ${escapeHtml(event.new_value || '')}</li>`)
          .join('')
      : '<li class="empty">暂无事件</li>';
    el('drawer').classList.remove('hidden');
    el('mask').classList.remove('hidden');
    renderSiteChart(data.history || []);
    if (state.siteChart) {
      setTimeout(() => state.siteChart.resize(), 40);
    }
  } catch (error) {
    alert('详情加载失败：' + error.message);
  }
}

function renderSiteChart(history) {
  if (!state.hasEcharts) return;
  const rows = (history || []).slice().reverse();
  const labels = rows.map((item) => item.checked_at);
  const response = rows.map((item) => item.response_ms || 0);
  if (!state.siteChart) {
    state.siteChart = echarts.init(el('siteChart'));
  }
  state.siteChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['响应时间(ms)'], top: 2, itemHeight: 8 },
    grid: { left: 50, right: 20, top: 34, bottom: 34 },
    xAxis: { type: 'category', data: labels, axisLabel: { rotate: 30, fontSize: 9 } },
    yAxis: [{ type: 'value', name: 'ms' }],
    series: [
      { name: '响应时间(ms)', type: 'line', data: response, smooth: true, itemStyle: { color: '#36a2eb' } },
    ],
  });
}

/* ------------------------------------------------------------------ 操作 */
let _checkPolling = false;
let _lastSitesReload = 0;
let _checkSeenRunning = false;
let _checkStartedAt = 0;
let _checkCancelRequested = false;

async function triggerCheck(domains, options) {
  if (_checkPolling) return;
  const selected = Array.isArray(domains) ? domains.filter(Boolean) : [];
  const all = selected.length === 0;
  const opts = options || {};
  const confirmText = opts.confirmText || (all
    ? `确认检测全部 ${state.total || ''} 个站点？`
    : `确认检测选中的 ${selected.length} 个站点？`);
  if (!confirm(confirmText)) return;
  try {
    await api('/api/system/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: all ? 'full' : 'manual_single',
        export: all,
        notify: false,
        domains: selected,
      }),
    });
    state.checkLabel = opts.label || (all ? '全部站点' : `选中的 ${selected.length} 个站点`);
    // 接口「启动即返回」，进入进度轮询
    startCheckPolling();
  } catch (error) {
    const message = (error && error.message) || '未知错误';
    if (message.includes('已有检测任务')) {
      alert('已有检测任务在运行，请等待当前检测完成后再触发。');
    } else {
      alert('检测启动失败：' + message);
    }
    resetCheckButton();
  }
}

function startCheckPolling() {
  if (_checkPolling) return;
  _checkPolling = true;
  _checkCancelRequested = false;
  _checkSeenRunning = false;
  _checkStartedAt = Date.now();
  el('btnCheck').disabled = true;
  el('btnCheck').textContent = '检测中…';
  el('btnCheckSelected').disabled = true;
  el('btnRetryFailed').disabled = true;
  el('btnCancelCheck').classList.remove('hidden');
  pollCheckStatus(0);
}

function stopCheckPolling() {
  _checkPolling = false;
  resetCheckButton();
}

function resetCheckButton() {
  el('btnCheck').disabled = false;
  el('btnCheck').textContent = '全部检测';
  const cancelButton = el('btnCancelCheck');
  cancelButton.disabled = false;
  cancelButton.textContent = '取消检测';
  cancelButton.classList.add('hidden');
  updateSelectedButton();
}

/** 每 2 秒轮询进度；连续 3 次失败才报错退出轮询，避免瞬时抖动误报。 */
async function pollCheckStatus(failCount) {
  if (!_checkPolling) return;
  let consecutiveFail = failCount || 0;
  try {
    const data = await api('/api/system/status');
    consecutiveFail = 0;
    const progress = data.progress || {};
    const scheduler = data.scheduler || {};
    const isCheckRunning = progress.phase === 'running';
    const isPipelineRunning = scheduler.pipeline_running === true;
    if (isCheckRunning || isPipelineRunning) _checkSeenRunning = true;
    updateCheckProgress(progress, isCheckRunning || isPipelineRunning);
    // 详细数字已显示在进度卡和提示条；按钮保持固定文案，避免工具栏随数字宽度反复跳动。
    el('btnCheck').textContent = '检测中…';

    if (!isCheckRunning && !isPipelineRunning) {
      // 启动接口先返回、后台线程后置位，首次轮询可能短暂看到 idle；避免误报“完成”。
      if (!_checkSeenRunning && Date.now() - _checkStartedAt < 5000) {
        setTimeout(() => pollCheckStatus(0), 500);
        return;
      }
      const done = Number(progress.done || 0);
      const total = Number(progress.total || 0);
      const unknown = Number(progress.unknown || 0);
      const label = state.checkLabel || '本轮站点';
      let allUnknown = unknown;
      try {
        const overview = await api('/api/overview');
        allUnknown = Number(overview.unknown_count || 0);
        state.failedTotal = allUnknown;
      } catch (error) {
        console.warn('overview after check failed', error);
      }
      state.selected.clear();
      state.checkLabel = '';
      stopCheckPolling();
      refreshAll();
      if (_checkCancelRequested) {
        alert(`检测已取消：已处理 ${done}/${total}，其余站点未再执行。`);
      } else {
        alert(`${label}检测完成：已处理 ${done}/${total}；本轮查询失败 ${unknown} 个；当前全部查询失败 ${allUnknown} 个。\n可点击“重查失败”再次查询剩余失败站点。`);
      }
      return;
    }
    const now = Date.now();
    if (now - _lastSitesReload > 2000) {
      _lastSitesReload = now;
      loadSites();
    }
    setTimeout(() => pollCheckStatus(0), 1000);
  } catch (error) {
    consecutiveFail += 1;
    if (consecutiveFail >= 3) {
      stopCheckPolling();
      alert('检测进度获取失败（已连续 3 次）：' + ((error && error.message) || '未知错误'));
      return;
    }
    setTimeout(() => pollCheckStatus(consecutiveFail), 1000);
  }
}

async function loadAllFailedDomains() {
  const result = [];
  let page = 1;
  let total = 0;
  do {
    const data = await api('/api/sites?' + buildQuery({
      index_status: 'unknown_need_manual_check', page, size: 500, sort: 'domain', desc: false,
    }));
    const items = data.items || [];
    items.forEach((item) => {
      if (item.domain) result.push(String(item.domain).toLowerCase());
    });
    total = Number(data.total || 0);
    page += 1;
  } while (result.length < total);
  return Array.from(new Set(result));
}

async function retryFailedSites() {
  if (_checkPolling) return;
  const btn = el('btnRetryFailed');
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '正在读取…';
  try {
    const domains = await loadAllFailedDomains();
    if (!domains.length) {
      state.failedTotal = 0;
      updateSelectedButton();
      alert('当前没有查询失败的站点。');
      return;
    }
    await triggerCheck(domains, {
      label: `${domains.length} 个查询失败站点`,
      confirmText: `确认重新查询全部 ${domains.length} 个失败站点？`,
    });
  } catch (error) {
    alert('读取失败站点失败：' + ((error && error.message) || '未知错误'));
  } finally {
    if (!_checkPolling) {
      btn.textContent = oldText;
      updateSelectedButton();
    }
  }
}

async function copySelectedDomains() {
  const domains = selectedDomains();
  if (!domains.length) return;
  const text = domains.join('\n');
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      if (!document.execCommand('copy')) throw new Error('浏览器拒绝复制');
      area.remove();
    }
    const btn = el('btnCopySelected');
    btn.textContent = `已复制 ${domains.length} 个`;
    window.setTimeout(updateSelectedButton, 1500);
  } catch (error) {
    alert('复制失败：' + ((error && error.message) || '请手动复制'));
  }
}

async function cancelCheck() {
  try {
    _checkCancelRequested = true;
    const button = el('btnCancelCheck');
    button.disabled = true;
    button.textContent = '正在取消…';
    await api('/api/system/cancel', { method: 'POST' });
    el('cProgress').textContent = '正在停止浏览器请求并清空待检测队列…';
  } catch (error) {
    _checkCancelRequested = false;
    const button = el('btnCancelCheck');
    button.disabled = false;
    button.textContent = '取消检测';
    alert('取消失败：' + ((error && error.message) || '未知错误'));
  }
}


function downloadReport(fmt) {
  window.location.href = `/api/reports/today/download?fmt=${fmt}`;
}

function refreshAll() {
  loadOverview();
  loadTrend();
  loadSites();
  loadStatus();
}

/* ------------------------------------------------------------------ 添加域名 */
/** 解析多行/逗号/空格分隔的域名文本，去重（忽略大小写）并过滤空行。 */
function parseDomains(text) {
  const seen = new Set();
  const result = [];
  String(text || '')
    .split(/[\s,，]+/)
    .forEach((raw) => {
      let domain = raw.trim().toLowerCase();
      domain = domain.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
      if (!domain) return;
      if (seen.has(domain)) return;
      seen.add(domain);
      result.push(domain);
    });
  return result;
}

function openAddModal() {
  el('domainInput').value = '';
  el('groupInput').value = '';
  el('addResult').classList.add('hidden');
  el('addResult').innerHTML = '';
  el('addProgress').textContent = '';
  el('addSiteModal').classList.remove('hidden');
}

function closeAddModal() {
  el('addSiteModal').classList.add('hidden');
}

/** 逐条提交域名，统计成功/失败并展示失败明细，最后刷新列表与分组下拉。 */
async function submitAddSites() {
  const domains = parseDomains(el('domainInput').value);
  const group = el('groupInput').value.trim();
  const resultEl = el('addResult');
  const submitBtn = el('addSubmit');
  const progress = el('addProgress');

  if (domains.length === 0) {
    resultEl.classList.remove('hidden');
    resultEl.innerHTML = '<div class="hint">请先粘贴至少一个有效域名。</div>';
    return;
  }

  submitBtn.disabled = true;
  progress.textContent = '提交中…';
  const failures = [];
  let successCount = 0;

  for (let i = 0; i < domains.length; i++) {
    const domain = domains[i];
    progress.textContent = `处理中 ${i + 1}/${domains.length}（${domain}）`;
    try {
      await api('/api/sites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(group ? { domain, group } : { domain }),
      });
      successCount += 1;
    } catch (error) {
      failures.push({ domain, reason: (error && error.message) || '未知错误' });
    }
  }

  submitBtn.disabled = false;
  progress.textContent = '';

  let html = `<div class="add-summary">成功 <b>${successCount}</b> 个，失败 <b>${failures.length}</b> 个。</div>`;
  if (failures.length) {
    html += '<div class="add-fail-title">失败明细：</div><ul class="add-fail-list">';
    failures.forEach((item) => {
      html += `<li><span class="domain">${escapeHtml(item.domain)}</span> — ${escapeHtml(item.reason)}</li>`;
    });
    html += '</ul>';
  }
  resultEl.innerHTML = html;
  resultEl.classList.remove('hidden');

  // 复用现有刷新逻辑：刷新站点列表与分组筛选下拉
  loadSites();
  loadGroups();
}

/* ------------------------------------------------------------------ 代理配置 */
function sourceLabel(source) {
  return { manual: '手动', tunnel: '隧道', batch: '批量', api: 'API' }[source] || source || '-';
}

function sourceBadge(source) {
  const map = {
    manual: ['src-manual', '手动'],
    tunnel: ['src-tunnel', '隧道'],
    batch: ['src-batch', '批量'],
    api: ['src-api', 'API'],
  };
  const hit = map[source] || ['gray', source || '-'];
  return `<span class="tag ${hit[0]}">${hit[1]}</span>`;
}

function openProxyModal() {
  loadProxies();
  loadApiSources();
  loadBrowserPref();
  el('proxyResult').classList.add('hidden');
  el('proxyProgress').textContent = '';
  el('proxyModal').classList.remove('hidden');
}

function syncBrowserWindowToggle() {
  const enabled = !!el('browserEnabled').checked;
  el('browserShowWindow').disabled = !enabled;
  el('browserWindows').disabled = !enabled;
  if (el('browserViaProxy')) el('browserViaProxy').disabled = !enabled;
  updateProxyModeHint();
}

function updateProxyModeHint() {
  const hint = el('proxyModeHint');
  if (!hint) return;
  const enabled = !!el('browserEnabled').checked;
  const viaProxy = !!el('browserViaProxy').checked;
  if (!enabled) {
    hint.textContent = '未开本机浏览器时，收录/TDK/打开网站都会走代理池（池空则直连）。检测进度和站点详情里的「本次出口」会写明实际 IP。';
    return;
  }
  if (viaProxy) {
    hint.textContent = '浏览器会套代理池 IP 去查百度。换 IP 可能要重启查询窗口。进度条和站点详情会显示出口 IP / 位置 / 延迟。';
    return;
  }
  hint.textContent = '代理池用于浏览器查询百度、抓取 TDK 和检测网站状态；没有可用代理时才会直连降级。';
}

function parseWindowCount(value) {
  const count = parseInt(value, 10);
  if (!Number.isFinite(count)) return 1;
  return Math.max(1, Math.min(8, count));
}

async function loadBrowserPref() {
  try {
    const data = await api('/api/system/settings');
    const cfg = (data.engine && data.engine.baidu && data.engine.baidu.browser) || {};
    el('browserEnabled').checked = !!cfg.enabled;
    el('browserShowWindow').checked = cfg.headless === false || cfg.headless === undefined;
    el('browserWindows').value = String(parseWindowCount(cfg.windows));
    if (el('browserViaProxy')) el('browserViaProxy').checked = !!cfg.via_proxy;
    syncBrowserWindowToggle();
  } catch (_err) {
    /* 设置读失败时保持当前勾选 */
  }
}

async function saveBrowserPref() {
  const enabled = !!el('browserEnabled').checked;
  const headless = !el('browserShowWindow').checked;
  const via_proxy = !!(el('browserViaProxy') && el('browserViaProxy').checked);
  const windows = parseWindowCount(el('browserWindows').value);
  el('browserWindows').value = String(windows);
  syncBrowserWindowToggle();
  try {
    await api('/api/system/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patch: { engine: { baidu: { browser: { enabled, headless, via_proxy, windows } } } } }),
    });
    let windowHint = '已关闭本机浏览器查收录。';
    if (enabled) {
      const vis = headless ? `后台 ${windows} 个窗口` : `弹出 ${windows} 个窗口`;
      const net = via_proxy ? '浏览器查询、TDK 和网站状态统一走代理池 IP' : '浏览器暂时直连（仅建议排障时使用）';
      windowHint = `已开启本机浏览器（${vis}；${net}）。`;
    }
    showProxyResult(`<div class="hint">${windowHint}下次检测开始后生效。</div>`);
  } catch (err) {
    showProxyResult(`<div class="hint">${escapeHtml(err.message || String(err))}</div>`);
  }
}

function closeProxyModal() {
  el('proxyModal').classList.add('hidden');
}

function showProxyResult(html) {
  const box = el('proxyResult');
  box.innerHTML = html;
  box.classList.remove('hidden');
}

async function loadProxies() {
  try {
    const data = await api('/api/system/proxies');
    const stats = data;
    el('proxyStats').innerHTML = [
      ['总数', stats.total],
      ['可用', stats.enabled],
      ['禁用', stats.disabled],
      ['健康', stats.healthy],
      ['手动', stats.manual_count || 0],
      ['隧道', stats.tunnel_count || 0],
      ['批量', stats.batch_count || 0],
      ['API', stats.api_count || 0],
    ].map((it) => `<div class="stat">${it[0]}<b>${it[1]}</b></div>`).join('');

    const items = (stats.items || []);
    el('proxyRows').innerHTML = items.length
      ? items.map((p) => `
        <tr data-id="${p.id}">
          <td class="domain">${escapeHtml(p.url)}</td>
          <td>${escapeHtml(p.protocol)}</td>
          <td>${sourceBadge(p.source)}</td>
          <td class="proxy-ip">${escapeHtml(p.last_egress_ip || '-')}</td>
          <td title="${escapeHtml(p.last_probe_error || '')}">${escapeHtml(p.last_location || (p.last_probe_error ? '探测失败' : '-'))}</td>
          <td>${formatLatency(p.last_latency_ms)}</td>
          <td>${p.disabled ? '<span class="tag disabled">已禁用</span>' : '正常'}</td>
          <td class="proxy-actions">
            <button class="btn small ghost" data-act="probe">测速</button>
            <button class="btn small ghost" data-act="toggle">${p.disabled ? '启用' : '禁用'}</button>
            <button class="btn small ghost" data-act="del">删除</button>
          </td>
        </tr>`).join('')
      : '<tr><td colspan="8" class="empty">代理池为空。API 源请点「立即刷新」拉取 IP。</td></tr>';

    Array.from(document.querySelectorAll('#proxyRows tr[data-id]')).forEach((row) => {
      const id = Number(row.getAttribute('data-id'));
      const toggleBtn = row.querySelector('[data-act="toggle"]');
      const delBtn = row.querySelector('[data-act="del"]');
      const probeBtn = row.querySelector('[data-act="probe"]');
      if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
          const disabled = toggleBtn.textContent.trim() === '禁用';
          toggleProxy(id, disabled);
        });
      }
      if (delBtn) delBtn.addEventListener('click', () => deleteProxy(id));
      if (probeBtn) probeBtn.addEventListener('click', () => probeProxy(id));
    });
  } catch (error) {
    el('proxyRows').innerHTML = `<tr><td colspan="8" class="empty">加载失败：${escapeHtml(error.message)}</td></tr>`;
  }
}

function formatLatency(ms) {
  if (ms === null || ms === undefined || ms === '') return '-';
  const value = Number(ms);
  if (Number.isNaN(value)) return '-';
  const cls = value >= 800 ? 'latency-slow' : 'latency-ok';
  return `<span class="${cls}">${value} ms</span>`;
}

async function loadApiSources() {
  try {
    const sources = await api('/api/system/proxies/api-sources');
    el('apiRows').innerHTML = sources.length
      ? sources.map((s) => `
        <tr data-id="${s.id}">
          <td title="${escapeHtml(s.name || '')}">${escapeHtml(s.name || 'API')}</td>
          <td class="api-url" title="${escapeHtml(s.url)}">${escapeHtml(s.url)}</td>
          <td class="api-strategy-cell">
            <select class="api-strategy" data-act="strategy" title="选择后立即保存">
              <option value="captcha" ${s.rotate_per_check ? '' : 'selected'}>验证码/风控时切换</option>
              <option value="each" ${s.rotate_per_check ? 'selected' : ''}>每查询一个站切换</option>
            </select>
          </td>
          <td>${s.proxy_count || 0}</td>
          <td class="hint" title="${escapeHtml(s.last_error || s.last_fetch_at || '')}">${escapeHtml(s.last_fetch_at || '-')}${s.last_fetch_count ? ' (' + s.last_fetch_count + ')' : ''}${s.last_error ? ' 失败' : ''}</td>
          <td class="proxy-actions">
            <button class="btn small ghost" data-act="refresh">拉取测速</button>
            <button class="btn small ghost" data-act="del">删除</button>
          </td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">暂无 API 源</td></tr>';

    Array.from(document.querySelectorAll('#apiRows tr[data-id]')).forEach((row) => {
      const id = Number(row.getAttribute('data-id'));
      const refreshBtn = row.querySelector('[data-act="refresh"]');
      const delBtn = row.querySelector('[data-act="del"]');
      const strategySelect = row.querySelector('[data-act="strategy"]');
      if (refreshBtn) refreshBtn.addEventListener('click', () => refreshApiSource(id));
      if (delBtn) delBtn.addEventListener('click', () => deleteApiSource(id));
      if (strategySelect) {
        strategySelect.dataset.previous = strategySelect.value;
        strategySelect.addEventListener('change', () => changeApiSourceStrategy(id, strategySelect));
      }
    });
  } catch (error) {
    el('apiRows').innerHTML = `<tr><td colspan="6" class="empty">加载失败：${escapeHtml(error.message)}</td></tr>`;
  }
}

async function changeApiSourceStrategy(id, select) {
  const previous = select.dataset.previous || 'captcha';
  const rotatePerCheck = select.value === 'each';
  select.disabled = true;
  try {
    const result = await api(`/api/system/proxies/api-sources/${id}/policy`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rotate_per_check: rotatePerCheck }),
    });
    select.dataset.previous = select.value;
    showProxyResult(`<div class="add-summary">切换策略已保存：<b>${escapeHtml(result.strategy || '')}</b></div>`);
  } catch (error) {
    select.value = previous;
    showProxyResult(`<div class="add-fail-title">策略保存失败：${escapeHtml(error.message || String(error))}</div>`);
  } finally {
    select.disabled = false;
  }
}

async function submitSingleProxy() {
  const type = el('singleType').value;
  const protocol = el('singleProtocol').value;
  const host = el('singleHost').value.trim();
  const port = Number(el('singlePort').value);
  const username = el('singleUser').value.trim();
  const password = el('singlePass').value;
  if (!host || !port) {
    showProxyResult('<div class="hint">请填写主机与端口。</div>');
    return;
  }
  try {
    const res = await api('/api/system/proxies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, port, protocol, username, password, source: type }),
    });
    showProxyResult(`<div class="add-summary">已添加代理，id=<b>${res.id}</b>（来源：${sourceLabel(type)}）。</div>`);
    el('singleHost').value = '';
    el('singlePort').value = '';
    el('singleUser').value = '';
    el('singlePass').value = '';
    loadProxies();
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">添加失败：${escapeHtml(error.message)}</div>`);
  }
}

async function submitBatchProxy() {
  const protocol = el('batchProtocol').value;
  const raw = el('batchInput').value;
  if (!raw.trim()) {
    showProxyResult('<div class="hint">请粘贴至少一行代理。</div>');
    return;
  }
  try {
    const res = await api('/api/system/proxies/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ protocol, raw }),
    });
    let html = `<div class="add-summary">成功 <b>${res.added}</b> 个，跳过 <b>${res.skipped}</b> 个，无效 <b>${res.invalid.length}</b> 个。</div>`;
    if (res.invalid.length) {
      html += '<div class="add-fail-title">无效行：</div><ul class="add-fail-list">';
      res.invalid.forEach((line) => { html += `<li>${escapeHtml(line)}</li>`; });
      html += '</ul>';
    }
    showProxyResult(html);
    loadProxies();
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">批量添加失败：${escapeHtml(error.message)}</div>`);
  }
}

async function saveApiSource() {
  const url = el('apiUrl').value.trim();
  if (!url) {
    showProxyResult('<div class="hint">请填写 API 地址。</div>');
    return;
  }
  const payload = {
    name: el('apiName').value.trim(),
    url,
    protocol: el('apiProtocol').value,
    username: el('apiUser').value.trim(),
    password: el('apiPass').value,
    refresh_interval_min: Number(el('apiInterval').value) || 30,
    rotate_per_check: el('apiRotateMode') ? el('apiRotateMode').value === 'each' : false,
    enabled: el('apiEnabled').checked,
  };
  try {
    const res = await api('/api/system/proxies/api-sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showProxyResult(`<div class="add-summary">已保存 API 源，id=<b>${res.id}</b>。正在拉取新 IP 并测速…</div>`);
    el('apiName').value = '';
    el('apiUrl').value = '';
    el('apiUser').value = '';
    el('apiPass').value = '';
    loadApiSources();
    await refreshApiSource(res.id);
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">保存失败：${escapeHtml(error.message)}</div>`);
  }
}

function formatProbeSummary(probes) {
  if (!probes || !probes.length) return '<div class="hint">未探测到可用代理，请检查 API 返回是否为 host:port。</div>';
  let html = '<ul class="add-fail-list">';
  probes.forEach((item) => {
    if (item.ok) {
      html += `<li>${escapeHtml(item.url || '')} → ${escapeHtml(item.ip || '-')} ${escapeHtml(item.location || '')} ${item.latency_ms != null ? item.latency_ms + ' ms' : ''}</li>`;
    } else {
      html += `<li>${escapeHtml(item.url || '')} — 探测失败：${escapeHtml(item.error || '')}</li>`;
    }
  });
  html += '</ul>';
  return html;
}

async function refreshApiSource(id) {
  try {
    el('proxyProgress').textContent = '正在拉取新 IP 并测速…';
    const res = await api(`/api/system/proxies/api-sources/${id}/refresh`, { method: 'POST' });
    el('proxyProgress').textContent = '';
    if (!res.ok) {
      showProxyResult(`<div class="add-fail-title">拉取失败：${escapeHtml(res.error || '未知错误')}</div>`);
    } else {
      showProxyResult(
        `<div class="add-summary">已拉取 <b>${res.count || 0}</b> 个 IP（新增 ${res.added || 0}，下线 ${res.disabled || 0}）。</div>`
        + formatProbeSummary(res.probes || [])
      );
    }
    loadApiSources();
    loadProxies();
  } catch (error) {
    el('proxyProgress').textContent = '';
    showProxyResult(`<div class="add-fail-title">刷新失败：${escapeHtml(error.message)}</div>`);
  }
}

async function probeProxy(id) {
  try {
    el('proxyProgress').textContent = '正在测速…';
    const res = await api(`/api/system/proxies/${id}/probe`, { method: 'POST' });
    el('proxyProgress').textContent = '';
    if (res.ok) {
      showProxyResult(`<div class="add-summary">${escapeHtml(res.url || '')} → ${escapeHtml(res.ip || '-')} ${escapeHtml(res.location || '')} ${res.latency_ms != null ? res.latency_ms + ' ms' : ''}</div>`);
    } else {
      showProxyResult(`<div class="add-fail-title">探测失败：${escapeHtml(res.error || '')}</div>`);
    }
    loadProxies();
  } catch (error) {
    el('proxyProgress').textContent = '';
    showProxyResult(`<div class="add-fail-title">探测失败：${escapeHtml(error.message)}</div>`);
  }
}

async function deleteApiSource(id) {
  if (!confirm('确认删除该 API 源？（已拉取的代理不会被自动删除）')) return;
  try {
    await api(`/api/system/proxies/api-sources/${id}`, { method: 'DELETE' });
    loadApiSources();
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">删除失败：${escapeHtml(error.message)}</div>`);
  }
}

async function toggleProxy(id, disabled) {
  try {
    await api(`/api/system/proxies/${id}/toggle`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled }),
    });
    loadProxies();
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">操作失败：${escapeHtml(error.message)}</div>`);
  }
}

async function deleteProxy(id) {
  if (!confirm('确认删除该代理？')) return;
  try {
    await api(`/api/system/proxies/${id}`, { method: 'DELETE' });
    loadProxies();
  } catch (error) {
    showProxyResult(`<div class="add-fail-title">删除失败：${escapeHtml(error.message)}</div>`);
  }
}

/* ------------------------------------------------------------------ 绑定 */
document.addEventListener('DOMContentLoaded', () => {
  loadGroups();
  refreshAll();

  el('btnRefresh').addEventListener('click', refreshAll);
  el('btnCheck').addEventListener('click', () => triggerCheck([]));
  el('btnCheckSelected').addEventListener('click', () => triggerCheck(selectedDomains()));
  el('btnCopySelected').addEventListener('click', copySelectedDomains);
  el('btnRetryFailed').addEventListener('click', retryFailedSites);
  el('btnCancelCheck').addEventListener('click', cancelCheck);
  el('checkAllPage').addEventListener('change', (event) => {
    const checked = event.target.checked;
    document.querySelectorAll('#siteRows tr[data-domain]').forEach((row) => {
      const domain = row.getAttribute('data-domain') || '';
      const pick = row.querySelector('.site-pick');
      if (pick) pick.checked = checked;
      toggleSelected(domain, checked);
    });
  });
  el('btnReportHtml').addEventListener('click', () => downloadReport('html'));
  el('btnReportXlsx').addEventListener('click', () => downloadReport('xlsx'));

  el('btnAddSite').addEventListener('click', openAddModal);
  el('addModalClose').addEventListener('click', closeAddModal);
  el('addSubmit').addEventListener('click', submitAddSites);
  el('addSiteModal').addEventListener('click', (event) => {
    if (event.target === el('addSiteModal')) closeAddModal();
  });

  // 代理配置弹窗
  el('btnProxy').addEventListener('click', openProxyModal);
  el('proxyModalClose').addEventListener('click', closeProxyModal);
  el('proxyModal').addEventListener('click', (event) => {
    if (event.target === el('proxyModal')) closeProxyModal();
  });
  el('proxyRefresh').addEventListener('click', () => { loadProxies(); loadApiSources(); });
  el('singleSubmit').addEventListener('click', submitSingleProxy);
  el('batchSubmit').addEventListener('click', submitBatchProxy);
  el('apiSave').addEventListener('click', saveApiSource);
  el('browserEnabled').addEventListener('change', saveBrowserPref);
  el('browserShowWindow').addEventListener('change', saveBrowserPref);
  el('browserWindows').addEventListener('change', saveBrowserPref);
  if (el('browserViaProxy')) el('browserViaProxy').addEventListener('change', saveBrowserPref);
  // 来源类型选项卡
  Array.from(document.querySelectorAll('#proxyTabs .tab')).forEach((tab) => {
    tab.addEventListener('click', () => {
      Array.from(document.querySelectorAll('#proxyTabs .tab')).forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      const src = tab.getAttribute('data-src');
      ['single', 'batch', 'api'].forEach((key) => {
        el('src' + key.charAt(0).toUpperCase() + key.slice(1)).classList.toggle('hidden', key !== src);
      });
    });
  });

  el('drawerClose').addEventListener('click', closeDrawer);
  el('drawerDelete').addEventListener('click', () => {
    deleteSite(el('drawerDelete').getAttribute('data-id'), el('drawerDelete').getAttribute('data-domain'));
  });
  el('mask').addEventListener('click', closeDrawer);

  let keywordTimer = null;
  el('keywordInput').addEventListener('input', (event) => {
    clearTimeout(keywordTimer);
    const value = event.target.value.trim();
    keywordTimer = setTimeout(() => {
      state.keyword = value;
      state.page = 1;
      loadSites();
    }, 350);
  });
  el('groupFilter').addEventListener('change', (event) => {
    state.group = event.target.value;
    state.page = 1;
    loadSites();
  });
  el('statusFilter').addEventListener('change', (event) => {
    state.indexStatus = event.target.value;
    state.page = 1;
    markOverviewFilter();
    loadSites();
  });
  document.querySelectorAll('.card-filter').forEach((card) => {
    card.addEventListener('click', () => filterByStatus(card.getAttribute('data-status') || ''));
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        filterByStatus(card.getAttribute('data-status') || '');
      }
    });
  });
  el('healthFilter').addEventListener('change', (event) => {
    state.health = event.target.value;
    state.page = 1;
    loadSites();
  });
  el('pageSize').addEventListener('change', (event) => {
    state.size = Number(event.target.value);
    state.page = 1;
    loadSites();
  });
  el('prevPage').addEventListener('click', () => {
    if (state.page > 1) {
      state.page -= 1;
      loadSites();
    }
  });
  el('nextPage').addEventListener('click', () => {
    if (state.page * state.size < state.total) {
      state.page += 1;
      loadSites();
    }
  });
  Array.from(document.querySelectorAll('#siteTable th[data-sort]')).forEach((cell) => {
    cell.style.cursor = 'pointer';
    cell.addEventListener('click', () => {
      const key = cell.getAttribute('data-sort');
      state.desc = state.sort === key ? !state.desc : true;
      state.sort = key;
      loadSites();
    });
  });

  window.addEventListener('resize', () => {
    if (state.trendChart) state.trendChart.resize();
    if (state.diffChart) state.diffChart.resize();
    if (state.siteChart) state.siteChart.resize();
  });

  if (!state.hasEcharts) {
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = '未检测到 echarts.min.js（web/static/vendor/），图表功能不可用，表格数据仍可正常查看。';
    document.querySelector('.charts').prepend(note);
  }

  loadStatus();
});
