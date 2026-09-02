from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Any
import json, asyncio, base64, logging, os, re, asyncio as aio
from collections import deque
from datetime import datetime
import urllib.request
import urllib.error
from io import BytesIO

from playwright.async_api import async_playwright

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# DevTools 反代需要 websockets（应用镜像 pip 安装）；缺失时 /devtools WS 不可用但服务可启动
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("chrome-driverless")

app = FastAPI(title="Chrome Driverless")

# BROWSER_DATA_DIR 可覆盖（统一镜像挂 /app/data 卷持久化登录态）
DATA_DIR = os.environ.get("BROWSER_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")   # 多 profile：profiles/<name>/ 下含 auth.json + profile/
AUTH_JSON_PATH = os.path.join(DATA_DIR, "auth.json")  # 兼容旧路径（脚本 AUTH_JSON_PATH 指向 profiles/<active>/auth.json）
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "browser-profile")  # 兼容旧路径
ACTIVE_PROFILE = "default"  # 当前激活的 profile
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PROFILES_DIR, exist_ok=True)

def _profile_dir(name=None):
    """当前/指定 profile 的目录（profiles/<name>/）。"""
    n = (name or ACTIVE_PROFILE).strip() or "default"
    n = n.replace("/", "_").replace("..", "_")
    d = os.path.join(PROFILES_DIR, n)
    os.makedirs(d, exist_ok=True)
    return d

def _profile_auth(name=None):
    return os.path.join(_profile_dir(name), "auth.json")

def _profile_browser(name=None):
    return os.path.join(_profile_dir(name), "profile")

def _set_active_profile(name):
    global ACTIVE_PROFILE, AUTH_JSON_PATH, BROWSER_PROFILE_DIR
    ACTIVE_PROFILE = name.strip() or "default"
    AUTH_JSON_PATH = _profile_auth()
    BROWSER_PROFILE_DIR = _profile_browser()
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    return ACTIVE_PROFILE

HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("HTTP_PROXY_DEFAULT", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTPS_PROXY_DEFAULT", "")
# NO_PROXY：走代理时的直连白名单（本地/回环），外网站点默认走代理
NO_PROXY = os.environ.get("NO_PROXY", "localhost,127.0.0.1,::1")

# EXTERNAL_URL: 外部访问地址，用于日志输出和 /debug/url 端点
EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "")

# CDP 调试端口（REMOTE_DEBUG_PORT 可覆盖）：job 脚本经 playwright connectOverCDP 直连同一持久浏览器，
# 共享登录态、可开独立 tab、探测不死锁（RULES §5：driverless 只做通用执行器，登录判断在 job 脚本）
CDP_PORT = int(os.environ.get("REMOTE_DEBUG_PORT", "9222"))

pw_instance = None
pw_browser = None
pw_context = None

# AI 配置（model/base_url/api_key）持久化文件：优先级 file > env(AI_MODEL/AI_BASE_URL/DEEPSEEK_API_KEY) > 默认
AI_CONFIG_PATH = os.path.join(DATA_DIR, "ai_config.json")
# ai/cancel 置位：pw/ai_task 每步检查，取消当前 AI 任务
ai_cancel_evt = asyncio.Event()

use_proxy = True  # 默认启用代理（外网站点走代理，本地/内网白名单由 NO_PROXY 直连）
task_lock = asyncio.Lock()
task_busy = False
mcp_logs = deque(maxlen=500)
mcp_logs_lock = asyncio.Lock()


async def _add_log(level, msg):
    entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
    async with mcp_logs_lock:
        mcp_logs.append(entry)
    logger.log(getattr(logging, level, logging.INFO), msg)


class MCPRequest(BaseModel):
    method: str
    params: Any = {}


# 反自动化检测（阿里云/Google 会检测 headless 指纹）。
# 覆盖社区 stealth 补丁：webdriver/plugins/chrome.runtime/permissions/UA/设备枚举/toString 补丁。
STEALTH_JS = r"""
(() => {
  // 0) WebAuthn 降级：让通行密钥请求立即失败（NotAllowedError），
  //    Google 等站点检测到“此设备无可用通行密钥”后会自动回落到密码登录 UI，
  //    否则 navigator.credentials.get 永久 pending，页面“试试其他方式”等控件全部假死。
  try {
    if (window.PublicKeyCredential) {
      const reject = () => Promise.reject(new DOMException('NotAllowedError', 'NotAllowedError'));
      const cc = navigator.credentials;
      if (cc) {
        cc.get = reject; cc.create = reject; cc.store = reject;
        Object.defineProperty(Navigator.prototype, 'credentials', {
          get: () => ({ get: reject, create: reject, store: reject, preventSilentAccess: () => Promise.resolve() }),
          configurable: true
        });
        Object.defineProperty(navigator, 'credentials', {
          get: () => ({ get: reject, create: reject, store: reject, preventSilentAccess: () => Promise.resolve() }),
          configurable: true
        });
      }
    }
  } catch(e) {}
  // 0b) WebGL 渲染器伪装：--use-gl=swiftshader 会让 getParameter 返回
  //     "SwiftShader"（软件渲染=虚拟机/无头标志），Google 验证码重点读取此项。
  try {
    const GPU_VENDOR = 'Intel';
    const GPU_RENDERER = 'Intel(R) UHD Graphics 630';
    const patchGL = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        try {
          if (p === 37445) return GPU_VENDOR;             // UNMASKED_VENDOR_WEBGL
          if (p === 37446) return GPU_RENDERER;           // UNMASKED_RENDERER_WEBGL
        } catch (e) {}
        return orig.call(this, p);
      };
      proto.getParameter.toString = () => 'function getParameter() { [native code] }';
    };
    if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
  } catch(e) {}
  // 1) 隐藏 webdriver（含 toString 打补丁，防 navigator.webdriver 探测）
  try {
    const wdDesc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    const fakeWd = Object.getOwnPropertyDescriptor(navigator, 'webdriver');
    if (fakeWd && fakeWd.get) {
      fakeWd.get.toString = () => 'function get() { [native code] }';
      fakeWd.get.toString.toString = () => 'function toString() { [native code] }';
    }
  } catch(e) {}
  // 2) 伪装插件列表（正常 Chrome 有 5 个）
  const plugins = [
    {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', desc:'Portable Document Format'},
    {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', desc:''},
    {name:'Native Client', filename:'internal-nacl-plugin', desc:''},
    {name:'Chromium PDF Viewer', filename:'internal-pdf-viewer', desc:'Portable Document Format'},
    {name:'Microsoft Edge PDF Viewer', filename:'internal-pdf-viewer', desc:'Portable Document Format'}
  ];
  if (navigator.plugins.length === 0) {
    const makePlugin = (p) => {
      const obj = { name:p.name, filename:p.filename, description:p.desc, length:1,
        item:()=>null, namedItem:()=>null };
      obj.item.toString = () => 'function item() { [native code] }';
      return obj;
    };
    const arr = plugins.map(makePlugin);
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    Object.defineProperty(navigator, 'plugins', { get: () => arr });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => {
      const mt = [1,2,3,4];
      mt.item = (i) => null; mt.namedItem = () => null;
      return mt;
    }});
  }
  // 3) 伪装 languages
  if (!navigator.languages || navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
  }
  // 4) 完整 chrome 对象（headless 缺 chrome.runtime）
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', { get: () => ({
      runtime: { connect: () => {}, sendMessage: () => {}, id: undefined,
        getManifest: () => ({}) },
      loadTimes: () => ({}), csi: () => ({}), app: {}
    })});
  } else {
    window.chrome.runtime = window.chrome.runtime || { connect: () => {}, sendMessage: () => {}, getManifest: () => ({}) };
  }
  // 5) permissions.query 伪装
  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(params);
      window.navigator.permissions.query.toString = () => 'function query() { [native code] }';
    }
  } catch(e) {}
  // 6) 设备枚举（headless 返回空列表）
  try {
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
        {deviceId:'', kind:'audioinput', label:'', groupId:''},
        {deviceId:'', kind:'videoinput', label:'', groupId:''}
      ]);
    }
  } catch(e) {}
})();
"""


