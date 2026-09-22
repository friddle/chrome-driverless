"""页面动作类 MCP 实现：导航/截图/点击(人性化轨迹)/输入/按键/滚动/元素枚举。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
import cd_state as S
from cd_state import _add_log
from cd_browser import _page_for

# 最近一次虚拟鼠标位置（Playwright 不暴露，自维护用于人性化轨迹起点）
pw_last_mouse = {"x": 0.0, "y": 0.0}

# ================= humanize v2（移植 invisible_playwright 行为层方案） =================
# 1) 种子化随机源：HUMANIZE_SEED 可固定种子 → 失败可复现（种子进审计报告）；缺省系统熵。
# 2) 设备节拍时序：uniform(0.004,0.018) 均匀分布是循环签名 → 改围绕 8ms 设备轮询率的
#    聚集分布（70% 6-10ms / 20% 10-16ms）+ 10% 概率 30-80ms 修正停顿。
# 3) idle 微漂移：重要点击前先做 2-3 个无目标小位移——「点击前零指针事件比曲线形状更响」。

MOTION_SEED = None
_seed_env = (os.environ.get("HUMANIZE_SEED") or "").strip()
if _seed_env.lstrip("-").isdigit():
    MOTION_SEED = int(_seed_env)
_motion_rng = random.Random(MOTION_SEED)  # None → 系统熵


def _beat() -> float:
    """设备节拍时序（秒）：非均匀、有聚集、偶发修正停顿 → 无循环签名。"""
    r = _motion_rng.random()
    if r < 0.70:
        return _motion_rng.uniform(0.006, 0.010)
    if r < 0.90:
        return _motion_rng.uniform(0.010, 0.016)
    return _motion_rng.uniform(0.030, 0.080)


async def _idle_drift(page):
    """点击前无目标微漂移：2-3 个随机小位移（半径 12-40px），总时长 ~120-300ms。"""
    cx, cy = pw_last_mouse["x"], pw_last_mouse["y"]
    for _ in range(_motion_rng.randint(2, 3)):
        ang = _motion_rng.uniform(0, 2 * math.pi)
        rad = _motion_rng.uniform(12, 40)
        cx = min(1438.0, max(2.0, cx + math.cos(ang) * rad))
        cy = min(898.0, max(2.0, cy + math.sin(ang) * rad))
        await page.mouse.move(cx, cy)
        await asyncio.sleep(_beat() * _motion_rng.uniform(2, 5))
    pw_last_mouse.update(x=cx, y=cy)


def _human_path(sx: float, sy: float, tx: float, ty: float):
    """生成类人鼠标轨迹：二次贝塞尔（垂直抖动控制点）+ 缓入缓出 + 过冲回弹 + 微抖。
    v2：全部随机量走种子化 _motion_rng。"""
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy) or 1.0
    # 控制点：中垂线方向随机偏移 → 弧线而非直线
    off = _motion_rng.uniform(-0.3, 0.3) * dist
    mx, my = (sx + tx) / 2 - (dy / dist) * off, (sy + ty) / 2 + (dx / dist) * off
    steps = max(14, min(48, int(dist / 6)))
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        te = t * t * (3 - 2 * t)  # smoothstep 缓入缓出
        x = (1 - te) ** 2 * sx + 2 * (1 - te) * te * mx + te ** 2 * tx
        y = (1 - te) ** 2 * sy + 2 * (1 - te) * te * my + te ** 2 * ty
        pts.append((x + _motion_rng.uniform(-0.7, 0.7), y + _motion_rng.uniform(-0.7, 0.7)))
    if dist > 40:  # 过冲 4~12px 再回弹（人手常见的修正动作）
        ox, oy = tx + dx / dist * _motion_rng.uniform(4, 12), ty + dy / dist * _motion_rng.uniform(4, 12)
        pts.append((ox, oy))
        pts.append((tx + _motion_rng.uniform(-0.5, 0.5), ty + _motion_rng.uniform(-0.5, 0.5)))
    else:
        pts.append((tx, ty))
    return pts


async def _human_move(page, tx: float, ty: float, *, start=None):
    """按类人轨迹移动到目标点并记录终点。v2：步间时序走设备节拍 _beat()。"""
    sx = start["x"] if start else pw_last_mouse["x"]
    sy = start["y"] if start else pw_last_mouse["y"]
    if math.hypot(tx - sx, ty - sy) < 2:
        return
    for (x, y) in _human_path(sx, sy, tx, ty):
        await page.mouse.move(x, y)
        await asyncio.sleep(_beat())
    pw_last_mouse.update(x=tx, y=ty)


async def _raw_click(page, cx: float, cy: float, *, button: str = "left", click_count: int = 1):
    """底层人性化坐标点击：idle 微漂移 → 贝塞尔轨迹 → 停顿 → 按压（随机时长）→ 释放。

    与 locator.click() 的 actionability 检查解耦，命中坐标处最顶层的元素。
    v2：点击前先做无目标微漂移，轨迹/时序全走种子化节拍。
    """
    # 点击前零指针事件比曲线形状更响：先做 2-3 个无目标小位移
    await _idle_drift(page)
    # 目标点带 ±2px 随机偏移（人不会每次都点正中心）
    tx, ty = cx + _motion_rng.uniform(-2, 2), cy + _motion_rng.uniform(-2, 2)
    await _human_move(page, tx, ty)
    await asyncio.sleep(_motion_rng.uniform(0.09, 0.24))  # 点击前停留
    await page.mouse.down()
    await asyncio.sleep(_motion_rng.uniform(0.06, 0.15))  # 按压时长
    await page.mouse.up()
    await asyncio.sleep(_motion_rng.uniform(0.04, 0.1))


async def _click_locator_raw(page, loc, *, button: str = "left", click_count: int = 1):
    """对 locator 的包围盒中心做一次底层坐标点击（raw 模式/auto 兜底用）。"""
    bbox = await loc.bounding_box()
    if not bbox:
        raise RuntimeError("element has no bounding box (not rendered)")
    cx = bbox["x"] + bbox["width"] / 2
    cy = bbox["y"] + bbox["height"] / 2
    await _raw_click(page, cx, cy)
    return {"x": cx, "y": cy}



async def pw_evaluate(params):
    """在当前页面执行 JS，返回 JSON 序列化结果：pw/evaluate {expression|function}。
    用于 job 脚本无法覆盖的通用兜底（抓数据/状态探测），与 pw/ai_task 同款 CDP 直连模式。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    expr = params.get("expression", "") or params.get("function", "")
    if not expr:
        return {"error": {"code": -2, "message": "expression is required"}}
    try:
        rv = await page.evaluate(expr)
        return {"result": {"value": rv, "url": page.url}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"evaluate failed: {e}"}}

