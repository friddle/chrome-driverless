
const API = window.location.origin + '/mcp';
let proxyEnabled = false;

/* ================= 真人模式 / GPU 状态 ================= */
let HUMAN = false;
let humanQuietUntil = 0;  // navigate 后的静默期（settle 窗口不做截图轮询，保持零附着）
const HUMAN_MAP = {
  'pw/screenshot': 'human/screenshot', 'pw/navigate': 'human/navigate', 'pw/reload': 'human/reload',
  'pw/back': 'human/back', 'pw/new_tab': 'human/new_tab', 'pw/tabs': 'human/tabs',
  'pw/tab_select': 'human/tab_select', 'pw/tab_close': 'human/tab_close',
  'pw/click': 'human/click', 'pw/hover': 'human/hover', 'pw/type': 'human/type',
  'pw/key': 'human/key', 'pw/clear': 'human/clear', 'pw/mouse_move': 'human/mouse_move',
  'pw/clip_read': 'human/clip_read',
  'pw/mouse_down': 'human/mouse_down', 'pw/mouse_up': 'human/mouse_up', 'pw/scroll_at': 'human/scroll_at',
};

/* ================= 远程声音（/audio.mp3） ================= */
let remoteAudioEl = null, remoteAudioOn = false;
function toggleRemoteAudio() {
  if (!remoteAudioEl) {
    remoteAudioEl = new Audio('/audio.mp3');
    remoteAudioEl.volume = 0.9;
  }
  remoteAudioOn = !remoteAudioOn;
  const btn = document.getElementById('audio-btn');
  if (remoteAudioOn) {
    remoteAudioEl.play().catch(() => {});
    btn.textContent = '🔊 声音';
  } else {
    remoteAudioEl.pause();
    btn.textContent = '🔇 声音';
  }
}

/* ================= 基础工具 ================= */
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = '', 2600);
}
function toggleCard(id) { document.getElementById(id).classList.toggle('collapsed'); }
function escHtml(s) { return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escAttr(s) { return escHtml(s); }

async function mcp(method, params = {}) {
  try {
    // 真人模式：浏览器操作类 pw/* 自动路由到 human/*（raw CDP Input/Page，零 Runtime.enable）；
    // 其余 pw/*（evaluate/elements/ai 等）在真人模式下不可用，直接报错提示。
    if (HUMAN && HUMAN_MAP[method]) {
      method = HUMAN_MAP[method];
      if (method === 'human/navigate') humanQuietUntil = Date.now() + 12000;
    } else if (HUMAN && method.startsWith('pw/')) {
      return { error: { code: -3, message: '真人模式运行中：' + method + ' 不可用（无 CDP Runtime）。用截图+指针操作，或关闭真人模式。' } };
    }
    const r = await fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, params }) });
    return await r.json();
  } catch (e) { addChat('error', '请求失败: ' + e.message); return null; }
}