def _browser_opts(storage_state=None):
    opts = dict(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    if storage_state:
        opts["storage_state"] = storage_state
    if use_proxy:
        # 代理：优先 HTTPS_PROXY（浏览器访问多为 https 站点，如 deepinfra/google）；
        # 两者通常指向同一出口；HTTP_PROXY 作为回退。playwright 单 server 同时服务 http+https(CONNECT)。
        proxy_server = HTTPS_PROXY or HTTP_PROXY
        if proxy_server:
            proxy = {"server": proxy_server}
            if NO_PROXY:
                proxy["bypass"] = NO_PROXY
            opts["proxy"] = proxy
    return opts


@app.on_event("startup")
async def startup():
    # 项目级 profile 隔离（RULES §5.8）：一个项目一个 profile（PROFILE_NAME 指定）；
    # 未设置时为 "debug"（临时调试用，与正式项目隔离）
    _set_active_profile(os.environ.get("PROFILE_NAME", "debug"))
    await _add_log("INFO", f"Chrome Driverless started, proxy={HTTP_PROXY}, profile={ACTIVE_PROFILE}")
    if EXTERNAL_URL:
        await _add_log("INFO", f"EXTERNAL_URL={EXTERNAL_URL}")


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
        "pw_browser": pw_browser is not None,
        "pw_context": pw_context is not None,
        "pages": [],
        "task_busy": task_busy,
        "data_dir": DATA_DIR,
    }
    if pw_context and pw_context.pages:
        for i, page in enumerate(pw_context.pages):
            try:
                status["pages"].append({"index": i, "url": page.url, "title": await page.title()})
            except Exception as e:
                status["pages"].append({"index": i, "url": "error", "title": str(e)})
    return status


@app.get("/debug/logs")
async def debug_logs():
    async with mcp_logs_lock:
        return {"logs": list(mcp_logs)}


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
    return {"data_files": files, "script_files": script_files, "auth_json_exists": os.path.exists(AUTH_JSON_PATH)}


@app.get("/debug/url")
async def debug_url():
    """返回远程浏览器地址 (由 EXTERNAL_URL 环境变量配置)"""
    return {
        "external_url": EXTERNAL_URL or "(not configured)",
        "mcp_endpoint": "/mcp",
    }


@app.post("/mcp")
async def mcp_handler(req: MCPRequest):
    method = req.method
    params = req.params if req.params else {}
    return await _dispatch(method, params)


