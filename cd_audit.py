"""stealth 审计（移植 invisible_playwright「Playwright detected as a bot」排查清单）。

把 0-7 号排查步骤变成可运行报告：
- 第 1 步 自己的覆盖层接缝：webdriver 应 undefined；抽样函数过 Function.prototype.toString
  源检查（能诚实暴露「WebGL 显示 Intel UHD 630 是 JS 伪造」这类 toString 补丁盖不住的接缝）。
- 第 3 步 机器层：WebGL 软渲染字符串 / screen / DPR / hardwareConcurrency/deviceMemory /
  AudioContext 采样率 / speechSynthesis voices / 字体清单。
- 第 4 步 自动化痕迹：window.chrome / plugins / languages。
- 第 6 步 一致性：UA ↔ brands ↔ platform ↔ userAgentData 交叉验证 + Widevine 能力探测
  （CfT 缺 Google Chrome brand、无 DRM 在此现形）。
- 第 0/7 步 网络层（Python 侧）：直连/代理双出口 IP + ip-api 的 ASN/isp/proxy/hosting 标记
  + 浏览器时区 vs 出口时区 vs TZ 环境变量三对齐。

双模式：pw 走 page.evaluate；human 走一次性 Runtime.evaluate（awaitPromise，不在挑战 tab 常驻
——审计前把活动 tab 切到 about:blank，别对挑战页跑）。
附赠 A/B 开关：PW_STEALTH_JS=0 不注入自己的覆盖层（bisect 用），审计报告会如实标注。
"""
import asyncio, json, os
import urllib.request, urllib.error

import cd_config as C
import cd_state as S
from cd_actions import MOTION_SEED
from cd_browser import _page_for
from cd_state import _add_log