function updateModeButtons(s) {
  s = s || {};
  if (s.human_mode !== undefined) HUMAN = !!s.human_mode;
  const hb = document.getElementById('human-btn');
  if (hb) { hb.classList.toggle('on', HUMAN); hb.textContent = HUMAN ? '🧑 真人模式 ON' : '🧑 真人模式'; }
  // GPU 徽标：纯状态展示（后端启动时自动探测，有 GPU 默认直通；这里只展示探测结果与生效情况）
  const gbd = document.getElementById('gpu-badge');
  if (gbd && s.gpu) {
    const avail = !!s.gpu.available;
    const w = (s.webgl && s.webgl.renderer) || '';
    const soft = !w || /SwiftShader|llvmpipe|Software/i.test(w);
    gbd.textContent = avail ? `🎮 ${s.gpu.detail || 'GPU'}` : '🎮 无GPU·软渲染';
    gbd.className = 'badge ' + (avail && !soft ? 'badge-ok' : 'badge-idle');
    gbd.title = avail
      ? `GPU: ${s.gpu.detail}｜WebGL renderer: ${w || '未知'}${soft ? '（⚠ 未生效：容器未挂 GPU/驱动缺失，检查 docker gpus 与 NVIDIA_DRIVER_CAPABILITIES=graphics）' : ''}`
      : '未检测到 GPU（无 nvidia-smi 且无 /dev/dri），WebGL 走 swiftshader 软渲染';
  }
  // 指纹档案下拉：真人模式置灰（零注入优先）
  const fpSel = document.getElementById('fp-select');
  if (fpSel) {
    if (s.fp_profile) fpSel.value = s.fp_profile;
    fpSel.disabled = !!HUMAN;
    fpSel.title = HUMAN ? '真人模式运行中：不注入指纹覆盖层（零 CDP 痕迹优先），切换已记录、回普通模式生效'
                        : '指纹档案（仅普通 Playwright 模式注入）。Emulation 类改动存在可检测面，最大隐身用『真实』';
  }
  // 引擎徽标：只读展示（BROWSER_ENGINE 容器启动时指定）
  const eb = document.getElementById('engine-badge');
  if (eb && s.engine) {
    eb.textContent = s.engine === 'chrome' ? '⚙ 真 Chrome' : '⚙ chromium';
    eb.className = 'badge ' + (s.engine === 'chrome' ? 'badge-ok' : 'badge-idle');
    eb.title = `引擎: ${s.engine_path || s.engine}｜容器启动时 BROWSER_ENGINE=${s.engine === 'chrome' ? 'chrome' : 'chromium'} 指定`;
  }
}

async function fpChange(p) {
  const r = await mcp('fp/set', { profile: p });
  if (r?.error) { toast(r.error.message, true); addChat('ERROR', r.error.message); return; }
  updateModeButtons(r.result);
  if (r.result?.note) addChat('INFO', r.result.note);
  else addChat('INFO', `🎭 指纹档案已切换: ${p}，浏览器已重启生效`);
  screenshot();
}

async function toggleHuman() {
  const r = await mcp('human/set_mode', { on: !HUMAN });
  if (r?.error) { toast(r.error.message, true); addChat('ERROR', r.error.message); return; }
  updateModeButtons(r.result);
  if (HUMAN) {
    humanQuietUntil = Date.now() + 3000;
    addChat('INFO', '🧑 真人模式 ON：裸浏览器零 CDP 痕迹（无 Runtime.enable/无注入）。挑战页用「前往」打开等 5-15s 自动过验；期间避免 job 脚本 connectOverCDP（会给 tab 挂会话破坏隐身）。');
    screenshot();
  } else {
    addChat('INFO', '真人模式 OFF：Playwright 浏览器已恢复，登录态保留。');
    screenshot();
  }
}
function addChat(type, text) {
  const el = document.getElementById('ai-log');
  const div = document.createElement('div');
  div.className = 'log-' + (type === 'error' ? 'ERROR' : 'INFO');
  div.innerHTML = `<span class="log-time">${new Date().toTimeString().slice(0, 8)}</span> ${text}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

/* ================= Tab 条 ================= */
let tabsCache = [];
async function refreshTabs() {
  const r = await mcp('pw/tabs');
  const box = document.getElementById('tab-items');
  if (!r || r.error) { return; }
  tabsCache = r.result?.tabs || [];
  box.innerHTML = tabsCache.map((t, i) => `
    <span class="btab ${t.active ? 'active' : ''}" onclick="selectTab(${i})" title="${escAttr(t.url)}">
      ${t.tag ? `<span class="tag">${escHtml(t.tag)}</span>` : ''}
      <span class="t">${escHtml((t.title || t.url || 'about:blank').slice(0, 40))}</span>
      <span class="x" onclick="event.stopPropagation();closeTab(${i})" title="关闭">✕</span>
    </span>`).join('');
}
async function selectTab(i) {
  const r = await mcp('pw/tab_select', { index: i });
  if (r?.result?.image) showShot(r);
  addChat('INFO', `已切到 Tab #${i}`);
  refreshTabs();
}
async function closeTab(i) {
  const r = await mcp('pw/tab_close', { index: i });
  if (r?.error) { toast(r.error.message, true); return; }
  addChat('INFO', `已关 Tab #${i}`);
  refreshTabs(); scheduleShot(300);
}
async function newTab() {
  const r = await mcp('pw/new_tab', {});
  if (r?.error) { toast(r.error.message, true); return; }
  addChat('INFO', '新开 Tab');
  if (r?.result?.image) showShot(r);
  refreshTabs();
  document.getElementById('url-input').focus();
}