async def _dispatch(method, params):
    handlers = {
        "pw/screenshot": lambda: pw_screenshot(params),
        "pw/navigate": lambda: pw_navigate(params),
        "pw/init_browser": lambda: pw_init_browser(),
        "pw/set_proxy": lambda: pw_set_proxy(params),
        "pw/save_auth": lambda: pw_save_auth(),
        "pw/ask_deepseek": lambda: pw_ask_deepseek(params),
        "pw/run_script": lambda: pw_run_script(params),
        "pw/run_script_content": lambda: pw_run_script_content(params),
        "pw/ai_task": lambda: pw_ai_task(params),
        "pw/click": lambda: pw_click(params),
        "pw/hover": lambda: pw_hover(params),
        "pw/type": lambda: pw_type(params),
        "pw/key": lambda: pw_key(params),
        "pw/auto_login": lambda: pw_auto_login(params),
        "pw/back": lambda: pw_back(params),
        "pw/reload": lambda: pw_reload(params),
        "pw/elements": lambda: pw_elements(params),
        "pw/clear": lambda: pw_clear(params),
        "pw/profile_list": lambda: pw_profile_list(),
        "pw/profile_set": lambda: pw_profile_set(params),
        "pw/evaluate": lambda: pw_evaluate(params),
        "pw/tabs": lambda: pw_tabs(),
        "pw/tab_select": lambda: pw_tab_select(params),
        "pw/tab_close": lambda: pw_tab_close(params),
        "pw/tab_tag": lambda: pw_tab_tag(params),
        "pw/tab_close_all": lambda: pw_tab_close_all(params),
        "pw/new_tab": lambda: pw_new_tab(params),
        "pw/mouse_move": lambda: pw_mouse_move(params),
        "pw/mouse_down": lambda: pw_mouse_down(params),
        "pw/mouse_up": lambda: pw_mouse_up(params),
        "pw/scroll_at": lambda: pw_scroll_at(params),
        "ai/config_get": lambda: ai_config_get(params),
        "ai/config_set": lambda: ai_config_set(params),
        "ai/test": lambda: ai_test(params),
        "ai/cancel": lambda: ai_cancel(params),
    }
    handler = handlers.get(method)
    if not handler:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    await _add_log("INFO", f"[MCP] {method} called")
    try:
        result = await handler()
        has_err = isinstance(result, dict) and "error" in result
        await _add_log("ERROR" if has_err else "INFO",
                       f"[MCP] {method} {'error: ' + result['error'].get('message','') if has_err else 'success'}")
        return result
    except Exception as e:
        await _add_log("ERROR", f"[MCP] {method} exception: {e}")
        return {"error": {"code": -1, "message": str(e)}}


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
    global pw_instance, pw_browser, pw_context, pw_active_page
    if pw_context and pw_context.pages:
        try:
            if pw_active_page and not pw_active_page.is_closed():
                return pw_active_page
        except Exception:
            pass
        # 活动页已关闭（弹窗自动关闭/手动关tab等）：回退到最近打开的页面，
        # 而不是 pages[0]（最早的tab，往往是用户意想不到的目标）。
        pw_active_page = pw_context.pages[-1]
        return pw_active_page
    async with _pw_init_lock:
        if pw_context and pw_context.pages:  # 双检：等锁期间别人已启动好
            pw_active_page = pw_context.pages[-1]
            return pw_active_page

        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        # 清理残留 profile 锁（异常退出会留下 Singleton*，导致 Chromium 认为 profile 被占用而卡死）
        try:
            for f in os.listdir(BROWSER_PROFILE_DIR):
                if f.startswith("Singleton"):
                    os.remove(os.path.join(BROWSER_PROFILE_DIR, f))
                    await _add_log("WARN", f"[PW] 清理残留锁: {f}")
        except Exception:
            pass
        n = _kill_orphan_chrome()
        if n:
            await _add_log("WARN", f"[PW] 清理孤儿 chromium 进程 ×{n}")

        if pw_browser:
            try: await pw_browser.close()
            except: pass
        if pw_instance:
            try: await pw_instance.stop()
            except: pass

    pw_instance = await async_playwright().start()
    opts = _browser_opts()
    opts["headless"] = False  # 有头模式（配合 xvfb 虚拟显示），规避 Google/阿里云 headless 检测
    opts["user_data_dir"] = BROWSER_PROFILE_DIR
    # 反自动化检测（阿里云/Google 会检测 headless 指纹：webdriver 标记、无插件、自动化特征）
    opts["args"] = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--start-maximized",
        "--window-size=1440,900",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--use-gl=swiftshader",
        "--enable-unsafe-swiftshader",
        "--disable-features=VizDisplayCompositor",
        # CDP 端口与 playwright 的 --remote-debugging-pipe 共存（实测可用）：
        # job 脚本 connectOverCDP 在同一浏览器开「任务级独立 tab」（RULES §5.8：一个任务一个 tab，结束必关）
        f"--remote-debugging-port={CDP_PORT}",
    ]
    # 启动带超时：悬死（端口被占/渲染卡住）时杀进程重试一次，而不是永远挂着
    last_err = None
    for attempt in range(2):
        try:
            pw_context = await asyncio.wait_for(
                pw_instance.chromium.launch_persistent_context(**opts), timeout=90)
            break
        except asyncio.TimeoutError:
            last_err = "launch timeout(90s)"
            await _add_log("ERROR", f"[PW] {last_err}，attempt={attempt+1}，清理孤儿进程后重试")
            _kill_orphan_chrome()
            pw_context = None
        except Exception as e:
            last_err = str(e)
            await _add_log("ERROR", f"[PW] launch 失败 attempt={attempt+1}: {e}")
            _kill_orphan_chrome()
            pw_context = None
            if "XServer" in last_err or "headless" in last_err:
                break  # 无显示服务，重试无用
    if pw_context is None:
        raise RuntimeError(f"browser launch failed: {last_err}")
    await pw_context.add_init_script(STEALTH_JS)

    # 启动即导出 storageState：job 脚本（headless + storageState）永远拿到新鲜登录态，
    # 不再依赖网关恢复时才 save_auth（probe 死循环根因之一）
    try:
        await pw_context.storage_state(path=AUTH_JSON_PATH)
        await _add_log("INFO", f"[Auth] startup saved -> {AUTH_JSON_PATH}")
    except Exception as e:
        await _add_log("WARN", f"[Auth] startup save failed: {e}")

    page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()
    pw_active_page = None  # 新 context：MCP 会话回落主 tab
    pw_browser = None  # persistent context manages the browser internally
    await _add_log("INFO", "[PW] Persistent browser ready (headed+xvfb, stealth)")

    return page


async def _page_for(params: dict):
    """解析 MCP 调用的目标页面。

    优先级：params["index"]（显式指定 tab，与 pw/tabs 编号一致）> pw_active_page >
    最近打开的页面。解决 active page 漂移问题：弹窗/新tab/页面关闭后，evaluate、
    click 等不再打到意料之外的页面上。显式传入 index 且越界时返回 None（调用方报错）。
    """
    global pw_active_page
    await _ensure_pw_context()
    if not pw_context or not pw_context.pages:
        return None
    pages = pw_context.pages
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
        pw_active_page = page
        return page
    if pw_active_page and not pw_active_page.is_closed():
        return pw_active_page
    pw_active_page = pages[-1]
    return pw_active_page


