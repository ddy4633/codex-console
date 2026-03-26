"""
xAI/Grok 专用 HTTP 客户端
基于 HTTPClient 封装，添加 xAI 特定请求方法
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from ..http_client import HTTPClient, RequestConfig

logger = logging.getLogger(__name__)

_XAI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_XAI_DEFAULT_HEADERS = {
    "User-Agent":      _XAI_UA,
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin":          "https://accounts.x.ai",
    "Referer":         "https://accounts.x.ai/",
    "Content-Type":    "application/json",
}


class XAIHTTPClient(HTTPClient):
    """
    xAI/Grok 专用 HTTP 客户端
    - impersonate="chrome" 绕过 Cloudflare 基础检测
    - 封装常用的 xAI REST API 调用
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None,
    ):
        if config is None:
            config = RequestConfig(
                timeout=30,
                max_retries=3,
                impersonate="chrome",
                verify_ssl=True,
            )
        super().__init__(proxy_url, config)
        self.default_headers = _XAI_DEFAULT_HEADERS.copy()

    # ── IP 检查 ────────────────────────────────────────────────────

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查代理 IP 地区，CN/HK/MO/TW 视为不可用"""
        try:
            resp = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            loc_m = re.search(r"loc=([A-Z]+)", resp.text)
            loc = loc_m.group(1) if loc_m else None
            if loc in ("CN", "HK", "MO", "TW"):
                return False, loc
            return True, loc
        except Exception as e:
            logger.error(f"检查 IP 地理位置失败: {e}")
            return False, None

    # ── xAI 注册 API 封装 ──────────────────────────────────────────

    def xai_post(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        """封装 xAI JSON POST，自动携带默认请求头"""
        headers = self.default_headers.copy()
        if extra_headers:
            headers.update(extra_headers)
        return self.session.post(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")),
            headers=headers,
            timeout=self.config.timeout,
        )

    def xai_get(
        self,
        endpoint: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        """封装 xAI GET，自动携带默认请求头"""
        headers = self.default_headers.copy()
        if extra_headers:
            headers.update(extra_headers)
        return self.session.get(
            endpoint,
            headers=headers,
            timeout=self.config.timeout,
        )
