"""浏览器 Agent（browser-use 架构原生移植）。

旧 pw_ai_task 让 LLM 盲猜 CSS 选择器，真实页面必然失败。本模块改为业界标准做法：
1. 每步从 DOM 提取可见可交互元素并编号（[3] <button>登录</button>），含 iframe 内元素；
2. LLM 拿「URL + 页面文本 + 编号元素表」输出动作 JSON（click index=3 / type index=5 ...）；
3. 执行动作（复用 cd_actions 人性化鼠标轨迹），结果反馈给下一步，直到 done / max_steps。
"""
import asyncio, base64, json, random

import cd_state as S
from cd_state import _add_log
from cd_browser import _ensure_pw_context
from cd_actions import _raw_click, _click_locator_raw, _motion_rng
from cd_ai import _llm_chat, _ai_cfg, _parse_ai_json

MAX_ELEMENTS = 120          # 提取元素上限（token 与覆盖面的平衡）
PAGE_TEXT_CHARS = 2400      # 页面文本摘录长度
KEEP_FULL_STATES = 4        # 保留完整页面状态的最少步数（更早的压缩为 URL 行，防上下文爆）

# ---------------- DOM 提取（browser-use 风格：编号 + 可交互 + 可见 + iframe） ----------------

_EXTRACT_JS = r"""() => {
  const out = [];
  const seen = new Set();
  const accepted = [];   // 已收录元素（用于「顶层可点击祖先优先」去重）

  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el, depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      let p = node.tagName.toLowerCase();
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const parent = node.parentNode;
      if (parent && parent.children) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) p += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(p);
      node = node.parentNode;
      depth++;
      if (node && node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
    }
    return parts.join(' > ');
  }

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return false;
    if (!el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  }

  function hasAcceptedAncestor(el) {
    let p = el.parentElement;
    while (p) {
      if (accepted.includes(p)) return true;
      p = p.parentElement;
    }
    return false;
  }

  function serialize(el, doc, frameCss, ox, oy) {
    const tag = el.tagName.toLowerCase();
    const r = el.getBoundingClientRect();
    const role = el.getAttribute('role') || '';
    const type = (el.getAttribute('type') || '').toLowerCase();
    const txt = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    const aria = (el.getAttribute('aria-label') || '').slice(0, 40);
    const title = (el.getAttribute('title') || '').slice(0, 40);
    const placeholder = (el.getAttribute('placeholder') || '').slice(0, 40);
    const name = el.getAttribute('name') || '';
    let href = (el.getAttribute('href') || '');
    if (href.length > 70) href = href.slice(0, 70) + '…';
    // 密码框的当前值不外泄（只说明是密码框）
    const value = (type === 'password') ? '' : ((el.value || '') + '').slice(0, 40);
    const editable = tag === 'input' || tag === 'textarea' || el.isContentEditable;
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const cx = ox + r.x + r.width / 2, cy = oy + r.y + r.height / 2;
    const inView = cx >= 0 && cx <= vw && cy >= 0 && cy <= vh;
    let options;
    if (tag === 'select') {
      options = Array.from(el.options).slice(0, 8).map(o =>
        ({value: (o.value || '').slice(0, 30), label: (o.label || o.text || '').trim().slice(0, 24)}));
    }
    return {tag, role, type, text: txt, aria, title, placeholder, name,
            href, value, editable, options, css: cssPath(el),
            frame: frameCss, x: Math.round(cx), y: Math.round(cy),
            w: Math.round(r.width), h: Math.round(r.height), view: inView};
  }

  function line(e) {
    let s = '<' + e.tag;
    if (e.role) s += ' role=' + e.role;
    if (e.type) s += ' type=' + e.type;
    s += '>';
    const attrs = [];
    if (e.text) attrs.push('"' + e.text + '"');
    if (e.placeholder) attrs.push('placeholder=' + e.placeholder);
    if (e.aria) attrs.push('aria=' + e.aria);
    if (e.title) attrs.push('title=' + e.title);
    if (e.name) attrs.push('name=' + e.name);
    if (e.href) attrs.push('href=' + e.href);
    if (e.value !== '' && e.editable) attrs.push('value=' + e.value);
    if (e.options && e.options.length) attrs.push('options=' + e.options.map(o => o.label || o.value).join('|'));
    if (!e.view) attrs.push('(视口外,需scroll_to)');
    if (e.frame) attrs.push('(iframe内)');
    return s + (attrs.length ? ' ' + attrs.join(' ') : '');
  }

  function collect(doc, frameCss, ox, oy, depth) {
    if (out.length >= %MAX_ELEMENTS%) return;
    const SEL = 'a, button, input, select, textarea, summary, label, [role], [onclick], [contenteditable="true"], [tabindex]';
    let cands = Array.from(doc.querySelectorAll(SEL));
    // 自定义可点击元素兜底：cursor:pointer 的常见容器（SPA 按钮 div/span/li），限量扫描
    if (cands.length < 40) {
      const extras = doc.querySelectorAll('div, span, li, td, p, img');
      for (const el of extras) {
        if (cands.length > 400) break;
        if (!el.childElementCount && (el.textContent || '').trim() &&
            getComputedStyle(el).cursor === 'pointer') cands.push(el);
      }
    }
    for (const el of cands) {
      if (out.length >= %MAX_ELEMENTS%) break;
      if (!visible(el) || seen.has(el)) continue;
      if (hasAcceptedAncestor(el)) continue;   // 顶层可点击祖先优先，去掉 a>span 重复
      // 纯装饰元素：无文本/aria/name/href/placeholder 且非输入类 → 跳过
      const t = (el.innerText || '').trim();
      const deco = !t && !el.getAttribute('aria-label') && !el.getAttribute('name') &&
                   !el.getAttribute('href') && !el.getAttribute('placeholder') &&
                   !['input','select','textarea'].includes(el.tagName.toLowerCase());
      if (deco) continue;
      seen.add(el);
      accepted.push(el);
      out.push(serialize(el, doc, frameCss, ox, oy));
    }
    // 同源 iframe 递归（登录表单常在 iframe 里，不进去 agent 就是瞎的）
    if (depth < 2) {
      for (const f of doc.querySelectorAll('iframe')) {
        if (out.length >= %MAX_ELEMENTS%) break;
        try {
          if (!visible(f)) continue;
          const fd = f.contentDocument;
          if (!fd || !fd.body) continue;       // 跨域 iframe 拒绝访问，跳过
          const r = f.getBoundingClientRect();
          collect(fd, cssPath(f), ox + r.x, oy + r.y, depth + 1);
        } catch (e) { /* cross-origin */ }
      }
    }
  }

  collect(document, '', 0, 0, 0);
  const lines = out.map((e, i) => '[' + (i + 1) + '] ' + line(e));
  let text = '';
  try { text = (document.body.innerText || '').replace(/\n{3,}/g, '\n\n').trim(); } catch (e) {}
  return {url: location.href, title: document.title, elements: out, lines: lines, text: text};
}""".replace("%MAX_ELEMENTS%", str(MAX_ELEMENTS))