async def pw_set_proxy(params):
    enable = params.get("enable", True)
    S.use_proxy = enable
    await _add_log("INFO", f"[Proxy] set to {enable}")
    # 时区一致性（invisible_playwright: timezone-proxy-mismatch）：重启前用新出口反查时区
    try:
        from cd_audit import _egress_timezone
        tz = await _egress_timezone(C.HTTPS_PROXY if enable else None)
        if tz and tz != S.resolved_timezone:
            S.resolved_timezone = tz
            await _add_log("INFO", f"[Proxy] 出口时区 {tz} → context 时区已对齐")
    except Exception as e:
        await _add_log("WARN", f"[Proxy] 出口时区解析失败（沿用 {S.resolved_timezone or 'Asia/Shanghai'}）: {e}")
    if S.pw_context:
        try: await S.pw_context.close()
        except: pass
    S.pw_browser = S.pw_context = None
    page = await _page_for(params)
    if page:
        return {"result": {"status": "ok", "proxy": S.use_proxy, "url": page.url}}
    return {"error": {"code": -1, "message": "Restart failed"}}

async def pw_save_auth():
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    try:
        await S.pw_context.storage_state(path=S.AUTH_JSON_PATH)
        await _add_log("INFO", f"[Auth] Saved to {S.AUTH_JSON_PATH}")
        return {"result": {"status": "ok", "path": S.AUTH_JSON_PATH}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"Save failed: {e}"}}


async def pw_screenshot(params: dict = None):
    page = await _page_for(params or {})
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed (or index out of range)"}}
    screenshot_bytes = await page.screenshot(type="png")
    return {"result": {"image": base64.b64encode(screenshot_bytes).decode("utf-8"), "url": page.url}}


async def pw_navigate(params):
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    url = params.get("url", "")
    if not url:
        return {"error": {"code": -2, "message": "url is required"}}
    # 导航重试：ERR_ABORTED / interrupted（多导航竞争，常见于 about:blank 初始化后立即跳转）
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            last_err = None
            break
        except Exception as e:
            last_err = str(e)
            if "ERR_ABORTED" in last_err or "interrupted" in last_err:
                await asyncio.sleep(2)
                continue
            raise
    if last_err:
        return {"error": {"code": -1, "message": f"navigate failed: {last_err}"}}
    await asyncio.sleep(2)
    screenshot_bytes = await page.screenshot(type="png")
    return {"result": {"status": "navigated", "url": page.url, "image": base64.b64encode(screenshot_bytes).decode("utf-8")}}


