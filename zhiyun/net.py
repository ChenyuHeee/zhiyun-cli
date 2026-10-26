"""网络层小工具：会话、重试、宽松 TLS，以及 JSON/主机名判断。"""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ZJU_HOST_SUFFIXES = (".zju.edu.cn", ".cmc.zju.edu.cn")


def _legacy_ssl_context() -> ssl.SSLContext:
    """浙大部分老服务器仍使用较弱 DH 参数，放宽安全等级以兼容。"""
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx


class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = _legacy_ssl_context()
        super().init_poolmanager(*args, **kwargs)


def make_session(retries: int = 2, *, with_retry: bool = True) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
    )
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}) if with_retry else frozenset(),
        raise_on_status=False,
    )
    adapter = LegacySSLAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def is_zju_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "zju.edu.cn" or host.endswith(ZJU_HOST_SUFFIXES)


def parse_json_maybe(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return {}
    return {}


def get_ci(mapping: Any, *names: str, default: Any = "") -> Any:
    """大小写不敏感地取字典字段。"""
    if not isinstance(mapping, dict):
        return default
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if name in mapping:
            return mapping[name]
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default
