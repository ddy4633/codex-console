"""
Grok/xAI 浏览器注册流程
"""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
import string
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, Optional

from ...config.constants import DEFAULT_PASSWORD_LENGTH, EmailServiceType, PASSWORD_CHARSET, XAI_OTP_CODE_PATTERN
from ...database import crud
from ...database.session import get_db
from ...services.base import BaseEmailService

logger = logging.getLogger(__name__)

TURNSTILE_PATCH_DIR = Path(__file__).with_name("turnstile_patch")
GROK_SUCCESS_PREFIXES = (
    "https://accounts.x.ai/account",
    "https://grok.com",
    "https://console.x.ai",
    "https://x.ai/",
)


def _require_drissionpage():
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError as exc:  # pragma: no cover - 依赖检查
        raise RuntimeError("缺少 DrissionPage 依赖，请重新安装依赖并重建镜像") from exc
    return Chromium, ChromiumOptions


def _parse_cookie_header(cookie_header: str) -> list[dict]:
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        return []

    jar = SimpleCookie()
    jar.load(cookie_header)

    cookies = []
    for name, morsel in jar.items():
        value = morsel.value.strip()
        if not value:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".x.ai",
                "path": "/",
                "secure": True,
            }
        )
    return cookies


def _create_proxy_extension(host: str, port: str, user: str, password: str) -> str:
    manifest = json.dumps(
        {
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Proxy Auth",
            "permissions": [
                "proxy",
                "tabs",
                "unlimitedStorage",
                "storage",
                "<all_urls>",
                "webRequest",
                "webRequestBlocking",
            ],
            "background": {"scripts": ["background.js"]},
            "minimum_chrome_version": "22.0.0",
        }
    )

    background = Template(
        """
var config = {
    mode: "fixed_servers",
    rules: {
        singleProxy: { scheme: "http", host: "${host}", port: parseInt(${port}) },
        bypassList: ["localhost"]
    }
};
chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});
chrome.webRequest.onAuthRequired.addListener(
    function(details) {
        return { authCredentials: { username: "${user}", password: "${password}" } };
    },
    {urls: ["<all_urls>"]},
    ["blocking"]
);
"""
    ).substitute(host=host, port=port, user=user, password=password)

    plugin_dir = Path(tempfile.mkdtemp(prefix="grok_proxy_plugin_"))
    (plugin_dir / "manifest.json").write_text(manifest, encoding="utf-8")
    (plugin_dir / "background.js").write_text(background, encoding="utf-8")
    return str(plugin_dir)


def _launch_browser(
    *,
    proxy_url: str = "",
    user_data_dir: str = "",
    user_agent: str = "",
):
    Chromium, ChromiumOptions = _require_drissionpage()

    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=2)
    if TURNSTILE_PATCH_DIR.exists():
        options.add_extension(str(TURNSTILE_PATCH_DIR))
    options.set_argument("--disable-blink-features=AutomationControlled")
    options.set_argument("--no-first-run")
    options.set_argument("--disable-dev-shm-usage")
    options.set_argument("--disable-gpu")
    options.set_argument("--no-sandbox")

    if user_agent.strip():
        options.set_user_agent(user_agent.strip())
    if user_data_dir.strip():
        options.set_argument("--user-data-dir", str(Path(user_data_dir.strip()).expanduser()))

    if proxy_url.strip():
        options.set_proxy(proxy_url.strip())
    else:
        proxy_host = os.environ.get("PROXY_HOST", "").strip()
        proxy_port = os.environ.get("PROXY_PORT", "").strip()
        proxy_user = os.environ.get("PROXY_USER", "").strip()
        proxy_pass = os.environ.get("PROXY_PASS", "").strip()
        if proxy_host and proxy_port:
            options.add_extension(_create_proxy_extension(proxy_host, proxy_port, proxy_user, proxy_pass))

    browser = Chromium(options)
    page = browser.get_tabs()[-1]
    return browser, page


