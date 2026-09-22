"""浏览器生命周期：Playwright persistent context 启动/恢复、tab 管理、profile 管理。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

from playwright.async_api import async_playwright
import cd_config as C
import cd_state as S
from cd_config import FP_PROFILES, HTTP_PROXY, HTTPS_PROXY, NO_PROXY, CDP_PORT, PROFILES_DIR, _engine_binary
from cd_stealth import STEALTH_JS, _fp_init_js, _apply_fp_cdp
from cd_cdp import _probe_webgl
from cd_state import _add_log, _profile_auth, _set_active_profile

def _browser_opts(storage_state=None):
    opts = dict(
        locale="zh-CN",
        # 时区：pw_set_proxy 时经代理反查出口时区写回（未解析到则默认上海）
        timezone_id=S.resolved_timezone or "Asia/Shanghai",
        viewport={"width": 1280, "height": 900},
        # UA 不覆盖：真 Linux headed Chrome 上报真实 UA + Client Hints(sec-ch-ua) 自洽，
        # 之前伪造 Mac/Chrome120 会与 navigator.platform(Linux)及真实内核版本冲突，抬高 CF 指纹不一致分
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    if storage_state:
        opts["storage_state"] = storage_state
    if S.use_proxy:
        # 代理：优先 HTTPS_PROXY（浏览器访问多为 https 站点，如 deepinfra/google）；
        # 两者通常指向同一出口；HTTP_PROXY 作为回退。playwright 单 server 同时服务 http+https(CONNECT)。
        proxy_server = HTTPS_PROXY or HTTP_PROXY
        if proxy_server:
            proxy = {"server": proxy_server}
            if NO_PROXY:
                proxy["bypass"] = NO_PROXY
            opts["proxy"] = proxy
    return opts



def _chrome_args(extra=None):
    """chromium 启动参数：Playwright launch 与真人模式裸进程共用同一套，保证两种模式指纹一致。
    S.gpu_mode=False：swiftshader 软渲染（无 GPU 容器的默认）；
    S.gpu_mode=True：去掉软渲染组，宿主 GPU 直通时 ANGLE 走真 GPU（WebGL renderer 为真实显卡）。"""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--start-maximized",
        "--window-size=1440,900",
        "--disable-dev-shm-usage",
    ]
    if S.gpu_mode:
        args += ["--ignore-gpu-blocklist", "--enable-gpu-rasterization"]
    else:
        args += [
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--use-gl=swiftshader",
            "--enable-unsafe-swiftshader",
            "--disable-features=VizDisplayCompositor",
        ]
    # CDP 端口与 playwright 的 --remote-debugging-pipe 共存（实测可用）：
    # job 脚本 connectOverCDP 在同一浏览器开「任务级独立 tab」；只绑 127.0.0.1 防外探测
    # WebRTC 泄漏（invisible_playwright: webrtc-leak-proxy）：代理场景禁非代理 UDP，
    # 否则 WebRTC 暴露的本地/公网 IP 与代理出口 IP 互相矛盾，双向都是 tell
    if S.use_proxy and (C.HTTPS_PROXY or C.HTTP_PROXY):
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    args += [f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1"]
    if extra:
        args += extra
    return args



def _find_chrome_binary():
    """定位 chromium 可执行文件：优先 BROWSER_ENGINE 指定的真 Chrome，
    其次 Playwright 运行时注册路径，最后扫 ms-playwright 缓存目录。"""
    bin_path, _ = _engine_binary()
    if bin_path:
        return bin_path
    try:
        if S.pw_instance:
            p = S.pw_instance.chromium.executable_path
            if p and os.path.exists(p):
                return p
    except Exception:
        pass
    roots = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        roots.append(env)
    roots.append(os.path.expanduser("~/.cache/ms-playwright"))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            if d.startswith("chromium-") and "headless" not in d:
                for sub in ("chrome-linux64/chrome", "chrome-linux/chrome"):
                    p = os.path.join(root, d, sub)
                    if os.path.exists(p):
                        return p
    return None



def _kill_orphan_chrome():
    """杀掉所有残留 chromium 进程（/proc 扫描）：孤儿进程占着 profile 和 CDP 端口，
    会让下一次 launch 悬死（端口被占 → playwright 等不到 DevTools listening）。"""
    killed = 0
    try:
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
                if "chrome-linux64/chrome" in cmd:
                    os.kill(int(p), 9)
                    killed += 1
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    except Exception:
        pass
    return killed


_pw_init_lock = asyncio.Lock()  # 串行化浏览器启动（并发 launch 会互相争 profile/端口而悬死）


pw_active_page = None  # MCP 会话当前操作的 tab（pw/tab_select 切换；None=主 tab pages[0]）
tab_tags = {}  # id(page) -> 任务标签（pw/tab_tag 设置，随 pw/tabs 展示；tab 关闭即失效）



async def _ensure_pw_context():
    if S.human_mode:
        raise RuntimeError("真人模式运行中（裸浏览器，无 Playwright）：先 human/set_mode {on:false} 再用 pw/*")
    if S.pw_context and S.pw_context.pages:
        try:
            if S.pw_active_page and not S.pw_active_page.is_closed():
                return S.pw_active_page
        except Exception:
            pass
        # 活动页已关闭（弹窗自动关闭/手动关tab等）：回退到最近打开的页面，
        # 而不是 pages[0]（最早的tab，往往是用户意想不到的目标）。
        S.pw_active_page = S.pw_context.pages[-1]
        return S.pw_active_page
    async with _pw_init_lock:
        if S.pw_context and S.pw_context.pages:  # 双检：等锁期间别人已启动好
            S.pw_active_page = S.pw_context.pages[-1]
            return S.pw_active_page

        os.makedirs(S.BROWSER_PROFILE_DIR, exist_ok=True)
        # 清理残留 profile 锁（异常退出会留下 Singleton*，导致 Chromium 认为 profile 被占用而卡死）
        try:
            for f in os.listdir(S.BROWSER_PROFILE_DIR):
                if f.startswith("Singleton"):
                    os.remove(os.path.join(S.BROWSER_PROFILE_DIR, f))
                    await _add_log("WARN", f"[PW] 清理残留锁: {f}")
        except Exception:
            pass
        n = _kill_orphan_chrome()
        if n:
            await _add_log("WARN", f"[PW] 清理孤儿 chromium 进程 ×{n}")

        if S.pw_browser:
            try: await S.pw_browser.close()
            except: pass
        if S.pw_instance:
            try: await S.pw_instance.stop()
            except: pass

    S.pw_instance = await async_playwright().start()
    opts = _browser_opts()
    opts["headless"] = False  # 有头模式（配合 xvfb 虚拟显示），规避 Google/阿里云 headless 检测
    opts["user_data_dir"] = S.BROWSER_PROFILE_DIR
    # 引擎选择：BROWSER_ENGINE=chrome 时用镜像内真 Google Chrome（UA/brands/Widevine 原生自洽）
    bin_path, engine_kind = _engine_binary()
    if bin_path:
        opts["executable_path"] = bin_path
    # 指纹档案（非 real）：HTTP 层 UA 由 context 选项覆盖；JS 层由 _fp_init_js 覆盖层接管；
    # 网络层 Client Hints 头由 _apply_fp_cdp 的 Emulation.setUserAgentOverride 同步
    fp = FP_PROFILES.get(S.FP_PROFILE)
    if fp and fp.get("user_agent"):
        opts["user_agent"] = fp["user_agent"]
    # 启动参数统一走 _chrome_args()：与真人模式裸进程共用同一套（反自动化 + GPU/软渲染开关）
    opts["args"] = _chrome_args()
    # 启动带超时：悬死（端口被占/渲染卡住）时杀进程重试一次，而不是永远挂着
    last_err = None
    for attempt in range(2):
        try:
            S.pw_context = await asyncio.wait_for(
                S.pw_instance.chromium.launch_persistent_context(**opts), timeout=90)
            break
        except asyncio.TimeoutError:
            last_err = "launch timeout(90s)"
            await _add_log("ERROR", f"[PW] {last_err}，attempt={attempt+1}，清理孤儿进程后重试")
            _kill_orphan_chrome()
            S.pw_context = None
        except Exception as e:
            last_err = str(e)
            await _add_log("ERROR", f"[PW] launch 失败 attempt={attempt+1}: {e}")
            _kill_orphan_chrome()
            S.pw_context = None
            if "XServer" in last_err or "headless" in last_err:
                break  # 无显示服务，重试无用
    if S.pw_context is None:
        raise RuntimeError(f"browser launch failed: {last_err}")
    # A/B 开关：PW_STEALTH_JS=0 不注入自己的 JS 覆盖层（stealth+FP），用于 bisect「自己的注入层接缝」
    stealth_js_on = os.environ.get("PW_STEALTH_JS", "1") != "0"
    if stealth_js_on:
        await S.pw_context.add_init_script(STEALTH_JS)
        fp_js = _fp_init_js(S.FP_PROFILE)
        if fp_js:
            await S.pw_context.add_init_script(fp_js)  # 指纹覆盖层：后注册的后执行，盖过 stealth 里的同名补丁
    if fp and fp.get("user_agent"):
        for pg in S.pw_context.pages:
            await _apply_fp_cdp(pg)  # 已存在的初始页：补一次网络层 Client Hints 同步

    # 启动即导出 storageState：job 脚本（headless + storageState）永远拿到新鲜登录态，
    # 不再依赖网关恢复时才 save_auth（probe 死循环根因之一）
    try:
        await S.pw_context.storage_state(path=S.AUTH_JSON_PATH)
        await _add_log("INFO", f"[Auth] startup saved -> {S.AUTH_JSON_PATH}")
    except Exception as e:
        await _add_log("WARN", f"[Auth] startup save failed: {e}")

    page = S.pw_context.pages[0] if S.pw_context.pages else await S.pw_context.new_page()
    S.pw_active_page = None  # 新 context：MCP 会话回落主 tab
    S.pw_browser = None  # persistent context manages the browser internally
    await _add_log("INFO", f"[PW] Persistent browser ready (headed+xvfb, stealth, gpu={'on' if S.gpu_mode else 'off'})")
    try:
        await _probe_webgl()  # WebGL renderer 徽标（swiftshader 或真 GPU）
    except Exception:
        pass

    return page


async def _page_for(params: dict):
    """解析 MCP 调用的目标页面。

    优先级：params["index"]（显式指定 tab，与 pw/tabs 编号一致）> S.pw_active_page >
    最近打开的页面。解决 active page 漂移问题：弹窗/新tab/页面关闭后，evaluate、
    click 等不再打到意料之外的页面上。显式传入 index 且越界时返回 None（调用方报错）。
    """
    await _ensure_pw_context()
    if not S.pw_context or not S.pw_context.pages:
        return None
    pages = S.pw_context.pages
    if params.get("index") is not None:
        try:
            i = int(params["index"])
        except Exception:
            return None
        if i < 0 or i >= len(pages):
            return None
        page = pages[i]
        if page.is_closed():
            return None
        S.pw_active_page = page
        return page
    if S.pw_active_page and not S.pw_active_page.is_closed():
        return S.pw_active_page
    S.pw_active_page = pages[-1]
    return S.pw_active_page


async def pw_init_browser():
    page = await _ensure_pw_context()
    if page:
        return {"result": {"status": "ready", "message": "Browser ready", "url": page.url}}
    return {"error": {"code": -1, "message": "Browser init failed"}}



async def pw_tabs():
    """列出浏览器全部 tab（前端选择器用）：pw/tabs。index 与 pw/tab_select 对应；tag 为任务标签。"""
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    tabs = []
    cur = S.pw_active_page if S.pw_active_page and not S.pw_active_page.is_closed() else (S.pw_context.pages[0] if S.pw_context.pages else None)
    for i, p in enumerate(S.pw_context.pages):
        try:
            title = await p.title()
        except Exception:
            title = ""
        try:
            url = p.url
        except Exception:
            url = ""
        tag = S.tab_tags.get(id(p), "")
        tabs.append({"index": i, "url": url, "title": title[:60], "tag": tag, "active": p is cur})
    return {"result": {"tabs": tabs, "active_profile": S.ACTIVE_PROFILE}}


async def pw_tab_select(params):
    """切换 MCP 会话操作的 tab：pw/tab_select {index}。后续 navigate/click/screenshot 都作用于它。"""
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    idx = int(params.get("index", -1))
    if idx < 0 or idx >= len(S.pw_context.pages):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    S.pw_active_page = S.pw_context.pages[idx]
    await _add_log("INFO", f"[Tab] select #{idx} {S.pw_active_page.url[:80]}")
    try:
        shot = await S.pw_active_page.screenshot(type="png")
        return {"result": {"status": "ok", "index": idx, "url": S.pw_active_page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"result": {"status": "ok", "index": idx, "url": S.pw_active_page.url}, "_shoterr": str(e)}


async def pw_tab_close(params):
    """关闭指定 tab：pw/tab_close {index}。最后一个 tab 不允许关（context 需至少一页）。"""
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    idx = int(params.get("index", -1))
    pages = S.pw_context.pages
    if idx < 0 or idx >= len(pages):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    if len(pages) <= 1:
        return {"error": {"code": -3, "message": "cannot close the last tab"}}
    target = pages[idx]
    url = target.url
    tag = S.tab_tags.pop(id(target), "")
    try:
        await target.close()
    except Exception as e:
        return {"error": {"code": -1, "message": f"close failed: {e}"}}
    if S.pw_active_page is target:
        S.pw_active_page = None  # 回落主 tab
    await _add_log("INFO", f"[Tab] closed #{idx} {url[:80]} tag={tag}")
    return {"result": {"status": "closed", "index": idx}}


async def pw_tab_tag(params):
    """给当前/指定 tab 打任务标签（浏览器状态栏可见、前端 tabs 列表展示）：
    pw/tab_tag {index?, tag}。tag 为空则清除该 tab 标签。"""
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    tag = str(params.get("tag", "")).strip()
    idx = params.get("index")
    if idx is None:
        p = S.pw_active_page if S.pw_active_page and not S.pw_active_page.is_closed() else S.pw_context.pages[0]
        if not p:
            return {"error": {"code": -2, "message": "no tab to tag"}}
    else:
        idx = int(idx)
        if idx < 0 or idx >= len(S.pw_context.pages):
            return {"error": {"code": -3, "message": f"index out of range: {idx}"}}
        p = S.pw_context.pages[idx]
        S.pw_active_page = p
    if tag:
        S.tab_tags[id(p)] = tag
    else:
        S.tab_tags.pop(id(p), None)
    await _add_log("INFO", f"[Tab] tag #{S.pw_context.pages.index(p)} = {tag or '(cleared)'}")
    return {"result": {"status": "ok", "index": S.pw_context.pages.index(p), "tag": tag}}


async def pw_tab_close_all(params):
    """按正则一键关闭所有匹配 tab：pw/tab_close_all {regex}（留主 tab 不关）。
    匹配 url 与标题；regex 为空则关闭全部非主 tab。返回关闭数。"""
    if not S.pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    raw = str(params.get("regex", "")).strip()
    pat = None
    if raw:
        try:
            pat = re.compile(raw)
        except re.error as e:
            return {"error": {"code": -2, "message": f"invalid regex: {e}"}}
    pages = S.pw_context.pages
    if len(pages) <= 1:
        return {"result": {"status": "ok", "closed": 0, "reason": "only main tab"}}
    closed = 0
    for p in list(pages[1:]):  # 永远保留主 tab pages[0]
        try:
            url = p.url
            title = await p.title()
        except Exception:
            url, title = "", ""
        if pat and not pat.search(url) and not pat.search(title):
            continue
        tag = S.tab_tags.pop(id(p), "")
        try:
            await p.close()
            closed += 1
            await _add_log("INFO", f"[Tab] close_all {url[:80]} tag={tag}")
        except Exception:
            pass
    if S.pw_active_page and S.pw_active_page.is_closed():
        S.pw_active_page = None
    return {"result": {"status": "ok", "closed": closed, "regex": raw}}



async def pw_profile_list():
    """列出所有 profile 及当前激活项：pw/profile_list。"""
    profiles = []
    for name in sorted(os.listdir(PROFILES_DIR)):
        d = os.path.join(PROFILES_DIR, name)
        if not os.path.isdir(d):
            continue
        auth = os.path.join(d, "auth.json")
        has_auth = os.path.exists(auth) and os.path.getsize(auth) > 100
        profiles.append({"name": name, "logged_in": has_auth,
                         "active": name == S.ACTIVE_PROFILE})
    return {"result": {"profiles": profiles, "active": S.ACTIVE_PROFILE}}


async def pw_profile_set(params):
    """切换/创建 profile：pw/profile_set {name}。切换会重启浏览器 context（原登录态保留在各自 profile）。"""
    name = params.get("name", "")
    if not name:
        return {"error": {"code": -2, "message": "name is required"}}
    if name != S.ACTIVE_PROFILE:
        # 关闭当前 context，切换 profile 目录
        if S.pw_context:
            try: await S.pw_context.close()
            except: pass
        if S.pw_instance:
            try: await S.pw_instance.stop()
            except: pass
        S.pw_browser = S.pw_context = S.pw_instance = None
        _set_active_profile(name)
        await _add_log("INFO", f"[Profile] 切换到 {name}")
    page = await _ensure_pw_context()
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    return {"result": {"status": "ok", "profile": S.ACTIVE_PROFILE,
                        "auth": os.path.exists(_profile_auth()),
                        "url": page.url}}



async def pw_new_tab(params):
    """新开 tab（可选 url，缺省 about:blank）并设为当前操作 tab：pw/new_tab {url}。"""
    if not S.pw_context:
        page = await _ensure_pw_context()
        if not page:
            return {"error": {"code": -1, "message": "Browser init failed"}}
    url = params.get("url") or "about:blank"
    page = await S.pw_context.new_page()
    S.pw_active_page = page
    await _apply_fp_cdp(page)  # 新 tab 同步网络层 Client Hints（指纹档案非 real 时）
    await _add_log("INFO", f"[Tab] new tab #{len(S.pw_context.pages)-1} {url[:80]}")
    if url != "about:blank":
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return {"error": {"code": -1, "message": f"new tab 导航失败: {e}"}}
    await asyncio.sleep(0.5)
    try:
        shot = await page.screenshot(type="png")
        return {"result": {"status": "ok", "index": len(S.pw_context.pages)-1, "url": page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception:
        return {"result": {"status": "ok", "index": len(S.pw_context.pages)-1, "url": page.url}}


