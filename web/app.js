/* Agent 后端监控器 · Web 前端逻辑 */
'use strict';

const TOKEN = window.__ABM_TOKEN__ || '';
const $ = (id) => document.getElementById(id);

const ROLE_LABEL = { agent: 'Agent 主体', backend: '后端服务', core: '辅助进程', child: '子进程' };
const ROLE_ICON = { agent: '◆', backend: '▲', core: '·', child: '○' };
const SYSTEM_PORTS = new Set([135, 139, 445, 5040, 5353, 5355, 5357, 7680, 1900, 3702, 137, 138,
  49664, 49665, 49666, 49667, 49668, 49669, 49670, 49671, 49672, 49673, 49674, 49675, 49676, 49677, 49678, 49679, 49680]);

const state = {
  data: null,
  index: new Map(),      // pid -> {node, agent}
  collapsed: new Set(),  // 折叠的 pid
  selected: null,
  tab: 'agents',
  auto: true,
  intervalMs: 2000,
  filters: { compact: true, onlyAgent: false, onlyPort: false, hideSys: true, onlyOwner: false, eventOnlyWarn: false },
  search: '', portSearch: '',
  loading: false,
  timer: null,
};

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtTime(sec) {
  if (!sec) return '-';
  return new Date(sec * 1000).toLocaleTimeString('zh-CN', { hour12: false });
}
function toast(msg, kind) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 4200);
}
function modal(title, body, onOk) {
  const box = $('modalBox');
  box.innerHTML = `<h3>${esc(title)}</h3><p>${esc(body)}</p>
    <div class="actions"><button class="btn" id="mCancel">取消</button>
    <button class="btn danger" id="mOk">确定</button></div>`;
  $('modal').classList.add('show');
  const close = () => { $('modal').classList.remove('show'); };
  $('mCancel').onclick = close;
  $('mOk').onclick = () => { close(); onOk(); };
}
async function fetchJSON(url, opt) {
  const res = await fetch(url, opt);
  const ctype = res.headers.get('content-type') || '';
  const text = await res.text();
  if (!ctype.includes('application/json') || text.trimStart().startsWith('<')) {
    const err = new Error('NON_JSON');
    err.detail = { status: res.status, sample: text.trim().slice(0, 140) };
    throw err;
  }
  const data = JSON.parse(text);
  if (res.status === 403) throw new Error('FORBIDDEN');
  if (!res.ok) {
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.detail = data;
    throw err;
  }
  return data;
}

async function api(path, body) {
  const opt = { method: body === undefined ? 'GET' : 'POST', headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.headers['X-ABM-Token'] = TOKEN;
    opt.body = JSON.stringify(body);
  }
  return fetchJSON(path, opt);
}

/** 统一的动作执行：任何失败都要有提示，绝不静默。 */
async function doAction(label, fn, okText) {
  try {
    const r = await fn();
    if (r && r.ok === false) throw new Error(r.error || '操作未成功');
    if (okText) {
      const msg = typeof okText === 'function' ? okText(r) : okText;
      toast(msg, (r && r.alive === false) ? 'err' : 'ok');
    }
  } catch (e) {
    if (e.message === 'FORBIDDEN') {
      toast('操作被拒绝：页面令牌失效，请按 Ctrl+F5 重新加载页面', 'err');
    } else {
      toast(`${label}失败：${e.message}`, 'err');
    }
  } finally {
    load(true);
  }
}

function setBanner(html, kind) {
  const el = $('banner');
  if (!el) return;
  if (!html) { el.className = 'banner'; el.innerHTML = ''; return; }
  el.className = 'banner show ' + (kind || '');
  el.innerHTML = html;
  const btn = $('bnRetry');
  if (btn) btn.onclick = () => load(true);
  const btn2 = $('bnReload');
  if (btn2) btn2.onclick = () => location.reload();
}

