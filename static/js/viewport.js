/* ================= 视口（多图层 + 缩放 + 拖拽 + 点击穿透） ================= */
const VP = { natW: 1280, natH: 900, scale: 1, tx: 0, ty: 0, fit: 1, elems: [], hl: null,
             cursor: { x: 640, y: 450 } };
const vpOuter = document.getElementById('vp-outer');
const vpWorld = document.getElementById('vp-world');
const vpImg = document.getElementById('vp-img');

function vpApply() {
  vpWorld.style.transform = `translate(${VP.tx}px, ${VP.ty}px) scale(${VP.scale})`;
  const pct = Math.round(VP.scale / VP.fit * 100) + '%';
  document.getElementById('vp-zoombadge').textContent = pct;
  document.getElementById('zoom-label').textContent = pct;
  vpDrawCursor();
}
function vpFit() {
  const r = vpOuter.getBoundingClientRect();
  if (VP.natW < 2 || r.width < 10) return;
  VP.fit = Math.min(r.width / VP.natW, r.height / VP.natH) || 1;
  VP.scale = VP.fit;
  VP.tx = (r.width - VP.natW * VP.scale) / 2;
  VP.ty = (r.height - VP.natH * VP.scale) / 2;
  vpApply();
}
function vpReset() { vpFit(); }
function vpZoom(factor, cx, cy) {
  const r = vpOuter.getBoundingClientRect();
  cx = cx ?? r.width / 2; cy = cy ?? r.height / 2;
  const ns = Math.min(Math.max(VP.scale * factor, VP.fit * 0.4), VP.fit * 8);
  const px = (cx - VP.tx) / VP.scale, py = (cy - VP.ty) / VP.scale;
  VP.tx = cx - px * ns; VP.ty = cy - py * ns; VP.scale = ns;
  vpApply();
}
function vpMapClient(cx, cy) {
  const r = vpImg.getBoundingClientRect();
  if (r.width < 2) return { x: 0, y: 0 };
  const px = (cx - r.left) / r.width * VP.natW;
  const py = (cy - r.top) / r.height * VP.natH;
  return { x: Math.max(0, Math.min(VP.natW, px)), y: Math.max(0, Math.min(VP.natH, py)) };
}
function vpDrawCursor() {
  const layer = document.getElementById('layer-cursor');
  const cx = layer.querySelector('.cx'), gv = layer.querySelector('.guide-v'), gh = layer.querySelector('.guide-h');
  cx.style.left = VP.cursor.x + 'px'; cx.style.top = VP.cursor.y + 'px';
  gv.style.left = VP.cursor.x + 'px'; gh.style.top = VP.cursor.y + 'px';
  const tp = document.getElementById('tp-dot');
  const pad = document.getElementById('trackpad').getBoundingClientRect();
  tp.style.left = (VP.cursor.x / VP.natW * pad.width) + 'px';
  tp.style.top = (VP.cursor.y / VP.natH * pad.height) + 'px';
}
function vpDrawHl() {
  const layer = document.getElementById('layer-elems');
  if (!VP.hl) { layer.innerHTML = ''; return; }
  const h = VP.hl;
  layer.innerHTML = `<div class="hl" style="left:${h.x - h.w / 2}px;top:${h.y - h.h / 2}px;width:${h.w}px;height:${h.h}px"></div>`;
}
vpImg.onload = () => {
  document.getElementById('vp-hint').style.display = 'none';
  const changed = vpImg.naturalWidth !== VP.natW;
  VP.natW = vpImg.naturalWidth || 1280;
  VP.natH = vpImg.naturalHeight || 900;
  vpWorld.style.width = VP.natW + 'px';
  vpWorld.style.height = VP.natH + 'px';
  if (changed) vpFit(); else vpApply();
};

