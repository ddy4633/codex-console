"""
Grok/xAI 浏览器注册引擎
基于 DrissionPage + turnstilePatch，融合本地 openaireg 的稳定注册链路。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from DrissionPage import Chromium, ChromiumOptions

from ..config.constants import DEFAULT_PASSWORD_LENGTH, PASSWORD_CHARSET, generate_random_user_info
from ..database import crud
from ..database.session import get_db
from ..services import BaseEmailService

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    RESOURCE_ROOT = Path(__file__).resolve().parents[2]

EXTENSION_PATH = RESOURCE_ROOT / "turnstilePatch"
PROXY_PLUGIN_PATH = "/tmp/codex_console_grok_proxy_plugin"


@dataclass
class GrokRegistrationResult:
    """Grok 注册结果"""

    success: bool
    email: str = ""
    password: str = ""
    name: str = ""
    sso_token: str = ""
    sso_rw_token: str = ""
    all_cookies: Dict[str, str] = field(default_factory=dict)
    error_message: str = ""
    logs: List[str] = field(default_factory=list)
    source: str = "register"

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
            "source": self.source,
        }


def parse_cookie_header(cookie_header: str) -> list[dict[str, Any]]:
    """把 Cookie 头解析成 DrissionPage 可写入的 cookie 列表。"""
    raw = (cookie_header or "").strip()
    if not raw:
        return []
    jar = SimpleCookie()
    jar.load(raw)
    cookies: list[dict[str, Any]] = []
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


def apply_bootstrap_cookies(page, *, cookie_header: str, cf_clearance: str, cf_bm: str) -> int:
    """在打开 sign-up 之前预注入 Cloudflare 相关 cookie。"""
    cookies: list[dict[str, Any]] = []
    cookies.extend(parse_cookie_header(cookie_header))
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


def is_cloudflare_blocked(page) -> bool:
    """判断是否直接落入 Cloudflare 拦截页。"""
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

    background = string.Template(
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
    ['blocking']
);
"""
    ).substitute(host=host, port=port, user=user, password=password)

    os.makedirs(PROXY_PLUGIN_PATH, exist_ok=True)
    Path(PROXY_PLUGIN_PATH, "manifest.json").write_text(manifest, encoding="utf-8")
    Path(PROXY_PLUGIN_PATH, "background.js").write_text(background, encoding="utf-8")
    return PROXY_PLUGIN_PATH


def get_turnstile_token(page) -> str | None:
    page.run_js("try { turnstile.reset() } catch(e) {}")
    turnstile_response = None

    for _ in range(5):
        try:
            turnstile_response = page.run_js(
                "try { return turnstile.getResponse() } catch(e) { return null }"
            )
            if turnstile_response:
                return turnstile_response

            challenge_solution = page.ele("@name=cf-turnstile-response")
            challenge_wrapper = challenge_solution.parent()
            challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
            challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_iframe_body.ele("tag:input")
            challenge_button.click()
        except Exception:
            pass
        time.sleep(1)

    return turnstile_response