/** 刷新失败时给出人话解释：服务停了？还是这个端口上根本不是本服务？ */
async function diagnose(err) {
  const here = location.origin;
  if (err.message === 'NON_JSON') {
    const d = err.detail || {};
    return `<b>这个地址返回的不是监控数据</b>（HTTP ${d.status || '?'}）：
      <code>${esc(d.sample || '')}</code><br>
      说明 <code>${esc(here)}</code> 上运行的不是本监控服务（端口可能被别的程序占用了）。
      请改用「启动网页版.bat」打印出来的地址打开。`;
  }
  try {
    const h = await fetchJSON('/api/health');
    return `与监控服务的连接暂时异常（${esc(err.message)}）；服务端自报 v${esc(h.version)}，PID ${h.pid}。
      <button class="btn" id="bnRetry">重试</button>`;
  } catch (_) {
    return `<b>连接不上监控服务</b>：<code>${esc(here)}</code> 没有响应。
      通常是「启动网页版.bat」的窗口被关掉了，重新启动后再点重试。
      <button class="btn" id="bnRetry">重试</button>`;
  }
}
function copy(text) {
  navigator.clipboard.writeText(text).then(
    () => toast('已复制到剪贴板', 'ok'),
    () => toast('复制失败，请手动选择文本', 'err'));
}

/** 采集源降级（没装 psutil）时必须说清楚，并给出一键修复入口 */
function applySourceBanner(data) {
  const src = (data && data.source) || '';
  if (src.indexOf('CIM') < 0) {
    setBanner('');
    return;
  }
  const better = data.suggest_python
    ? `本机检测到可用的解释器：<code>${esc(data.suggest_python)}</code>，下次用它启动即为全量模式。`
    : '';
  const out = (data.install_output || '').trim();
  setBanner(`<b>当前为降级采集模式</b>：启动监控的 Python（<code>${esc(data.python || '?')}</code>）
    没有安装 <code>psutil</code>。端口、命令行、工作目录仍可读（已用 PEB 兜底），
    但内存 / 启动时间等信息会缺失。<br>${better}
    ${out ? `<details><summary>安装输出</summary><pre>${esc(out)}</pre></details>` : ''}
    <button class="btn primary" id="bnInstall">一键安装 psutil 并启用</button>
    <button class="btn" id="bnReload">重新加载页面</button>`, 'err');
  const btn = $('bnInstall');
  if (btn) {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = '正在安装…';
      try {
        const r = await api('/api/enable-psutil', { install: true });
        if (r.ok) {
          toast(`已启用 psutil ${r.version}，采集切回全量模式`, 'ok');
          state.data.install_output = '';
          load(true);
        } else {
          state.data.install_output = (r.output || '') + (r.error || '');
          toast('安装失败：' + (r.error || '未知错误'), 'err');
          applySourceBanner(state.data);
        }
      } catch (e) {
        toast('安装请求失败：' + e.message, 'err');
        btn.disabled = false;
        btn.textContent = '一键安装 psutil 并启用';
      }
    };
  }
}

/* ------------------------------------------------------------------ */
/* 数据刷新                                                            */
/* ------------------------------------------------------------------ */
async function load(manual) {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await fetchJSON('/api/snapshot', { cache: 'no-store' });
    state.data = data;
    state.failures = 0;
    applySourceBanner(data);
    renderAll();
    setLive(true);
  } catch (e) {
    state.failures = (state.failures || 0) + 1;
    setLive(false);
    if (manual || state.failures >= 2) {
      setBanner(await diagnose(e), 'err');
    }
    if (manual) toast('刷新失败：' + e.message, 'err');
  } finally {
    state.loading = false;
  }
}
function setLive(ok) {
  const el = $('live');
  el.classList.toggle('off', !ok || !state.auto);
  $('liveText').textContent = ok ? (state.auto ? 'LIVE' : '已暂停') : '连接中断';
}
function schedule() {
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (state.auto && !document.hidden) load(false);
  }, state.intervalMs);
}
function setIntervalMs(ms) {
  state.intervalMs = ms;
  schedule();
}

/* ------------------------------------------------------------------ */
/* 渲染总入口                                                          */
/* ------------------------------------------------------------------ */
function renderAll() {
  if (!state.data) return;
  renderStats();
  renderAgents();
  renderPorts();
  renderEvents();
  renderDetail();
}