/* ================= Profile ================= */
async function refreshProfiles() {
  const r = await mcp('pw/profile_list');
  if (!r || r.error) return;
  const sel = document.getElementById('profile-select');
  const ps = r.result?.profiles || [];
  sel.innerHTML = ps.map(p =>
    `<option value="${escAttr(p.name)}" ${p.active ? 'selected' : ''}>${escHtml(p.name)}${p.logged_in ? ' ✅' : ' ○'}</option>`).join('');
  document.getElementById('st-profile').textContent = r.result?.active || '-';
}
async function selectProfile(name) {
  if (!name) return;
  if (!confirm(`切换到 profile "${name}"？浏览器将重启（各 profile 登录态独立保留）`)) { refreshProfiles(); return; }
  const r = await mcp('pw/profile_set', { name });
  if (r?.error) toast(r.error.message, true); else addChat('INFO', `已切换 profile: ${name}`);
  refreshProfiles(); refreshTabs(); scheduleShot(500);
}

/* ================= 工具栏 ================= */
async function toggleProxy() {
  proxyEnabled = !proxyEnabled;
  document.getElementById('proxy-btn').classList.toggle('on', proxyEnabled);
  const r = await mcp('pw/set_proxy', { enable: proxyEnabled });
  if (r?.result) { addChat('INFO', '代理已' + (proxyEnabled ? '开启' : '关闭')); scheduleShot(400); }
}
async function goBack() { addChat('INFO', '回退'); showShot(await mcp('pw/back')); }
async function reload() { addChat('INFO', '刷新'); showShot(await mcp('pw/reload')); }
async function nav(url) {
  addChat('INFO', '导航: ' + url);
  const r = await mcp('pw/navigate', { url });
  showShot(r);
  showElements(false);
}
async function gotoUrl() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  nav(url.startsWith('http') ? url : 'https://' + url);
}

/* ================= 截图 ================= */
let shotBusy = false, shotTimer = null;
function scheduleShot(delay) {
  clearTimeout(shotTimer);
  shotTimer = setTimeout(screenshot, delay || 600);
}
function showShot(r, silent) {
  if (r?.result?.image) {
    vpImg.src = 'data:image/png;base64,' + r.result.image;
  } else if (!silent && r?.error) {
    toast(r.error.message, true);
  }
}
async function screenshot() {
  if (shotBusy) return;
  shotBusy = true;
  try { showShot(await mcp('pw/screenshot')); } finally { shotBusy = false; }
}

