# chrome-driverless

[中文文档](README.zh-CN.md)

A **persistent, headed Chrome** driven by Playwright, exposed through an **MCP-style HTTP API** (`POST /mcp`) with a built-in web console. Humans and AI agents share one long-lived browser: log in once through the console, then drive it via HTTP, CDP, Node.js scripts — or let an LLM complete tasks for you.

## Highlights

- **Persistent context** — login state survives restarts in `data/profiles/<name>/auth.json` (exportable for reuse)
- **MCP-style HTTP API** — navigate, screenshot, click, type, keys, hover, evaluate, tabs, profiles, proxy toggle, raw mouse / scroll control
- **Web console** — layered live viewport (screenshot / element-highlight / virtual-cursor layers), touchpad-style pointer control, element picker, built-in DevTools
- **Human mode** — a *bare* Chromium with zero Playwright/CDP attachment, purpose-built for Cloudflare / Turnstile challenge pages (open the challenge, wait 5–15 s, it passes)
- **Fingerprint profiles** — `real` / Chrome·Win / Chrome·Mac / Safari·Mac, each self-consistent across HTTP `User-Agent`, JS `platform`/`vendor`/`userAgentData`, and `sec-ch-ua` headers
- **Stealth audit** — the classic 0–7 anti-detection checklist made runnable in one call (`pw/stealth_audit`, `human/stealth_audit`)
- **Humanized input** — mouse trajectories and keystroke cadence with reproducible seeds (`HUMANIZE_SEED`)
- **Network consistency** — browser timezone aligned with the proxy's egress timezone; UA / `sec-ch-ua` coherence
- **Real Google Chrome engine** — set `BROWSER_ENGINE=chrome` for native brands + Widevine (fixes the two biggest Chromium-for-Testing fingerprint tells)
- **GPU passthrough** — WebGL renderer probing; real GPU by default when available (no SwiftShader software-rendering tell)
- **Audio streaming** — hear the browser at `/audio.mp3` (PulseAudio null-sink → parec → ffmpeg)
- **AI tasks** — any OpenAI-compatible `/chat/completions` endpoint (DeepSeek preset included); the model sees screenshots and drives the browser step by step
- **Env-driven auto login** — `pw/auto_login` fills login forms from `BROWSER_LOGIN_*` env vars, no credentials in code
- **Node.js job scripts** — `pw/run_script` executes scripts that `connectOverCDP` into the *same* browser, sharing login state
- **Modular codebase** — `main.py` + `cd_*.py` modules, each ≤600 lines

## Quick start

```bash
pip install -r requirements.txt
python main.py        # serves 0.0.0.0:9223
```

Or with Docker (images built by GitHub Actions):

```bash
docker run -d --name chrome-driverless \
  -p 9223:9223 \
  -v chrome-data:/app/data \
  ghcr.io/friddle/chrome-driverless:latest

# real Google Chrome engine instead of Chromium-for-Testing:
docker run -d -p 9223:9223 -v chrome-data:/app/data \
  -e BROWSER_ENGINE=chrome \
  ghcr.io/friddle/chrome-driverless:latest
```

Open `http://localhost:9223/` for the console.

## Environment variables

| Variable | Description | Default |
| --- | --- | --- |
| `BROWSER_DATA_DIR` | Persistent data dir (profiles / auth.json / browser profile) | `./data` |
| `PROFILE_NAME` | Profile activated at startup | `debug` |
| `BROWSER_ENGINE` | `chromium` (Playwright CfT) or `chrome` (real Google Chrome) | `chromium` |
| `HTTP_PROXY` / `HTTPS_PROXY` | Browser proxy (empty = no proxy) | empty |
| `NO_PROXY` | Comma-separated no-proxy list | `localhost,127.0.0.1` |
| `REMOTE_DEBUG_PORT` | Chrome CDP port for external `connectOverCDP` scripts | `9222` |
| `EXTERNAL_URL` | External access URL (shown in logs / `/debug/url`) | empty |
| `AI_MODEL` / `AI_BASE_URL` | Default model + endpoint for AI tasks | empty |
| `BROWSER_LOGIN_URL` / `BROWSER_LOGIN_USERNAME` / `BROWSER_LOGIN_PASSWORD` | Credentials for `pw/auto_login` | empty |
| `HUMANIZE_SEED` | Fix the humanized-input RNG seed for reproducibility | random |