function renderStats() {
  const d = state.data, s = d.stats || {};
  const portRows = d.ports || [];
  const agentPorts = portRows.filter(r => r.owner_rule).length;
  const t = d.generated_at ? String(d.generated_at).split(' ')[1] : '-';
  const cards = [
    ['🧩', '进程总数', s.processes, 'var(--accent)'],
    ['🛰', 'Agent 主体', s.agents, 'var(--purple)'],
    ['▲', '后端服务', s.backends, 'var(--green)'],
    ['🔌', '监听端口', s.listen, 'var(--cyan)'],
    ['🎯', '归属于 Agent 的端口', agentPorts, 'var(--green)'],
    ['📋', '事件', s.events, 'var(--orange)'],
    ['⏱', '采集耗时', `${d.collect_ms || 0}<small>ms</small>`, 'var(--fg-dim)'],
    ['🕘', '上次刷新', t, 'var(--fg-dim)'],
  ];
  $('stats').innerHTML = cards.map(([ico, k, v, c]) =>
    `<div class="card" style="--c:${c}"><span class="ico">${ico}</span>
       <div><div class="k">${k}</div><div class="v">${v}</div></div></div>`).join('');
  $('cntAgents').textContent = s.agents || 0;
  $('cntPorts').textContent = s.listen || 0;
  $('cntEvents').textContent = s.events || 0;
}

/* ---------------------------- 进程树 ---------------------------- */
function nodeMatches(n, q) {
  if (!q) return true;
  const hay = [n.name, n.cmdline, n.cwd, n.reason, n.exe, n.port_text, n.pid].join(' ').toLowerCase();
  return hay.includes(q);
}
function nodeVisible(n) {
  const f = state.filters;
  if (f.compact && n.role === 'core') return false;
  if (f.onlyPort && n.role === 'backend' && !(n.ports && n.ports.length) && !/MCP/.test(n.reason || '')) return false;
  if (f.onlyPort && n.role === 'child' && !(n.ports && n.ports.length)) return false;
  return true;
}
/** 计算子树里是否有命中搜索的对象（命中自身或后代） */
function markMatches(n, q, memo) {
  let hit = nodeMatches(n, q);
  for (const c of n.children || []) {
    if (markMatches(c, q, memo)) hit = true;
  }
  memo.set(n.pid, hit);
  return hit;
}
function renderAgents() {
  const wrap = $('tree');
  const d = state.data;
  const memo = new Map();
  state.index = new Map();
  let agents = (d.agents || []);
  if (state.filters.onlyAgent) agents = agents.filter(g => g.category === 'agent');
  const q = state.search.trim().toLowerCase();

  const html = [];
  for (const g of agents) {
    const tree = g.tree;
    if (q) {
      const hit = nodeMatches(tree, q) || (tree.children || []).some(c => markMatches(c, q, memo))
        || (g.backends || []).some(b => (b.name + b.cmdline + b.cwd + b.port_text).toLowerCase().includes(q));
      if (!hit) continue;
    }
    indexTree(tree, g);
    const kidCount = visibleChildren(tree, q, memo).length;
    const hiddenCount = (tree.children || []).length - kidCount;
    const emptyHint = hiddenCount > 0
      ? `已隐藏 ${hiddenCount} 个 Agent 辅助进程（取消勾选「精简模式」可查看）`
      : '该 Agent 当前没有子进程';
    const portChips = (g.ports || []).map(p => `<span class="chip port">${p.port}/${p.proto.toLowerCase()}</span>`).join('');
    html.push(`
      <div class="agent-block" style="--c:${g.color}">
        <div class="agent-head" data-select="${g.pid}">
          <span class="expander ${kidCount ? '' : 'leaf'}" data-toggle="${g.pid}">${state.collapsed.has(g.pid) ? '▸' : '▾'}</span>
          <span style="color:${g.color}">◆</span>
          <span class="title">${esc(g.label)}</span>
          <span class="pid">${esc(g.name)} · PID ${g.pid}</span>
          <span class="role agent">Agent 主体</span>
          ${portChips}
          <span class="grow"></span>
          <span class="chip">后端 ${g.backend_count}</span>
          <span class="chip">${esc(g.rss_text)}</span>
          <span class="chip">运行 ${esc(g.uptime)}</span>
        </div>
        <div class="agent-body ${state.collapsed.has(g.pid) ? 'collapsed' : ''}" data-body="${g.pid}">
          ${renderKids(tree, 0, q, memo) || `<div class="empty">${esc(emptyHint)}</div>`}
        </div>
      </div>`);
  }
  wrap.innerHTML = html.join('') || '<div class="empty">没有识别到已知的 Agent 进程。<br>可以在 <code>config/agents.json</code> 里添加识别规则。</div>';

  wrap.querySelectorAll('[data-toggle]').forEach(el => {
    el.onclick = (e) => {
      e.stopPropagation();
      const pid = +el.dataset.toggle;
      if (state.collapsed.has(pid)) state.collapsed.delete(pid); else state.collapsed.add(pid);
      renderAgents();
    };
  });
  wrap.querySelectorAll('[data-select]').forEach(el => {
    el.onclick = () => select(+el.dataset.select);
  });
  highlightSelection();
}