class GrokRegistrationEngine:
    """Grok 浏览器注册引擎。"""

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        password: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        cf_clearance: Optional[str] = None,
        cf_bm: Optional[str] = None,
        cf_cookie_header: Optional[str] = None,
        user_agent: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = (proxy_url or "").strip()
        self.password = (password or "").strip()
        self.user_data_dir = (user_data_dir or "").strip()
        self.cf_clearance = (cf_clearance or "").strip()
        self.cf_bm = (cf_bm or "").strip()
        self.cf_cookie_header = (cf_cookie_header or "").strip()
        self.user_agent = (user_agent or "").strip()
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        self.email: str = ""
        self.name: str = ""
        self.email_info: Optional[Dict[str, Any]] = None
        self.logs: List[str] = []

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.logs.append(line)
        self.callback_logger(line)
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, line)
            except Exception as exc:
                logger.warning(f"记录任务日志失败: {exc}")

        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self) -> str:
        if self.password:
            return self.password
        return "".join(random.choice(PASSWORD_CHARSET) for _ in range(DEFAULT_PASSWORD_LENGTH))

    def _generate_name(self) -> str:
        return generate_random_user_info()["name"]

    def _create_email(self) -> bool:
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()
            self.email = str(self.email_info.get("email") or "").strip()
            if not self.email:
                raise ValueError("邮箱服务未返回 email")
            self._log(f"邮箱已就绪: {self.email}")
            return True
        except Exception as exc:
            self._log(f"创建邮箱失败: {exc}", "error")
            return False

    def _wait_for_otp(self) -> str:
        email_id = (self.email_info or {}).get("service_id")
        self._log("等待 OTP 邮件...")
        otp = self.email_service.get_verification_code(
            email=self.email,
            email_id=email_id,
            timeout=180,
            pattern=r"([A-Z]{3}-[A-Z0-9]{3})|(\b\d{6}\b)",
        )
        if otp:
            self._log(f"OTP 获取成功: {otp}")
        else:
            self._log("OTP 等待超时", "error")
        return otp or ""

    def _extract_tokens(self, page) -> dict[str, Any]:
        tokens: dict[str, Any] = {"sso": "", "sso-rw": "", "all_cookies": {}}
        try:
            current_url = page.url
            if not current_url.startswith("https://grok.com"):
                self._log("跳转到 grok.com 以触发 SSO Cookie 写入...")
                page.get("https://grok.com")
                time.sleep(3)

            try:
                cdp_cookies = page.run_cdp("Network.getAllCookies")
                for cookie in cdp_cookies.get("cookies", []):
                    name = cookie.get("name", "")
                    value = cookie.get("value", "")
                    domain = cookie.get("domain", "")
                    if any(mark in domain for mark in (".x.ai", "x.ai", ".grok.com", "grok.com")):
                        tokens["all_cookies"][f"{domain}|{name}"] = value
                        if name == "sso":
                            tokens["sso"] = value
                        elif name == "sso-rw":
                            tokens["sso-rw"] = value
                self._log(f"已提取 {len(tokens['all_cookies'])} 个相关 Cookie")
            except Exception as exc:
                self._log(f"CDP 读取 Cookie 失败: {exc}", "warning")

            if not tokens["sso-rw"]:
                try:
                    for cookie in page.cookies():
                        name = cookie.get("name", "")
                        value = cookie.get("value", "")
                        if name == "sso":
                            tokens["sso"] = value
                        elif name == "sso-rw":
                            tokens["sso-rw"] = value
                except Exception as exc:
                    self._log(f"页面 Cookie 回退提取失败: {exc}", "warning")
        except Exception as exc:
            self._log(f"提取 Token 异常: {exc}", "warning")

        if tokens["sso-rw"]:
            self._log(f"sso-rw 提取成功，长度 {len(tokens['sso-rw'])}")
        else:
            self._log("未能提取到 sso-rw", "warning")
        return tokens

    def _build_browser(self) -> tuple[Any, Any]:
        if not EXTENSION_PATH.exists():
            raise RuntimeError(f"缺少 turnstilePatch 扩展目录: {EXTENSION_PATH}")

        options = ChromiumOptions()
        options.auto_port()
        options.set_timeouts(base=2)
        options.add_extension(str(EXTENSION_PATH))
        options.set_argument("--disable-blink-features=AutomationControlled")
        options.set_argument("--no-first-run")
        options.set_argument("--disable-dev-shm-usage")

        if self.user_agent:
            options.set_user_agent(self.user_agent)
        if self.user_data_dir:
            options.set_argument("--user-data-dir", str(Path(self.user_data_dir).expanduser()))
            self._log(f"使用浏览器用户目录: {self.user_data_dir}")

        if self.proxy_url:
            options.set_proxy(self.proxy_url)
            self._log(f"使用代理: {self.proxy_url}")
        else:
            proxy_host = os.environ.get("PROXY_HOST", "").strip()
            proxy_port = os.environ.get("PROXY_PORT", "").strip()
            proxy_user = os.environ.get("PROXY_USER", "").strip()
            proxy_pass = os.environ.get("PROXY_PASS", "").strip()
            if proxy_host and proxy_port:
                options.add_extension(_create_proxy_extension(proxy_host, proxy_port, proxy_user, proxy_pass))
                self._log(f"使用环境变量代理: {proxy_host}:{proxy_port}")

        browser = Chromium(options)
        page = browser.get_tabs()[-1]
        return browser, page

    def _save_account(self, result: GrokRegistrationResult) -> None:
        try:
            with get_db() as db:
                extra_data = {
                    "platform": "grok",
                    "name": result.name,
                    "all_cookies": result.all_cookies,
                    "source": result.source,
                }
                existing = crud.get_account_by_email(db, result.email)
                if existing:
                    merged = dict(existing.extra_data or {})
                    merged.update(extra_data)
                    crud.update_account(
                        db,
                        existing.id,
                        password=result.password or existing.password,
                        status="active" if result.success else "failed",
                        source=result.source,
                        platform="grok",
                        sso_token=result.sso_token,
                        sso_rw_token=result.sso_rw_token,
                        extra_data=merged,
                    )
                    self._log("账号已存在，已更新数据库")
                else:
                    created = crud.create_account(
                        db,
                        email=result.email,
                        password=result.password,
                        email_service=self.email_service.service_type.value,
                        email_service_id=(self.email_info or {}).get("service_id"),
                        status="active" if result.success else "failed",
                        source=result.source,
                        extra_data=extra_data,
                    )
                    crud.update_account(
                        db,
                        created.id,
                        platform="grok",
                        sso_token=result.sso_token,
                        sso_rw_token=result.sso_rw_token,
                    )
                    self._log("账号已保存到数据库")
        except Exception as exc:
            self._log(f"保存账号到数据库失败: {exc}", "warning")

    def register(self) -> GrokRegistrationResult:
        result = GrokRegistrationResult(success=False)

        if not self._create_email():
            result.error_message = "创建邮箱失败"
            result.logs = self.logs
            return result

        result.email = self.email
        self.password = self._generate_password()
        self.name = self._generate_name()

        browser = None
        try:
            browser, page = self._build_browser()

            self._log("打开 xAI 注册页...")
            page.get("https://accounts.x.ai/")
            time.sleep(1)
            injected = apply_bootstrap_cookies(
                page,
                cookie_header=self.cf_cookie_header,
                cf_clearance=self.cf_clearance,
                cf_bm=self.cf_bm,
            )
            if injected:
                self._log(f"已注入 {injected} 个预置 Cookie")

            page.get("https://accounts.x.ai/sign-up")
            time.sleep(2)

            if is_cloudflare_blocked(page):
                raise RuntimeError("被 Cloudflare 拦截，当前出口或 Cookie 不可用")

            self._log("切换到邮箱注册入口...")
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

            self._log(f"填写邮箱: {self.email}")
            page.ele("@data-testid=email").input(self.email)
            time.sleep(0.5)
            submit_btn = page.ele("@data-testid=submit", timeout=2) or page.ele("@type=submit")
            submit_btn.click()
            time.sleep(3)

            otp = self._wait_for_otp()
            if not otp:
                raise RuntimeError("OTP 超时")

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
            while time.time() < deadline:
                try:
                    url = page.url
                    body_text = page.ele("tag:body").text
                except Exception:
                    time.sleep(1)
                    continue

                self._log(f"页面状态: {url}")

                if any(
                    url.startswith(prefix)
                    for prefix in (
                        "https://accounts.x.ai/account",
                        "https://grok.com",
                        "https://console.x.ai",
                        "https://x.ai/",
                    )
                ):
                    tokens = self._extract_tokens(page)
                    result.success = True
                    result.email = self.email
                    result.password = self.password
                    result.name = self.name
                    result.sso_token = tokens["sso"]
                    result.sso_rw_token = tokens["sso-rw"]
                    result.all_cookies = tokens["all_cookies"]
                    self._save_account(result)
                    result.logs = self.logs
                    return result

                if "accept-tos" in url:
                    self._log("检测到 TOS 页面，自动勾选并继续...")
                    time.sleep(2)
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
                    page.run_js(
                        """
                        const btns = Array.from(document.querySelectorAll('button'));
                        const cont = btns.find(
                            b => b.textContent.toLowerCase().includes('continue')
                              || b.textContent.toLowerCase().includes('agree')
                              || b.textContent.toLowerCase().includes('accept')
                              || b.textContent.trim() === '继续'
                        );
                        if (cont) cont.click();
                        """
                    )
                    time.sleep(4)
                    continue

                has_given_name = page.ele("@name=givenName", timeout=1)
                if has_given_name or "givenName" in page.html:
                    page.ele("@name=givenName").input(self.name)
                    page.ele("@name=familyName").input(self.name)
                    page.ele("@name=password").input(self.password)
                    time.sleep(0.5)

                    for attempt in range(3):
                        token = get_turnstile_token(page)
                        self._log(f"Turnstile 第 {attempt + 1} 次尝试: {'成功' if token else '未完成'}")
                        if token:
                            break
                        time.sleep(2)

                    page.run_js(
                        """
                        const btns = Array.from(document.querySelectorAll('button'));
                        const b = btns.find(
                            b => b.type === 'submit' || /continue|next|submit/i.test(b.textContent)
                        );
                        if (b) b.click();
                        """
                    )
                    time.sleep(5)
                    continue

                if "Welcome" in body_text or "欢迎" in body_text:
                    tokens = self._extract_tokens(page)
                    result.success = True
                    result.email = self.email
                    result.password = self.password
                    result.name = self.name
                    result.sso_token = tokens["sso"]
                    result.sso_rw_token = tokens["sso-rw"]
                    result.all_cookies = tokens["all_cookies"]
                    self._save_account(result)
                    result.logs = self.logs
                    return result

                time.sleep(2)

            tokens = self._extract_tokens(page)
            result.email = self.email
            result.password = self.password
            result.name = self.name
            result.sso_token = tokens["sso"]
            result.sso_rw_token = tokens["sso-rw"]
            result.all_cookies = tokens["all_cookies"]
            result.error_message = "页面跳转超时"
            if result.sso_rw_token:
                result.success = True
                self._save_account(result)
            result.logs = self.logs
            return result

        except Exception as exc:
            self._log(f"注册流程异常: {exc}", "error")
            result.email = self.email
            result.password = self.password
            result.name = self.name
            result.error_message = str(exc)
            result.logs = self.logs
            return result
        finally:
            if browser is not None:
                try:
                    browser.quit()
                except Exception:
                    pass
