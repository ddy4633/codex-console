"""
Vibemail 邮箱服务
对接 VibecodingHub 临时邮箱 API，用于 Grok/xAI 注册接码
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests as http_requests

from ..config.constants import EmailServiceType, XAI_OTP_CODE_PATTERN
from .base import BaseEmailService, EmailServiceError

logger = logging.getLogger(__name__)

_DEFAULT_API = "https://tmpmail.vibecodinghub.cloud"


class VibemailService(BaseEmailService):
    """
    Vibemail 临时邮箱服务

    config 参数示例：
    {
        "user_jwt": "eyJ...",
        "api_base": "https://...",
        "proxy_url": "http://127.0.0.1:7890",
    }
    """

    def __init__(self, config: Dict[str, Any], name: str = None):
        super().__init__(EmailServiceType.VIBEMAIL, name or "vibemail")
        self.user_jwt: str = config.get("user_jwt", "")
        self.api_base: str = (config.get("api_base") or _DEFAULT_API).rstrip("/")

        if not self.user_jwt:
            raise EmailServiceError("Vibemail 需要 user_jwt 参数")

        proxy_url = config.get("proxy_url")
        self._session = http_requests.Session()
        self._session.headers.update({
            "Origin": "https://mail.vibecodinghub.cloud",
            "Referer": "https://mail.vibecodinghub.cloud/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
        })
        if proxy_url:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})
            self._session.verify = False

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """创建临时邮箱"""
        try:
            resp = self._session.post(
                f"{self.api_base}/api/new_address",
                json={},
                headers={"x-user-token": self.user_jwt},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            address = data["address"]
            address_jwt = data["jwt"]
            logger.info("[Vibemail] 创建邮箱: %s", address)
            self.update_status(True)
            return {
                "email": address,
                "service_id": address_jwt,
                "jwt": address_jwt,
            }
        except Exception as exc:
            self.update_status(False, exc)
            raise EmailServiceError(f"Vibemail 创建邮箱失败: {exc}") from exc

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 180,
        pattern: str = XAI_OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """轮询邮箱获取验证码"""
        address_jwt = email_id
        if not address_jwt:
            logger.error("[Vibemail] get_verification_code 需要 email_id(address_jwt)")
            return None

        code_re = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = self._fetch_code(address_jwt, code_re, otp_sent_at)
            if code:
                return code
            time.sleep(4)

        logger.error("[Vibemail] 验证码等待超时")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        try:
            resp = self._session.get(self.api_base, timeout=5)
            return resp.status_code < 500
        except Exception:
            return False

    def _fetch_code(
        self,
        address_jwt: str,
        code_re: re.Pattern,
        otp_sent_at: Optional[float],
    ) -> Optional[str]:
        try:
            resp = self._session.get(
                f"{self.api_base}/api/mails?limit=20&offset=0",
                headers={"Authorization": f"Bearer {address_jwt}"},
                timeout=15,
            )
            resp.raise_for_status()
            for mail in resp.json().get("results", []):
                if otp_sent_at:
                    received = mail.get("received_at") or mail.get("date", 0)
                    if isinstance(received, (int, float)) and received < otp_sent_at:
                        continue

                raw = str(mail.get("raw", ""))
                subject = str(mail.get("subject", ""))
                matched = code_re.search(subject + "\n" + raw)
                if matched:
                    raw_code = matched.group(1) if matched.group(1) else matched.group(2)
                    otp = raw_code.replace("-", "")
                    logger.info("[Vibemail] OTP: %s -> %s", raw_code, otp)
                    return otp
        except Exception as exc:
            logger.warning("[Vibemail] 轮询邮件失败: %s", exc)
        return None
