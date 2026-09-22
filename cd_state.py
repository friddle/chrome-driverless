"""全部可变运行时状态（跨模块一律 cd_state.S.xxx 访问，禁止 from-import 快照）。"""
import asyncio, json, logging, os
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
from cd_config import logger

PROFILES_DIR = C.PROFILES_DIR
AUTH_JSON_PATH = os.path.join(C.DATA_DIR, "auth.json")  # 兼容旧路径（脚本 AUTH_JSON_PATH 指向 profiles/<active>/auth.json）
BROWSER_PROFILE_DIR = os.path.join(C.DATA_DIR, "browser-profile")  # 兼容旧路径
ACTIVE_PROFILE = "default"  # 当前激活的 profile


# Playwright 常驻对象（persistent context）
pw_instance = None
pw_browser = None
pw_context = None
pw_active_page = None   # MCP 会话当前操作的 tab
tab_tags = {}           # id(page) -> 任务标签

use_proxy = True  # 默认启用代理（外网站点走代理，本地/回环白名单由 NO_PROXY 直连）
task_lock = asyncio.Lock()
task_busy = False
mcp_logs = deque(maxlen=500)
mcp_logs_lock = asyncio.Lock()
ai_cancel_evt = asyncio.Event()

FP_PROFILE = "real"     # 当前指纹档案（real/chrome_win/chrome_mac/safari_mac）
resolved_timezone = None  # pw_set_proxy 时经代理反查出口时区写回；None=默认 Asia/Shanghai
human_mode = False      # 真人模式开关（裸 chromium + raw CDP Input/Page）
gpu_mode = False        # GPU 直通开关（启动时按 _detect_gpu 自动置位）
bare_proc = None        # 真人模式下的裸 chromium 进程
human_targets = []      # /json/list 的 page target 缓存
human_active_idx = 0
human_active_id = None  # 活动 tab 的 target id（/json/list 顺序漂移时按 id 重定位）
webgl_info = {"renderer": "", "vendor": ""}

os.makedirs(C.DATA_DIR, exist_ok=True)
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
    # chromium 声音输出 → pulse 虚拟声卡（/audio.mp3 流式回放给 UI，见 cd_audio.py）
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/pulse")
    os.environ.setdefault("PULSE_SERVER", "unix:/tmp/pulse/native")


def _load_browser_state():
    global human_mode, FP_PROFILE
    try:
        with open(C.HUMAN_STATE_PATH) as f:
            st = json.load(f)
        human_mode = bool(st.get("human_mode", False))
        if st.get("fp_profile") in C.FP_PROFILES:
            FP_PROFILE = st["fp_profile"]
    except Exception:
        pass


def _save_browser_state():
    try:
        with open(C.HUMAN_STATE_PATH, "w") as f:
            json.dump({"human_mode": human_mode, "fp_profile": FP_PROFILE}, f)
    except Exception:
        pass


_load_browser_state()
# GPU 直通不是开关：每次启动自动探测，宿主机有 GPU（NVIDIA runtime 注入 / DRI 直通）就默认启用，
# 没有则回落 swiftshader 软渲染。前端只做状态徽标展示。



async def _add_log(level, msg):
    entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
    async with mcp_logs_lock:
        mcp_logs.append(entry)
    logger.log(getattr(logging, level, logging.INFO), msg)



_load_browser_state()