function indexTree(node, agent) {
  state.index.set(node.pid, { node, agent });
  for (const c of node.children || []) indexTree(c, agent);
}

function matched(n, q, memo) {
  return !q || memo.get(n.pid) || nodeMatches(n, q);
}

/** 取得"可见的子节点"列表：被过滤掉的节点，其可见后代会被提升上来 */
function visibleChildren(n, q, memo) {
  const out = [];
  for (const c of n.children || []) {
    if (nodeVisible(c) && matched(c, q, memo)) out.push(c);
    else out.push(...visibleChildren(c, q, memo));
  }
  return out;
}

function renderKids(n, depth, q, memo) {
  return visibleChildren(n, q, memo).map(c => renderNode(c, depth, q, memo)).join('');
}

function renderNode(n, depth, q, memo) {
  const kids = visibleChildren(n, q, memo);
  const collapsed = state.collapsed.has(n.pid);
  const portChips = (n.ports || []).map(p => `<span class="chip port">${p.port}/${p.proto.toLowerCase()}</span>`).join('');
  const icon = ROLE_ICON[n.role] || '○';
  const roleCls = n.role;
  const branch = depth > 0 ? '<span class="branch">└</span>' : '';
  const rows = [`<div class="row" data-select="${n.pid}" style="margin-left:${depth * 16}px">
      <div class="main">
        ${branch}
        <span class="expander ${kids.length ? '' : 'leaf'}" data-toggle="${n.pid}">${collapsed ? '▸' : '▾'}</span>
        <span class="role ${roleCls}" style="padding:0 5px">${icon}</span>
        <span class="name">${esc(n.name)}</span>
        <span class="meta">
          <span>PID ${n.pid}</span>
          ${n.rss ? `<span>${esc(n.rss_text)}</span>` : ''}
          ${n.uptime && n.uptime !== '-' ? `<span>运行 ${esc(n.uptime)}</span>` : ''}
          ${n.reason ? `<span class="chip reason">${esc(n.reason)}</span>` : ''}
        </span>
      </div>
      <div class="right">${portChips}<span class="role ${roleCls}">${ROLE_LABEL[n.role] || n.role}</span></div>
    </div>`];
  if (!collapsed) {
    for (const c of kids) rows.push(renderNode(c, depth + 1, q, memo));
  }
  return rows.join('');
}

