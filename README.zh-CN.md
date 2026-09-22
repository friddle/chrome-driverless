# chrome-driverless

[English](README.md)

基于 **Playwright** 驱动的一个**有头（headed + xvfb）持久化 Chrome**，对外提供 **MCP 风格 HTTP 接口**（`POST /mcp`），并内置 Web 控制台。人和 AI 共用同一个长驻浏览器：在控制台里登录一次，之后通过 HTTP、CDP、Node.js 脚本驱动它，或直接把任务交给大模型完成。

## 核心特性

- **持久化 context** —— 登录态保存在 `data/profiles/<name>/auth.json`，重启不丢，可导出复用
- **MCP 风格 HTTP API** —— 导航、截图、点击、输入、按键、悬停、执行 JS、多 Tab、多 Profile、代理切换、原始鼠标/滚动控制
- **Web 控制台** —— 多图层实时视口（截图层/元素高亮层/虚拟光标层）、触摸板式指针控制、元素选择器、内置 DevTools
- **真人模式** —— 零 Playwright/CDP 附着的**裸 Chromium**，专过 Cloudflare / Turnstile 挑战页（打开挑战页等 5-15 秒自动过验）
- **指纹档案** —— `真实` / Chrome·Win / Chrome·Mac / Safari·Mac 四套，HTTP UA、JS platform/vendor/userAgentData、`sec-ch-ua` 网络头整套自洽
- **stealth 审计** —— 经典 0-7 号反检测排查清单可运行化（`pw/stealth_audit`、`human/stealth_audit`）
- **人性化输入** —— 鼠标轨迹 + 击键节奏，可用 `HUMANIZE_SEED` 固定随机种子复现
- **剪贴板互通** —— Ctrl+C / Ctrl+V 在本地与远端页面间桥接（粘贴 = 一次性 insertText，复制读远端选中文字）
- **网络一致性** —— 浏览器时区与代理出口时区对齐；UA / `sec-ch-ua` 自洽
- **真 Google Chrome 引擎** —— `BROWSER_ENGINE=chrome` 启用，brands + Widevine 原生自洽（一次解决 CfT 二进制的两大指纹硬伤）
- **GPU 直通** —— WebGL 渲染器探测，有 GPU 默认直通（消除 SwiftShader 软渲染特征）
- **声音串流** —— `/audio.mp3` 实时听取浏览器内声音（PulseAudio null-sink → parec → ffmpeg）
- **AI 任务** —— browser-use 架构 agent：DOM 提炼为编号元素表（`[3] <button>登录</button>`），LLM 按编号输出动作并根据执行结果自我纠错；兼容任意 OpenAI 风格接口（内置 DeepSeek 预设），视觉模式可为多模态模型逐步附截图
- **环境变量自动登录** —— `pw/auto_login` 按 `BROWSER_LOGIN_*` 环境变量填写登录表单，凭据不落代码
- **Node.js 作业脚本** —— `pw/run_script` 跑 `connectOverCDP` 脚本，直连同一浏览器共享登录态
- **模块化代码** —— `main.py` + `cd_*.py` 拆分，每文件 ≤600 行

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 默认 0.0.0.0:9223
```

或使用 Docker（镜像由 GitHub Actions 构建）：

```bash
docker run -d --name chrome-driverless \
  -p 9223:9223 \
  -v chrome-data:/app/data \
  ghcr.io/friddle/chrome-driverless:latest

# 用真 Google Chrome 引擎替代 CfT chromium：
docker run -d -p 9223:9223 -v chrome-data:/app/data \
  -e BROWSER_ENGINE=chrome \
  ghcr.io/friddle/chrome-driverless:latest
