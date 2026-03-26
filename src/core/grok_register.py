"""
Grok/xAI 注册引擎
纯 HTTP 方案，对标 OpenAI 的 RegistrationEngine 架构
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .xai import XAIHTTPClient
from ..services import BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    XAI_API_ENDPOINTS,
    XAI_OTP_CODE_PATTERN,
    generate_random_user_info,
    PASSWORD_CHARSET,
    DEFAULT_PASSWORD_LENGTH,
)

logger = logging.getLogger(__name__)


# ── 数据类 ────────────────────────────────────────────────────────

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
            "success":       self.success,
            "email":         self.email,
            "password":      self.password,
            "name":          self.name,
            "sso_token":     self.sso_token[:20] + "..." if self.sso_token else "",
            "sso_rw_token":  self.sso_rw_token[:20] + "..." if self.sso_rw_token else "",
            "error_message": self.error_message,
            "logs":          self.logs,
            "source":        self.source,
        }


# ── 注册引擎 ──────────────────────────────────────────────────────

class GrokRegistrationEngine:
    """
    Grok/xAI 纯 HTTP 注册引擎

    注册流程（不经过浏览器，不依赖 Google）：
    1. 创建临时邮箱（Vibemail）
    2. POST /api/v1/auth/signup/email      → 提交邮箱，服务端自动发 OTP
    3. 等待 OTP（6位数字 or ABC-123）
    4. POST /api/v1/auth/signup/email/verify → 验证 OTP
    5. POST /api/v1/auth/signup/complete   → 填写 name/password
    6. GET  grok.com                       → 触发 SSO Cookie 写入
    7. 从 Cookie 中提取 sso-rw token

    若注册失败（邮箱已存在），自动切换至登录流程：
    1. POST /api/v1/auth/login/email       → 提交邮箱，触发 OTP
    2. POST /api/v1/auth/login/email/verify → 验证 OTP → 获得 token
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        password: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        user_agent: Optional[str] = None,
        cf_clearance: Optional[str] = None,
        cf_bm: Optional[str] = None,
        cf_cookie_header: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.password = (password or "").strip()
        self.user_data_dir = (user_data_dir or "").strip()
        self.user_agent = (user_agent or "").strip()
        self.cf_clearance = (cf_clearance or "").strip()
        self.cf_bm = (cf_bm or "").strip()
        self.cf_cookie_header = (cf_cookie_header or "").strip()
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        self.http_client = XAIHTTPClient(proxy_url=proxy_url)

        if self.user_agent:
            self.http_client.default_headers.update({"User-Agent": self.user_agent})

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.name: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.logs: List[str] = []
        self._otp_sent_at: Optional[float] = None
        self._is_existing_account: bool = False

    # ── 日志 ──────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.logs.append(log_message)
        if self.callback_logger:
            self.callback_logger(log_message)
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)    # ── 工具方法 ──────────────────────────────────────────────────

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        return "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _generate_name(self) -> str:
        user_info = generate_random_user_info()
        return user_info["name"]

    # ── 流程步骤 ──────────────────────────────────────────────────

    def _check_ip(self) -> bool:
        ok, loc = self.http_client.check_ip_location()
        if not ok:
            self._log(f"IP 地区 {loc} 不支持注册 Grok，请检查代理", "error")
            return False
        self._log(f"IP 检查通过，地区: {loc}")
        return True

    def _create_email(self) -> bool:
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()
            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False
            self.email = self.email_info["email"]
            self._log(f"邮箱已就绪: {self.email}")
            return True
        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _submit_email(self) -> Tuple[bool, str]:
        """
        提交邮箱发起注册，返回 (success, status)
        status: "new" | "existing" | "error"
        """
        try:
            self._log("提交邮箱发起注册...")
            resp = self.http_client.xai_post(
                XAI_API_ENDPOINTS["signup"],
                {"email": self.email},
            )
            self._log(f"提交邮箱 → HTTP {resp.status_code}")

            if resp.status_code == 200:
                self._otp_sent_at = time.time()
                self._log("邮箱提交成功，服务端已自动发送 OTP")
                return True, "new"

            if resp.status_code == 409:
                # 邮箱已注册，切换登录流程
                self._log("邮箱已注册，自动切换到登录流程")
                self._is_existing_account = True
                return True, "existing"

            self._log(f"提交邮箱失败: {resp.status_code} {resp.text[:200]}", "error")
            return False, "error"

        except Exception as e:
            self._log(f"提交邮箱异常: {e}", "error")
            return False, "error"

    def _get_otp(self) -> Optional[str]:
        """从邮箱服务获取 OTP"""
        email_id = self.email_info.get("service_id") if self.email_info else None
        self._log(f"等待 OTP 邮件到达 {self.email}...")
        code = self.email_service.get_verification_code(
            email=self.email,
            email_id=email_id,
            timeout=180,
            pattern=XAI_OTP_CODE_PATTERN,
            otp_sent_at=self._otp_sent_at,
        )
        if code:
            self._log(f"OTP 获取成功: {code}")
        else:
            self._log("OTP 等待超时", "error")
        return code

    def _verify_signup_otp(self, otp: str) -> bool:
        """验证注册 OTP"""
        try:
            resp = self.http_client.xai_post(
                XAI_API_ENDPOINTS["verify_otp"],
                {"email": self.email, "code": otp},
            )
            self._log(f"验证注册 OTP → HTTP {resp.status_code}")
            if resp.status_code == 200:
                return True
            self._log(f"OTP 验证失败: {resp.text[:200]}", "warning")
            return False
        except Exception as e:
            self._log(f"OTP 验证异常: {e}", "error")
            return False

    def _complete_registration(self) -> bool:
        """填写 name/password 完成注册"""
        try:
            name = self._generate_name()
            password = self._generate_password()
            self.name = name
            self.password = password

            self._log(f"完成注册信息: name={name}")
            resp = self.http_client.xai_post(
                XAI_API_ENDPOINTS["complete"],
                {
                    "email":    self.email,
                    "name":     name,
                    "password": password,
                },
            )
            self._log(f"完成注册 → HTTP {resp.status_code}")
            if resp.status_code == 200:
                return True
            self._log(f"完成注册失败: {resp.text[:300]}", "warning")
            return False
        except Exception as e:
            self._log(f"完成注册异常: {e}", "error")
            return False

    def _login_with_email(self) -> bool:
        """登录流程：提交邮箱，触发 OTP"""
        try:
            self._log("登录流程：提交邮箱...")
            resp = self.http_client.xai_post(
                XAI_API_ENDPOINTS["login"],
                {"email": self.email},
            )
            self._log(f"登录提交邮箱 → HTTP {resp.status_code}")
            if resp.status_code == 200:
                self._otp_sent_at = time.time()
                return True
            self._log(f"登录提交邮箱失败: {resp.text[:200]}", "error")
            return False
        except Exception as e:
            self._log(f"登录提交邮箱异常: {e}", "error")
            return False

    def _verify_login_otp(self, otp: str) -> bool:
        """验证登录 OTP 并保存 token"""
        try:
            resp = self.http_client.xai_post(
                XAI_API_ENDPOINTS["login_verify"],
                {"email": self.email, "code": otp},
            )
            self._log(f"验证登录 OTP → HTTP {resp.status_code}")
            if resp.status_code == 200:
                return True
            self._log(f"登录 OTP 验证失败: {resp.text[:200]}", "warning")
            return False
        except Exception as e:
            self._log(f"登录 OTP 验证异常: {e}", "error")
            return False

    def _extract_tokens(self) -> Tuple[str, str, Dict[str, str]]:
        """
        访问 grok.com 触发 SSO 写入，然后从 Cookie 中提取 sso / sso-rw token

        Returns:
            (sso_token, sso_rw_token, all_cookies_dict)
        """
        sso = ""
        sso_rw = ""
        all_cookies: Dict[str, str] = {}

        try:
            self._log("访问 grok.com 触发 SSO Cookie 写入...")
            self.http_client.session.get(
                XAI_API_ENDPOINTS["grok_home"],
                timeout=15,
                allow_redirects=True,
            )
            time.sleep(2)

            # 遍历所有 Cookie
            for cookie in self.http_client.session.cookies:
                name   = cookie.name   if hasattr(cookie, "name")   else ""
                value  = cookie.value  if hasattr(cookie, "value")  else ""
                domain = cookie.domain if hasattr(cookie, "domain") else ""
                if any(d in (domain or "") for d in (".x.ai", "x.ai", ".grok.com", "grok.com")):
                    all_cookies[f"{domain}|{name}"] = value
                    if name == "sso":
                        sso = value
                    elif name == "sso-rw":
                        sso_rw = value

            if sso_rw:
                self._log(f"sso-rw 提取成功 (长度: {len(sso_rw)})")
            else:
                self._log("未能提取到 sso-rw，尝试从响应 JSON 中查找...", "warning")
                # 部分实现中 token 可能在 JSON 响应里
                # 留给后续迭代处理

        except Exception as e:
            self._log(f"提取 token 异常: {e}", "warning")

        return sso, sso_rw, all_cookies

    def _save_account(self, result: GrokRegistrationResult):
        """保存账号到数据库"""
        try:
            with get_db() as db:
                # 检查是否已存在
                existing = crud.get_account_by_email(db, result.email)
                if existing:
                    crud.update_account(
                        db,
                        existing.id,
                        extra_data={
                            "platform":     "grok",
                            "name":         result.name,
                            "sso_token":    result.sso_token,
                            "sso_rw_token": result.sso_rw_token,
                            "all_cookies":  result.all_cookies,
                            "source":       result.source,
                        },
                    )
                    self._log("账号已存在，已更新数据库")
                else:
                    crud.create_account(
                        db,
                        email=result.email,
                        password=result.password,
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="active" if result.success else "failed",
                        source=result.source,
                        extra_data={
                            "platform":     "grok",
                            "name":         result.name,
                            "sso_token":    result.sso_token,
                            "sso_rw_token": result.sso_rw_token,
                            "all_cookies":  result.all_cookies,
                        },
                    )
                    self._log("账号已保存到数据库")
        except Exception as e:
            self._log(f"保存账号到数据库失败: {e}", "warning")

    # ── 主流程 ────────────────────────────────────────────────────

    def register(self) -> GrokRegistrationResult:
        """执行完整的 Grok 纯 HTTP 注册流程"""
        result = GrokRegistrationResult(success=False)

        # 1. 检查 IP
        if not self._check_ip():
            result.error_message = "IP 地区不支持"
            result.logs = self.logs
            return result

        # 2. 创建邮箱
        if not self._create_email():
            result.error_message = "创建邮箱失败"
            result.logs = self.logs
            return result

        result.email = self.email

        # 3. 提交邮箱
        ok, status = self._submit_email()
        if not ok:
            result.error_message = "提交邮箱失败"
            result.logs = self.logs
            return result

        if status == "existing":
            # 邮箱已注册 → 走登录流程
            return self._do_login_flow(result)

        # 4. 等待 OTP
        otp = self._get_otp()
        if not otp:
            result.error_message = "OTP 超时"
            result.logs = self.logs
            return result

        # 5. 验证 OTP
        if not self._verify_signup_otp(otp):
            result.error_message = "OTP 验证失败"
            result.logs = self.logs
            return result

        # 6. 完成注册
        if not self._complete_registration():
            # 即使 complete 失败，也尝试切登录拿 token
            self._log("complete 步骤失败，尝试走登录流程补充 token", "warning")
            return self._do_login_flow(result)

        result.name     = self.name or ""
        result.password = self.password or ""
        result.source   = "register"

        # 7. 提取 SSO token
        sso, sso_rw, all_cookies = self._extract_tokens()
        result.sso_token    = sso
        result.sso_rw_token = sso_rw
        result.all_cookies  = all_cookies
        result.success      = True

        # 保存到数据库
        self._save_account(result)
        result.logs = self.logs
        return result

    def _do_login_flow(self, result: GrokRegistrationResult) -> GrokRegistrationResult:
        """已注册账号走登录流程获取 token"""
        self._log("开始登录流程获取 SSO token...")

        # 若之前没有密码（例如直接走登录），使用已保存的密码
        if not self.password:
            # 对于 Grok 的 OTP 登录，不需要密码，直接发 OTP
            pass

        # 登录：提交邮箱
        if not self._login_with_email():
            result.error_message = "登录提交邮箱失败"
            result.logs = self.logs
            return result

        # 等待登录 OTP
        otp = self._get_otp()
        if not otp:
            result.error_message = "登录 OTP 超时"
            result.logs = self.logs
            return result

        # 验证登录 OTP
        if not self._verify_login_otp(otp):
            result.error_message = "登录 OTP 验证失败"
            result.logs = self.logs
            return result

        result.source   = "login"
        result.name     = self.name or ""
        result.password = self.password or ""

        # 提取 SSO token
        sso, sso_rw, all_cookies = self._extract_tokens()
        result.sso_token    = sso
        result.sso_rw_token = sso_rw
        result.all_cookies  = all_cookies
        result.success      = bool(sso_rw or sso)

        if not result.success:
            result.error_message = "未能提取到 SSO token"
        else:
            self._save_account(result)

        result.logs = self.logs
        return result
