# chrome-driverless

基于 **Playwright** 驱动的一个有头（headed + xvfb）持久化 Chrome，对外提供一套 **MCP 风格 HTTP 接口**（`/mcp`），用于：导航、截图、点击、输入、多 Tab、多 Profile、代理切换、鼠标/触控板控制、AI 任务执行、浏览器自愈、登录态导出（cookies / auth.json）。

浏览器为**持久化 context**，登录态保存在 `data/profiles/<name>/auth.json`（也可由环境变量 `BROWSER_DATA_DIR` 覆盖）。可嵌入到任意 Web 应用主界面，以页面内 Tab 形式打开本控制台使用。

---

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 默认 0.0.0.0:9223
```

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BROWSER_DATA_DIR` | 持久化数据目录（profiles / auth.json / 浏览器 profile） | `./data` |
| `PROFILE_NAME` | 启动时激活的 profile | `debug` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 浏览器代理（可空；留空则不启用代理） | 空 |
| `NO_PROXY` | 直连白名单（逗号分隔） | `localhost,127.0.0.1` |
| `REMOTE_DEBUG_PORT` | Chrome CDP 调试端口（供外部脚本 connectOverCDP） | `9222` |
| `EXTERNAL_URL` | 外部访问地址（仅日志 / `/debug/url` 展示） | 空 |
| `AI_MODEL` / `AI_BASE_URL` | AI 任务执行的默认模型与接口地址 | 空 |

> 代理默认**留空**，需要时通过环境变量注入，避免把固定地址写进代码。

## HTTP 接口

- `GET /` —— Web 控制台（内置 tab 页面，暗色界面）
- `GET /health`
- `POST /mcp` —— MCP 方法调用，body `{"method":"pw/...","params":{...}}`
- `GET /debug/status`、`/debug/logs`、`/debug/files`、`/debug/url` —— 运行状态 / 日志 / 产物 / 地址
- `GET /devtools/targets`、`GET /devtools/{rest}` —— Chrome DevTools 调试入口

### 常用 MCP 方法

| 方法 | 说明 |
| --- | --- |
| `pw/init_browser` | 初始化 / 复用持久化浏览器 |
| `pw/navigate`, `pw/back`, `pw/reload` | 导航 / 回退 / 刷新（返回截图） |
| `pw/screenshot` | 当前页截图（base64） |
| `pw/click`, `pw/type`, `pw/key`, `pw/clear` | 点击 / 输入 / 按键 / 清空输入 |
| `pw/evaluate` | 在当前页执行 JS |
| `pw/elements` | 列出可交互元素（id / selector / 坐标） |
| `pw/tabs`, `pw/tab_select`, `pw/tab_close`, `pw/tab_close_all` | 浏览器多 Tab 管理 |
| `pw/new_tab` | 打开新 Tab |
| `pw/profile_list`, `pw/profile_set` | 多 Profile 隔离登录态 |
| `pw/save_auth` | 将当前登录态导出为 auth.json |
| `pw/set_proxy` | 开关浏览器代理 |
| `pw/mouse_move`, `pw/mouse_down`, `pw/mouse_up` | 鼠标 / 触控板控制 |
| `pw/scroll_at` | 在指定坐标进行滚动 |
| `pw/ai_task` | 交给大模型按步完成页面任务 |
| `ai/config_get`, `ai/config_set`, `ai/test`, `ai/cancel` | AI 任务配置 / 测试 / 取消 |

## 嵌入到 Web 主界面

1. 通过 docker / docker-compose 运行本服务，暴露端口 `9223`。
2. 在 Web 应用前端设置 `CHROME_DRIVERLESS_URL=http://<host>:9223`。
3. 在前端加一个入口（如导航栏的"内置浏览器"），以页面内 Tab / iframe 形式打开本控制台地址。
4. 在浏览器中登录目标站点，登录态自动保存（auth.json），供后续脚本 / CDP 连接复用。

> 本服务自身**无外部登录**，作为内网 / 嵌入式工具直接使用。若需访问控制，请在前置反向代理上加。

---

## 声明

仅用于个人学习 / 自用自动化。请遵守各网站的服务条款。