def _apply_bootstrap_cookies(page, *, cookie_header: str, cf_clearance: str, cf_bm: str) -> int:
    cookies = []
    cookies.extend(_parse_cookie_header(cookie_header))

    if cf_clearance.strip():
        cookies.append(
            {
                "name": "cf_clearance",
                "value": cf_clearance.strip(),
                "domain": ".x.ai",
                "path": "/",
                "secure": True,
            }
        )
    if cf_bm.strip():
        cookies.append(
            {
                "name": "__cf_bm",
                "value": cf_bm.strip(),
                "domain": ".x.ai",
                "path": "/",
                "secure": True,
            }
        )
    if cookies:
        page.set.cookies(cookies)
    return len(cookies)


def _is_cloudflare_blocked(page) -> bool:
    try:
        title = str(page.title or "").lower()
    except Exception:
        title = ""
    try:
        body = str(page.ele("tag:body", timeout=2).text or "").lower()
    except Exception:
        body = ""

    return (
        "attention required" in title
        or "you have been blocked" in body
        or ("cloudflare ray id" in body and "unable to access" in body)
    )


def _get_turnstile_token(page) -> Optional[str]:
    try:
        page.run_js("try { turnstile.reset() } catch(e) { }")
    except Exception:
        pass

    token = None
    for _ in range(5):
        try:
            token = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if token:
                return token

            challenge_solution = page.ele("@name=cf-turnstile-response")
            challenge_wrapper = challenge_solution.parent()
            challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
            challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_iframe_body.ele("tag:input")
            challenge_button.click()
        except Exception:
            pass
        time.sleep(1)
    return token


def _extract_tokens(page) -> dict:
    tokens = {"sso": "", "sso-rw": "", "all_cookies": {}}
    try:
        current_url = page.url
        if not current_url.startswith("https://grok.com"):
            page.get("https://grok.com")
            time.sleep(3)

        try:
            cdp_cookies = page.run_cdp("Network.getAllCookies")
            cookie_list = cdp_cookies.get("cookies", [])
            for cookie in cookie_list:
                name = cookie.get("name", "")
                value = cookie.get("value", "")
                domain = cookie.get("domain", "")
                if any(part in domain for part in [".x.ai", "x.ai", ".grok.com", "grok.com"]):
                    tokens["all_cookies"][f"{domain}|{name}"] = value
                    if name == "sso":
                        tokens["sso"] = value
                    elif name == "sso-rw":
                        tokens["sso-rw"] = value
        except Exception:
            pass

        if not tokens["sso-rw"]:
            try:
                for cookie in page.cookies():
                    name = cookie.get("name", "")
                    value = cookie.get("value", "")
                    domain = cookie.get("domain", "")
                    if any(part in domain for part in [".x.ai", "x.ai", ".grok.com", "grok.com"]):
                        tokens["all_cookies"][f"{domain}|{name}"] = value
                    if name == "sso":
                        tokens["sso"] = value
                    elif name == "sso-rw":
                        tokens["sso-rw"] = value
            except Exception:
                pass
    except Exception as exc:
        logger.warning("提取 Grok token 失败: %s", exc)

    return tokens


@dataclass
class GrokRegistrationResult:
    success: bool
    email: str = ""
    password: str = ""
    name: str = ""
    sso_token: str = ""
    sso_rw_token: str = ""
    cookies: dict = field(default_factory=dict)
    error_message: str = ""
    logs: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "name": self.name,
            "sso_token": self.sso_token[:20] + "..." if self.sso_token else "",
            "sso_rw_token": self.sso_rw_token[:20] + "..." if self.sso_rw_token else "",
            "error_message": self.error_message,
            "logs": self.logs,
            "metadata": self.metadata,
        }