async def _extract_state(page):
    """提取当前页面状态（URL/标题/文本/编号元素表）。"""
    data = await page.evaluate(_EXTRACT_JS)
    text = data.get("text", "") or ""
    head = text[:PAGE_TEXT_CHARS]
    if len(text) > PAGE_TEXT_CHARS:
        head += "\n…(文本过长截断，可 scroll 后再看)"
    state = {
        "url": data.get("url", ""), "title": (data.get("title") or "")[:100],
        "elements": data.get("elements", []), "text": head,
    }
    return state


def _state_message(state, step):
    """把页面状态格式化成 LLM user 消息文本。"""
    els = state["elements"]
    lines = [f"[步骤 {step}] [URL] {state['url']}", f"[标题] {state['title']}",
             "[页面文本摘录]", state["text"] or "(空)",
             f"[可交互元素]（共 {len(els)} 个，动作用 index 引用）"]
    # 用重新生成的行避免 JS 端 lines 与 elements 漂移
    lines += [f"[{i+1}] {_elem_line(e)}" for i, e in enumerate(els)]
    return "\n".join(lines)


def _elem_line(e):
    parts = []
    for k, label in (("text", None), ("placeholder", "placeholder"), ("aria", "aria"),
                     ("title", "title"), ("name", "name"), ("href", "href"), ("value", "value")):
        v = (e.get(k) or "").strip() if isinstance(e.get(k), str) else e.get(k)
        if v:
            parts.append(f'"{v}"' if k == "text" else f"{label}={v}")
    if e.get("options"):
        parts.append("options=" + "|".join(o.get("label") or o.get("value") or "" for o in e["options"][:8]))
    if not e.get("view", True):
        parts.append("(视口外,需scroll_to)")
    if e.get("frame"):
        parts.append("(iframe内)")
    head = f"<{e['tag']}" + (f" role={e['role']}" if e.get("role") else "") + \
           (f" type={e['type']}" if e.get("type") else "") + ">"
    return head + (" " + " ".join(parts) if parts else "")


