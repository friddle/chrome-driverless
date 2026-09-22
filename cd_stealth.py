"""反自动化 stealth 注入脚本 + 指纹档案 JS 覆盖层（仅普通 Playwright 模式注入）。"""
import asyncio, base64, json, math, os, random, re, shutil, subprocess
import urllib.request, urllib.error, urllib.parse
from collections import deque
from datetime import datetime

import cd_state as S
from cd_config import FP_PROFILES
from cd_state import _add_log

# 反自动化检测（阿里云/Google 会检测 headless 指纹）。
# 覆盖社区 stealth 补丁：webdriver/plugins/chrome.runtime/permissions/UA/设备枚举/toString 补丁。
STEALTH_JS = r"""
(() => {
  // 0) WebAuthn 降级：让通行密钥请求立即失败（NotAllowedError），
  //    Google 等站点检测到“此设备无可用通行密钥”后会自动回落到密码登录 UI，
  //    否则 navigator.credentials.get 永久 pending，页面“试试其他方式”等控件全部假死。
  try {
    if (window.PublicKeyCredential) {
      const reject = () => Promise.reject(new DOMException('NotAllowedError', 'NotAllowedError'));
      const cc = navigator.credentials;
      if (cc) {
        cc.get = reject; cc.create = reject; cc.store = reject;
        Object.defineProperty(Navigator.prototype, 'credentials', {
          get: () => ({ get: reject, create: reject, store: reject, preventSilentAccess: () => Promise.resolve() }),
          configurable: true
        });
        Object.defineProperty(navigator, 'credentials', {
          get: () => ({ get: reject, create: reject, store: reject, preventSilentAccess: () => Promise.resolve() }),
          configurable: true
        });
      }
    }
  } catch(e) {}
  // 0b) WebGL 渲染器伪装：--use-gl=swiftshader 会让 getParameter 返回
  //     "SwiftShader"（软件渲染=虚拟机/无头标志），Google 验证码重点读取此项。
  try {
    const GPU_VENDOR = 'Intel';
    const GPU_RENDERER = 'Intel(R) UHD Graphics 630';
    const patchGL = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        try {
          if (p === 37445) return GPU_VENDOR;             // UNMASKED_VENDOR_WEBGL
          if (p === 37446) return GPU_RENDERER;           // UNMASKED_RENDERER_WEBGL
        } catch (e) {}
        return orig.call(this, p);
      };
      proto.getParameter.toString = () => 'function getParameter() { [native code] }';
    };
    if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
  } catch(e) {}
  // 1) 隐藏 webdriver（含 toString 打补丁，防 navigator.webdriver 探测）
  try {
    const wdDesc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    const fakeWd = Object.getOwnPropertyDescriptor(navigator, 'webdriver');
    if (fakeWd && fakeWd.get) {
      fakeWd.get.toString = () => 'function get() { [native code] }';
      fakeWd.get.toString.toString = () => 'function toString() { [native code] }';
    }
  } catch(e) {}
  // 2) 伪装插件列表（正常 Chrome 有 5 个）
  const plugins = [
    {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', desc:'Portable Document Format'},
    {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', desc:''},
    {name:'Native Client', filename:'internal-nacl-plugin', desc:''},
    {name:'Chromium PDF Viewer', filename:'internal-pdf-viewer', desc:'Portable Document Format'},
    {name:'Microsoft Edge PDF Viewer', filename:'internal-pdf-viewer', desc:'Portable Document Format'}
  ];
  if (navigator.plugins.length === 0) {
    const makePlugin = (p) => {
      const obj = { name:p.name, filename:p.filename, description:p.desc, length:1,
        item:()=>null, namedItem:()=>null };
      obj.item.toString = () => 'function item() { [native code] }';
      return obj;
    };
    const arr = plugins.map(makePlugin);
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    Object.defineProperty(navigator, 'plugins', { get: () => arr });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => {
      const mt = [1,2,3,4];
      mt.item = (i) => null; mt.namedItem = () => null;
      return mt;
    }});
  }
  // 3) 伪装 languages
  if (!navigator.languages || navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
  }
  // 4) 完整 chrome 对象（headless 缺 chrome.runtime）
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', { get: () => ({
      runtime: { connect: () => {}, sendMessage: () => {}, id: undefined,
        getManifest: () => ({}) },
      loadTimes: () => ({}), csi: () => ({}), app: {}
    })});
  } else {
    window.chrome.runtime = window.chrome.runtime || { connect: () => {}, sendMessage: () => {}, getManifest: () => ({}) };
  }
  // 5) permissions.query 伪装
  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(params);
      window.navigator.permissions.query.toString = () => 'function query() { [native code] }';
    }
  } catch(e) {}
  // 6) 设备枚举（headless 返回空列表）
  try {
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
        {deviceId:'', kind:'audioinput', label:'', groupId:''},
        {deviceId:'', kind:'videoinput', label:'', groupId:''}
      ]);
    }
  } catch(e) {}
})();
"""