async def pw_click(params):
    """远程鼠标点击：pw/click {selector|text|x,y[,index][,mode]}。

    - index: 指定目标 tab（与 pw/tabs 编号一致），缺省=当前活动页
    - mode:  auto(默认)=locator 优先、失败自动降级底层坐标点击；
             locator=仅 Playwright 定位点击（带 actionability 检查）；
             raw=直接对元素包围盒中心（或 x,y 坐标）发底层鼠标事件
    - button: left/right/middle；double: true 双击
    坐标兜底模式可命中覆盖层、动画中、hover 才出现等 locator 点不动的元素。
    """
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed (or index out of range)"}}
    selector = params.get("selector", "")
    text = params.get("text", "")
    x, y = params.get("x"), params.get("y")
    mode = str(params.get("mode", "auto") or "auto").lower()
    button = str(params.get("button", "left") or "left").lower()
    click_count = 2 if params.get("double") else 1
    try:
        if selector or text:
            loc = page.locator(selector).first if selector else page.get_by_text(text, exact=False).first
            try:
                await loc.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass  # 元素可能在视口外也点得到（框架滚动容器），留给 click/raw 兜底
            clicked = False
            if mode in ("auto", "locator"):
                try:
                    await loc.click(timeout=8000, button=button, click_count=click_count)
                    clicked = True
                except Exception:
                    if mode == "locator":
                        raise
            if not clicked and mode in ("auto", "raw"):
                # 底层坐标点击：locator 点不动（遮挡/动画/框架事件绑定特殊）时的兜底
                pos = await _click_locator_raw(page, loc, button=button)
                clicked = True
        elif x is not None and y is not None:
            await _raw_click(page, float(x), float(y))
        else:
            return {"error": {"code": -2, "message": "need selector|text|x,y"}}
        await asyncio.sleep(1)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "clicked", "url": page.url, "mode": mode if (selector or text) else "raw",
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"click failed: {e}"}}


async def pw_hover(params):
    """悬停：pw/hover {selector|text|index}。hover 才出现的菜单/按钮，先 hover 再 click。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed (or index out of range)"}}
    selector = params.get("selector", "")
    text = params.get("text", "")
    x, y = params.get("x"), params.get("y")
    try:
        if selector or text:
            loc = page.locator(selector).first if selector else page.get_by_text(text, exact=False).first
            bbox = await loc.bounding_box()
            if not bbox:
                raise RuntimeError("element has no bounding box")
            cx, cy = bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2
        elif x is not None and y is not None:
            cx, cy = float(x), float(y)
        else:
            return {"error": {"code": -2, "message": "need selector|text|x,y"}}
        await page.mouse.move(cx, cy, steps=6)
        await asyncio.sleep(0.6)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "hovered", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"hover failed: {e}"}}


async def pw_type(params):
    """远程键盘输入：pw/type {selector, text} 或 {x, y, text}（先点击目标再输入）。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    selector = params.get("selector", "")
    text = params.get("text", "")
    x, y = params.get("x"), params.get("y")
    if text is None:
        return {"error": {"code": -2, "message": "text is required"}}
    try:
        instant = bool(params.get("instant"))
        if instant:
            # 粘贴语义：一次性 insertText（真实粘贴是瞬时一整块，不逐字符拟人）
            if selector:
                loc = page.locator(selector).first
                await loc.scroll_into_view_if_needed()
                bbox = await loc.bounding_box()
                if bbox:
                    await _raw_click(page, bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
                else:
                    await loc.click(timeout=10000)
            elif x is not None and y is not None:
                await _raw_click(page, float(x), float(y))
            await page.keyboard.insertText(str(text))
        elif selector:
            loc = page.locator(selector).first
            await loc.scroll_into_view_if_needed()
            bbox = await loc.bounding_box()
            if bbox:
                await _raw_click(page, bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
            else:
                await loc.click(timeout=10000)
            await page.keyboard.type(str(text), delay=_motion_rng.uniform(50, 130))
        elif x is not None and y is not None:
            await _raw_click(page, float(x), float(y))
            await page.keyboard.type(str(text), delay=_motion_rng.uniform(50, 130))
        else:
            await page.keyboard.type(str(text), delay=_motion_rng.uniform(50, 130))
        await asyncio.sleep(0.5)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "pasted" if instant else "typed", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"type failed: {e}"}}


