"""全局配置/常量：路径、代理、CDP 端口、浏览器引擎、指纹档案。无内部依赖（日志在此初始化）。"""
import logging, os
try:
    import websockets
    HAS_WS = True
except ImportError:
    websockets = None
    HAS_WS = False

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("chrome-driverless")

DATA_DIR = os.environ.get("BROWSER_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
os.makedirs(DATA_DIR, exist_ok=True)   # 多 profile：profiles/<name>/ 下含 auth.json + profile/

# EXTERNAL_URL: 外部访问地址，用于日志输出和 /debug/url 端点
# 代理：默认留空（需要时通过环境变量注入，避免把固定地址写进代码）
HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("HTTP_PROXY_DEFAULT", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTPS_PROXY_DEFAULT", "")
# NO_PROXY：走代理时的直连白名单（本地/回环），外网站点默认走代理
NO_PROXY = os.environ.get("NO_PROXY", "localhost,127.0.0.1,::1")

EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "")

# CDP 调试端口（REMOTE_DEBUG_PORT 可覆盖）：外部脚本经 playwright connectOverCDP 直连同一持久浏览器，
# 共享登录态、可开独立 tab、探测不死锁
CDP_PORT = int(os.environ.get("REMOTE_DEBUG_PORT", "9222"))

# 浏览器引擎（容器启动时用环境变量指定，运行期不可切）：
#   BROWSER_ENGINE=chromium（默认）→ Playwright 的 Chrome for Testing（/root/.cache/ms-playwright）
#   BROWSER_ENGINE=chrome         → 镜像内打包的真 Google Chrome（/opt/google/chrome/chrome，
#                                    UA/brands/Widevine 原生自洽；见 Dockerfile.base）
ENGINE = os.environ.get("BROWSER_ENGINE", "chromium").strip().lower()
CHROME_REAL_PATH = "/opt/google/chrome/chrome"
_engine_warned = False


def _engine_binary():
    """按 BROWSER_ENGINE 返回 (二进制路径, 引擎名)；chrome 不可用时回退 chromium 并告警。"""
    global _engine_warned
    if ENGINE == "chrome":
        if os.path.exists(CHROME_REAL_PATH):
            return CHROME_REAL_PATH, "chrome"
        if not _engine_warned:
            _engine_warned = True
            logger.warning("[Engine] BROWSER_ENGINE=chrome 但 %s 不存在，回退 chromium", CHROME_REAL_PATH)
        return None, "chromium"
    return None, "chromium"


# ================= 指纹档案（仅普通 Playwright 模式生效；真人模式保持零注入） =================
# 之前 e07f571 去掉了 UA 伪造是因为「只改 UA 字符串」会与 sec-ch-ua/platform 打架；
# 这里做成整套自洽档案：HTTP UA + JS UA/platform/vendor + userAgentData(Client Hints) +
# CDP Emulation.setUserAgentOverride(含 userAgentMetadata，网络层 sec-ch-ua 头同步) 一起换。
# 注意：Emulation.* 属 rebrowser 标记的可检测命令；要最大隐身用 real 档案或真人模式。
FP_PROFILES = {
    "real": {"label": "真实（推荐，最大隐身）"},
    "chrome_win": {
        "label": "Chrome · Windows",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
        "platform": "Win32", "vendor": "Google Chrome",
        "ua_metadata": {
            "brands": [{"brand": "Not?A_Brand", "version": "24"},
                       {"brand": "Chromium", "version": "151"},
                       {"brand": "Google Chrome", "version": "151"}],
            "fullVersionList": [{"brand": "Not?A_Brand", "version": "24"},
                                {"brand": "Chromium", "version": "151"},
                                {"brand": "Google Chrome", "version": "151.0.0.0"}],
            "fullVersion": "151.0.0.0", "platform": "Windows", "platformVersion": "15.0.0",
            "architecture": "x86", "model": "", "mobile": False, "bitness": "64", "wow64": False,
        },
    },
    "chrome_mac": {
        "label": "Chrome · macOS",
        "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
        "platform": "MacIntel", "vendor": "Google Chrome",
        "ua_metadata": {
            "brands": [{"brand": "Not?A_Brand", "version": "24"},
                       {"brand": "Chromium", "version": "151"},
                       {"brand": "Google Chrome", "version": "151"}],
            "fullVersionList": [{"brand": "Not?A_Brand", "version": "24"},
                                {"brand": "Chromium", "version": "151"},
                                {"brand": "Google Chrome", "version": "151.0.0.0"}],
            "fullVersion": "151.0.0.0", "platform": "macOS", "platformVersion": "14.6.0",
            "architecture": "arm", "model": "", "mobile": False, "bitness": "64", "wow64": False,
        },
    },
    "safari_mac": {
        "label": "Safari · macOS",
        "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"),
        "platform": "MacIntel", "vendor": "Apple Computer, Inc.", "safari": True,
    },
}
FP_PROFILE = "real"



# AI 配置持久化 + 真人模式/指纹档案持久化文件
AI_CONFIG_PATH = os.path.join(DATA_DIR, "ai_config.json")
HUMAN_STATE_PATH = os.path.join(DATA_DIR, "browser_state.json")