# 最近一次虚拟鼠标位置（Playwright 不暴露，自维护用于人性化轨迹起点）
pw_last_mouse = {"x": 0.0, "y": 0.0}


def _human_path(sx: float, sy: float, tx: float, ty: float):
    """生成类人鼠标轨迹：二次贝塞尔（垂直抖动控制点）+ 缓入缓出 + 过冲回弹 + 微抖。"""
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy) or 1.0
    # 控制点：中垂线方向随机偏移 → 弧线而非直线
    off = random.uniform(-0.3, 0.3) * dist
    mx, my = (sx + tx) / 2 - (dy / dist) * off, (sy + ty) / 2 + (dx / dist) * off
    steps = max(14, min(48, int(dist / 6)))
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        te = t * t * (3 - 2 * t)  # smoothstep 缓入缓出
        x = (1 - te) ** 2 * sx + 2 * (1 - te) * te * mx + te ** 2 * tx
        y = (1 - te) ** 2 * sy + 2 * (1 - te) * te * my + te ** 2 * ty
        pts.append((x + random.uniform(-0.7, 0.7), y + random.uniform(-0.7, 0.7)))
    if dist > 40:  # 过冲 4~12px 再回弹（人手常见的修正动作）
        ox, oy = tx + dx / dist * random.uniform(4, 12), ty + dy / dist * random.uniform(4, 12)
        pts.append((ox, oy))
        pts.append((tx + random.uniform(-0.5, 0.5), ty + random.uniform(-0.5, 0.5)))
    else:
        pts.append((tx, ty))
    return pts


async def _human_move(page, tx: float, ty: float, *, start=None):
    """按类人轨迹移动到目标点并记录终点。"""
    sx = start["x"] if start else pw_last_mouse["x"]
    sy = start["y"] if start else pw_last_mouse["y"]
    if math.hypot(tx - sx, ty - sy) < 2:
        return
    for (x, y) in _human_path(sx, sy, tx, ty):
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.004, 0.018))
    pw_last_mouse.update(x=tx, y=ty)


async def _raw_click(page, cx: float, cy: float, *, button: str = "left", click_count: int = 1):
    """底层人性化坐标点击：贝塞尔轨迹移动 → 停顿 → 按压（随机时长）→ 释放。

    与 locator.click() 的 actionability 检查解耦，命中坐标处最顶层的元素。
    轨迹/时序按人类行为模拟（reCAPTCHA 等行为分析检测直线匀速瞬移）。
    """
    # 目标点带 ±2px 随机偏移（人不会每次都点正中心）
    tx, ty = cx + random.uniform(-2, 2), cy + random.uniform(-2, 2)
    await _human_move(page, tx, ty)
    await asyncio.sleep(random.uniform(0.09, 0.24))  # 点击前停留
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.15))  # 按压时长
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.04, 0.1))


async def _click_locator_raw(page, loc, *, button: str = "left", click_count: int = 1):
    """对 locator 的包围盒中心做一次底层坐标点击（raw 模式/auto 兜底用）。"""
    bbox = await loc.bounding_box()
    if not bbox:
        raise RuntimeError("element has no bounding box (not rendered)")
    cx = bbox["x"] + bbox["width"] / 2
    cy = bbox["y"] + bbox["height"] / 2
    await _raw_click(page, cx, cy)
    return {"x": cx, "y": cy}


async def pw_init_browser():
    page = await _ensure_pw_context()
    if page:
        return {"result": {"status": "ready", "message": "Browser ready", "url": page.url}}
    return {"error": {"code": -1, "message": "Browser init failed"}}


async def pw_tabs():
    """列出浏览器全部 tab（前端选择器用）：pw/tabs。index 与 pw/tab_select 对应；tag 为任务标签。"""
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    tabs = []
    cur = pw_active_page if pw_active_page and not pw_active_page.is_closed() else (pw_context.pages[0] if pw_context.pages else None)
    for i, p in enumerate(pw_context.pages):
        try:
            title = await p.title()
        except Exception:
            title = ""
        try:
            url = p.url
        except Exception:
            url = ""
        tag = tab_tags.get(id(p), "")
        tabs.append({"index": i, "url": url, "title": title[:60], "tag": tag, "active": p is cur})
    return {"result": {"tabs": tabs, "active_profile": ACTIVE_PROFILE}}