async def pw_key(params):
    """发送键盘按键（Enter/Tab/Escape 等）：pw/key {key}。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    key = params.get("key", "Enter")
    try:
        await page.keyboard.press(key)
        await asyncio.sleep(1)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "key_pressed", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"key failed: {e}"}}


async def pw_clip_read(params: dict = None):
    """读页面选中文本（兼容 input/textarea 内选中段）：本地↔远端剪贴板互通的复制侧。"""
    page = await _page_for(params or {})
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    expr = ("const a=document.activeElement;"
            "if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')&&a.value"
            "&&a.selectionStart!=null&&a.selectionEnd>a.selectionStart)"
            "return a.value.slice(a.selectionStart,a.selectionEnd);"
            "const s=String(window.getSelection?window.getSelection():'');"
            "return s==='[object Selection]'?'':s;")
    try:
        val = await page.evaluate("(() => {" + expr + "})()")
        return {"result": {"text": str(val or "")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"clip_read failed: {e}"}}


async def pw_back(params: dict = None):
    """浏览器后退：pw/back [index]。"""
    page = await _page_for(params or {})
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        await page.go_back(timeout=15000)
        await asyncio.sleep(1)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "backed", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"back failed: {e}"}}


async def pw_reload(params: dict = None):
    """刷新当前页面：pw/reload [index]。"""
    page = await _page_for(params or {})
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "reloaded", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"reload failed: {e}"}}


async def pw_clear(params=None):
    """清空浏览器输入：pw/clear。
    策略：①清空当前聚焦的 input/textarea；②否则退格 Backspace ×N（默认10，兼容无焦点场景）。"""
    params = params or {}
    n = int(params.get("n", 10) or 10)
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        # ① 清空聚焦 input
        cleared = await page.evaluate("""() => {
          const el = document.activeElement;
          if (!el) return false;
          const tag = el.tagName.toLowerCase();
          if (tag === 'input' || tag === 'textarea') {
            const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, '');
            el.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
          }
          return false;
        }""")
        if not cleared:
            # ② 退格清空（无聚焦 input 时的兜底）
            for _ in range(n):
                await page.keyboard.press("Backspace")
            await _add_log("INFO", f"[Clear] focus 非输入框，Backspace ×{n}")
        shot = await page.screenshot(type="png")
        return {"result": {"status": "cleared", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"clear failed: {e}"}}



async def pw_elements(params: dict = None):
    """列出页面可交互元素的 id/selector/坐标/类型/文本（定位登录框用）：pw/elements [index]。"""
    page = await _page_for(params or {})
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        data = await page.evaluate("""() => {
          const sel = 'input, button, [role=button], a, select, textarea, [tabindex]';
          const out = [];
          document.querySelectorAll(sel).forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 5 || r.height < 5) return;
            const tag = el.tagName.toLowerCase();
            const id = el.id || '';
            const name = el.name || '';
            const type = el.type || '';
            const txt = (el.innerText || el.value || el.placeholder || '').toString().trim().slice(0, 40);
            out.push({
              tag, id, name, type, text: txt,
              x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
              w: Math.round(r.width), h: Math.round(r.height),
              selector: (id ? '#'+id : tag + (name ? '[name="'+name+'"]' : ''))
            });
          });
          return out.slice(0, 60);
        }""")
        return {"result": {"elements": data, "url": page.url}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"elements failed: {e}"}}



async def pw_mouse_move(params):
    """移动虚拟鼠标（不回截图，触摸板高频调用用）：pw/mouse_move {x,y}。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    x, y = params.get("x"), params.get("y")
    if x is None or y is None:
        return {"error": {"code": -2, "message": "x,y required"}}
    try:
        await page.mouse.move(float(x), float(y))
        pw_last_mouse.update(x=float(x), y=float(y))
        return {"result": {"status": "moved", "x": float(x), "y": float(y)}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"move failed: {e}"}}


async def pw_mouse_down(params):
    """按下鼠标（触摸板按压）：pw/mouse_down {x,y,button=left|right|middle}。不回截图。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    button = params.get("button", "left")
    try:
        x, y = params.get("x"), params.get("y")
        if x is not None and y is not None:
            await page.mouse.move(float(x), float(y))
        await page.mouse.down(button=button)
        return {"result": {"status": "down", "button": button}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"mouse_down failed: {e}"}}


async def pw_mouse_up(params):
    """抬起鼠标（触摸板松开），回新截图：pw/mouse_up {x,y,button}。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    button = params.get("button", "left")
    try:
        x, y = params.get("x"), params.get("y")
        if x is not None and y is not None:
            await page.mouse.move(float(x), float(y))
        await page.mouse.up(button=button)
        await asyncio.sleep(0.6)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "up", "button": button, "url": page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"mouse_up failed: {e}"}}


async def pw_scroll_at(params):
    """在指定坐标滚动（触摸板双指/滚轮）：pw/scroll_at {x,y,dx,dy}。"""
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        x, y = params.get("x"), params.get("y")
        dx = int(params.get("dx", 0) or 0)
        dy = int(params.get("dy", 0) or 0)
        if x is not None and y is not None:
            await page.mouse.move(float(x), float(y))
        await page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.3)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "scrolled", "url": page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"scroll failed: {e}"}}