/* ================= 元素模块（目标两栏 + 文字行 + 底部按钮） ================= */
/* 坐标必须是 "x,y" 文字格式，避免与元素选择器混在一个框里产生歧义 */
function parseXY(v) {
  v = (v || '').trim();
  if (!v) return null;
  const m = v.match(/^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/);
  if (!m) return null;
  return { x: parseFloat(m[1]), y: parseFloat(m[2]) };
}
function elemTarget() {
  /* 目标优先级：坐标文字 > 固定元素下拉。返回 {t, label} 或 null */
  const xy = parseXY(document.getElementById('xy-input').value);
  if (xy) return { t: { x: xy.x, y: xy.y }, label: `坐标(${xy.x},${xy.y})` };
  const sel = document.getElementById('elem-select').value;
  if (sel) return { t: { selector: sel }, label: '元素 ' + sel };
  return null;
}
function vpFocusElem(sel) {
  const e = VP.elems.find(el => (el.selector || '') === sel);
  if (!e) { VP.hl = null; vpDrawHl(); return; }
  VP.cursor = { x: e.x, y: e.y }; vpApply();
  VP.hl = { x: e.x, y: e.y, w: e.w || 30, h: e.h || 20 };
  vpDrawHl();
}
async function doClick() {
  const tg = elemTarget();
  if (!tg) { toast('先选「固定元素」或填「坐标 x,y」', true); return; }
  addChat('INFO', '点击: ' + escHtml(tg.label));
  showShot(await mcp('pw/click', tg.t));
}
async function doType() {
  const text = document.getElementById('type-input').value;
  if (!text) { toast('先填要输入的文字', true); return; }
  const tg = elemTarget();
  if (!tg) { toast('先选「固定元素」或填「坐标 x,y」', true); return; }
  addChat('INFO', '输入 → ' + escHtml(tg.label) + ': ' + escHtml(text));
  showShot(await mcp('pw/type', { ...tg.t, text }));
}
async function showElements(manual) {
  if (manual) addChat('INFO', '获取页面元素...');
  const r = await mcp('pw/elements');
  const box = document.getElementById('elements-list');
  const selEl = document.getElementById('elem-select');
  if (r?.result?.elements) {
    VP.elems = r.result.elements;
    // 同步「固定元素」下拉（保留第一个占位项）
    if (selEl) {
      selEl.innerHTML = '<option value="">固定元素（先「刷新元素」再选）</option>' +
        VP.elems.map(e => {
          const v = e.selector || (e.id ? '#' + e.id : '');
          const label = `<${e.tag}${e.id ? '#' + e.id : ''}> ${(e.text || '').slice(0, 24)} (${e.x},${e.y})`;
          return `<option value="${escAttr(v)}">${escHtml(label)}</option>`;
        }).join('');
    }
    box.innerHTML = `<div style="color:var(--muted);padding:4px 6px 6px;">${VP.elems.length} 个元素 · ${escHtml(r.result.url)}（悬停高亮，点击执行点击）</div>` +
      VP.elems.map((e, i) => `
        <div class="el-item" onclick="clickElement(${i})" onmouseenter="hoverElement(${i})" onmouseleave="hoverElement(-1)">
          <span class="etag">&lt;${escHtml(e.tag)}${e.id ? '#' + escHtml(e.id) : ''}&gt;</span>
          <span class="etxt">${escHtml(e.text || e.selector || '')}</span>
          <span class="ecoord">${e.x},${e.y}</span>
        </div>`).join('');
  } else {
    VP.elems = [];
    if (selEl) selEl.innerHTML = '<option value="">固定元素（先「刷新元素」再选）</option>';
    box.innerHTML = `<div style="color:var(--danger);padding:6px;">获取元素失败: ${escHtml(r?.error?.message || '')}</div>`;
  }
}
function hoverElement(i) {
  if (i < 0) { VP.hl = null; vpDrawHl(); return; }
  const e = VP.elems[i];
  if (!e) return;
  VP.hl = { x: e.x, y: e.y, w: e.w || 30, h: e.h || 20 };
  vpDrawHl();
}
async function clickElement(i) {
  const e = VP.elems[i];
  if (!e) return;
  // 列表条目与「固定元素」下拉联动
  const selEl = document.getElementById('elem-select');
  if (selEl && e.selector) selEl.value = e.selector;
  VP.cursor = { x: e.x, y: e.y }; vpApply();
  addChat('INFO', `点击元素 &lt;${e.tag}&gt; ${escHtml(e.text || e.selector || '')} @(${e.x},${e.y})`);
  await mcp('pw/mouse_down', { x: e.x, y: e.y, button: 'left' });
  showShot(await mcp('pw/mouse_up', { x: e.x, y: e.y, button: 'left' }));
}
async function doKey(key) {
  if (!key) return;
  addChat('INFO', '按键: ' + key);
  showShot(await mcp('pw/key', { key }));
  document.getElementById('key-select').value = '';
}
async function clearInputs() {
  addChat('INFO', '清空输入框');
  showShot(await mcp('pw/clear', { n: 10 }));
}

