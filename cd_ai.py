"""AI 能力：DeepSeek 兼容 LLM 配置/调用、AI 自动任务、node 脚本执行器。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_config as C
import cd_state as S
from cd_config import logger, SCRIPTS_DIR, DATA_DIR, CDP_PORT, AI_CONFIG_PATH
from cd_browser import _page_for, _ensure_pw_context
from cd_state import _add_log

def _ai_cfg():
    """AI 有效配置：data/ai_config.json > 环境变量 > 默认。
    flash 等别名模型指向 deepseek 官方必然 400（Model Not Exist），自动回退 deepseek-chat 并告警。
    vision=多模态（agent 步骤附截图）；纯文本模型（deepseek-chat）保持 False 走 DOM 元素表。"""
    cfg = {"model": "", "base_url": "", "api_key": "", "vision": False}
    try:
        with open(AI_CONFIG_PATH, "r") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg["model"] = (saved.get("model") or "").strip()
            cfg["base_url"] = (saved.get("base_url") or "").strip().rstrip("/")
            cfg["api_key"] = (saved.get("api_key") or "").strip()
            cfg["vision"] = bool(saved.get("vision"))
    except Exception:
        pass
    if not cfg["model"]:
        cfg["model"] = os.environ.get("AI_MODEL", "").strip()
    if not cfg["base_url"]:
        cfg["base_url"] = os.environ.get("AI_BASE_URL", "").strip().rstrip("/")
    if not cfg["api_key"]:
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not cfg["vision"]:
        cfg["vision"] = os.environ.get("AI_VISION", "").strip().lower() in ("1", "true", "yes")
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
    return await _llm_chat([{"role": "user", "content": prompt}], override=override)


async def _llm_chat(messages, override=None, use_vision=False):
    """OpenAI 兼容 chat/completions：messages 数组（agent 多轮对话用）。
    use_vision 时 messages 内 content 可为 [{type:text},{type:image_url}] 分片（多模态模型）。
    temperature=0：agent 输出 JSON 动作，确定性优先。"""
    cfg = override or _ai_cfg()
    if not cfg["api_key"]:
        raise Exception("API key 未配置（DEEPSEEK_API_KEY 或前端 AI 设置）")
    req_body = {"model": cfg["model"], "messages": messages,
                "max_tokens": 4096, "temperature": 0}
    req = urllib.request.Request(f"{cfg['base_url']}/chat/completions",
        data=json.dumps(req_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        timeout = 90 if use_vision else 60
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        "vision": cfg["vision"],
        "from_file": os.path.exists(AI_CONFIG_PATH),
    }}


async def ai_config_set(params):
    """保存 AI 配置到 data/ai_config.json（覆盖 env；api_key 留空=沿用服务端已配密钥）。"""
    model = (params.get("model") or "").strip()
    base_url = (params.get("base_url") or "").strip().rstrip("/")
    api_key = (params.get("api_key") or "").strip()
    vision = bool(params.get("vision", False))
    if not model or not base_url:
        return {"error": {"code": -2, "message": "model 和 base_url 均必填"}}
    if not base_url.startswith(("http://", "https://")):
        return {"error": {"code": -2, "message": "base_url 必须以 http(s):// 开头"}}
    saved = {"model": model, "base_url": base_url, "vision": vision}
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
    S.ai_cancel_evt.set()
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
    if os.path.exists(S.AUTH_JSON_PATH):
        env["AUTH_JSON_PATH"] = S.AUTH_JSON_PATH
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
    if os.path.exists(S.AUTH_JSON_PATH):
        env["AUTH_JSON_PATH"] = S.AUTH_JSON_PATH
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
    """pw/ai_task 入口 → cd_agent.run_agent（browser-use 架构）。
    旧实现（LLM 盲猜 CSS 选择器）已删除：真实页面全部失败。"""
    import cd_agent
    return await cd_agent.run_agent(params)


# ---------------- DevTools 反代（HTTP + WS → 本容器 CDP 9222）----------------
# 前端「DevTools」按钮打开 /devtools/inspector.html?ws=<host>/devtools/page/<id>，
# 经这里转发到 chromium 自带 DevTools 前端与 CDP WS（外网无需直连 9222）。

CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"