> Proxies are intentionally **empty by default** — inject them via env vars instead of hardcoding addresses.

## HTTP endpoints

- `GET /` — web console
- `GET /health`
- `POST /mcp` — MCP method call: `{"method": "pw/...", "params": {...}}`
- `GET /debug/status` · `/debug/logs` · `/debug/files` · `/debug/url`
- `GET /audio.mp3` — live browser audio stream
- `GET /devtools/targets`, `GET /devtools/{rest}` (+ WS bridges) — Chrome DevTools

### MCP methods

| Method | Description |
| --- | --- |
| `pw/init_browser` | Init / reuse the persistent browser |
| `pw/navigate`, `pw/back`, `pw/reload` | Navigation (returns screenshot) |
| `pw/screenshot` | Screenshot of the current page (base64) |
| `pw/click`, `pw/hover`, `pw/type`, `pw/key`, `pw/clear` | Humanized click / hover / typing / key / clear |
| `pw/evaluate` | Run JS in the current page |
| `pw/elements` | List interactive elements (id / selector / coordinates) |
| `pw/auto_login` | Env-driven form login (QR → password tab switch included) |
| `pw/tabs`, `pw/tab_select`, `pw/tab_close`, `pw/tab_close_all`, `pw/tab_tag`, `pw/new_tab` | Multi-tab management |
| `pw/profile_list`, `pw/profile_set` | Profile-isolated login states |
| `pw/save_auth` | Export current login state to auth.json |
| `pw/set_proxy` | Toggle browser proxy (aligns timezone to egress) |
| `pw/mouse_move`, `pw/mouse_down`, `pw/mouse_up`, `pw/scroll_at` | Raw pointer / scroll control |
| `pw/run_script`, `pw/run_script_content` | Run Node.js scripts via `connectOverCDP` (same browser, shared auth) |
| `pw/ai_task`, `pw/ask_deepseek` | LLM-driven task execution |
| `pw/stealth_audit` | Stealth audit on the Playwright-driven page |
| `human/status`, `human/set_mode`, `human/gpu_set` | Human mode / GPU passthrough switching |
| `human/screenshot`, `human/navigate`, `human/reload`, `human/tabs`, `human/new_tab`, `human/tab_select`, `human/tab_close`, `human/back` | Human-mode page ops |
| `human/click`, `human/hover`, `human/type`, `human/key`, `human/clear` | Human-mode input |
| `human/mouse_move`, `human/mouse_down`, `human/mouse_up`, `human/scroll_at` | Human-mode pointer control |
| `human/stealth_audit` | Stealth audit inside human mode |
| `fp/set` | Fingerprint profile: `real` \| `chrome_win` \| `chrome_mac` \| `safari_mac` |
| `ai/config_get`, `ai/config_set`, `ai/test`, `ai/cancel` | AI task configuration |

## Module map

| File | Responsibility |
| --- | --- |
| `main.py` | MCP dispatch + startup events |
| `cd_app.py` / `cd_config.py` / `cd_state.py` | FastAPI app · env config & fingerprint profiles · global state & persistence |
| `cd_routes.py` | HTTP/WS routes, debug endpoints, DevTools proxy |
| `cd_browser.py` | Persistent Playwright context, tabs, profiles |
| `cd_actions.py` | Page actions, humanized input, auto login |
| `cd_human.py` / `cd_human_ops.py` | Human-mode lifecycle · human-mode page operations |
| `cd_audit.py` / `cd_stealth.py` | Stealth audit checklist · stealth JS injection |
| `cd_cdp.py` | Raw CDP helpers, GPU / WebGL probing |
| `cd_ai.py` | LLM integration, AI task loop, Node script runner |
| `cd_audio.py` | PulseAudio → parec → ffmpeg → `/audio.mp3` |

## Embedding into your app

1. Run the service (docker or bare) and expose port `9223`.
2. Open the console URL in an in-app tab / iframe.
3. Log in to target sites in the console — state persists in auth.json for later scripts / CDP connections.

> The service itself has **no built-in auth** — it is meant as an internal / embedded tool. Put an authenticating reverse proxy in front if you need access control.

## Disclaimer

For personal learning and self-hosted automation only. Respect the terms of service of the websites you visit.