# 页面侧审计脚本（async IIFE，pw evaluate 与 Runtime.evaluate(awaitPromise) 通用）
AUDIT_SRC = """(async () => {
  const C = [];
  const add = (step, id, ok, detail) => C.push({step, id, ok, detail});
  const S1 = (fn) => { try { return Function.prototype.toString.call(fn); } catch (e) { return ''; } };
  const isNative = (fn) => S1(fn).includes('[native code]');

  // ---- 第 1 步：自己的覆盖层接缝 ----
  let wd = 'absent';
  try { wd = String(navigator.webdriver); } catch (e) { wd = 'err'; }
  // 判据：true=自动化铁证(BAD)。false=真 Chrome 原生默认值；undefined=stealth 补丁效果——两者都非自动化信号
  add(1, 'webdriver', wd === 'false' || wd === 'undefined',
      'navigator.webdriver=' + wd + '（true 才是自动化铁证；false=原生默认/undefined=补丁效果）');
  const seams = [];
  const fnSeams = [
    ['permissions.query', () => navigator.permissions && navigator.permissions.query],
    ['webgl.getParameter', () => window.WebGLRenderingContext && WebGLRenderingContext.prototype.getParameter],
    ['webgl2.getParameter', () => window.WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter],
    ['mediaDevices.enumerateDevices', () => navigator.mediaDevices && navigator.mediaDevices.enumerateDevices],
    ['Function.prototype.toString', () => Function.prototype.toString],
    ['console.log', () => console.log],
  ];
  for (const [label, get] of fnSeams) {
    try { const f = get(); if (typeof f === 'function' && !isNative(f)) seams.push(label); } catch (e) {}
  }
  const gSeams = [
    ['navigator.userAgent', navigator, 'userAgent'],
    ['navigator.platform', navigator, 'platform'],
    ['navigator.vendor', navigator, 'vendor'],
    ['navigator.webdriver', navigator, 'webdriver'],
    ['navigator.languages', navigator, 'languages'],
    ['navigator.plugins', navigator, 'plugins'],
  ];
  for (const [label, obj, key] of gSeams) {
    try {
      const d = Object.getOwnPropertyDescriptor(obj, key);
      if (d && d.get && !isNative(d.get)) seams.push(label + '(getter)');
    } catch (e) {}
  }
  add(1, 'toString-seams', seams.length === 0,
      seams.length ? 'JS 伪造接缝（Function.prototype.toString 源检查不过）: ' + seams.join(', ')
                   : '抽样函数均通过源检查');

  // ---- 第 3 步：机器层 ----
  let gl = '', glv = '';
  try {
    const cv = document.createElement('canvas');
    const g = cv.getContext('webgl') || cv.getContext('experimental-webgl');
    if (g) {
      const d = g.getExtension('WEBGL_debug_renderer_info');
      gl = String(g.getParameter(d.UNMASKED_RENDERER_WEBGL));
      glv = String(g.getParameter(d.UNMASKED_VENDOR_WEBGL));
    }
  } catch (e) { gl = 'err:' + e.message; }
  const soft = /swiftshader|llvmpipe|software|basic render/i.test(gl);
  add(3, 'webgl-renderer', !!gl && !soft,
      'renderer=' + gl + ' vendor=' + glv + (soft ? '（软渲染=虚拟机/无头 tell）' : ''));
  const scr = { w: screen.width, h: screen.height, ah: screen.availHeight, dpr: window.devicePixelRatio };
  add(3, 'screen', scr.w >= 1024 && scr.h >= 600 && scr.dpr >= 1,
      `screen=${scr.w}x${scr.h} availH=${scr.ah} dpr=${scr.dpr}`);
  const hc = navigator.hardwareConcurrency, dm = navigator.deviceMemory;
  add(3, 'hw', !!hc && hc >= 2, `hardwareConcurrency=${hc} deviceMemory=${dm}`);
  let sr = 0;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) { const ac = new AC(); sr = ac.sampleRate; if (ac.close) ac.close(); }
  } catch (e) {}
  add(3, 'audio-sample-rate', sr === 44100 || sr === 48000, 'sampleRate=' + sr);
  let voices = -1;
  try { voices = speechSynthesis.getVoices().length; } catch (e) { voices = 'err'; }
  add(3, 'speech-voices', voices !== 0 && voices !== 'err', 'getVoices().length=' + voices + '（0=headless 常见）');
  const probeFonts = ['Microsoft YaHei', 'SimSun', 'Arial', 'Times New Roman', 'Noto Sans CJK SC', 'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei'];
  let have = [];
  try { have = probeFonts.filter(f => document.fonts.check('12px "' + f + '"')); } catch (e) {}
  add(3, 'fonts', have.length >= 2, '可用: ' + (have.join(', ') || '(无)') + '（容器只装中文字体时英文字体缺失是 tell）');

  // ---- 第 4 步：自动化痕迹 ----
  add(4, 'window.chrome', !!window.chrome,
      'window.chrome=' + (window.chrome ? 'present' : 'absent') + '（Chromium 应 present；Safari 档案应 absent）');
  add(4, 'plugins', navigator.plugins.length > 0, 'plugins.length=' + navigator.plugins.length);
  add(4, 'languages', (navigator.languages || []).length > 0, 'languages=' + (navigator.languages || []).join(','));

  // ---- 第 6 步：一致性 ----
  const ua = navigator.userAgent;
  const uaChrome = (ua.match(/Chrome\\/(\\d+)/) || [])[1] || '';
  const uaSafari = /Version\\/[\\d.]+ Safari/.test(ua) && !ua.includes('Chrome');
  let brands = [], uadPlatform = '(no userAgentData)';
  try {
    if (navigator.userAgentData) {
      uadPlatform = String(navigator.userAgentData.platform);
      brands = navigator.userAgentData.brands.map(b => b.brand + '/' + b.version);
    }
  } catch (e) { uadPlatform = 'err:' + e.message; }
  const hasGoogleBrand = brands.some(b => b.startsWith('Google Chrome'));
  add(6, 'ua-vs-brands', uaSafari ? true : (!!uaChrome && brands.some(b => b.includes('/' + uaChrome))),
      `UA Chrome/${uaChrome || '?'} vs brands=[${brands.join(', ')}]`);
  add(6, 'chrome-brand', uaSafari ? brands.length === 0 : hasGoogleBrand,
      hasGoogleBrand ? 'Google Chrome brand 在' :
      (uaSafari ? 'Safari 档案无 brands，正常' : '缺 Google Chrome brand（CfT/Chromium 签名）'));
  const platOk = uadPlatform.startsWith('(') || uadPlatform.startsWith('err') ||
      navigator.platform.toLowerCase().includes(uadPlatform.toLowerCase()) ||
      uadPlatform.toLowerCase().includes(navigator.platform.toLowerCase());
  add(6, 'platform-coherence', platOk, `platform=${navigator.platform} uad.platform=${uadPlatform}`);
  // Widevine：能力探测不是字符串——UA 说 Chrome 但答 no = 开发者/自动化人群
  let wv = 'err';
  try {
    await navigator.requestMediaKeySystemAccess('com.widevine.alpha',
      [{ initDataTypes: ['cenc'], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"' }] }]);
    wv = 'yes';
  } catch (e) { wv = 'no'; }
  add(6, 'widevine', !uaChrome || uaSafari || wv === 'yes',
      `widevine=${wv}` + (uaChrome && !uaSafari && wv === 'no' ? '（UA 说 Chrome 但无 DRM）' : ''));
  let tz = '';
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}

  return { checks: C, tz, gl, glv, ua, platform: navigator.platform, brands,
           hw: { hardwareConcurrency: hc, deviceMemory: dm } };
})()"""