/* ================= AI 任务（browser-use 架构 agent） ================= */
let aiRunning = false, aiProgressTimer = null;
async function doCmd() {
  const input = document.getElementById('cmd-input');
  const cmd = input.value.trim();
  if (!cmd || aiRunning) return;
  if (cmd.startsWith('/goto ')) { input.value = ''; nav(cmd.slice(6)); return; }
  addChat('INFO', 'AI任务: ' + escHtml(cmd.length > 80 ? cmd.slice(0, 80) + '…' : cmd));
  aiRunning = true;
  document.getElementById('ai-run-btn').disabled = true;
  document.getElementById('ai-running').style.display = 'flex';
  // 实时进度：轮询服务端日志里的 [Agent] 行（每步 thought/actions 都会写日志）
  aiProgressTimer = setInterval(async () => {
    try {
      const r = await fetch('/debug/logs').then(x => x.json());
      const lines = (r.logs || []).filter(l => (l.msg || '').includes('[Agent]'));
      if (lines.length) document.getElementById('ai-running-text').textContent = lines[lines.length - 1].msg.slice(0, 90);
    } catch (e) {}
  }, 3000);
  try {
    const useVision = document.getElementById('ai-vision-run')?.checked;
    const r = await mcp('pw/ai_task', { task: cmd, max_steps: 50, use_vision: !!useVision });
    if (r?.result) {
      if (r.result.status === 'cancelled') addChat('error', '已取消（' + r.result.steps + ' 步）');
      else if (r.result.status === 'max_steps') addChat('error', `达到最大步数 ${r.result.steps}（未得到 done）最后URL: ${escHtml(r.result.final_url || '')}`);
      else addChat('INFO', '完成: ' + escHtml(r.result.result || 'done') + ' (步骤:' + r.result.steps + ')');
    } else if (r?.error) addChat('error', escHtml(r.error.message));
  } finally {
    aiRunning = false;
    clearInterval(aiProgressTimer);
    document.getElementById('ai-run-btn').disabled = false;
    document.getElementById('ai-running').style.display = 'none';
    document.getElementById('ai-running-text').textContent = 'AI 执行中…';
    scheduleShot(300);
  }
}
// 大指令框：Enter 执行 / Shift+Enter 换行（textarea 原生 Enter 是换行，这里反过来）
document.getElementById('cmd-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); doCmd(); }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doCmd(); }
});
async function cancelAiTask() {
  await mcp('ai/cancel');
  document.getElementById('ai-running-text').textContent = '正在取消…';
}

/* ================= AI 设置弹窗 ================= */
function openAiSettings() {
  document.getElementById('ai-settings-mask').classList.add('show');
  document.getElementById('ai-test-result').style.display = 'none';
  mcp('ai/config_get').then(r => {
    if (r?.result) {
      document.getElementById('ai-model').value = r.result.model || '';
      document.getElementById('ai-baseurl').value = r.result.base_url || '';
      document.getElementById('ai-key').value = '';
      document.getElementById('ai-key').placeholder = r.result.api_key_set
        ? `已配置 ${r.result.api_key_masked}（留空沿用）` : 'sk-...（未配置）';
      document.getElementById('ai-vision').checked = !!r.result.vision;
      const vr = document.getElementById('ai-vision-run');
      if (vr) vr.checked = !!r.result.vision;
    }
  });
}
function closeAiSettings() { document.getElementById('ai-settings-mask').classList.remove('show'); }
function aiPreset(kind) {
  if (kind === 'deepseek') {
    document.getElementById('ai-model').value = 'deepseek-chat';
    document.getElementById('ai-baseurl').value = 'https://api.deepseek.com/v1';
    document.getElementById('ai-baseurl').placeholder = 'https://api.deepseek.com/v1';
  } else {
    document.getElementById('ai-model').value = 'flash';
    document.getElementById('ai-baseurl').value = '';
    document.getElementById('ai-baseurl').placeholder = 'https://<flash 所在网关>/v1';
    toast('请填入 flash 所在网关的 Base URL 后保存');
  }
}
async function aiTest() {
  const box = document.getElementById('ai-test-result');
  box.style.display = 'block';
  box.className = '';
  box.textContent = '测试中...（使用当前表单值临时验证）';
  const r = await mcp('ai/test', {
    prompt: '只回复两个字符: ok',
    model: document.getElementById('ai-model').value.trim(),
    base_url: document.getElementById('ai-baseurl').value.trim(),
    api_key: document.getElementById('ai-key').value.trim(),
  });
  if (r?.result?.ok) { box.className = 'ok'; box.textContent = '✅ 连接成功: ' + (r.result.answer || ''); }
  else { box.className = 'bad'; box.textContent = '❌ ' + (r?.result?.error || r?.error?.message || '未知错误'); }
}
async function aiSave() {
  const model = document.getElementById('ai-model').value.trim();
  const base_url = document.getElementById('ai-baseurl').value.trim();
  const api_key = document.getElementById('ai-key').value.trim();
  const vision = document.getElementById('ai-vision').checked;
  if (!model || !base_url) { toast('模型和 Base URL 必填', true); return; }
  const r = await mcp('ai/config_set', { model, base_url, api_key, vision });
  if (r?.error) { toast(r.error.message, true); return; }
  addChat('INFO', `AI 配置已保存: ${escHtml(r.result.model)} @ ${escHtml(r.result.base_url)}${r.result.vision ? '（视觉 ON）' : ''}`);
  const vr2 = document.getElementById('ai-vision-run');
  if (vr2) vr2.checked = !!r.result.vision;
  toast('已保存并生效');
  closeAiSettings();
}
document.getElementById('ai-settings-mask').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeAiSettings();
});

