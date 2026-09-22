"""HTTP/WS 路由：健康检查、静态 UI、debug 端点、DevTools 反代（HTTP+WS）。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

from fastapi import WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from cd_app import app
import cd_config as C
import cd_state as S
from cd_config import websockets, HAS_WS, DATA_DIR, SCRIPTS_DIR, EXTERNAL_URL, CDP_PORT
from cd_ai import _llm_http_error

CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/files/download")
async def download_file(path: str):
    safe_path = os.path.normpath(path).lstrip("/")
    full_path = os.path.join(DATA_DIR, safe_path)
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(full_path, filename=os.path.basename(full_path))


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "r") as f:
        return f.read()


@app.get("/debug/status")
async def debug_status():
    status = {
        "pw_browser": S.pw_browser is not None,
        "pw_context": S.pw_context is not None,
        "pages": [],
        "task_busy": S.task_busy,
        "data_dir": DATA_DIR,
    }
    if S.pw_context and S.pw_context.pages:
        for i, page in enumerate(S.pw_context.pages):
            try:
                status["pages"].append({"index": i, "url": page.url, "title": await page.title()})
            except Exception as e:
                status["pages"].append({"index": i, "url": "error", "title": str(e)})
    return status


@app.get("/debug/logs")
async def debug_logs():
    async with S.mcp_logs_lock:
        return {"logs": list(S.mcp_logs)}


@app.get("/debug/files")
async def debug_files():
    files = []
    for root, dirs, filenames in os.walk(DATA_DIR):
        for f in filenames:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                files.append({"path": os.path.relpath(fp, DATA_DIR), "size": st.st_size,
                              "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
            except Exception:
                pass
    script_files = []
    if os.path.isdir(SCRIPTS_DIR):
        for f in os.listdir(SCRIPTS_DIR):
            fp = os.path.join(SCRIPTS_DIR, f)
            if os.path.isfile(fp):
                script_files.append({"name": f, "size": os.path.getsize(fp)})
    return {"data_files": files, "script_files": script_files, "auth_json_exists": os.path.exists(S.AUTH_JSON_PATH)}


@app.get("/debug/url")
async def debug_url():
    """返回远程浏览器地址 (由 EXTERNAL_URL 环境变量配置)"""
    return {
        "external_url": EXTERNAL_URL or "(not configured)",
        "mcp_endpoint": "/mcp",
    }



@app.get("/devtools/targets")
async def devtools_targets():
    """CDP /json 的 page 目标列表（前端选当前 tab 对应的 target 打开 inspector）。"""
    def fetch():
        with urllib.request.urlopen(f"{CDP_HTTP}/json", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    try:
        targets = await asyncio.to_thread(fetch)
    except Exception as e:
        return {"error": f"CDP /json failed: {e}"}
    pages = []
    for t in targets:
        if t.get("type") == "page":
            pages.append({"id": t.get("id"), "title": (t.get("title") or "")[:80], "url": t.get("url")})
    return {"targets": pages}


@app.get("/devtools/{rest:path}")
async def devtools_http(rest: str):
    """静态资源与 inspector.html 反代（GET；WS 走 /devtools/page|browser 专用路由）。"""
    url = f"{CDP_HTTP}/devtools/{rest}"
    def fetch():
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers.get("Content-Type", "application/octet-stream"), resp.read()
    try:
        status, ctype, body = await asyncio.to_thread(fetch)
    except urllib.error.HTTPError as e:
        return JSONResponse({"error": _llm_http_error(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"devtools proxy failed: {e}"}, status_code=502)
    return Response(content=body, media_type=ctype, status_code=status)


async def _ws_bridge(ws_url_path: str, ws: "WebSocket"):
    """CDP WebSocket 双向桥：浏览器前端 ↔ chromium CDP。"""
    if not HAS_WS:
        await ws.close(code=1011, reason="websockets lib not installed")
        return
    await ws.accept()
    try:
        upstream = await websockets.connect(f"ws://127.0.0.1:{CDP_PORT}{ws_url_path}",
                                            max_size=None, max_queue=None, ping_interval=None)
    except Exception as e:
        await ws.close(code=1011, reason=f"cdp connect failed: {e}")
        return

    async def c2s():
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if (msg.get("text") or "") == "" and msg.get("bytes") is None:
                    continue
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass
        finally:
            try: await upstream.close()
            except Exception: pass

    async def s2c():
        try:
            async for msg in upstream:
                if isinstance(msg, str):
                    await ws.send_text(msg)
                else:
                    await ws.send_bytes(msg)
        except Exception:
            pass
        finally:
            try: await ws.close()
            except Exception: pass

    await asyncio.gather(c2s(), s2c())


@app.websocket("/devtools/page/{page_id}")
async def devtools_page_ws(ws: "WebSocket", page_id: str):
    await _ws_bridge(f"/devtools/page/{page_id}", ws)


@app.websocket("/devtools/browser")
async def devtools_browser_ws(ws: "WebSocket"):
    def fetch_version():
        with urllib.request.urlopen(f"{CDP_HTTP}/json/version", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    try:
        ver = await asyncio.to_thread(fetch_version)
        ws_url = ver.get("webSocketDebuggerUrl", "")
        path = ws_url[ws_url.find("/devtools"):] if "/devtools" in ws_url else "/devtools/browser"
    except Exception:
        path = "/devtools/browser"
    await _ws_bridge(path, ws)




# 前端静态资源（style.css / js/*.js）
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