def _fetch_ipapi(proxy_server=None):
    """经指定代理（None=直连）查 ip-api：出口 IP + ASN/isp/proxy/hosting/时区。"""
    handlers = {"http": proxy_server, "https": proxy_server} if proxy_server else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(handlers))
    with opener.open("http://ip-api.com/json/?fields=query,timezone,as,isp,proxy,hosting", timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


async def _egress_timezone(proxy_server=None):
    """pw_set_proxy 用：经代理反查出口 IP 的时区（失败返回 None，不阻塞代理切换）。"""
    def _do():
        d = _fetch_ipapi(proxy_server)
        return d.get("timezone")
    try:
        return await asyncio.to_thread(_do)
    except Exception:
        return None


async def _network_audit():
    """第 0/7 步（网络层，Python 侧）：双出口 IP + 信誉标记 + 时区三对齐。"""
    out = {"direct_ip": None, "proxy_ip": None, "ipapi": None, "tz_browser": None,
           "tz_env": os.environ.get("TZ"), "tz_egress": None,
           "proxy_in_use": bool(S.use_proxy and (C.HTTPS_PROXY or C.HTTP_PROXY))}
    proxy_server = C.HTTPS_PROXY if S.use_proxy else None
    try:
        d = await asyncio.to_thread(_fetch_ipapi, None)
        out["direct_ip"] = d.get("query")
        if not out["proxy_in_use"]:
            out["ipapi"] = {k: d.get(k) for k in ("timezone", "as", "isp", "proxy", "hosting")}
            out["tz_egress"] = d.get("timezone")
    except Exception as e:
        out["direct_ip"] = f"err: {e}"
    if proxy_server:
        try:
            d = await asyncio.to_thread(_fetch_ipapi, proxy_server)
            out["proxy_ip"] = d.get("query")
            out["ipapi"] = {k: d.get(k) for k in ("timezone", "as", "isp", "proxy", "hosting")}
            out["tz_egress"] = d.get("timezone")
        except Exception as e:
            out["proxy_ip"] = f"err: {e}"
    marks = out.get("ipapi") or {}
    warns = []
    if marks.get("proxy") or marks.get("hosting"):
        warns.append(f"出口被标记 proxy={marks.get('proxy')} hosting={marks.get('hosting')} as={marks.get('as')}（数据中心出口=弹 Captcha 最大嫌疑）")
    return out, warns


def _summarize(checks, net, net_warns):
    bad = [c for c in checks if not c["ok"]]
    tz_mismatch = None
    tzset = {net.get("tz_browser"), net.get("tz_egress"), net.get("tz_env")}
    tzset.discard(None)
    if len(tzset) > 1:
        tz_mismatch = f"时区不一致: browser={net.get('tz_browser')} egress={net.get('tz_egress')} TZ={net.get('tz_env')}"
    return {"bad": len(bad) + len(net_warns) + (1 if tz_mismatch else 0),
            "bad_items": [c["id"] for c in bad] + net_warns + ([tz_mismatch] if tz_mismatch else []),
            "tz_mismatch": tz_mismatch}


async def pw_stealth_audit(params=None):
    """普通模式审计：pw/stealth_audit {index?}。在目标页跑页面侧清单 + Python 侧网络层。"""
    params = params or {}
    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed"}}
    try:
        js = await page.evaluate(AUDIT_SRC)
    except Exception as e:
        return {"error": {"code": -1, "message": f"audit evaluate failed: {e}"}}
    net, net_warns = await _network_audit()
    net["tz_browser"] = js.get("tz")
    summary = _summarize(js.get("checks", []), net, net_warns)
    return {"result": {"mode": "pw", "url": page.url,
                       "engine": C.ENGINE, "gpu_mode": S.gpu_mode, "fp_profile": S.FP_PROFILE,
                       "motion_seed": MOTION_SEED,
                       "stealth_js_enabled": os.environ.get("PW_STEALTH_JS", "1") != "0",
                       "checks": js.get("checks", []), "network": net, "net_warns": net_warns,
                       "summary": summary}}


async def human_stealth_audit(params=None):
    """真人模式审计：human/stealth_audit {index?}。一次性 Runtime.evaluate（awaitPromise）。
    注意：会短暂在该 tab 附着会话并求值——先切到 about:blank 再跑，别对挑战页审计。"""
    params = params or {}
    from cd_cdp import _cdp_rpc, _human_targets_refresh, _human_ws  # 局部导入避免环
    await _human_targets_refresh()
    ws, t = _human_ws(params.get("index"))
    r = await _cdp_rpc(ws, "Runtime.evaluate",
                       {"expression": AUDIT_SRC, "returnByValue": True,
                        "awaitPromise": True, "timeout": 15000})
    js = (r.get("result") or {}).get("value")
    if not js:
        return {"error": {"code": -1, "message": "audit evaluate returned no value"}}
    net, net_warns = await _network_audit()
    net["tz_browser"] = js.get("tz")
    summary = _summarize(js.get("checks", []), net, net_warns)
    return {"result": {"mode": "human", "url": t.get("url", ""),
                       "engine": C.ENGINE, "gpu_mode": S.gpu_mode, "fp_profile": S.FP_PROFILE,
                       "motion_seed": MOTION_SEED,
                       "stealth_js_enabled": os.environ.get("PW_STEALTH_JS", "1") != "0",
                       "checks": js.get("checks", []), "network": net, "net_warns": net_warns,
                       "summary": summary}}