/* ================= DevTools ================= */
async function openDevtools() {
  try {
    const r = await fetch('/devtools/targets').then(x => x.json());
    const targets = r.targets || [];
    if (!targets.length) { toast('无可用 DevTools 目标（先启动浏览器）', true); return; }
    const cur = tabsCache.find(t => t.active);
    const target = targets.find(t => cur && t.url === cur.url) || targets[0];
    const ws = `${location.host}/devtools/page/${target.id}`;
    window.open(`/devtools/inspector.html?ws=${ws}`, '_blank');
    addChat('INFO', 'DevTools 已打开: ' + escHtml(target.title || target.url));
  } catch (e) { toast('DevTools 打开失败: ' + e.message, true); }
}

/* ================= 状态轮询 ================= */
async function refreshStatus() {
  const [statusR, logsR, filesR, humanR] = await Promise.all([
    fetch('/debug/status').then(r => r.json()).catch(() => null),
    fetch('/debug/logs').then(r => r.json()).catch(() => null),
    fetch('/debug/files').then(r => r.json()).catch(() => null),
    mcp('human/status')
  ]);
  if (humanR?.result) updateModeButtons(humanR.result);
  if (statusR) {
    document.getElementById('st-browser').textContent = (humanR?.result?.human_mode || HUMAN) ? '真人模式' : (statusR.pw_context ? '运行中' : '未启动');
    document.getElementById('st-task').textContent = statusR.task_busy ? 'BUSY' : 'IDLE';
    const tb = document.getElementById('task-badge');
    tb.textContent = statusR.task_busy ? 'BUSY' : 'IDLE';
    tb.className = 'badge ' + (statusR.task_busy ? 'badge-busy' : 'badge-idle');
    let html = '';
    if (statusR.pages) for (const p of statusR.pages) html += `<div>P${p.index}: ${escHtml(p.url)}</div>`;
    document.getElementById('pages-list').innerHTML = html;
  }
  if (filesR) document.getElementById('st-auth').textContent = filesR.auth_json_exists ? '存在' : '不存在';
  if (logsR) {
    let html = '';
    for (const l of (logsR.logs || []).slice(-100).reverse())
      html += `<div class="log-${l.level}"><span class="log-time">${(l.time || '').slice(11, 19)}</span> ${escHtml(l.msg)}</div>`;
    document.getElementById('log-terminal').innerHTML = html;
  }
}

