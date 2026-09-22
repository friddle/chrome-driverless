"""真人模式（Pass Cloudflare）：裸 chromium 生命周期 + 模式/GPU/指纹档案切换。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
import cd_state as S
from cd_config import FP_PROFILES, _engine_binary
from cd_cdp import _cdp_http, _probe_webgl, _detect_gpu
from cd_browser import _ensure_pw_context, _find_chrome_binary, _kill_orphan_chrome, _chrome_args
from cd_state import _add_log, _save_browser_state

async def _human_start():
    """切到真人模式：存登录态 → 关 Playwright 浏览器 → 裸进程启动 chromium（同一 profile）。"""
    if S.human_mode and S.bare_proc:
        return
    if S.pw_context:
        try:
            await S.pw_context.storage_state(path=S.AUTH_JSON_PATH)
            await _add_log("INFO", f"[Human] 切模式前保存登录态 -> {S.AUTH_JSON_PATH}")
        except Exception:
            pass
    if S.pw_context:
        try:
            await S.pw_context.close()
        except Exception:
            pass
    if S.pw_instance:
        try:
            await S.pw_instance.stop()
        except Exception:
            pass
    S.pw_browser = S.pw_context = S.pw_instance = None
    S.pw_active_page = None
    await asyncio.sleep(1)
    _kill_orphan_chrome()
    chrome = _find_chrome_binary()
    if not chrome:
        raise RuntimeError("找不到 chromium 可执行文件（ms-playwright 缓存目录）")
    os.makedirs(S.BROWSER_PROFILE_DIR, exist_ok=True)
    for f in os.listdir(S.BROWSER_PROFILE_DIR):
        if f.startswith("Singleton"):
            try:
                os.remove(os.path.join(S.BROWSER_PROFILE_DIR, f))
            except Exception:
                pass
    args = [chrome, f"--user-data-dir={S.BROWSER_PROFILE_DIR}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-crash-reporter", "--disable-breakpad", "--hide-crash-restore-bubble"] + _chrome_args()
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":99")
    # 时区三对齐（真人模式侧）：裸 chromium 没有 playwright context 的 timezone_id，
    # 必须用 TZ 环境变量对齐出口时区，否则 browser=UTC vs egress=Asia/Hong_Kong 是一致性 tell
    if S.resolved_timezone:
        env["TZ"] = S.resolved_timezone
    S.bare_proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = asyncio.get_event_loop().time() + 30
    while True:
        try:
            await _cdp_http("/json/version", timeout=2)
            break
        except Exception:
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"裸 chromium 启动超时（pid={S.bare_proc.pid}）")
            await asyncio.sleep(0.5)
    S.human_mode = True
    _save_browser_state()
    await _probe_webgl()
    await _add_log("INFO", f"[Human] 真人模式 ON：裸 chromium pid={S.bare_proc.pid} webgl={S.webgl_info.get('renderer', '')[:60]}")


async def _human_stop():
    """退出真人模式：杀裸 chromium → 恢复 Playwright 浏览器（登录态在同一 profile 里天然保留）。"""
    if S.bare_proc:
        try:
            S.bare_proc.terminate()
        except Exception:
            pass
    S.bare_proc = None
    await asyncio.sleep(0.5)
    _kill_orphan_chrome()
    S.human_mode = False
    _save_browser_state()
    await _ensure_pw_context()
    await _add_log("INFO", "[Human] 真人模式 OFF：Playwright 浏览器已恢复")


async def _restart_browser():
    """按当前 S.human_mode/S.gpu_mode 重启浏览器（GPU 开关切换用，保持所在模式不变）。"""
    if S.human_mode:
        if S.bare_proc:
            try:
                S.bare_proc.terminate()
            except Exception:
                pass
        S.bare_proc = None
        await asyncio.sleep(0.5)
        _kill_orphan_chrome()
        S.human_mode = False  # 让 _human_start 重新走完整启动
        await _human_start()
    else:
        if S.pw_context:
            try:
                await S.pw_context.close()
            except Exception:
                pass
        S.pw_context = S.pw_instance = None
        await _ensure_pw_context()
        await _probe_webgl()


async def human_status(params=None):
    bin_path, engine_kind = _engine_binary()
    return {"result": {"human_mode": S.human_mode, "gpu_mode": S.gpu_mode,
                       "gpu": _detect_gpu(), "webgl": S.webgl_info,
                       "pid": S.bare_proc.pid if S.bare_proc else None,
                       "engine": engine_kind if engine_kind == "chrome" else "chromium",
                       "engine_path": _find_chrome_binary(),
                       "fp_profile": S.FP_PROFILE,
                       "fp_options": {k: v.get("label", k) for k, v in FP_PROFILES.items()}}}


async def fp_set(params):
    """切换指纹档案：fp/set {profile: real|chrome_win|chrome_mac|safari_mac}。
    普通模式立即重启浏览器生效；真人模式不注入（保持零痕迹），记录后下次普通模式生效。"""
    p = (params.get("profile") or "real").strip()
    if p not in FP_PROFILES:
        return {"error": {"code": -2, "message": f"unknown profile: {p}（可选: {', '.join(FP_PROFILES)}）"}}
    if p != S.FP_PROFILE:
        S.FP_PROFILE = p
        _save_browser_state()
        await _add_log("INFO", f"[FP] 指纹档案 -> {p}（{FP_PROFILES[p].get('label', '')}）")
        if S.human_mode:
            pass  # 真人模式零注入；下次回到普通模式时生效
        else:
            if S.pw_context:
                try:
                    await S.pw_context.close()
                except Exception:
                    pass
            S.pw_context = S.pw_instance = None
            await _ensure_pw_context()
    st = (await human_status())["result"]
    if S.human_mode and p != "real":
        st["note"] = "真人模式不套指纹（零注入优先）；已记录，回普通模式后生效"
    return {"result": st}


async def human_set_mode(params):
    on = bool(params.get("on"))
    if on and not S.human_mode:
        await _human_start()
    elif not on and S.human_mode:
        await _human_stop()
    return await human_status()


async def human_gpu_set(params):
    on = bool(params.get("on"))
    gpu = _detect_gpu()
    if on and not gpu.get("available"):
        return {"error": {"code": -2, "message": "未检测到 GPU（无 nvidia-smi 且无 /dev/dri），无法开启 GPU 直通"}}
    if on == S.gpu_mode:
        return await human_status()
    S.gpu_mode = on
    _save_browser_state()
    try:
        await _restart_browser()
    except Exception as e:
        return {"error": {"code": -1, "message": f"浏览器重启失败: {e}"}}
    await _add_log("INFO", f"[GPU] 直通 {'ON' if S.gpu_mode else 'OFF'} webgl={S.webgl_info.get('renderer', '')[:60]}")
    return await human_status()