_SYSTEM_PROMPT = """你是一个浏览器自动化 Agent（browser-use 架构）。每一步你会收到：
[URL] [标题] [页面文本摘录] [可交互元素表]（元素带编号 index）。
你要返回下一步动作，格式是严格 JSON，不要输出任何其他内容：
{"evaluation": "对上一步效果的简要评估（第一步填'无'）",
 "memory": "已完成的进度与关键信息（账号、URL、已取得的数值等）",
 "next_goal": "这一步要做什么",
 "actions": [动作对象, ...]}

动作类型（actions 数组每项一个，每步最多 3 个）：
- {"action":"navigate", "url":"https://..."}
- {"action":"click", "index": 元素编号}
- {"action":"click_text", "text":"可见文字"}        # 无合适编号时的兜底，按可见文本点击
- {"action":"type", "index": 元素编号, "text":"输入内容", "clear": true}   # 输入框；clear=true 先清空
- {"action":"press", "key":"Enter"}                  # Tab/Escape/ArrowDown 等
- {"action":"select", "index": 元素编号, "value":"选项value"}   # 下拉框
- {"action":"scroll", "direction":"down|up", "amount": 像素}   # amount 缺省 600
- {"action":"scroll_to", "index": 元素编号}          # 滚动到视口外元素
- {"action":"wait", "seconds": 2}                    # 等页面加载，1-10
- {"action":"new_tab", "url":"https://..."}
- {"action":"switch_tab", "index": tab编号}
- {"action":"close_tab", "index": tab编号}
- {"action":"go_back"}
- {"action":"done", "result": "任务最终结果（要求汇报具体数值/结论，不能只说完成）"}

规则：
1. index 只能引用本次元素表里的编号，每一步编号会重新生成，不要沿用旧编号。
2. 每步动作后你会看到新的页面状态与执行结果；上一步失败时换一种做法（如 click 失败改 click_text 或先 scroll_to）。
3. 登录流程：先点击账号输入框 type 账号，再点击密码框 type 密码，然后点登录/提交按钮。密码不要读出来。
4. 需要页面上的数据时，直接从[页面文本摘录]读取；文本被截断就 scroll 后再读。
5. 遇到滑块/验证码：先 wait 2 秒看是否自动通过；确实无法越过则 done 并说明卡在哪。
6. 任务完成或确认无法继续时必须 done；不要原地重复相同动作。"""


# ---------------- 动作执行 ----------------

def _locator(page, el):
    """元素数据 → Playwright Locator（支持 iframe 内元素）。"""
    css = el.get("css") or ""
    if not css:
        raise RuntimeError("元素缺少 css 路径")
    if el.get("frame"):
        return page.frame_locator(el["frame"]).locator(css)
    return page.locator(css).first