/* 视口指针：拖动=平移 / 单击=远程点击 / 右键=远程右键 */
let vpPress = null;
vpOuter.addEventListener('pointerdown', (e) => {
  if (e.button === 2) return;
  vpPress = { x: e.clientX, y: e.clientY, tx: VP.tx, ty: VP.ty, moved: false, id: e.pointerId };
  vpOuter.setPointerCapture(e.pointerId);
});
vpOuter.addEventListener('pointermove', (e) => {
  if (!vpPress || e.pointerId !== vpPress.id) return;
  const dx = e.clientX - vpPress.x, dy = e.clientY - vpPress.y;
  if (!vpPress.moved && Math.hypot(dx, dy) > 5) { vpPress.moved = true; vpOuter.classList.add('panning'); }
  if (vpPress.moved) { VP.tx = vpPress.tx + dx; VP.ty = vpPress.ty + dy; vpApply(); }
});
vpOuter.addEventListener('pointerup', (e) => {
  if (!vpPress || e.pointerId !== vpPress.id) return;
  const wasClick = !vpPress.moved;
  vpPress = null;
  vpOuter.classList.remove('panning');
  if (!wasClick) return;
  const p = vpMapClient(e.clientX, e.clientY);
  VP.cursor = p; vpApply();
  remoteClickAt(p.x, p.y, 'left');
});
vpOuter.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  const p = vpMapClient(e.clientX, e.clientY);
  VP.cursor = p; vpApply();
  remoteClickAt(p.x, p.y, 'right');
});
vpOuter.addEventListener('dblclick', () => vpReset());
vpOuter.addEventListener('wheel', (e) => {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    const r = vpOuter.getBoundingClientRect();
    vpZoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
    return;
  }
  const p = vpMapClient(e.clientX, e.clientY);
  const unit = e.deltaMode === 1 ? 16 : 1;
  throttledScroll(p.x, p.y, e.deltaX * unit, e.deltaY * unit);
}, { passive: false });

async function remoteClickAt(x, y, button) {
  await mcp('pw/mouse_down', { x, y, button });
  showShot(await mcp('pw/mouse_up', { x, y, button }));
}

/* ================= 触摸板 ================= */
let tpButtonMode = 'left';
function tpButton(b) {
  tpButtonMode = b;
  document.getElementById('tp-btn-left').classList.toggle('on', b === 'left');
  document.getElementById('tp-btn-right').classList.toggle('on', b === 'right');
}
const tpEl = document.getElementById('trackpad');
let tpPtrs = new Map();      // pointerId -> {x,y}
let tpPressing = false;
let tpMoveTimer = null;      // mouse_move 节流（80ms）
let tpPending = null;
let tpScrollT = 0;

function tpPadSize() { const r = tpEl.getBoundingClientRect(); return { w: r.width || 1, h: r.height || 1 }; }
function tpMoveCursor(dx, dy) {
  const { w, h } = tpPadSize();
  const sens = 1.15;
  VP.cursor.x = Math.max(0, Math.min(VP.natW, VP.cursor.x + dx * (VP.natW / w) * sens));
  VP.cursor.y = Math.max(0, Math.min(VP.natH, VP.cursor.y + dy * (VP.natH / h) * sens));
  vpDrawCursor();
  tpPending = { x: VP.cursor.x, y: VP.cursor.y };
  if (!tpMoveTimer) {
    tpMoveTimer = setTimeout(async () => {
      tpMoveTimer = null;
      if (tpPending) { await mcp('pw/mouse_move', tpPending); }
    }, 80);
  }
}
tpEl.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  tpEl.setPointerCapture(e.pointerId);
  tpPtrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (tpPtrs.size === 1) {           // 单指按压 = 鼠标按下（整板上下压一致）
    tpPressing = true;
    tpEl.classList.add('pressing');
    mcp('pw/mouse_down', { x: VP.cursor.x, y: VP.cursor.y, button: tpButtonMode });
  }
});
tpEl.addEventListener('pointermove', (e) => {
  const prev = tpPtrs.get(e.pointerId);
  if (!prev) return;
  const dx = e.clientX - prev.x, dy = e.clientY - prev.y;
  tpPtrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (tpPtrs.size >= 2) {            // 双指 = 滚动
    throttledScroll(VP.cursor.x, VP.cursor.y, dx * 2, dy * 2);
  } else {
    tpMoveCursor(dx, dy);            // 单指滑动 = 光标移动（按压中即页面拖拽）
  }
});
function tpRelease(e) {
  if (!tpPtrs.has(e.pointerId)) return;
  tpPtrs.delete(e.pointerId);
  if (tpPtrs.size === 0 && tpPressing) {   // 抬起 = 鼠标抬起（与按压一致 → 完成点击）
    tpPressing = false;
    tpEl.classList.remove('pressing');
    mcp('pw/mouse_up', { x: VP.cursor.x, y: VP.cursor.y, button: tpButtonMode }).then(showShot);
  }
}
tpEl.addEventListener('pointerup', tpRelease);
tpEl.addEventListener('pointercancel', tpRelease);
tpEl.addEventListener('wheel', (e) => {
  e.preventDefault();
  throttledScroll(VP.cursor.x, VP.cursor.y, e.deltaX, e.deltaY);
}, { passive: false });

function throttledScroll(x, y, dx, dy) {
  const now = Date.now();
  if (now - tpScrollT < 120) return;
  tpScrollT = now;
  mcp('pw/scroll_at', { x, y, dx, dy }).then(r => { if (r?.result?.image) showShot(r, true); });
}

