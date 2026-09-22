"""raw CDP 通道（DevTools HTTP + 单命令 WebSocket）与 GPU 探测。绝不发 Runtime.enable。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
import cd_state as S
from cd_config import websockets, HAS_WS, CDP_PORT
from cd_state import _add_log

def _detect_gpu():
    """探测容器内 GPU：nvidia-smi（NVIDIA container runtime 注入）或 /dev/dri（intel/amd 直通）。"""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=5)
            names = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
            if names:
                return {"available": True, "kind": "nvidia", "detail": names[0]}
        except Exception:
            pass
    try:
        dri = [f for f in os.listdir("/dev/dri") if f.startswith("card")]
        if dri:
            return {"available": True, "kind": "dri", "detail": ",".join(dri)}
    except Exception:
        pass
    return {"available": False, "kind": None, "detail": ""}



async def _cdp_http(path, method="GET", timeout=5):
    """DevTools HTTP 端点（/json/*）。PUT /json/new 开 tab 不产生任何 CDP 会话（隐身导航的关键）。
    /json/close 等端点返回纯文本（"Target is closing"），非 JSON 不再崩解析。"""
    def _do():
        req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}{path}", method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
        try:
            return json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            return {"raw": body}
    return await asyncio.to_thread(_do)


async def _cdp_rpc(ws_url, cdp_method, cdp_params=None, timeout=20):
    """raw CDP 单命令：独立短连接，绝不发送 Runtime.enable / Page.addScriptToEvaluateOnNewDocument
    / Runtime.addBinding。真人模式只允许无注入副作用的命令：Input.*、Page.captureScreenshot、
    Page.navigate、Page.getNavigationHistory、Runtime.evaluate（一次性 infra 自检）。"""
    if not HAS_WS:
        raise RuntimeError("websockets 未安装，真人模式不可用")
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        mid = random.randint(1, 10 ** 9)
        await ws.send(json.dumps({"id": mid, "method": cdp_method, "params": cdp_params or {}}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {cdp_method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result") or {}


async def _human_targets_refresh():
    lst = await _cdp_http("/json/list")
    S.human_targets = [t for t in lst if t.get("type") == "page"]
    # /json/list 顺序随 target 增删漂移：按 id 重定位活动 tab，位置索引只作展示/交互
    aid = getattr(S, "human_active_id", None)
    idx = next((i for i, t in enumerate(S.human_targets) if t.get("id") == aid), None)
    if idx is not None:
        S.human_active_idx = idx
    if S.human_active_idx >= len(S.human_targets):
        S.human_active_idx = max(0, len(S.human_targets) - 1)
    if S.human_targets:
        S.human_active_id = S.human_targets[S.human_active_idx].get("id")
    return S.human_targets


def _human_ws(idx=None):
    if not S.human_targets:
        raise RuntimeError("没有可用的浏览器 tab")
    i = S.human_active_idx if idx is None else idx
    t = S.human_targets[i]
    ws = t.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError(f"tab #{i} 无 webSocketDebuggerUrl")
    return ws, t


async def _human_input(cdp_method, cdp_params):
    await _human_targets_refresh()
    ws, _ = _human_ws()
    return await _cdp_rpc(ws, cdp_method, cdp_params)


async def _human_input_stream(steps):
    """同一 WS 会话顺序执行输入步骤：steps = [([ (method, params), ... ], pause_s), ...]。
    双击的 press/release 对必须在同一输入流里——Chromium 跨连接的事件不累加
    clickCount/detail，会被识别成两次独立单击（dblclick 消失）。"""
    await _human_targets_refresh()
    ws, _ = _human_ws()
    async with websockets.connect(ws, max_size=64 * 1024 * 1024) as conn:
        mid = random.randint(1, 10 ** 9)
        for events, pause in steps:
            for method, params in events:
                await conn.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(conn.recv(), 20))
                    if msg.get("id") == mid:
                        break
                mid += 1
            if pause:
                await asyncio.sleep(pause)


async def _human_shot(idx=None):
    """当前（或指定）tab 截图：Page.captureScreenshot（无需 enable，页面零感知）。"""
    await _human_targets_refresh()
    ws, t = _human_ws(idx)
    r = await _cdp_rpc(ws, "Page.captureScreenshot", {"format": "png"})
    return {"image": r.get("data", ""), "url": t.get("url", ""), "title": t.get("title", "")}


async def _probe_webgl():
    """WebGL renderer 一次性探测（Runtime.evaluate 临时求值，不在挑战 tab 常驻）。"""
    try:
        await _human_targets_refresh()
        if not S.human_targets:
            return S.webgl_info
        ws, _ = _human_ws()
        expr = ("(()=>{try{const c=document.createElement('canvas');"
                "const g=c.getContext('webgl')||c.getContext('experimental-webgl');"
                "if(!g)return{renderer:'',vendor:''};"
                "const d=g.getExtension('WEBGL_debug_renderer_info');"
                "return{renderer:String(g.getParameter(d.UNMASKED_RENDERER_WEBGL)),"
                "vendor:String(g.getParameter(d.UNMASKED_VENDOR_WEBGL))};}catch(e){return{renderer:'err:'+e.message,vendor:''}}})()")
        r = await _cdp_rpc(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        val = (r.get("result") or {}).get("value") or {}
        if val.get("renderer"):
            S.webgl_info = {"renderer": val["renderer"], "vendor": val["vendor"]}
    except Exception as e:
        await _add_log("WARN", f"[GPU] WebGL 探测失败: {e}")
    return S.webgl_info




# GPU 直通不是开关：启动时自动探测，宿主机有 GPU（NVIDIA runtime/DRI 直通）就默认启用，
# 没有则回落 swiftshader 软渲染。前端只做状态徽标展示。
S.gpu_mode = _detect_gpu()["available"]