async def _act(page, act, state):
    """执行单个动作，返回观察文本（ok: ... / failed: ...）。"""
    kind = act.get("action", "")
    els = state["elements"]

    def el_by_index(idx):
        i = int(idx) - 1
        if i < 0 or i >= len(els):
            raise RuntimeError(f"index {idx} 不在当前元素表(1-{len(els)})")
        return els[i]

    if kind == "click":
        el = el_by_index(act.get("index"))
        loc = _locator(page, el)
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        try:
            await loc.click(timeout=6000)
        except Exception:
            # 遮挡/动画元素：人性化坐标点击兜底（iframe 元素坐标已在提取时换算为页面绝对坐标）
            await _raw_click(page, el["x"], el["y"])
        label = (el.get("text") or el.get("placeholder") or el.get("aria") or el.get("css", ""))[:40]
        return f"ok: 已点击 [{act.get('index')}] {el['tag']} {label}"

    if kind == "click_text":
        text = str(act.get("text", "")).strip()
        loc = page.get_by_text(text, exact=False).first
        await _click_locator_raw(page, loc)
        return f"ok: 已按文本点击 {text[:40]}"

    if kind == "type":
        el = el_by_index(act.get("index"))
        loc = _locator(page, el)
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        try:
            await loc.click(timeout=6000)
        except Exception:
            await _raw_click(page, el["x"], el["y"])
        if act.get("clear"):
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Delete")
        await page.keyboard.type(str(act.get("text", "")), delay=_motion_rng.uniform(35, 90))
        return f"ok: 已在 [{act.get('index')}] {el['tag']} 输入内容(len={len(str(act.get('text','')))})"

    if kind == "press":
        await page.keyboard.press(str(act.get("key", "Enter")))
        return f"ok: 已按键 {act.get('key')}"

    if kind == "select":
        el = el_by_index(act.get("index"))
        loc = _locator(page, el)
        try:
            await loc.select_option(act.get("value"), timeout=6000)
        except Exception:
            await loc.select_option(label=str(act.get("value")), timeout=6000)
        return f"ok: 已选择 {act.get('value')}"

    if kind == "scroll":
        dy = int(act.get("amount") or 600)
        if str(act.get("direction", "down")) == "up":
            dy = -dy
        await page.mouse.wheel(0, dy)
        return f"ok: 已滚动 {dy}px"

    if kind == "scroll_to":
        el = el_by_index(act.get("index"))
        await _locator(page, el).scroll_into_view_if_needed(timeout=6000)
        return f"ok: 已滚动到 [{act.get('index')}]"

    if kind == "wait":
        s = max(1, min(10, int(act.get("seconds") or 2)))
        await asyncio.sleep(s)
        return f"ok: 已等待 {s}s"

    if kind == "navigate":
        url = str(act.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        last_err = None
        for _ in range(3):
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
            raise RuntimeError(f"导航失败: {last_err[:120]}")
        return f"ok: 已导航 {url[:80]}"

    if kind == "new_tab":
        url = str(act.get("url", "") or "about:blank")
        if url != "about:blank" and not url.startswith(("http://", "https://")):
            url = "https://" + url
        p = await S.pw_context.new_page()
        S.pw_active_page = p
        if url != "about:blank":
            await p.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"ok: 已开新 tab #{len(S.pw_context.pages)-1} {url[:60]}"

    if kind == "switch_tab":
        pages = S.pw_context.pages
        i = int(act.get("index", 0))
        if i < 0 or i >= len(pages):
            raise RuntimeError(f"tab index 越界(0-{len(pages)-1})")
        S.pw_active_page = pages[i]
        return f"ok: 已切到 tab #{i} {pages[i].url[:60]}"

    if kind == "close_tab":
        pages = S.pw_context.pages
        i = int(act.get("index", 0))
        if len(pages) <= 1:
            raise RuntimeError("最后一个 tab 不能关")
        await pages[i].close()
        if S.pw_active_page is None or S.pw_active_page.is_closed():
            S.pw_active_page = S.pw_context.pages[-1]
        return f"ok: 已关 tab #{i}"

    if kind == "go_back":
        await page.go_back(timeout=15000)
        return "ok: 已后退"

    raise RuntimeError(f"未知动作: {kind}")


# ---------------- Agent 主循环 ----------------

async def run_agent(params):
    """pw/ai_task 入口：browser-use 风格任务循环。"""
    task = str(params.get("task", "")).strip()
    if not task:
        return {"error": {"code": -2, "message": "task is required"}}
    max_steps = max(1, min(100, int(params.get("max_steps", 50))))
    cfg = _ai_cfg()
    use_vision = params.get("use_vision")
    use_vision = cfg.get("vision", False) if use_vision is None else bool(use_vision)

    if S.human_mode:
        return {"error": {"code": -2, "message": "真人模式运行中（无 Playwright）：请先关闭真人模式再用 AI 任务"}}

    await _add_log("INFO", f"[Agent] 任务开始: {task[:120]} (max_steps={max_steps}, vision={use_vision}, model={cfg['model']})")
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    state_urls = []       # (message_index, url) 用于历史压缩
    history = []          # 返回给前端的人读摘要
    llm_fail = parse_fail = 0

    for step in range(1, max_steps + 1):
        if S.ai_cancel_evt.is_set():
            S.ai_cancel_evt.clear()
            await _add_log("WARN", "[Agent] 已取消")
            return {"result": {"status": "cancelled", "steps": step - 1, "history": history}}
        page = await _ensure_pw_context()
        try:
            state = await _extract_state(page)
        except Exception as e:
            await asyncio.sleep(2)
            try:
                state = await _extract_state(page)
            except Exception as e2:
                return {"error": {"code": -1, "message": f"页面状态提取失败: {e2}"}}

        state_msg = _state_message(state, step)
        content = state_msg + "\n\n[任务] " + task if step == 1 else state_msg
        # 视觉模型附截图（JPEG 压缩省 token）；调用失败自动降级纯文本
        if use_vision:
            try:
                shot = await page.screenshot(type="jpeg", quality=60)
                content = [{"type": "text", "text": content},
                           {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(shot).decode()}}]
            except Exception as e:
                await _add_log("WARN", f"[Agent] 截图失败，转纯文本: {e}")
                use_vision = False
        messages.append({"role": "user", "content": content})
        state_urls.append((len(messages) - 1, state["url"]))
        _compact_history(messages, state_urls, KEEP_FULL_STATES)

        try:
            answer = await _llm_chat(messages, use_vision=use_vision)
            llm_fail = 0
        except Exception as e:
            llm_fail += 1
            await _add_log("ERROR", f"[Agent] Step {step} LLM 调用失败({llm_fail}/3): {e}")
            if use_vision and llm_fail >= 1:
                use_vision = False   # 视觉模型 400 时降级纯文本重试
                continue
            if llm_fail >= 3:
                return {"error": {"code": -3, "message": f"LLM 连续失败×3 终止（检查 AI 设置）: {e}"}}
            await asyncio.sleep(2)
            continue

        messages.append({"role": "assistant", "content": answer})
        data = _parse_ai_json(answer)
        if not data or "actions" not in data:
            parse_fail += 1
            messages.append({"role": "user", "content": "上次返回不是合法 JSON（缺少 actions 字段）。请严格只输出规定的 JSON 对象。"})
            if parse_fail >= 3:
                return {"error": {"code": -3, "message": "LLM 连续 3 次输出无法解析，终止"}}
            continue
        parse_fail = 0

        thought = str(data.get("next_goal", ""))[:120]
        actions = data["actions"][:5] if isinstance(data["actions"], list) else [data["actions"]]
        await _add_log("INFO", f"[Agent] Step {step}: {thought} | actions={[a.get('action') for a in actions]}")
        history.append(f"S{step}: {thought} → " + ", ".join(a.get("action", "?") for a in actions))

        observations = []
        done_result = None
        for act in actions:
            if S.ai_cancel_evt.is_set():
                break
            if not isinstance(act, dict):
                continue
            if act.get("action") == "done":
                done_result = str(act.get("result") or data.get("memory") or "任务完成")
                observations.append("ok: 任务完成")
                break
            try:
                observations.append(await _act(page, act, state))
            except Exception as e:
                observations.append(f"failed: {str(e)[:160]}")
        if S.ai_cancel_evt.is_set():
            S.ai_cancel_evt.clear()
            await _add_log("WARN", "[Agent] 已取消")
            return {"result": {"status": "cancelled", "steps": step, "history": history}}
        if done_result is not None:
            await _add_log("INFO", f"[Agent] 完成(step {step}): {done_result[:200]}")
            return {"result": {"status": "done", "steps": step, "result": done_result,
                               "final_url": state["url"], "history": history}}

        # 动作后给页面时间生效（导航/弹窗/路由），下一轮再重新提取状态
        await asyncio.sleep(1.8)
        messages.append({"role": "user",
                         "content": "[执行结果]\n" + "\n".join(observations) + "\n（新的页面状态见下一条消息）"})

    await _add_log("WARN", f"[Agent] 达到最大步数 {max_steps}")
    return {"result": {"status": "max_steps", "steps": max_steps, "history": history,
                       "final_url": state.get("url", "")}}


def _compact_history(messages, state_urls, keep):
    """上下文压缩：只保留最近 keep 个完整页面状态（含截图），更早的替换为 URL 摘要行。"""
    if len(state_urls) <= keep:
        return
    idx, url = state_urls.pop(0)
    messages[idx]["content"] = f"(历史页面状态已省略，当时 URL: {url[:100]})"
