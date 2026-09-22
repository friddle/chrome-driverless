"""真人模式页面操作：raw CDP Input/Page 域（零 Runtime.enable/零注入）。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
import cd_state as S
from cd_actions import _motion_rng, _beat
from cd_cdp import _cdp_http, _cdp_rpc, _human_targets_refresh, _human_ws, _human_input, _human_shot
from cd_state import _add_log

async def human_screenshot(params=None):
    params = params or {}
    try:
        return {"result": await _human_shot(params.get("index"))}
    except Exception as e:
        return {"error": {"code": -1, "message": f"screenshot failed: {e}"}}


async def _navigate_tab(idx, tid, url):
    """对指定 tab 发 WS Page.navigate 并等 target url 离开 about:blank。
    新版 chromium 出于安全忽略 /json/new 的 url 参数（新 tab 必是 about:blank），
    必须补一条 Page.navigate（Page 域无需 enable，零注入原则不变）。"""
    ws, _ = _human_ws(idx)
    r = await _cdp_rpc(ws, "Page.navigate", {"url": url})
    if r.get("errorText"):
        raise RuntimeError(f"导航失败: {r['errorText']}")
    deadline = asyncio.get_event_loop().time() + 15
    while True:
        pages = await _human_targets_refresh()
        p = next((p for p in pages if p.get("id") == tid), None)
        if p and (p.get("url") or "") not in ("", "about:blank"):
            return
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError("页面未能开始加载（仍停在 about:blank）")
        await asyncio.sleep(0.4)


async def human_navigate(params):
    """真人模式导航：PUT /json/new 开空白 tab + WS Page.navigate——全程零 Runtime.enable/零注入。
    settle_ms 默认 6000：给 Turnstile/JS 挑战留自动完成时间；挑战页可传 10000-15000。"""
    url = (params.get("url") or "").strip()
    if not url:
        return {"error": {"code": -2, "message": "url is required"}}
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):  # scheme: 形式（含 data:/about:/file: 这类无 // 的）
        url = "https://" + url
    settle_ms = int(params.get("settle_ms", 6000) or 6000)
    try:
        t = await _cdp_http("/json/new", method="PUT")
    except Exception as e:
        return {"error": {"code": -1, "message": f"打开失败: {e}"}}
    new_id = t.get("id")
    deadline = asyncio.get_event_loop().time() + 10
    while True:
        pages = await _human_targets_refresh()
        idx = next((i for i, p in enumerate(pages) if p.get("id") == new_id), None)
        if idx is not None:
            S.human_active_idx = idx
            S.human_active_id = new_id
            break
        if asyncio.get_event_loop().time() > deadline:
            return {"error": {"code": -1, "message": "新 tab 未出现在 target 列表"}}
        await asyncio.sleep(0.3)
    try:
        await _navigate_tab(idx, new_id, url)
    except Exception as e:
        return {"error": {"code": -1, "message": str(e)}}
    await asyncio.sleep(max(0, settle_ms) / 1000.0)
    shot = await _human_shot()
    return {"result": {"status": "navigated", "url": url, "target_id": new_id, **shot}}


async def human_reload(params=None):
    """真人模式刷新：同 URL 走 /json/new 重开（保持零 CDP 加载），再关掉旧 tab。"""
    await _human_targets_refresh()
    if not S.human_targets:
        return {"error": {"code": -1, "message": "没有 tab"}}
    old_id = S.human_targets[S.human_active_idx].get("id")
    url = S.human_targets[S.human_active_idx].get("url") or "about:blank"
    if url == "about:blank":
        return {"result": {"status": "reloaded", **await _human_shot()}}
    r = await human_navigate({"url": url})
    try:
        pages = await _human_targets_refresh()
        old_idx = next((i for i, p in enumerate(pages) if p.get("id") == old_id), None)
        if old_idx is not None:
            await _cdp_http(f"/json/close/{old_id}")
            if S.human_active_idx > old_idx:
                S.human_active_idx -= 1
            await _human_targets_refresh()
    except Exception:
        pass
    return r


async def human_new_tab(params):
    url = (params.get("url") or "about:blank").strip() or "about:blank"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    try:
        t = await _cdp_http("/json/new", method="PUT")
    except Exception as e:
        return {"error": {"code": -1, "message": f"打开失败: {e}"}}
    new_id = t.get("id")
    deadline = asyncio.get_event_loop().time() + 10
    while True:
        pages = await _human_targets_refresh()
        idx = next((i for i, p in enumerate(pages) if p.get("id") == new_id), None)
        if idx is not None:
            S.human_active_idx = idx
            S.human_active_id = new_id
            break
        if asyncio.get_event_loop().time() > deadline:
            return {"error": {"code": -1, "message": "新 tab 未出现在 target 列表"}}
        await asyncio.sleep(0.3)
    if url != "about:blank":
        try:
            await _navigate_tab(idx, new_id, url)
        except Exception as e:
            return {"error": {"code": -1, "message": str(e)}}
    await asyncio.sleep(1)
    return {"result": {"status": "ok", **await _human_shot()}}


async def human_tabs(params=None):
    try:
        pages = await _human_targets_refresh()
    except Exception as e:
        return {"error": {"code": -1, "message": str(e)}}
    tabs = [{"index": i, "url": p.get("url", ""), "title": (p.get("title") or "")[:60],
             "tag": "", "active": i == S.human_active_idx} for i, p in enumerate(pages)]
    return {"result": {"tabs": tabs, "active_profile": S.ACTIVE_PROFILE}}


async def human_tab_select(params):
    await _human_targets_refresh()
    idx = int(params.get("index", -1))
    if idx < 0 or idx >= len(S.human_targets):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    S.human_active_idx = idx
    S.human_active_id = S.human_targets[idx].get("id")
    await _add_log("INFO", f"[Human] tab select #{idx} {S.human_targets[idx].get('url', '')[:80]}")
    return {"result": {"status": "ok", "index": idx, **await _human_shot()}}


async def human_tab_close(params):
    await _human_targets_refresh()
    idx = int(params.get("index", -1))
    if idx < 0 or idx >= len(S.human_targets):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    if len(S.human_targets) <= 1:
        return {"error": {"code": -3, "message": "cannot close the last tab"}}
    tid = S.human_targets[idx].get("id")
    await _cdp_http(f"/json/close/{tid}")
    if S.human_active_idx >= idx and S.human_active_idx > 0:
        S.human_active_idx -= 1
    await _human_targets_refresh()
    return {"result": {"status": "closed", "index": idx, **await _human_shot()}}


async def human_back(params=None):
    """后退：Page.getNavigationHistory + navigateToHistoryEntry（Page 域，无需 enable）。"""
    await _human_targets_refresh()
    ws, _ = _human_ws()
    hist = await _cdp_rpc(ws, "Page.getNavigationHistory")
    cur = hist.get("currentIndex", 0)
    entries = hist.get("entries", [])
    if cur <= 0 or not entries:
        return {"error": {"code": -1, "message": "no history to go back"}}
    entry_id = entries[cur - 1].get("id")
    await _cdp_rpc(ws, "Page.navigateToHistoryEntry", {"entryId": entry_id})
    await asyncio.sleep(2)
    return {"result": {"status": "back", **await _human_shot()}}


async def human_mouse_move(params):
    x, y = params.get("x"), params.get("y")
    if x is None or y is None:
        return {"error": {"code": -2, "message": "x,y required"}}
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none"})
    return {"result": {"status": "moved", "x": float(x), "y": float(y)}}


async def human_mouse_down(params):
    button = params.get("button", "left")
    x, y = params.get("x"), params.get("y")
    if x is not None and y is not None:
        await _human_input("Input.dispatchMouseEvent",
                           {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none"})
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mousePressed", "x": float(x or 0), "y": float(y or 0),
                        "button": button, "buttons": 1 if button == "left" else 2, "clickCount": 1})
    return {"result": {"status": "down", "button": button}}


async def human_mouse_up(params):
    """抬起鼠标并回新截图（与 pw/mouse_up 行为一致，触摸板松开依赖此返回图）。"""
    button = params.get("button", "left")
    x, y = params.get("x"), params.get("y")
    if x is not None and y is not None:
        await _human_input("Input.dispatchMouseEvent",
                           {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none"})
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mouseReleased", "x": float(x or 0), "y": float(y or 0),
                        "button": button, "buttons": 0, "clickCount": 1})
    await asyncio.sleep(0.6)
    return {"result": {"status": "up", "button": button, **await _human_shot()}}


async def human_click(params):
    """raw CDP 人性化点击：v2 idle 微漂移（点击前 2-3 个无目标小位移）→ 移动 → 节拍按压/释放。
    漂移与停顿时序走 cd_actions 的种子化节拍（HUMANIZE_SEED 可复现）。"""
    button = params.get("button", "left")
    x, y = float(params.get("x", 0)), float(params.get("y", 0))
    clicks = 2 if params.get("double") else 1
    # idle 微漂移：以目标点为圆心做 2-3 个无目标小位移（点击前零指针事件比曲线形状更响）
    cx, cy = x, y
    for _ in range(_motion_rng.randint(2, 3)):
        ang = _motion_rng.uniform(0, 2 * math.pi)
        rad = _motion_rng.uniform(12, 40)
        cx = min(1438.0, max(2.0, cx + math.cos(ang) * rad))
        cy = min(898.0, max(2.0, cy + math.sin(ang) * rad))
        await _human_input("Input.dispatchMouseEvent",
                           {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"})
        await asyncio.sleep(_beat() * _motion_rng.uniform(2, 5))
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
    for c in range(1, clicks + 1):
        await _human_input("Input.dispatchMouseEvent",
                           {"type": "mousePressed", "x": x, "y": y, "button": button,
                            "buttons": 1 if button == "left" else 2, "clickCount": c})
        await asyncio.sleep(_motion_rng.uniform(0.05, 0.12))
        await _human_input("Input.dispatchMouseEvent",
                           {"type": "mouseReleased", "x": x, "y": y, "button": button,
                            "buttons": 0, "clickCount": c})
        if c < clicks:
            await asyncio.sleep(_motion_rng.uniform(0.08, 0.18))
    await asyncio.sleep(0.6)
    return {"result": {"status": "clicked", "x": x, "y": y, **await _human_shot()}}


async def human_hover(params):
    x, y = params.get("x"), params.get("y")
    if x is None or y is None:
        return {"error": {"code": -2, "message": "x,y required"}}
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none"})
    await asyncio.sleep(0.3)
    return {"result": {"status": "hovered", **await _human_shot()}}


async def human_scroll_at(params):
    x, y = float(params.get("x", 640)), float(params.get("y", 450))
    dx = float(params.get("dx", 0) or 0)
    dy = float(params.get("dy", 0) or 0)
    await _human_input("Input.dispatchMouseEvent",
                       {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})
    await asyncio.sleep(0.3)
    return {"result": {"status": "scrolled", **await _human_shot()}}


_KEYMAP = {
    "Enter": (13, "Enter", "\r"), "Backspace": (8, "Backspace", ""), "Delete": (46, "Delete", ""),
    "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""),
    "ArrowUp": (38, "ArrowUp", ""), "ArrowDown": (40, "ArrowDown", ""),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""),
    "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""),
    "Space": (32, "Space", " "),
    **{f"F{i}": (111 + i, f"F{i}", "") for i in range(1, 13)},  # F1=112 ... F12=123
}
_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "command": 4, "shift": 8}


async def _human_press_key(key, modifiers=0):
    """单键：特殊键走映射（windowsVirtualKeyCode），单字符走 keyDown{text}+keyUp。"""
    if key in _KEYMAP:
        vk, code, text = _KEYMAP[key]
        down = {"type": "keyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        if text:
            down["text"] = text
            down["unmodifiedText"] = text
        await _human_input("Input.dispatchKeyEvent", down)
        up = {"type": "keyUp", "key": key, "code": code,
              "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        await _human_input("Input.dispatchKeyEvent", up)
    else:
        await _human_input("Input.dispatchKeyEvent",
                           {"type": "keyDown", "key": key, "text": key, "unmodifiedText": key,
                            "modifiers": modifiers})
        await _human_input("Input.dispatchKeyEvent",
                           {"type": "keyUp", "key": key, "text": key, "modifiers": modifiers})


async def human_key(params):
    """按键：human/key {key}。支持组合 "ctrl+a" / "shift+Tab" / 单键。回新截图。"""
    raw = (params.get("key") or "").strip()
    if not raw:
        return {"error": {"code": -2, "message": "key is required"}}
    modifiers = 0
    parts = [p for p in raw.split("+") if p] if "+" in raw else [raw]
    if len(parts) > 1:
        for m in parts[:-1]:
            modifiers |= _MODIFIERS.get(m.lower(), 0)
        key = parts[-1]
    else:
        key = parts[0]
    if len(key) == 1:
        # 显式 shift 修饰才强制大写；裸字符保留原样——shift+a 在前端 e.key 已是结果字符 'A'，
        # 此处 lower() 会把它打回小写（密码框/输入框大写变小写的根因）
        key = key.upper() if modifiers & 8 else key
    await _human_press_key(key, modifiers)
    await asyncio.sleep(0.4)
    return {"result": {"status": "key", "key": raw, **await _human_shot()}}


async def human_type(params):
    """输入文本：human/type {text}。ASCII 逐字符 keyDown/Up（带 30-90ms 随机间隔，触发
    页面 keydown 监听）；非 ASCII（中文等）走 Input.insertText（组合输入的正确通道）。"""
    text = params.get("text", "")
    if not text:
        return {"error": {"code": -2, "message": "text is required"}}
    buf = ""
    for ch in text:
        if ord(ch) < 128:
            await _human_press_key(ch)
            await asyncio.sleep(random.uniform(0.03, 0.09))
        else:
            buf += ch
    if buf:
        await _human_input("Input.insertText", {"text": buf})
    await asyncio.sleep(0.4)
    return {"result": {"status": "typed", "text": text, **await _human_shot()}}


async def human_clear(params=None):
    """清空输入框：ctrl+a 全选 + Delete。"""
    await _human_press_key("a", modifiers=2)
    await asyncio.sleep(0.15)
    await _human_press_key("Delete")
    await asyncio.sleep(0.3)
    return {"result": {"status": "cleared", **await _human_shot()}}


