# chrome-driverless（内嵌无头浏览器服务）

基于 **Playwright** 驱动一个有头（headed + xvfb）的持久化 Chrome，对外提供一套 **MCP 风格 HTTP 接口**（`/mcp`），用于：导航、截图、点击、输入、多 Tab、多 Profile、代理切换、登录态导出（cookies / auth.json）。

它被用作本项目（ytb-dl-web）的**内置浏览器 Tab**：在浏览器里登录网易云 / QQ音乐 / Bilibili，登录态自动落盘，下载时被 yt-dlp 复用。

---

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 默认 0.0.0.0:9223
```

浏览器为**持久化 context**，登录态保存在 `data/profiles/<name>/auth.json`（也可由环境变量 `BROWSER_DATA_DIR` 覆盖）。

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BROWSER_DATA_DIR` | 持久化数据目录（profiles / auth.json / 浏览器 profile） | `./data` |
| `PROFILE_NAME` | 启动时激活的 profile | `debug` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 浏览器代理（可空；留空则不启用代理） | 空 |
| `NO_PROXY` | 直连白名单（逗号分隔） | `localhost,127.0.0.1` |
| `REMOTE_DEBUG_PORT` | Chrome CDP 调试端口（供 job 脚本 connectOverCDP） | `9222` |
| `EXTERNAL_URL` | 外部访问地址（仅日志 / `/debug/url` 展示） | 空 |

> 代理默认**留空**，需要代理时通过环境变量注入，避免把固定地址写进代码（已清理历史敏感默认值）。

## HTTP 接口

- `GET /` —— Web 控制台（内置 tab 页面，支持 **中 / 英** 切换，暗色界面）
- `GET /health`
- `POST /mcp` —— MCP 方法调用，body `{"method":"pw/...","params":{...}}`

### 常用 MCP 方法

| 方法 | 说明 |
| --- | --- |
| `pw/init_browser` | 初始化 / 复用持久化浏览器 |
| `pw/navigate`, `pw/back`, `pw/reload` | 导航 / 回退 / 刷新（返回截图） |
| `pw/screenshot` | 当前页截图（base64） |
| `pw/click`, `pw/type`, `pw/key`, `pw/clear` | 点击 / 输入 / 按键 / 清空输入 |
| `pw/elements` | 列出可交互元素（id / selector / 坐标） |
| `pw/tabs`, `pw/tab_select`, `pw/tab_close`, `pw/tab_close_all` | 浏览器多 Tab 管理 |
| `pw/profile_list`, `pw/profile_set` | 多 Profile 隔离登录态 |
| `pw/save_auth` | 将当前登录态导出为 auth.json |
| `pw/set_proxy` | 开关浏览器代理 |
| `pw/ai_task` | 交给大模型按步完成页面任务 |
| `pw/evaluate` | 在当前页执行 JS |

## 嵌入到 ytb-dl-web 主界面

1. 通过 docker-compose / all-in-one 镜像运行本服务，暴露端口 `9223`。
2. 在 ytb-dl-web 设置 `CHROME_DRIVERLESS_URL=http://<host>:9223`。
3. 打开 ytb-dl-web 主界面，点导航栏 **🛩️ 内置浏览器**，即在页面内以 Tab 形式打开本控制台。
4. 在此浏览器中登录各平台一次，登录态自动保存；下载时被 yt-dlp 复用。

> 本服务自身**无外部 LDAP 登录**（已移除历史硬编码外部认证域名 / 内网地址 / 私有仓库镜像引用），作为内网/嵌入式工具直接使用。若需访问控制，请在前置反向代理上加。

---

## 声明

仅用于个人学习 / 自用自动化。请遵守各网站的服务条款。