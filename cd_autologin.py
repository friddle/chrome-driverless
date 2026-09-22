"""环境变量驱动的自动登录（pw/auto_login）：凭据来自环境变量，不落代码/仓库。"""
import asyncio
import base64
import os

from cd_actions import _page_for


# ---------------------------------------------------------------------------
# 环境变量驱动的自动登录（pw/auto_login）：凭据来自环境变量，不落代码/仓库
# ---------------------------------------------------------------------------
async def _fill_username(page, username):
    """Find the username input and fill it (semantic selectors first)."""
    candidates = [
        "input[autocomplete='username']",
        "input[type='text'][name*='user' i], input[type='text'][id*='user' i]",
        "input[placeholder*='账号' i], input[placeholder*='帐号' i], input[placeholder*='手机' i], input[placeholder*='邮箱' i], input[placeholder*='用户名' i], input[placeholder*='user' i], input[placeholder*='account' i], input[placeholder*='phone' i], input[placeholder*='login' i]",
        "input:not([type='password']):not([type='submit']):not([type='hidden'])",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                await loc.fill(str(username))
                return True
        except Exception:
            continue
    return False


async def _fill_password(page, password):
    try:
        loc = page.locator("input[type='password']").first
        if await loc.count() and await loc.is_visible():
            await loc.click(timeout=5000)
            await loc.fill(str(password))
            return True
    except Exception:
        pass
    return False


async def _click_login_button(page):
    """Click the login/submit button (common zh/en labels first)."""
    candidates = [
        "button[type='submit'], input[type='submit']",
        "button:has-text('登录'), button:has-text('登入'), button:has-text('立即登录'), button:has-text('授权并登录'), button:has-text('Sign in'), button:has-text('Log in'), button:has-text('Login')",
        "button[type='button']:has-text('登录')",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


async def pw_auto_login(params):
    """
    Env-driven login: pw/auto_login {url?}
    Credentials come from environment variables:
      BROWSER_LOGIN_URL, BROWSER_LOGIN_USERNAME, BROWSER_LOGIN_PASSWORD
    Navigates to the login page, fills the form automatically, clicks the
    login button and returns a screenshot.
    """
    username = os.environ.get("BROWSER_LOGIN_USERNAME", "").strip()
    password = os.environ.get("BROWSER_LOGIN_PASSWORD", "")
    target_url = (params.get("url") or os.environ.get("BROWSER_LOGIN_URL", "")).strip()
    if not username or not password:
        return {"error": {"code": -2, "message": "BROWSER_LOGIN_USERNAME / BROWSER_LOGIN_PASSWORD not configured"}}
    if not target_url:
        return {"error": {"code": -2, "message": "login url not set (params.url or BROWSER_LOGIN_URL)"}}

    page = await _page_for(params)
    if not page:
        return {"error": {"code": -1, "message": "Browser init failed (or index out of range)"}}
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        # Switch from QR mode to username/password login if present.
        for tab in ["text=账号密码登录", "text=密码登录", "text=Account login", "text=账号登录", "text=帐号密码登录"]:
            try:
                el = page.get_by_text(tab, exact=False).first
                if await el.count() and await el.is_visible():
                    await el.click(timeout=3000)
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue
        ok_pass = await _fill_password(page, password)
        ok_user = await _fill_username(page, username)
        await asyncio.sleep(0.5)
        await _click_login_button(page)
        await asyncio.sleep(3)
        shot = await page.screenshot(type="png")
        return {
            "result": {
                "status": "auto_login_attempted",
                "url": page.url,
                "user_filled": ok_user,
                "password_filled": ok_pass,
                "image": base64.b64encode(shot).decode("utf-8"),
            }
        }
    except Exception as e:
        return {"error": {"code": -1, "message": f"auto_login failed: {e}"}}