/* ================= 启动 ================= */
async function showMain() {
  document.getElementById('main-ui').style.display = 'flex';
  // 先同步真人模式/GPU 状态（服务端可能持久化为真人模式 ON）
  const hs = await mcp('human/status');
  if (hs?.result) updateModeButtons(hs.result);
  initBrowser();
  refreshStatus();
  refreshTabs();
  refreshProfiles();
  setInterval(refreshStatus, 5000);
  setInterval(refreshTabs, 15000);
  setInterval(() => { if (document.getElementById('follow-chk').checked && !aiRunning) screenshot(); }, 15000);
  // 真人模式：4s 轻量截图轮询（仅 Page.captureScreenshot，零 Runtime.enable），
  // 导航后 12s 静默期跳过，保证挑战页加载窗口完全零附着
  setInterval(() => { if (HUMAN && !document.hidden && Date.now() > humanQuietUntil) screenshot(); }, 4000);
  window.addEventListener('resize', vpFit);
}
async function initBrowser() {
  if (HUMAN) {
    addChat('INFO', '🧑 真人模式运行中：裸浏览器（无 Playwright），跳过 pw/init_browser');
    screenshot();
    return;
  }
  addChat('INFO', '正在初始化浏览器...');
  const r = await mcp('pw/init_browser');
  if (r?.result) { addChat('INFO', '浏览器就绪: ' + r.result.status); screenshot(); }
  else if (r?.error) addChat('error', '浏览器初始化失败: ' + escHtml(r.error.message));
}

/* ================= 键盘转发 ================= */
const KB_KEYS = new Set(['Enter', 'Tab', 'Escape', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' ', 'Delete', 'Home', 'End', 'PageUp', 'PageDown']);
let kbActive = false;
document.getElementById('kb-fwd')?.addEventListener('change', (e) => {
  kbActive = e.target.checked;
  if (kbActive) {
    vpOuter.focus();
    addChat('INFO', '键盘转发已开启（点击浏览器视口后按键直达 Chrome）');
  }
});
document.addEventListener('keydown', async (e) => {
  if (!kbActive) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  if (document.getElementById('ai-settings-mask').classList.contains('show')) return;
  const key = e.key;
  // Ctrl+C / Ctrl+V：本地↔远端剪贴板互通（键盘转发开启、焦点不在输入框时拦截）
  if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && (key === 'c' || key === 'v')) {
    e.preventDefault();
    key === 'v' ? vkbPaste() : vkbCopy();
    return;
  }
  // 修饰键组合（Shift+Tab / Ctrl+Enter 等）：单字符+shift 的 e.key 已是结果字符（如 'A'）直接发；
  // meta/alt 组合不拦——浏览器/OS 快捷键（Cmd+W 等）优先，需要 Cmd/Alt 组合时用「⌨ 小键盘」
  const mods = [];
  if (e.shiftKey && key !== 'Shift') mods.push('Shift');
  if (e.ctrlKey && key !== 'Control') mods.push('Control');
  const combo = mods.length && key.length > 1;
  if (combo && KB_KEYS.has(key)) {
    e.preventDefault();
    const k = mods.join('+') + '+' + key;
    addChat('INFO', '按键转发: ' + k);
    await mcp('pw/key', { key: k });
    scheduleShot(200);
  } else if (KB_KEYS.has(key) || (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey)) {
    e.preventDefault();
    addChat('INFO', '按键转发: ' + (key === ' ' ? 'Space' : key));
    await mcp('pw/key', { key: key === ' ' ? 'Space' : key });
    scheduleShot(200);
  }
});