class GrokRegistrationEngine:
    def __init__(
        self,
        email_service: BaseEmailService,
        *,
        proxy_url: Optional[str] = None,
        password: Optional[str] = None,
        user_data_dir: str = "",
        cf_clearance: str = "",
        cf_bm: str = "",
        cf_cookie_header: str = "",
        user_agent: str = "",
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url or ""
        self.password = password or self._generate_password()
        self.user_data_dir = user_data_dir
        self.cf_clearance = cf_clearance
        self.cf_bm = cf_bm
        self.cf_cookie_header = cf_cookie_header
        self.user_agent = user_agent
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.logs: list[str] = []

        self.email: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None

    def _log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.logs.append(log_message)
        self.callback_logger(log_message)

        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        return "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _create_email(self) -> bool:
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱")
            self.email_info = self.email_service.create_email()
            self.email = self.email_info["email"]
            self._log(f"邮箱已创建: {self.email}")
            return True
        except Exception as exc:
            self._log(f"创建邮箱失败: {exc}", "error")
            return False

    def _wait_for_otp(self) -> str:
        self._log("等待 Grok 邮箱验证码")
        code = self.email_service.get_verification_code(
            self.email,
            self.email_info.get("service_id") if self.email_info else None,
            timeout=180,
            pattern=XAI_OTP_CODE_PATTERN,
            otp_sent_at=time.time(),
        )
        return (code or "").replace("-", "")

    def _persist_account(self, result: GrokRegistrationResult):
        cookies_json = json.dumps(result.cookies or {}, ensure_ascii=False)
        extra_data = {
            "provider": "grok",
            "display_name": result.name,
            "sso": result.sso_token,
            "sso_rw": result.sso_rw_token,
            "all_cookies": result.cookies,
            **(result.metadata or {}),
        }

        with get_db() as db:
            existing = crud.get_account_by_email(db, result.email)
            if existing:
                crud.update_account(
                    db,
                    existing.id,
                    password=result.password,
                    email_service="grok",
                    email_service_id=str(self.email_info.get("service_id") if self.email_info else ""),
                    session_token=result.sso_rw_token,
                    cookies=cookies_json,
                    extra_data=extra_data,
                    status="active",
                    source="grok_register",
                    proxy_used=self.proxy_url or None,
                    expires_at=None,
                )
                return

            crud.create_account(
                db,
                email=result.email,
                password=result.password,
                email_service="grok",
                email_service_id=str(self.email_info.get("service_id") if self.email_info else ""),
                session_token=result.sso_rw_token,
                access_token=result.sso_token,
                proxy_used=self.proxy_url or None,
                extra_data=extra_data,
                status="active",
                source="grok_register",
            )
            account = crud.get_account_by_email(db, result.email)
            if account:
                crud.update_account(db, account.id, cookies=cookies_json)

    def run(self) -> GrokRegistrationResult:
        result = GrokRegistrationResult(success=False, password=self.password, logs=self.logs)
        browser = None

        try:
            self._log("=" * 60)
            self._log("Grok 注册流程启动")

            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            browser, page = _launch_browser(
                proxy_url=self.proxy_url,
                user_data_dir=self.user_data_dir,
                user_agent=self.user_agent,
            )

            page.get("https://accounts.x.ai/")
            time.sleep(1)
            cookie_count = _apply_bootstrap_cookies(
                page,
                cookie_header=self.cf_cookie_header,
                cf_clearance=self.cf_clearance,
                cf_bm=self.cf_bm,
            )
            if cookie_count:
                self._log(f"已注入 {cookie_count} 个 Cloudflare 预置 Cookie")

            page.get("https://accounts.x.ai/sign-up")
            time.sleep(2)

            if _is_cloudflare_blocked(page):
                result.error_message = "当前出口被 Cloudflare 拦截，或 clearance 已失效"
                self._log(result.error_message, "error")
                return result

            page.run_js(
                """
                const btn = Array.from(document.querySelectorAll('button')).find(
                    b => b.textContent.includes('邮箱') || b.textContent.toLowerCase().includes('email')
                );
                if (btn) btn.click();
                else throw new Error('email button not found');
                """
            )
            time.sleep(2)

            page.ele("@data-testid=email").input(self.email)
            time.sleep(0.5)
            submit_btn = page.ele("@data-testid=submit", timeout=2) or page.ele("@type=submit")
            submit_btn.click()
            time.sleep(3)

            otp = self._wait_for_otp()
            if not otp:
                result.error_message = "邮箱验证码超时"
                self._log(result.error_message, "error")
                return result

            self._log(f"验证码已收到: {otp}")
            otp_input = page.ele("@autocomplete=one-time-code", timeout=10) or page.ele("@name=code", timeout=5)
            otp_input.click()
            time.sleep(0.3)
            for char in otp:
                otp_input.input(char)
                time.sleep(0.1)
            time.sleep(0.8)
            page.ele("@type=submit").click()
            time.sleep(3)

            deadline = time.time() + 120
            display_name = ""
            while time.time() < deadline:
                try:
                    url = page.url
                    body_text = page.ele("tag:body").text
                except Exception:
                    time.sleep(1)
                    continue

                self._log(f"当前页面: {url}")

                if any(url.startswith(prefix) for prefix in GROK_SUCCESS_PREFIXES):
                    tokens = _extract_tokens(page)
                    result.success = True
                    result.email = self.email or ""
                    result.name = display_name
                    result.sso_token = tokens.get("sso", "")
                    result.sso_rw_token = tokens.get("sso-rw", "")
                    result.cookies = tokens.get("all_cookies", {})
                    result.metadata = {
                        "current_url": url,
                        "email_service_type": self.email_service.service_type.value,
                    }
                    self._persist_account(result)
                    self._log("Grok 注册成功，账号已入库")
                    return result

                if "accept-tos" in url:
                    page.run_js(
                        """
                        document.querySelectorAll('input[type=checkbox]').forEach(cb => {
                            if (!cb.checked) cb.click();
                        });
                        """
                    )
                    time.sleep(0.5)
                    page.run_js(
                        """
                        document.querySelectorAll('button[role=checkbox]').forEach(cb => {
                            if (cb.getAttribute('aria-checked') !== 'true') cb.click();
                        });
                        """
                    )
                    time.sleep(1)
                    clicked = page.run_js(
                        """
                        const btns = Array.from(document.querySelectorAll('button'));
                        const cont = btns.find(
                            b => b.textContent.toLowerCase().includes('continue')
                              || b.textContent.toLowerCase().includes('agree')
                              || b.textContent.toLowerCase().includes('accept')
                              || b.textContent.trim() === '继续'
                        );
                        if (cont) { cont.click(); return cont.textContent.trim(); }
                        return null;
                        """
                    )
                    self._log(f"已处理 TOS 页面，点击按钮: {clicked or '未找到'}")
                    time.sleep(4)
                    continue

                has_given_name = page.ele("@name=givenName", timeout=1)
                if has_given_name or ("givenName" in page.html):
                    if not display_name:
                        display_name = "".join(random.choices(string.ascii_lowercase, k=6)).capitalize()
                        page.ele("@name=givenName").input(display_name)
                        page.ele("@name=familyName").input(display_name)
                        page.ele("@name=password").input(self.password)
                        time.sleep(0.5)
                        for attempt in range(3):
                            token = _get_turnstile_token(page)
                            self._log(f"Turnstile 第 {attempt + 1} 次尝试: {'成功' if token else '未通过'}")
                            if token:
                                break
                            time.sleep(2)
                        clicked = page.run_js(
                            """
                            const btns = Array.from(document.querySelectorAll('button'));
                            const b = btns.find(btn => btn.type === 'submit' || /continue|next|submit/i.test(btn.textContent));
                            if (b) { b.click(); return b.textContent.trim(); }
                            return null;
                            """
                        )
                        self._log(f"已填写姓名和密码，点击按钮: {clicked or '未找到'}")
                        time.sleep(5)
                        continue
                    time.sleep(2)
                    continue

                if "Welcome" in body_text or "欢迎" in body_text:
                    tokens = _extract_tokens(page)
                    result.success = True
                    result.email = self.email or ""
                    result.name = display_name
                    result.sso_token = tokens.get("sso", "")
                    result.sso_rw_token = tokens.get("sso-rw", "")
                    result.cookies = tokens.get("all_cookies", {})
                    result.metadata = {
                        "current_url": url,
                        "email_service_type": self.email_service.service_type.value,
                    }
                    self._persist_account(result)
                    self._log("Grok 注册成功，欢迎页已出现")
                    return result

                time.sleep(2)

            result.error_message = f"Grok 注册超时，最终页面: {page.url}"
            self._log(result.error_message, "error")
            tokens = _extract_tokens(page)
            result.sso_token = tokens.get("sso", "")
            result.sso_rw_token = tokens.get("sso-rw", "")
            result.cookies = tokens.get("all_cookies", {})
            result.email = self.email or ""
            result.name = display_name
            result.metadata = {
                "current_url": getattr(page, "url", ""),
                "email_service_type": self.email_service.service_type.value,
            }
            return result

        except Exception as exc:
            result.error_message = str(exc)
            self._log(f"Grok 注册异常: {exc}", "error")
            result.email = self.email or ""
            return result
        finally:
            result.logs = self.logs
            if browser:
                try:
                    browser.quit()
                except Exception:
                    pass
