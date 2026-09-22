"""Chrome Driverless 入口：MCP 分发 + 启动事件。实现按 cd_*.py 模块拆分（每文件 ≤600 行）。"""
import asyncio
import os
from typing import Any

from pydantic import BaseModel

from cd_app import app
import cd_config as C
import cd_state as S
import cd_routes  # noqa: F401  注册 HTTP/WS 路由（含 /static 挂载）
import cd_audio  # noqa: F401  注册 /audio.mp3 声音流（pulse → parec → ffmpeg → mp3）
import cd_browser as B
import cd_actions as A
import cd_human as H
import cd_human_ops as HO
import cd_ai as AI
import cd_cdp as DP
import cd_audit as AU
import cd_stealth  # noqa: F401  被 cd_browser 引用


class MCPRequest(BaseModel):
    method: str
    params: Any = {}


@app.post("/mcp")
async def mcp_handler(req: MCPRequest):
    method = req.method
    params = req.params if req.params else {}
    return await _dispatch(method, params)


async def _dispatch(method, params):
    handlers = {
        "pw/screenshot": lambda: A.pw_screenshot(params),
        "pw/navigate": lambda: A.pw_navigate(params),
        "pw/init_browser": lambda: B.pw_init_browser(),
        "pw/set_proxy": lambda: A.pw_set_proxy(params),
        "pw/save_auth": lambda: A.pw_save_auth(),
        "pw/ask_deepseek": lambda: AI.pw_ask_deepseek(params),
        "pw/run_script": lambda: AI.pw_run_script(params),
        "pw/run_script_content": lambda: AI.pw_run_script_content(params),
        "pw/ai_task": lambda: AI.pw_ai_task(params),
        "pw/click": lambda: A.pw_click(params),
        "pw/hover": lambda: A.pw_hover(params),
        "pw/type": lambda: A.pw_type(params),
        "pw/key": lambda: A.pw_key(params),
        "pw/back": lambda: A.pw_back(params),
        "pw/reload": lambda: A.pw_reload(params),
        "pw/elements": lambda: A.pw_elements(params),
        "pw/clear": lambda: A.pw_clear(params),
        "pw/auto_login": lambda: A.pw_auto_login(params),
        "pw/profile_list": lambda: B.pw_profile_list(),
        "pw/profile_set": lambda: B.pw_profile_set(params),
        "pw/evaluate": lambda: A.pw_evaluate(params),
        "pw/tabs": lambda: B.pw_tabs(),
        "pw/tab_select": lambda: B.pw_tab_select(params),
        "pw/tab_close": lambda: B.pw_tab_close(params),
        "pw/tab_tag": lambda: B.pw_tab_tag(params),
        "pw/tab_close_all": lambda: B.pw_tab_close_all(params),
        "pw/new_tab": lambda: B.pw_new_tab(params),
        "pw/mouse_move": lambda: A.pw_mouse_move(params),
        "pw/mouse_down": lambda: A.pw_mouse_down(params),
        "pw/mouse_up": lambda: A.pw_mouse_up(params),
        "pw/scroll_at": lambda: A.pw_scroll_at(params),
        "ai/config_get": lambda: AI.ai_config_get(params),
        "ai/config_set": lambda: AI.ai_config_set(params),
        "ai/test": lambda: AI.ai_test(params),
        "ai/cancel": lambda: AI.ai_cancel(params),
        # 真人模式（Pass Cloudflare）+ GPU + 指纹档案
        "human/status": lambda: H.human_status(params),
        "human/set_mode": lambda: H.human_set_mode(params),
        "human/gpu_set": lambda: H.human_gpu_set(params),
        "human/screenshot": lambda: HO.human_screenshot(params),
        "human/navigate": lambda: HO.human_navigate(params),
        "human/reload": lambda: HO.human_reload(params),
        "human/new_tab": lambda: HO.human_new_tab(params),
        "human/tabs": lambda: HO.human_tabs(params),
        "human/tab_select": lambda: HO.human_tab_select(params),
        "human/tab_close": lambda: HO.human_tab_close(params),
        "human/back": lambda: HO.human_back(params),
        "human/click": lambda: HO.human_click(params),
        "human/hover": lambda: HO.human_hover(params),
        "human/type": lambda: HO.human_type(params),
        "human/key": lambda: HO.human_key(params),
        "human/clear": lambda: HO.human_clear(params),
        "human/mouse_move": lambda: HO.human_mouse_move(params),
        "human/mouse_down": lambda: HO.human_mouse_down(params),
        "human/mouse_up": lambda: HO.human_mouse_up(params),
        "human/scroll_at": lambda: HO.human_scroll_at(params),
        "fp/set": lambda: H.fp_set(params),
        # stealth 审计（invisible_playwright 移植：0-7 号排查清单可运行化）
        "pw/stealth_audit": lambda: AU.pw_stealth_audit(params),
        "human/stealth_audit": lambda: AU.human_stealth_audit(params),
    }
    handler = handlers.get(method)
    if not handler:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    await S._add_log("INFO", f"[MCP] {method} called")
    try:
        result = await handler()
        has_err = isinstance(result, dict) and "error" in result
        await S._add_log("ERROR" if has_err else "INFO",
                         f"[MCP] {method} {'error: ' + result['error'].get('message','') if has_err else 'success'}")
        return result
    except Exception as e:
        await S._add_log("ERROR", f"[MCP] {method} exception: {e}")
        return {"error": {"code": -1, "message": str(e)}}


@app.on_event("startup")
async def startup():
    # 项目级 profile 隔离：一个项目一个 profile（PROFILE_NAME 指定）；
    # 未设置时为 "debug"（临时调试用，与正式项目隔离）
    S._set_active_profile(os.environ.get("PROFILE_NAME", "debug"))
    await S._add_log("INFO", f"Chrome Driverless started, proxy={C.HTTP_PROXY}, profile={S.ACTIVE_PROFILE}, engine={C.ENGINE}")
    if C.EXTERNAL_URL:
        await S._add_log("INFO", f"EXTERNAL_URL={C.EXTERNAL_URL}")
    gpu = DP._detect_gpu()
    await S._add_log("INFO", f"[GPU] {'直通启用: ' + gpu.get('detail', '') if gpu.get('available') else '未检测到 GPU，swiftshader 软渲染'}")
    await S._add_log("INFO", f"[Humanize] motion seed={A.MOTION_SEED if A.MOTION_SEED is not None else '(random/urandom)'}（设 HUMANIZE_SEED 可固定复现）")
    # 后台启动声音管线（pulse → parec → ffmpeg → /audio.mp3）
    asyncio.create_task(cd_audio._audio_pipeline())
    # 恢复持久化的真人模式（裸浏览器）
    if S.human_mode:
        try:
            await H._human_start()
            await S._add_log("INFO", "[Human] 启动时恢复真人模式（裸浏览器）")
        except Exception as e:
            await S._add_log("ERROR", f"[Human] 恢复真人模式失败，回退普通模式: {e}")
            S.human_mode = False
            S._save_browser_state()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9223)