/* ================= 虚拟小键盘（修饰键组合 + 功能键 + 多语言 IME 输入） ================= */
const vkbMods = new Set();
function toggleVkb(force) {
  const p = document.getElementById('vkb-panel');
  const show = force === undefined ? p.style.display === 'none' : !!force;
  p.style.display = show ? 'flex' : 'none';
  document.getElementById('vkb-btn').classList.toggle('on', show);
  if (show) document.getElementById('vkb-input').focus();
}
function vkbClearMods() {
  vkbMods.clear();
  document.querySelectorAll('.vkb-mod').forEach(b => b.classList.remove('on'));
}
document.querySelectorAll('.vkb-mod').forEach(b => b.addEventListener('click', () => {
  const m = b.dataset.mod;
  if (vkbMods.has(m)) { vkbMods.delete(m); b.classList.remove('on'); }
  else { vkbMods.add(m); b.classList.add('on'); }
}));
document.querySelectorAll('#vkb-panel .vkb-grid button').forEach(b => b.addEventListener('click', async () => {
  const k = [...vkbMods, b.dataset.key].join('+');
  addChat('INFO', '小键盘: ' + k);
  await mcp('pw/key', { key: k });
  vkbClearMods();            // 组合发送后修饰键自动熄灭（单击下一个键不会误带）
  scheduleShot(200);
}));
function vkbSendText() {
  const el = document.getElementById('vkb-input');
  const t = el.value;
  if (!t) return;
  addChat('INFO', '小键盘文本: ' + (t.length > 30 ? t.slice(0, 30) + '…' : t));
  el.value = '';
  mcp('pw/type', { text: t }).then(showShot);   // 真人模式自动路由 human/type（insertText 支持任意语言）
}
function vkbClearText() { document.getElementById('vkb-input').value = ''; }
document.getElementById('vkb-input')?.addEventListener('keydown', (e) => {
  e.stopPropagation();        // 文本框内的按键不进键盘转发
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); vkbSendText(); }
});

/* ================= 剪贴板互通（本地 ↔ 远端） ================= */
async function vkbPaste() {
  // 优先直读本地剪贴板（https/localhost 可用）；http 域名无权限则回落到小键盘文本框手动贴
  let text = '';
  try {
    if (navigator.clipboard?.readText) text = await navigator.clipboard.readText();
  } catch (e) { /* 非 Secure Context 或权限拒绝 */ }
  if (text) {
    addChat('INFO', '粘贴到远端: ' + (text.length > 30 ? text.slice(0, 30) + '…' : text));
    mcp('pw/type', { text, instant: true }).then(showShot);   // 真人模式自动路由 human/type（一次性 insertText）
    return;
  }
  toggleVkb(true);
  const el = document.getElementById('vkb-input');
  el.value = '';
  el.placeholder = '剪贴板直读不可用（http 页面无权限）：在这里 Ctrl+V 粘贴，再点「发送」';
  el.focus();
  toast('请在小键盘文本框里 Ctrl+V，再点发送');
}
async function vkbCopy() {
  const r = await mcp('pw/clip_read');
  const text = r?.result?.text || '';
  if (!text) { toast('远端页面没有选中的文本', true); return; }
  const box = document.getElementById('vkb-copybox');
  const ta = document.getElementById('vkb-copy-text');
  ta.value = text;
  box.style.display = 'flex';
  addChat('INFO', '远端选中 ' + text.length + ' 字符（面板里可复制回本地）');
}
function vkbCopyToLocal() {
  const ta = document.getElementById('vkb-copy-text');
  ta.select(); ta.setSelectionRange(0, ta.value.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  if (!ok && navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(ta.value).then(() => toast('已复制到本地剪贴板'), () => toast('复制失败，请手动 Ctrl+C', true));
    return;
  }
  toast(ok ? '已复制到本地剪贴板' : '复制失败，请手动 Ctrl+C', !ok);
}

/* ================= 视口全屏 ================= */
function toggleFullscreen() {
  const el = document.getElementById('vp-outer');
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else {
    const fn = el.requestFullscreen || el.webkitRequestFullscreen;
    if (fn) fn.call(el); else toast('当前浏览器不支持全屏 API', true);
  }
}
function _fsChanged() {
  const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
  const b = document.getElementById('fs-btn');
  b.classList.toggle('on', on);
  b.textContent = on ? '⛶ 退出全屏' : '⛶ 全屏';
  vpFit();                    // 全屏/退出后视口尺寸变了，重新适应
}
document.addEventListener('fullscreenchange', _fsChanged);
document.addEventListener('webkitfullscreenchange', _fsChanged);   // Safari

showMain();