async def pw_tab_select(params):
    """切换 MCP 会话操作的 tab：pw/tab_select {index}。后续 navigate/click/screenshot 都作用于它。"""
    global pw_active_page
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    idx = int(params.get("index", -1))
    if idx < 0 or idx >= len(pw_context.pages):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    pw_active_page = pw_context.pages[idx]
    await _add_log("INFO", f"[Tab] select #{idx} {pw_active_page.url[:80]}")
    try:
        shot = await pw_active_page.screenshot(type="png")
        return {"result": {"status": "ok", "index": idx, "url": pw_active_page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"result": {"status": "ok", "index": idx, "url": pw_active_page.url}, "_shoterr": str(e)}


async def pw_tab_close(params):
    """关闭指定 tab：pw/tab_close {index}。最后一个 tab 不允许关（context 需至少一页）。"""
    global pw_active_page
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    idx = int(params.get("index", -1))
    pages = pw_context.pages
    if idx < 0 or idx >= len(pages):
        return {"error": {"code": -2, "message": f"index out of range: {idx}"}}
    if len(pages) <= 1:
        return {"error": {"code": -3, "message": "cannot close the last tab"}}
    target = pages[idx]
    url = target.url
    tag = tab_tags.pop(id(target), "")
    try:
        await target.close()
    except Exception as e:
        return {"error": {"code": -1, "message": f"close failed: {e}"}}
    if pw_active_page is target:
        pw_active_page = None  # 回落主 tab
    await _add_log("INFO", f"[Tab] closed #{idx} {url[:80]} tag={tag}")
    return {"result": {"status": "closed", "index": idx}}


async def pw_tab_tag(params):
    """给当前/指定 tab 打任务标签（浏览器状态栏可见、前端 tabs 列表展示）：
    pw/tab_tag {index?, tag}。tag 为空则清除该 tab 标签。"""
    global pw_active_page
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    tag = str(params.get("tag", "")).strip()
    idx = params.get("index")
    if idx is None:
        p = pw_active_page if pw_active_page and not pw_active_page.is_closed() else pw_context.pages[0]
        if not p:
            return {"error": {"code": -2, "message": "no tab to tag"}}
    else:
        idx = int(idx)
        if idx < 0 or idx >= len(pw_context.pages):
            return {"error": {"code": -3, "message": f"index out of range: {idx}"}}
        p = pw_context.pages[idx]
        pw_active_page = p
    if tag:
        tab_tags[id(p)] = tag
    else:
        tab_tags.pop(id(p), None)
    await _add_log("INFO", f"[Tab] tag #{pw_context.pages.index(p)} = {tag or '(cleared)'}")
    return {"result": {"status": "ok", "index": pw_context.pages.index(p), "tag": tag}}


async def pw_tab_close_all(params):
    """按正则一键关闭所有匹配 tab：pw/tab_close_all {regex}（留主 tab 不关）。
    匹配 url 与标题；regex 为空则关闭全部非主 tab。返回关闭数。"""
    global pw_active_page
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    raw = str(params.get("regex", "")).strip()
    pat = None
    if raw:
        try:
            pat = re.compile(raw)
        except re.error as e:
            return {"error": {"code": -2, "message": f"invalid regex: {e}"}}
    pages = pw_context.pages
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
        tag = tab_tags.pop(id(p), "")
        try:
            await p.close()
            closed += 1
            await _add_log("INFO", f"[Tab] close_all {url[:80]} tag={tag}")
        except Exception:
            pass
    if pw_active_page and pw_active_page.is_closed():
        pw_active_page = None
    return {"result": {"status": "ok", "closed": closed, "regex": raw}}


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
    global pw_browser, pw_context, use_proxy
    enable = params.get("enable", True)
    use_proxy = enable
    await _add_log("INFO", f"[Proxy] set to {enable}")
    if pw_context:
        try: await pw_context.close()
        except: pass
    pw_browser = pw_context = None
    page = await _page_for(params)
    if page:
        return {"result": {"status": "ok", "proxy": use_proxy, "url": page.url}}
    return {"error": {"code": -1, "message": "Restart failed"}}

async def pw_save_auth():
    if not pw_context:
        return {"error": {"code": -1, "message": "No browser context"}}
    try:
        await pw_context.storage_state(path=AUTH_JSON_PATH)
        await _add_log("INFO", f"[Auth] Saved to {AUTH_JSON_PATH}")
        return {"result": {"status": "ok", "path": AUTH_JSON_PATH}}
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
        if selector:
            loc = page.locator(selector).first
            await loc.scroll_into_view_if_needed()
            bbox = await loc.bounding_box()
            if bbox:
                await _raw_click(page, bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
            else:
                await loc.click(timeout=10000)
            await page.keyboard.type(str(text), delay=random.uniform(50, 130))
        elif x is not None and y is not None:
            await _raw_click(page, float(x), float(y))
            await page.keyboard.type(str(text), delay=random.uniform(50, 130))
        else:
            await page.keyboard.type(str(text), delay=random.uniform(50, 130))
        await asyncio.sleep(0.5)
        shot = await page.screenshot(type="png")
        return {"result": {"status": "typed", "url": page.url, "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"type failed: {e}"}}


async def _fill_username(page, username):
    """Find the username input and fill it (semantic selectors first)."""
    candidates = [
        "input[autocomplete='username']",
        "input[type='text'][name*='user' i], input[type='text'][id*='user' i]",
        "input[placeholder*='账号' i], input[placeholder*='帐号' i], input[placeholder*='手机' i], input[placeholder*='邮箱' i], input[placeholder*='用户名' i], input[placeholder*='user' i], input[placeholder*='account' i], input[placeholder*='phone' i], input[placeholder*='login' i]",
        "input:not([type='password']):not([type='submit']):not([type='hidden'])",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                await loc.fill(str(username))
                return True
        except Exception:
            continue
    return False


async def _fill_password(page, password):
    try:
        loc = page.locator("input[type='password']").first
        if await loc.count() and await loc.is_visible():
            await loc.click(timeout=5000)
            await loc.fill(str(password))
            return True
    except Exception:
        pass
    return False


async def _click_login_button(page):
    """Click the login/submit button (common zh/en labels first)."""
    candidates = [
        "button[type='submit'], input[type='submit']",
        "button:has-text('登录'), button:has-text('登入'), button:has-text('立即登录'), button:has-text('授权并登录'), button:has-text('Sign in'), button:has-text('Log in'), button:has-text('Login')",
        "button[type='button']:has-text('登录')",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


async def pw_auto_login(params):
    """
    Env-driven login: pw/auto_login {url?}
    Credentials come from environment variables:
      BROWSER_LOGIN_URL, BROWSER_LOGIN_USERNAME, BROWSER_LOGIN_PASSWORD
    Navigates to the login page, fills the form automatically, clicks the
    login button and returns a screenshot.
    """
    username = os.environ.get("BROWSER_LOGIN_USERNAME", "").strip()
    password = os.environ.get("BROWSER_LOGIN_PASSWORD", "")
    target_url = (params.get("url") or os.environ.get("BROWSER_LOGIN_URL", "")).strip()
    if not username or not password:
        return {"error": {"code": -2, "message": "BROWSER_LOGIN_USERNAME / BROWSER_LOGIN_PASSWORD not configured"}}
    if not target_url:
        return {"error": {"code": -2, "message": "login url not set (params.url or BROWSER_LOGIN_URL)"}}

    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed (or index out of range)"}}
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        # Switch from QR mode to username/password login if present.
        for tab in ["text=账号密码登录", "text=密码登录", "text=Account login", "text=账号登录", "text=帐号密码登录"]:
            try:
                el = page.get_by_text(tab, exact=False).first
                if await el.count() and await el.is_visible():
                    await el.click(timeout=3000)
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue
        ok_pass = await _fill_password(page, password)
        ok_user = await _fill_username(page, username)
        await asyncio.sleep(0.5)
        await _click_login_button(page)
        await asyncio.sleep(3)
        shot = await page.screenshot(type="png")
        return {
            "result": {
                "status": "auto_login_attempted",
                "url": page.url,
                "user_filled": ok_user,
                "password_filled": ok_pass,
                "image": base64.b64encode(shot).decode("utf-8"),
            }
        }
    except Exception as e:
        return {"error": {"code": -1, "message": f"auto_login failed: {e}"}}


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
                         "active": name == ACTIVE_PROFILE})
    return {"result": {"profiles": profiles, "active": ACTIVE_PROFILE}}


async def pw_profile_set(params):
    """切换/创建 profile：pw/profile_set {name}。切换会重启浏览器 context（原登录态保留在各自 profile）。"""
    global pw_instance, pw_browser, pw_context
    name = params.get("name", "")
    if not name:
        return {"error": {"code": -2, "message": "name is required"}}
    if name != ACTIVE_PROFILE:
        # 关闭当前 context，切换 profile 目录
        if pw_context:
            try: await pw_context.close()
            except: pass
        if pw_instance:
            try: await pw_instance.stop()
            except: pass
        pw_browser = pw_context = pw_instance = None
        _set_active_profile(name)
        await _add_log("INFO", f"[Profile] 切换到 {name}")
    page = await _ensure_pw_context()
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    return {"result": {"status": "ok", "profile": ACTIVE_PROFILE,
                        "auth": os.path.exists(_profile_auth()),
                        "url": page.url}}


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


async def pw_new_tab(params):
    """新开 tab（可选 url，缺省 about:blank）并设为当前操作 tab：pw/new_tab {url}。"""
    global pw_active_page
    if not pw_context:
        page = await _ensure_pw_context()
        if not page:
            return {"error": {"code": -1, "message": "Browser init failed"}}
    url = params.get("url") or "about:blank"
    page = await pw_context.new_page()
    pw_active_page = page
    await _add_log("INFO", f"[Tab] new tab #{len(pw_context.pages)-1} {url[:80]}")
    if url != "about:blank":
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return {"error": {"code": -1, "message": f"new tab 导航失败: {e}"}}
    await asyncio.sleep(0.5)
    try:
        shot = await page.screenshot(type="png")
        return {"result": {"status": "ok", "index": len(pw_context.pages)-1, "url": page.url,
                           "image": base64.b64encode(shot).decode("utf-8")}}
    except Exception:
        return {"result": {"status": "ok", "index": len(pw_context.pages)-1, "url": page.url}}


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


def _ai_cfg():
    """AI 有效配置：data/ai_config.json > 环境变量 > 默认。
    flash 等别名模型指向 deepseek 官方必然 400（Model Not Exist），自动回退 deepseek-chat 并告警。"""
    cfg = {"model": "", "base_url": "", "api_key": ""}
    try:
        with open(AI_CONFIG_PATH, "r") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg["model"] = (saved.get("model") or "").strip()
            cfg["base_url"] = (saved.get("base_url") or "").strip().rstrip("/")
            cfg["api_key"] = (saved.get("api_key") or "").strip()
    except Exception:
        pass
    if not cfg["model"]:
        cfg["model"] = os.environ.get("AI_MODEL", "").strip()
    if not cfg["base_url"]:
        cfg["base_url"] = os.environ.get("AI_BASE_URL", "").strip().rstrip("/")
    if not cfg["api_key"]:
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not cfg["base_url"]:
        cfg["base_url"] = "https://api.deepseek.com/v1"
    if not cfg["model"]:
        cfg["model"] = "deepseek-chat"
    if cfg["model"] == "flash" and "api.deepseek.com" in cfg["base_url"]:
        logger.warning("AI 模型 %s 在 %s 不存在(400)，回退 deepseek-chat；请在前端设置正确的模型/地址",
                       cfg["model"], cfg["base_url"])
        cfg["model"] = "deepseek-chat"
    return cfg


def _llm_http_error(e):
    """HTTPError → 带响应体的可读错误（400 Model Not Exist 等根因不再被吞）。"""
    try:
        body = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        body = ""
    return f"HTTP {e.code} {e.reason} url={e.url} body={body or '(空)'}"


async def _call_deepseek(prompt, override=None):
    """调用 OpenAI 兼容 chat/completions（配置见 _ai_cfg；override 用于 ai/test 表单值临时验证）。
    国内 API 直连不走代理（HTTP_PROXY 是国外代理，绕行反而失败）。"""
    cfg = override or _ai_cfg()
    if not cfg["api_key"]:
        raise Exception("API key 未配置（DEEPSEEK_API_KEY 或前端 AI 设置）")
    req_body = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 2048}
    req = urllib.request.Request(f"{cfg['base_url']}/chat/completions",
        data=json.dumps(req_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except urllib.error.HTTPError as e:
        raise Exception(f"LLM 调用失败({_llm_http_error(e)}) model={cfg['model']} base={cfg['base_url']}")
    except Exception as e:
        raise Exception(f"LLM 调用失败({e}) model={cfg['model']} base={cfg['base_url']}")


async def ai_config_get(params):
    cfg = _ai_cfg()
    return {"result": {
        "model": cfg["model"], "base_url": cfg["base_url"],
        "api_key_set": bool(cfg["api_key"]),
        "api_key_masked": (cfg["api_key"][:6] + "..." + cfg["api_key"][-4:]) if len(cfg["api_key"]) > 12 else ("***" if cfg["api_key"] else ""),
        "from_file": os.path.exists(AI_CONFIG_PATH),
    }}


async def ai_config_set(params):
    """保存 AI 配置到 data/ai_config.json（覆盖 env；api_key 留空=沿用服务端已配密钥）。"""
    model = (params.get("model") or "").strip()
    base_url = (params.get("base_url") or "").strip().rstrip("/")
    api_key = (params.get("api_key") or "").strip()
    if not model or not base_url:
        return {"error": {"code": -2, "message": "model 和 base_url 均必填"}}
    if not base_url.startswith(("http://", "https://")):
        return {"error": {"code": -2, "message": "base_url 必须以 http(s):// 开头"}}
    saved = {"model": model, "base_url": base_url}
    if api_key:
        saved["api_key"] = api_key
    try:
        with open(AI_CONFIG_PATH, "w") as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"error": {"code": -1, "message": f"写入配置失败: {e}"}}
    await _add_log("INFO", f"[AI] 配置已保存: model={model} base={base_url} key={'新设置' if api_key else '沿用'}")
    return await ai_config_get({})


async def ai_test(params):
    """验证 AI 配置可用性：默认用当前有效配置；也可传 model/base_url/api_key 临时覆盖（保存前先测表单值）。
    覆盖值缺 api_key 时沿用服务端已配密钥。"""
    overrides = None
    if (params.get("model") or "").strip() and (params.get("base_url") or "").strip():
        overrides = _ai_cfg()
        overrides["model"] = params["model"].strip()
        overrides["base_url"] = params["base_url"].strip().rstrip("/")
        if (params.get("api_key") or "").strip():
            overrides["api_key"] = params["api_key"].strip()
    prompt = (params.get("prompt") or "只回复两个字符: ok").strip()
    try:
        answer = await _call_deepseek(prompt, overrides)
        return {"result": {"ok": True, "answer": (answer or "")[:200]}}
    except Exception as e:
        return {"result": {"ok": False, "error": str(e)}}


async def ai_cancel(params):
    ai_cancel_evt.set()
    await _add_log("INFO", "[AI] 收到取消请求")
    return {"result": {"status": "cancelling"}}


def _parse_ai_json(text):
    """健壮解析 AI 返回的 JSON：去代码块/尾随逗号/JSONP 前缀，逐级降级提取。"""
    if not text:
        return None
    t = text.strip()
    # 1) 直接解析
    try:
        return json.loads(t)
    except Exception:
        pass
    # 2) 去尾随逗号（{...}, 或 键:值,）再解析
    fixed = re.sub(r',\s*([}\]])', r'\1', t)
    try:
        return json.loads(fixed)
    except Exception:
        pass
    # 3) 提取 { 到最后一个 } 的子串（去前后杂文本），同样去尾随逗号
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        sub = t[s:e+1]
        try:
            return json.loads(sub)
        except Exception:
            pass
        try:
            return json.loads(re.sub(r',\s*([}\]])', r'\1', sub))
        except Exception:
            pass
    return None


async def pw_ask_deepseek(params):
    prompt = params.get("prompt", "")
    if not prompt:
        return {"error": {"code": -2, "message": "prompt is required"}}
    page = await _ensure_pw_context()
    try:
        # DeepSeek 纯文本（不支持图片），页面 URL 作为上下文附加
        ctx_prompt = prompt
        if page:
            ctx_prompt = f"[当前页面 {page.url}]\n{prompt}"
        answer = await _call_deepseek(ctx_prompt)
        return {"result": {"answer": answer, "page_url": page.url if page else ""}}
    except Exception as e:
        return {"error": {"code": -3, "message": f"DeepSeek API failed: {e}"}}


async def pw_run_script(params):
    script_name = params.get("script", "")
    if not script_name:
        return {"error": {"code": -1, "message": "script name required"}}
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        return {"error": {"code": -2, "message": f"Script not found: {script_name}"}}
    args = params.get("args", [])
    # 脚本 connectOverCDP 直连本机 chromium 的 CDP 端口（--remote-debugging-port），
    # 而 chromium 是懒启动的：容器重启后若没任何 MCP 调用触发过启动，端口未监听，
    # 脚本会 ECONNREFUSED。这里先确保浏览器已启动，再执行脚本。
    await _ensure_pw_context()
    cmd = ["node", script_path] + args
    env = {**os.environ, "HOME": os.path.dirname(__file__),
           "BROWSER_DATA_DIR": DATA_DIR, "CDP_URL": f"http://127.0.0.1:{CDP_PORT}"}
    task_id = params.get("task_id")
    if task_id:
        env["TASK_ID"] = str(task_id)
    job = params.get("job")
    if job:
        env["JOB_NAME"] = str(job)
    extra_env = params.get("env")
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            env[str(k)] = str(v)
    if os.path.exists(AUTH_JSON_PATH):
        env["AUTH_JSON_PATH"] = AUTH_JSON_PATH
    await _add_log("INFO", f"[Script] Running: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=SCRIPTS_DIR)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        await _add_log("INFO", f"[Script] Done exit={proc.returncode}")
        return {"result": {"exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace")}}
    except asyncio.TimeoutError:
        return {"error": {"code": -3, "message": "Script timed out (5min)"}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"Script failed: {e}"}}


async def pw_run_script_content(params):
    content = params.get("content", "")
    if not content:
        return {"error": {"code": -1, "message": "content is required"}}
    script_name = params.get("script_name", "tmp_script.js")
    if not script_name.endswith(".js"):
        script_name += ".js"
    args = params.get("args", []) or []
    if not isinstance(args, list):
        args = [str(args)]
    # 脚本 connectOverCDP 直连本机 chromium 的 CDP 端口（--remote-debugging-port），
    # chromium 懒启动：容器重启后未触发过启动则端口未监听，脚本会 ECONNREFUSED。
    # 先确保浏览器已启动（含登录态恢复），再执行脚本。
    await _ensure_pw_context()
    tmp_path = os.path.join(SCRIPTS_DIR, script_name)
    with open(tmp_path, "w") as f:
        f.write(content)
    env = {**os.environ, "HOME": os.path.dirname(__file__),
           "BROWSER_DATA_DIR": DATA_DIR, "CDP_URL": f"http://127.0.0.1:{CDP_PORT}"}
    task_id = params.get("task_id")
    if task_id:
        env["TASK_ID"] = str(task_id)
    job = params.get("job")
    if job:
        env["JOB_NAME"] = str(job)
    # 自定义环境变量透传（如 OTP 验证码回复、重跑参数），网关按需注入
    extra_env = params.get("env")
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            env[str(k)] = str(v)
    if os.path.exists(AUTH_JSON_PATH):
        env["AUTH_JSON_PATH"] = AUTH_JSON_PATH
    await _add_log("INFO", f"[ScriptContent] Running: {script_name} args={args}")
    try:
        proc = await asyncio.create_subprocess_exec("node", tmp_path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=SCRIPTS_DIR)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        await _add_log("INFO", f"[ScriptContent] Done exit={proc.returncode}")
        return {"result": {"exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace")}}
    except asyncio.TimeoutError:
        return {"error": {"code": -3, "message": "Script timed out (5min)"}}
    except Exception as e:
        return {"error": {"code": -1, "message": f"Script failed: {e}"}}


async def pw_ai_task(params):
    task_desc = params.get("task", "分析当前页面内容")
    max_steps = params.get("max_steps", 20)
    system_prompt = f"""你是一个浏览器自动化助手。你会收到一张页面截图和一个任务目标。
你需要分析页面内容，返回下一步操作。
返回格式必须是严格的 JSON，不要有其他内容：
{{"thought": "你的分析", "action": "操作类型", "params": {{}}, "done": false}}
action 类型：
- click: {{"action": "click", "params": {{"selector": "CSS选择器"}}}}
- type: {{"action": "type", "params": {{"selector": "CSS选择器", "text": "输入内容"}}}}
- scroll_down/scroll_up/wait/navigate/done
当前任务：{task_desc}"""
    history = []
    llm_fail_streak = 0  # 连续 LLM 调用失败（模型/地址/密钥错）：≥3 直接终止并报错，不空转烧完 max_steps
    for step in range(max_steps):
        if ai_cancel_evt.is_set():
            ai_cancel_evt.clear()
            await _add_log("WARN", "[AI Task] 已取消")
            return {"result": {"status": "cancelled", "steps": step, "history": history}}
        page = await _ensure_pw_context()
        if not page:
            return {"error": {"code": -1, "message": "Browser closed during task"}}
        try:
            page_txt = ""
            try:
                page_txt = (await page.evaluate("document.body.innerText"))[:800]
            except: pass
            history_text = ""
            if history:
                history_text = "\n\n之前的操作历史：\n" + "\n".join([f"步骤{i+1}: {h}" for i, h in enumerate(history[-5:])])
            context = f"[当前页面 {page.url}]\n[页面可见文本]\n{page_txt}\n"
            try:
                answer = await _call_deepseek(context + system_prompt + history_text)
                llm_fail_streak = 0
            except Exception as e:
                llm_fail_streak += 1
                await _add_log("ERROR", f"[AI Task] Step {step+1} LLM 调用失败({llm_fail_streak}/3): {e}")
                history.append(f"LLM 调用失败: {e}")
                if llm_fail_streak >= 3:
                    return {"error": {"code": -3, "message": f"AI 连续调用失败×3 已终止（检查 AI 设置的模型/地址/密钥）: {e}"}}
                await asyncio.sleep(2)
                continue
            answer_clean = answer.strip()
            if "```json" in answer_clean:
                answer_clean = answer_clean.split("```json")[1].split("```")[0].strip()
            elif "```" in answer_clean:
                answer_clean = answer_clean.split("```")[1].split("```")[0].strip()
            action_data = _parse_ai_json(answer_clean)
            if action_data is None:
                history.append("AI返回了无法解析的内容")
                continue
            thought = action_data.get("thought", "")
            action = action_data.get("action", "")
            ap = action_data.get("params", {})
            is_done = action_data.get("done", False)
            await _add_log("INFO", f"[AI Task] Step {step+1}: thought={thought[:80]} action={action}")
            if is_done or action == "done":
                result = ap.get("result", thought)
                await _add_log("INFO", f"[AI Task] Completed: {result}")
                return {"result": {"status": "done", "steps": step + 1, "result": result, "history": history}}
            if action == "click":
                try: await page.click(ap.get("selector",""), timeout=5000); history.append(f"点击 {ap.get('selector','')}")
                except Exception as e: history.append(f"点击失败: {e}")
            elif action == "type":
                try: await page.fill(ap.get("selector",""), ap.get("text","")); history.append(f"输入")
                except Exception as e: history.append(f"输入失败: {e}")
            elif action == "scroll_down": await page.mouse.wheel(0, 500); history.append("向下滚动")
            elif action == "scroll_up": await page.mouse.wheel(0, -500); history.append("向上滚动")
            elif action == "wait": await asyncio.sleep(ap.get("seconds",2)); history.append("等待")
            elif action == "navigate":
                await page.goto(ap.get("url",""), wait_until="domcontentloaded", timeout=30000); history.append("导航")
            else: history.append(f"未知操作: {action}")
            await asyncio.sleep(2)
        except Exception as e:
            await _add_log("ERROR", f"[AI Task] Step {step+1} exception: {e}")
            history.append(f"异常: {e}")
            await asyncio.sleep(3)
    await _add_log("WARN", f"[AI Task] Reached max steps {max_steps}")
    return {"result": {"status": "max_steps", "steps": max_steps, "history": history}}


# ---------------- DevTools 反代（HTTP + WS → 本容器 CDP 9222）----------------
# 前端「DevTools」按钮打开 /devtools/inspector.html?ws=<host>/devtools/page/<id>，
# 经这里转发到 chromium 自带 DevTools 前端与 CDP WS（外网无需直连 9222）。

CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9223)