def _fp_init_js(profile):
    """指纹档案的 JS 覆盖层（在 STEALTH_JS 之后注册：后注册的后执行，覆盖生效）。
    把 navigator.userAgent/appVersion/platform/vendor/userAgentData 换成整套自洽值；
    Safari 档案额外：无 userAgentData（Chromium 专有）、无 window.chrome、plugins 为空。"""
    fp = FP_PROFILES.get(profile)
    if not fp or profile == "real":
        return None
    ua = fp["user_agent"].replace("\\", "\\\\").replace("'", "\\'")
    platform = fp["platform"]
    vendor = fp["vendor"]
    safari = "true" if fp.get("safari") else "false"
    meta = json.dumps(fp.get("ua_metadata")) if fp.get("ua_metadata") else "null"
    return r"""(() => {
  // 指纹档案覆盖层（在 STEALTH_JS 之后执行，后写的赢）。仅普通 Playwright 模式注入，
  // 真人模式保持零注入。Emulation 类改动存在可检测面——最大隐身用『真实』档案。
  try {
    const UA = '__UA__';
    const PLATFORM = '__PLATFORM__';
    const VENDOR = '__VENDOR__';
    const SAFARI = __SAFARI__;
    const META = __META__;
    const def = (obj, prop, getter) => {
      Object.defineProperty(obj, prop, { get: getter, configurable: true });
      const d = Object.getOwnPropertyDescriptor(obj, prop);
      if (d && d.get) d.get.toString = () => 'function get() { [native code] }';
    };
    def(navigator, 'userAgent', () => UA);
    def(navigator, 'appVersion', () => UA.replace(/^Mozilla\//, ''));
    def(navigator, 'platform', () => PLATFORM);
    def(navigator, 'vendor', () => VENDOR);
    if (SAFARI) {
      // Safari 没有 userAgentData（Chromium 专有 API）、没有 window.chrome、plugins 为空
      def(navigator, 'userAgentData', () => undefined);
      Object.defineProperty(window, 'chrome', { get: () => undefined, configurable: true });
      def(navigator, 'plugins', () => { const a = []; a.item = () => null; a.namedItem = () => null; a.refresh = () => {}; return a; });
      def(navigator, 'mimeTypes', () => { const a = []; a.item = () => null; a.namedItem = () => null; return a; });
    } else if (META) {
      const uad = {
        brands: META.brands.map(b => ({ brand: b.brand, version: b.version })),
        mobile: META.mobile,
        platform: META.platform,
        getHighEntropyValues: (hints) => Promise.resolve({
          architecture: META.architecture, bitness: META.bitness, model: META.model,
          platformVersion: META.platformVersion, uaFullVersion: META.fullVersion,
          fullVersionList: (META.fullVersionList || META.brands),
          wow64: !!META.wow64
        }),
        toJSON: () => ({ brands: uad.brands, mobile: uad.mobile, platform: uad.platform })
      };
      uad.getHighEntropyValues.toString = () => 'function getHighEntropyValues() { [native code] }';
      uad.toJSON.toString = () => 'function toJSON() { [native code] }';
      def(navigator, 'userAgentData', () => uad);
    }
  } catch (e) {}
})();""".replace('__UA__', ua).replace('__PLATFORM__', platform).replace('__VENDOR__', vendor).replace('__SAFARI__', safari).replace('__META__', meta)


async def _apply_fp_cdp(page):
    """Emulation.setUserAgentOverride（含 userAgentMetadata）：让网络层 sec-ch-ua 系列请求头与
    伪造的 UA 同源（仅靠 JS 补丁盖不住网络头）。Safari 档案不发 Client Hints，无需此步。
    rebrowser 将 Emulation.* 列为可检测命令——这是显式选指纹档案的代价，real 档案不会走到这里。"""
    fp = FP_PROFILES.get(S.FP_PROFILE)
    if not fp or not fp.get("ua_metadata") or not S.pw_context:
        return
    try:
        cdp = await S.pw_context.new_cdp_session(page)
        await cdp.send("Emulation.setUserAgentOverride", {
            "userAgent": fp["user_agent"],
            "acceptLanguage": "zh-CN,zh;q=0.9",
            "platform": fp["platform"],
            "userAgentMetadata": fp["ua_metadata"],
        })
    except Exception as e:
        await _add_log("WARN", f"[FP] CDP UA 覆盖失败: {e}")