```

打开 `http://localhost:9223/` 进入控制台。

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BROWSER_DATA_DIR` | 持久化数据目录（profiles / auth.json / 浏览器 profile） | `./data` |
| `PROFILE_NAME` | 启动时激活的 profile | `debug` |
| `BROWSER_ENGINE` | `chromium`（Playwright CfT）或 `chrome`（真 Google Chrome） | `chromium` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 浏览器代理（留空 = 不启用代理） | 空 |
| `NO_PROXY` | 直连白名单（逗号分隔） | `localhost,127.0.0.1` |
| `REMOTE_DEBUG_PORT` | Chrome CDP 调试端口（供外部脚本 connectOverCDP） | `9222` |
| `EXTERNAL_URL` | 外部访问地址（仅日志 / `/debug/url` 展示） | 空 |
| `AI_MODEL` / `AI_BASE_URL` | AI 任务执行的默认模型与接口地址 | 空 |
| `BROWSER_LOGIN_URL` / `BROWSER_LOGIN_USERNAME` / `BROWSER_LOGIN_PASSWORD` | `pw/auto_login` 的登录凭据 | 空 |
| `HUMANIZE_SEED` | 固定人性化输入随机种子（可复现） | 随机 |

> 代理默认**留空**，需要时通过环境变量注入，避免把固定地址写进代码。

## HTTP 接口

- `GET /` —— Web 控制台
- `GET /health`
- `POST /mcp` —— MCP 方法调用，body `{"method":"pw/...","params":{...}}`
- `GET /debug/status`、`/debug/logs`、`/debug/files`、`/debug/url` —— 运行状态 / 日志 / 产物 / 地址
- `GET /audio.mp3` —— 浏览器声音实时流
- `GET /devtools/targets`、`GET /devtools/{rest}`（含 WS 桥）—— Chrome DevTools 调试入口

### MCP 方法

| 方法 | 说明 |
| --- | --- |
| `pw/init_browser` | 初始化 / 复用持久化浏览器 |
| `pw/navigate`, `pw/back`, `pw/reload` | 导航 / 回退 / 刷新（返回截图） |
| `pw/screenshot` | 当前页截图（base64） |
| `pw/click`, `pw/hover`, `pw/type`, `pw/key`, `pw/clear` | 人性化点击 / 悬停 / 输入（`instant` = 粘贴语义）/ 按键 / 清空 |
| `pw/clip_read`, `human/clip_read` | 读远端页面选中的文字（剪贴板互通的复制侧） |
| `pw/evaluate` | 在当前页执行 JS |
| `pw/elements` | 列出可交互元素（id / selector / 坐标） |
| `pw/auto_login` | 环境变量驱动的表单登录（含二维码→密码 Tab 切换） |
| `pw/tabs`, `pw/tab_select`, `pw/tab_close`, `pw/tab_close_all`, `pw/tab_tag`, `pw/new_tab` | 浏览器多 Tab 管理 |
| `pw/profile_list`, `pw/profile_set` | 多 Profile 隔离登录态 |
| `pw/save_auth` | 将当前登录态导出为 auth.json |
| `pw/set_proxy` | 开关浏览器代理（时区对齐出口） |
| `pw/mouse_move`, `pw/mouse_down`, `pw/mouse_up`, `pw/scroll_at` | 原始鼠标 / 滚动控制 |
| `pw/run_script`, `pw/run_script_content` | 跑 Node.js 脚本（connectOverCDP 同浏览器共享登录态） |
| `pw/ai_task`, `pw/ask_deepseek` | 大模型按步完成页面任务 |
| `pw/stealth_audit` | Playwright 页面的 stealth 审计 |
| `human/status`, `human/set_mode`, `human/gpu_set` | 真人模式 / GPU 直通切换 |
| `human/screenshot`, `human/navigate`, `human/reload`, `human/tabs`, `human/new_tab`, `human/tab_select`, `human/tab_close`, `human/back` | 真人模式页面操作 |
| `human/click`, `human/hover`, `human/type`, `human/key`, `human/clear` | 真人模式输入 |
| `human/mouse_move`, `human/mouse_down`, `human/mouse_up`, `human/scroll_at` | 真人模式指针控制 |
| `human/stealth_audit` | 真人模式内 stealth 审计 |
| `fp/set` | 指纹档案：`real` \| `chrome_win` \| `chrome_mac` \| `safari_mac` |
| `ai/config_get`, `ai/config_set`, `ai/test`, `ai/cancel` | AI 任务配置 / 测试 / 取消 |

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | MCP 分发 + 启动事件 |
| `cd_app.py` / `cd_config.py` / `cd_state.py` | FastAPI app · 环境配置与指纹档案 · 全局状态与持久化 |
| `cd_routes.py` | HTTP/WS 路由、debug 端点、DevTools 反代 |
| `cd_browser.py` | 持久化 Playwright context、Tab、Profile |
| `cd_actions.py` | 页面动作、人性化输入、自动登录 |
| `cd_human.py` / `cd_human_ops.py` | 真人模式生命周期 · 真人模式页面操作 |
| `cd_audit.py` / `cd_stealth.py` | stealth 审计清单 · stealth JS 注入 |
| `cd_cdp.py` | 原始 CDP 辅助、GPU / WebGL 探测 |
| `cd_agent.py` | browser-use 架构任务 agent：元素编号动作空间、同源 iframe 递归、逐步日志 |
| `cd_ai.py` | LLM 客户端（messages + 视觉分片）、AI 配置、Node 脚本执行 |
| `cd_audio.py` | PulseAudio → parec → ffmpeg → `/audio.mp3` |

## 嵌入到 Web 主界面

1. 通过 docker / 裸机运行本服务，暴露端口 `9223`。
2. 在 Web 应用中以页面内 Tab / iframe 打开控制台地址。
3. 在控制台里登录目标站点——登录态自动保存到 auth.json，供后续脚本 / CDP 连接复用。

> 本服务自身**无内置鉴权**，定位是内网 / 嵌入式工具。需要访问控制请在前置反向代理上加。

---

## 声明

仅用于个人学习 / 自用自动化。请遵守各网站的服务条款。