function select(pid) {
  state.selected = pid;
  highlightSelection();
  renderDetail();
  if (window.innerWidth < 1080) $('detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function highlightSelection() {
  document.querySelectorAll('#tree .row, #tree .agent-head').forEach(el => {
    el.classList.toggle('sel', +el.dataset.select === state.selected);
  });
}

/* ---------------------------- 详情面板 ---------------------------- */
function renderDetail() {
  const box = $('detail');
  const entry = state.selected != null ? state.index.get(state.selected) : null;
  if (!entry) {
    box.innerHTML = `
      <div class="detail-head">
        <div class="icon">🛰</div>
        <div>
          <h2>进程详情</h2>
          <div class="lead">在左侧点选一个 Agent 或后端进程</div>
        </div>
      </div>
      <div class="note">本工具关注的问题：Agent 关闭后，它在自己进程树下启动的后端（本地 HTTP 服务、
      MCP server 等）往往会被一起终止。左侧 <b>▲</b> 就是这类进程，选中后这里会显示它的完整启动详情。</div>`;
    return;
  }
  const n = entry.node, g = entry.agent;
  const chain = parentChain(n.pid);
  const ports = (n.ports || []).map(p => `<span class="pill port">${p.proto} ${esc(p.addr)}:${p.port}${p.state === 'UDP' ? '' : ' ' + p.state}</span>`).join('') || '<span class="dim">无（没有监听任何 TCP/UDP 端口）</span>';
  const kids = (n.children || []).map(c =>
    `<div style="margin-bottom:6px"><span class="pill ${c.role === 'backend' ? 'backend' : (c.role === 'agent' ? 'agent' : '')}">${ROLE_ICON[c.role] || '○'} ${esc(c.name)}</span>
     <span class="dim">PID ${c.pid}</span> ${c.port_text ? `<span class="pill port">${esc(c.port_text)}</span>` : ''}
     <div class="dim" style="font-size:11.5px">${esc(c.reason || '')}</div></div>`).join('') || '<span class="dim">无</span>';

  box.innerHTML = `
    <div class="detail-head">
      <div class="icon">${ROLE_ICON[n.role] || '○'}</div>
      <div style="min-width:0">
        <h2>${esc(n.name)}</h2>
        <div class="lead">PID <span class="mono">${n.pid}</span>　·　${ROLE_LABEL[n.role] || n.role}${n.reason ? '　·　' + esc(n.reason) : ''}</div>
      </div>
    </div>

    <div class="actions">
      <button class="btn primary" id="dCopyCmd">复制启动命令</button>
      <button class="btn" id="dCopy">复制详情</button>
      <button class="btn" id="dRelaunch" title="用原命令、原目录重新拉起，并尝试脱离 Agent 会话">独立重启</button>
      <button class="btn danger" id="dKill">结束进程树</button>
    </div>

    <div class="section">
      <h3>启动详情</h3>
      <div class="section-card">
        <div class="kv">
          <div class="k">命令行</div><div class="v mono">${esc(n.cmdline || '（读取被拒绝，试试以管理员身份运行监控器）')}</div>
          <div class="k">工作目录</div><div class="v mono">${esc(n.cwd || '（未获取到）')}</div>
          <div class="k">可执行文件</div><div class="v mono">${esc(n.exe || '-')}</div>
          <div class="k">启动时间</div><div class="v">${esc(n.created)}　<span class="dim">已运行 ${esc(n.uptime)}</span></div>
          <div class="k">内存 / 线程</div><div class="v">${esc(n.rss_text)}　/　${n.threads} 线程　<span class="dim">CPU ${n.cpu}%</span></div>
          <div class="k">状态</div><div class="v">${esc(n.status || '-')}</div>
          <div class="k">父进程链</div><div class="v">${esc(chain)}</div>
        </div>
      </div>
    </div>

    <div class="section"><h3>监听端口</h3>
      <div class="section-card">${ports}
        ${n.conn_count ? `<div class="dim" style="margin-top:6px">活跃连接 ${n.conn_count} 条</div>` : ''}
      </div>
    </div>

    <div class="section"><h3>直接子进程（${(n.children || []).length}）</h3>
      <div class="section-card">${kids}</div>
    </div>

    <div class="section"><h3>所属 Agent 的后端（${g.backend_count}）</h3>
      <div class="section-card">
        ${(g.backends || []).map(b => `<div><span class="pill backend">▲ ${esc(b.name)}</span>
          <span class="dim">PID ${b.pid}</span> ${b.port_text ? `<span class="pill port">${esc(b.port_text)}</span>` : ''}
          <div class="dim" style="font-size:11.5px;margin-bottom:4px">${esc(b.reason)}　${esc(b.cwd || '')}</div></div>`).join('') || '<span class="dim">无</span>'}
        ${(g.ports || []).length ? `<div class="note">该 Agent 自身也在监听 ${(g.ports || []).map(p => p.port).join('、')} 端口。
          Agent 一退出，这些端口和后端通常一起消失；需要长期可用请用 <b>独立重启</b>。</div>` : ''}
      </div>
    </div>`;

  $('dCopy').onclick = () => copy(box.innerText.trim());
  $('dCopyCmd').onclick = () => copy(cmdText(n));
  $('dKill').onclick = () => {
    modal('结束进程树', `确定要结束 ${n.name} (PID ${n.pid}) 及其全部子进程吗？此操作不可撤销。`, () => {
      doAction('结束进程树', () => api('/api/kill', { pid: n.pid, tree: true }),
        r => `已结束 ${r.killed.length} 个进程`);
    });
  };
  $('dRelaunch').onclick = () => {
    const ports = (n.ports || []).map(p => p.port);
    const portText = ports.length ? ports.join('、') : '（不占端口）';
    modal('重启并独占原端口',
      `步骤：\n`
      + `① 结束当前进程 PID ${n.pid} 及其子进程${ports.length ? '，释放端口 ' + portText : ''}\n`
      + `② 等端口真正释放\n`
      + `③ 用原命令行、原工作目录，以「脱离 Agent」的方式重新启动并监听同一个端口\n\n`
      + `注意：会先中断当前正在跑的服务（约 1~3 秒）。\n\n命令：\n${n.cmdline || ''}`, () => {
        doAction('重启', () => api('/api/relaunch', { pid: n.pid, kill: true }),
          r => {
            if (!r.alive) {
              return `重启失败：${r.warning || '新进程没能活下来'}`;
            }
            let msg;
            if (r.bound_ports && r.bound_ports.length) {
              msg = `已重启：旧进程 ${r.killed.length} 个已结束，端口 ${r.bound_ports.join('、')} `
                + `已由新进程 PID ${r.pid} 重新监听`;
            } else {
              msg = `已启动新进程 PID ${r.pid}（${r.how}）`;
            }
            if (r.warning) msg += `　⚠ ${r.warning}`;
            return msg;
          });
      });
  };
}

function cmdText(n) {
  const cmd = (n.cmdline || '').trim();
  if (!cmd) return '';
  const prefix = n.cwd ? `cd '${n.cwd}'; ` : '';
  return prefix + '& ' + cmd.replace(/^"([^"]+)"/, "'$1'");
}
function parentChain(pid) {
  const parts = [];
  const seen = new Set();
  let cur = state.index.get(pid);
  while (cur && parts.length < 12) {
    const n = cur.node;
    if (!n.ppid || seen.has(n.ppid)) break;
    seen.add(n.ppid);
    const p = state.index.get(n.ppid);
    if (!p) { parts.push('PID ' + n.ppid + '(不在 Agent 树中)'); break; }
    parts.push(`${p.node.name}(PID ${p.node.pid})`);
    cur = p;
  }
  return parts.length ? parts.join(' ← ') : '（父进程已退出或不在 Agent 树中）';
}

/* ---------------------------- 端口表 ---------------------------- */
function renderPorts() {
  const q = state.portSearch.trim().toLowerCase();
  const rows = (state.data.ports || []).filter(r => {
    if (state.filters.hideSys && (r.system || r.port < 1024) && !r.owner_rule) return false;
    if (state.filters.onlyOwner && !r.owner_rule) return false;
    if (state.filters.onlyAgent && !r.owner_rule) return false;
    if (q) {
      const hay = [r.port, r.name, r.owner, r.cwd, r.cmdline, r.addr].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const body = document.querySelector('#portTable tbody');
  body.innerHTML = rows.map(r => {
    const cls = (r.role === 'backend' || r.owner_rule) ? 'backend' : 'sys';
    return `<tr class="${cls}" data-pid="${r.pid}" title="双击跳到进程树">
      <td class="num">${r.port}</td><td>${r.proto}</td><td class="mono">${esc(r.addr)}</td>
      <td class="num">${r.pid}</td><td>${esc(r.name)}</td>
      <td>${r.owner_rule ? '◆ ' + esc(r.owner) : '<span class="dim">' + esc(r.owner) + '</span>'}</td>
      <td>${ROLE_LABEL[r.role] || r.role}</td>
      <td class="mono clip">${esc(r.cwd || '-')}</td>
      <td class="mono clip" title="${esc(r.cmdline)}">${esc(r.cmdline || '-')}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="empty">没有匹配的监听端口</td></tr>';
  body.querySelectorAll('tr[data-pid]').forEach(tr => {
    tr.ondblclick = () => jumpToPid(+tr.dataset.pid);
  });
}

function jumpToPid(pid) {
  switchTab('agents');
  const entry = state.index.get(pid);
  if (!entry) {
    state.search = '';
    $('search').value = '';
    state.filters.compact = false;
    $('compact').checked = false;
    renderAgents();
  }
  // 展开所有祖先
  let cur = state.index.get(pid);
  const seen = new Set();
  while (cur && !seen.has(cur.node.pid)) {
    seen.add(cur.node.pid);
    state.collapsed.delete(cur.node.pid);
    cur = state.index.get(cur.node.ppid);
  }
  state.collapsed.delete(pid);
  renderAgents();
  select(pid);
  const el = document.querySelector(`#tree [data-select="${pid}"]`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  else toast('该进程不在 Agent 进程树中（可能是独立进程）', 'err');
}

/* ---------------------------- 事件表 ---------------------------- */
function renderEvents() {
  const warnOnly = state.filters.eventOnlyWarn;
  const events = (state.data.events || []).slice().reverse()
    .filter(e => !warnOnly || e.level === 'warn' || e.level === 'err');
  const body = document.querySelector('#eventTable tbody');
  body.innerHTML = events.map(e => `<tr class="ev-row lv-${e.level}">
      <td>${fmtTime(e.ts)}</td>
      <td><span class="tag ${e.level}">${e.level.toUpperCase()}</span></td>
      <td>${esc(e.text)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="empty">还没有事件。等 Agent 启动或退出一个后端，这里就会出现记录。</td></tr>';
}

/* ---------------------------- 页签与控件 ---------------------------- */
function switchTab(name) {
  state.tab = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
}
function bind() {
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

  $('btnRefresh').onclick = () => doAction('刷新', () => api('/api/refresh', {}), null);
  $('autoRefresh').onchange = (e) => { state.auto = e.target.checked; setLive(true); };
  $('intervalSel').onchange = (e) => setIntervalMs(+e.target.value * 1000);

  $('btnReport').onclick = () => { location.href = '/api/report?format=md'; toast('正在下载 Markdown 报告…', 'ok'); };
  $('btnReportJson').onclick = () => { location.href = '/api/report?format=json'; toast('正在下载 JSON 快照…', 'ok'); };

  $('search').oninput = (e) => { state.search = e.target.value; renderAgents(); };
  $('portSearch').oninput = (e) => { state.portSearch = e.target.value; renderPorts(); };
  $('compact').onchange = (e) => { state.filters.compact = e.target.checked; renderAgents(); };
  $('onlyAgent').onchange = (e) => { state.filters.onlyAgent = e.target.checked; renderAgents(); renderPorts(); };
  $('onlyPort').onchange = (e) => { state.filters.onlyPort = e.target.checked; renderAgents(); };
  $('hideSys').onchange = (e) => { state.filters.hideSys = e.target.checked; renderPorts(); };
  $('onlyOwner').onchange = (e) => { state.filters.onlyOwner = e.target.checked; renderPorts(); };
  $('eventOnlyWarn').onchange = (e) => { state.filters.eventOnlyWarn = e.target.checked; renderEvents(); };

  document.addEventListener('keydown', (e) => {
    // 单独的 F5 = 立即刷新数据；Ctrl/Shift + F5 留给浏览器做真正的重新加载
    if (e.key === 'F5' && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      e.preventDefault();
      doAction('刷新', () => api('/api/refresh', {}), null);
    }
    if (e.key === 'Escape') $('modal').classList.remove('show');
  });
  document.addEventListener('visibilitychange', () => setLive(true));
}

/* ---------------------------- 说明页 ---------------------------- */
function renderHelp() {
  $('helpBody').innerHTML = `
    <h2>这个工具解决什么问题</h2>
    <p>很多 Agent 工具会把后端（本地 HTTP 服务、MCP server、语言服务器等）作为自己的子进程启动。
    父进程一退出，这些后端往往被一起收走，端口随即失效——排查时很难说清「是谁启动的、跑在哪、为什么没了」。
    本页面把 Agent 的进程树、每个后端的启动命令 / 工作目录 / 监听端口完整摊开，并持续记录它们的生灭事件。</p>

    <h2>三个页签</h2>
    <ul>
      <li><b>Agent 与后端</b>：◆ Agent 主体，▲ 后端服务，· Agent 自身的辅助进程（Electron 渲染/GPU 等）。点任意一行看右侧详情。</li>
      <li><b>端口总览</b>：本机所有监听端口及其占用进程，并标注归属哪个 Agent。双击一行跳到进程树。</li>
      <li><b>事件时间线</b>：后端启动 / 退出、端口监听 / 释放。当一个 Agent 退出并连带其子进程同时消失时，会出现
      「Agent 退出 → 连带终止 N 个后端」的记录，这就是「关闭 Agent 后端也关掉」的直接证据。</li>
    </ul>

    <h2>常用操作</h2>
    <ul>
      <li><b>独立重启</b>：用原命令行、原工作目录重新拉起该后端，并尝试脱离 Agent 的 Job 对象与控制台，使 Agent 退出后它仍然存活。</li>
      <li><b>复制启动命令</b>：生成可直接粘到 PowerShell 的命令。</li>
      <li><b>结束进程树</b>：结束选中进程及其所有后代，请谨慎使用。</li>
      <li><b>导出报告</b>：右上角可下载 Markdown / JSON 快照。</li>
    </ul>

    <h2>识别规则可以自己改</h2>
    <p>规则文件：<code>config/agents.json</code>。每条规则的 <code>proc</code> 是进程名正则，<code>cmd</code> 是命令行 /
    可执行文件路径正则，两者都命中才算识别为某个 Agent。改完在页面右上角刷新即可（服务端会自动重读文件需重启服务，
    或在进程内调用 <code>/api/reload-rules</code>）。</p>
    <pre>{
  "id": "my-agent",
  "label": "我的 Agent",
  "proc": ["^node\\\\.exe$"],
  "cmd":  ["my-agent[\\\\\\\\/]server"]
}</pre>

    <h2>权限说明</h2>
    <p>普通权限下可以读取同用户进程；对以管理员身份运行的进程，命令行 / 工作目录可能为空。
    以管理员身份启动本服务可获得最完整的信息。</p>

    <h2>命令行用法</h2>
    <pre>python agent_backend_web.py --port 8737 --interval 2 --no-browser
python agent_backend_monitor.py --report      # 桌面版也能直接打印报告
python agent_backend_monitor.py               # 桌面版图形界面</pre>
    <p class="dim">当前版本 v${esc(window.__ABM_VERSION__)}</p>`;
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
bind();
renderHelp();
schedule();
if (!TOKEN) {
  setBanner('页面没有拿到与服务端同步的访问令牌（页面可能来自旧的服务实例）。'
    + '请按 <b>Ctrl+F5</b> 重新加载页面后再操作。<button class="btn" id="bnReload">重新加载</button>', 'err');
}
const initialTab = location.hash.slice(1);
if (['agents', 'ports', 'events', 'help'].includes(initialTab)) switchTab(initialTab);
load(true).then(() => {
  // 首次加载后自动选中第一个 Agent，右侧直接有内容可看
  if (state.selected == null && state.data && (state.data.agents || []).length) {
    select(state.data.agents[0].pid);
  }
});
setInterval(() => { if (state.auto && !document.hidden) setLive(true); }, 5000);
