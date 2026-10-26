"""浙江大学统一身份认证（CAS）与智云课堂 OAuth 登录。

纯 HTTP 流程：
  1. GET  CAS 登录页，拿 execution 和隐藏字段；
  2. GET  公钥，按官方前端同样的方式做裸 RSA（密码按大端整数编码）；
  3. POST 学号 + 加密密码，拿 iPlanetDirectoryPro；
  4. 走 tgmedia -> CAS OAuth 2.0 -> tgmedia get-info，从 cookie/响应里取智云 JWT。

如果 CAS 要求验证码或学校改了流程，可以用 Playwright 打开浏览器人工登录兜底。
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
import time
import urllib.parse
from typing import Any, Callable, Iterable

import requests

from .config import Config
from .errors import LoginError
from .net import USER_AGENT, make_session


CAS_BASE = "https://zjuam.zju.edu.cn/cas"
CAS_LOGIN = CAS_BASE + "/login"
CAS_PUBKEY = CAS_BASE + "/v2/getPubKey"
CAS_KAPTCHA_STATUS = CAS_BASE + "/v2/getKaptchaStatus"
ZHIYUN_ENTRY = "https://tgmedia.cmc.zju.edu.cn/index.php"
CLASSROOM_HOME = "https://classroom.zju.edu.cn/"

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}")
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I | re.S)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.S)
PREFERRED_COOKIE_NAMES = {
    "_token",
    "jwtuser",
    "_identity",
    "token",
    "jwt",
    "access_token",
    "authorization",
}

Verbose = Callable[[str], None]


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def _hidden_inputs(page: str) -> dict[str, str]:
    """提取 CAS 表单里的 hidden 字段（execution、ys、uuid 等）。"""
    fields: dict[str, str] = {}
    for tag in INPUT_RE.findall(page):
        attrs: dict[str, str] = {}
        for match in ATTR_RE.finditer(tag):
            name = match.group(1).lower()
            value = match.group(2)
            if value is None:
                value = match.group(3)
            if value is None:
                value = match.group(4) or ""
            attrs[name] = html.unescape(value)
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
            fields[attrs["name"]] = attrs.get("value", "")
    return fields


def encrypt_password(password: str, modulus_hex: str, exponent_hex: str) -> str:
    """复现 zjuam 前端的密码加密：密码字节按大端整数做裸 RSA。"""
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    data = password.encode("utf-8")
    if not data:
        raise LoginError("密码不能为空")
    if len(data) * 8 > modulus.bit_length():
        raise LoginError("密码过长，当前 RSA 公钥无法加密")
    encrypted = pow(int.from_bytes(data, "big"), exponent, modulus)
    hex_width = (modulus.bit_length() + 3) // 4
    return format(encrypted, "x").zfill(hex_width)


def normalize_jwt(raw: str) -> str:
    """接受裸 JWT、Bearer 头、cookie 值或整段文本，提取出 JWT。"""
    if not raw:
        return ""
    text = str(raw).strip().strip('"').strip("'")
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    text = urllib.parse.unquote(text)
    match = JWT_RE.search(text)
    return match.group(0) if match else ""


def jwt_payload(jwt: str) -> dict[str, Any]:
    token = normalize_jwt(jwt)
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8", "replace")
        data = json.loads(decoded)
    except (ValueError, TypeError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_jwt(candidate: str) -> bool:
    return bool(candidate) and bool(jwt_payload(candidate))


def extract_jwt_from_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """从 (cookie 名, 值) 序列中提取 JWT。优先 _token / JWTUser 等。"""
    preferred: list[str] = []
    others: list[str] = []
    for name, value in pairs:
        url_decoded = urllib.parse.unquote(str(value))
        candidates = []
        for text in (str(value), url_decoded, urllib.parse.unquote(url_decoded)):
            match = JWT_RE.search(text)
            if match:
                candidates.append(match.group(0))
        target = preferred if str(name).lower() in PREFERRED_COOKIE_NAMES else others
        target.extend(candidates)

    for group in (preferred, others):
        for candidate in group:
            if _valid_jwt(candidate):
                return candidate
    return ""


def extract_jwt_from_text(text: str) -> str:
    if not text:
        return ""
    for match in JWT_RE.finditer(text):
        candidate = match.group(0)
        if _valid_jwt(candidate):
            return candidate
    return ""


def _session_cookie_pairs(session: requests.Session) -> Iterable[tuple[str, str]]:
    for cookie in session.cookies:
        yield cookie.name, cookie.value


def _find_jwt(session: requests.Session, *texts: str) -> str:
    jwt = extract_jwt_from_pairs(_session_cookie_pairs(session))
    if jwt:
        return jwt
    for text in texts:
        jwt = extract_jwt_from_text(text or "")
        if jwt:
            return jwt
    return ""


# ---------------------------------------------------------------------------
# CAS 登录
# ---------------------------------------------------------------------------


def _extract_error_message(page: str) -> str:
    if not page:
        return ""
    for pattern in (
        r'id=["\']errormsg["\'][^>]*>(.*?)</',
        r'id=["\']msg["\'][^>]*>(.*?)</',
        r'class=["\'][^"\']*(?:error|alert|message)[^"\']*["\'][^>]*>(.*?)</',
    ):
        match = re.search(pattern, page, re.I | re.S)
        if match:
            text = re.sub(r"<[^>]+>", " ", match.group(1))
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                return text[:200]
    return ""


def cas_login(
    session: requests.Session,
    username: str,
    password: str,
    *,
    verbose: Verbose = print,
) -> str:
    """登录统一身份认证，返回 iPlanetDirectoryPro。"""
    try:
        resp = session.get(CAS_LOGIN, timeout=25)
    except requests.RequestException as exc:
        raise LoginError(f"无法访问统一身份认证：{exc}") from exc
    if resp.status_code != 200:
        raise LoginError(f"统一身份认证返回 HTTP {resp.status_code}")

    page = resp.text
    execution_match = re.search(r'name=["\']execution["\']\s+value=["\']([^"\']+)', page)
    if not execution_match:
        raise LoginError("登录页里没有找到 execution 字段，学校可能调整了登录流程。请尝试 --browser 登录。")
    execution = html.unescape(execution_match.group(1))

    try:
        kaptcha = session.get(CAS_KAPTCHA_STATUS, timeout=15).text.strip().lower()
    except requests.RequestException:
        kaptcha = ""
    if kaptcha == "true":
        raise LoginError("统一身份认证当前要求验证码或二次验证，请使用 `zhiyun login --browser` 登录。")

    try:
        key_resp = session.get(CAS_PUBKEY, timeout=20)
        key_data = key_resp.json()
        modulus_hex = str(key_data.get("modulus", ""))
        exponent_hex = str(key_data.get("exponent", ""))
    except (requests.RequestException, ValueError) as exc:
        raise LoginError(f"获取登录公钥失败：{exc}") from exc
    if not modulus_hex or not exponent_hex:
        raise LoginError("登录公钥格式异常")

    encrypted = encrypt_password(password, modulus_hex, exponent_hex)

    form = _hidden_inputs(page)
    form.update(
        {
            "username": username,
            "password": encrypted,
            "_eventId": "submit",
            "rememberMe": "true",
        }
    )
    form.setdefault("authcode", "")

    try:
        resp = session.post(
            CAS_LOGIN,
            data=form,
            headers={
                "Referer": CAS_LOGIN,
                "Origin": "https://zjuam.zju.edu.cn",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise LoginError(f"提交登录失败：{exc}") from exc

    # 正常情况下 iPlanetDirectoryPro 会随 POST 响应或紧接着的重定向下发。
    for _ in range(4):
        iplanet = (
            session.cookies.get("iPlanetDirectoryPro")
            or session.cookies.get("iPlanetDirectoryPro", domain="zjuam.zju.edu.cn")
        )
        if iplanet:
            verbose("  统一身份认证成功")
            return iplanet
        if resp.is_redirect:
            location = resp.headers.get("Location", "")
            if not location:
                break
            next_url = urllib.parse.urljoin(resp.url, location)
            try:
                resp = session.get(next_url, timeout=25, allow_redirects=False)
            except requests.RequestException as exc:
                raise LoginError(f"跟随登录跳转失败：{exc}") from exc
            continue
        break

    message = _extract_error_message(resp.text)
    if message:
        raise LoginError(f"统一身份认证登录失败：{message}")
    raise LoginError(
        "统一身份认证未返回有效凭证。可能原因：学号/密码错误、账号需要二次验证，"
        "或登录流程已变化。可尝试 `zhiyun login --browser`。"
    )


def _js_redirect_target(body: str) -> str:
    """从简单 JS 跳转页里提取目标 URL（只在没有 Location 头时使用）。"""
    if not body:
        return ""
    patterns = (
        r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        r"""location\.replace\(\s*["']([^"']+)["']\s*\)""",
        r"""<meta[^>]+http-equiv=["']refresh["'][^>]+url=([^"'>\s]+)""",
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            return html.unescape(match.group(1))
    return ""


# ---------------------------------------------------------------------------
# 智云 OAuth
# ---------------------------------------------------------------------------


def zhiyun_login_from_cas(
    session: requests.Session, *, verbose: Verbose = print
) -> str:
    """在已完成 CAS 登录的会话上走智云 OAuth，返回 JWT。"""
    entry = ZHIYUN_ENTRY + "?" + urllib.parse.urlencode(
        {
            "r": "auth/login",
            "auType": "",
            "tenant_code": "112",
            "forward": CLASSROOM_HOME,
        }
    )

    url = entry
    seen: set[str] = set()
    last_body = ""
    for step in range(30):
        if url in seen:
            break
        seen.add(url)
        try:
            resp = session.get(
                url,
                timeout=25,
                allow_redirects=False,
                headers={"Referer": CLASSROOM_HOME},
            )
        except requests.RequestException as exc:
            raise LoginError(f"访问智云认证失败：{exc}") from exc

        last_body = resp.text or ""
        jwt = _find_jwt(session, last_body, resp.headers.get("Location", ""), url)
        if jwt:
            verbose("  智云课堂凭证获取成功")
            return jwt

        if resp.is_redirect:
            location = resp.headers.get("Location", "")
            if not location:
                break
            url = urllib.parse.urljoin(resp.url, location)
            if step == 0:
                verbose("  正在通过统一认证授权智云课堂")
            continue

        # 个别情况下是 JS 跳转而不是 3xx。
        js_target = _js_redirect_target(last_body)
        if js_target:
            js_target = urllib.parse.urljoin(resp.url, js_target)
            if js_target not in seen:
                url = js_target
                continue
        break

    # 有些实现会把 _token 留在 classroom 首页的 cookie 桥里，再访问一次首页。
    try:
        home = session.get(CLASSROOM_HOME, timeout=25, allow_redirects=True)
        jwt = _find_jwt(session, home.text)
        if jwt:
            verbose("  智云课堂凭证获取成功")
            return jwt
    except requests.RequestException:
        pass

    raise LoginError(
        "已通过统一身份认证，但没能自动拿到智云课堂 JWT。"
        "请尝试 `zhiyun login --browser`（浏览器里人工登录一次）。"
    )


def login(
    username: str,
    password: str,
    *,
    verbose: Verbose = print,
) -> Config:
    """完整登录：CAS + 智云 OAuth，返回待保存的 Config。"""
    session = make_session(with_retry=False)
    verbose(f"正在登录统一身份认证（{username}）…")
    cas_login(session, username, password, verbose=verbose)
    jwt = zhiyun_login_from_cas(session, verbose=verbose)
    payload = jwt_payload(jwt)
    expires_at = 0
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    return Config(
        jwt=jwt,
        username=username,
        student_id=username,
        expires_at=expires_at,
        saved_at=int(time.time()),
        source="password",
    )


# ---------------------------------------------------------------------------
# 浏览器兜底
# ---------------------------------------------------------------------------


def browser_login(*, timeout: int = 300, verbose: Verbose = print) -> Config:
    """打开 Playwright 浏览器，由用户完成登录，抓取 JWT。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginError(
            "未安装 Playwright。请先运行：\n"
            "  pip install 'zhiyun-cli[browser]'\n"
            "  python -m playwright install chromium"
        ) from exc

    verbose("即将打开浏览器，请在窗口里用统一身份认证登录（密码或扫码均可）。")
    verbose("登录成功并进入智云课堂后，工具会自动抓取凭证并关闭窗口。")

    deadline = time.time() + max(30, timeout)
    jwt = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(viewport={"width": 1280, "height": 880})
            page = context.new_page()
            try:
                page.goto(CLASSROOM_HOME, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                # 页面加载超时不致命，用户仍可以在窗口里继续操作。
                pass

            while time.time() < deadline:
                try:
                    cookies = context.cookies()
                except Exception:
                    break
                jwt = extract_jwt_from_pairs((c.get("name", ""), c.get("value", "")) for c in cookies)
                if jwt:
                    break

                for expression in (
                    "() => document.cookie",
                    "() => { try { return Object.keys(localStorage).map(k => localStorage.getItem(k) || '').join('\\n'); } catch (e) { return ''; } }",
                    "() => { try { return Object.keys(sessionStorage).map(k => sessionStorage.getItem(k) || '').join('\\n'); } catch (e) { return ''; } }",
                ):
                    try:
                        text = page.evaluate(expression) or ""
                    except Exception:
                        continue
                    jwt = extract_jwt_from_text(text)
                    if jwt:
                        break
                if jwt:
                    break

                try:
                    if page.is_closed():
                        break
                    page.wait_for_timeout(1000)
                except Exception:
                    break

            try:
                browser.close()
            except Exception:
                pass
    except LoginError:
        raise
    except Exception as exc:
        raise LoginError(f"浏览器登录失败：{exc}") from exc

    if not jwt:
        raise LoginError("等待登录超时，没有拿到智云课堂 JWT。请重试，或使用 `zhiyun login --token` 手动设置。")

    payload = jwt_payload(jwt)
    expires_at = 0
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    verbose("  已从浏览器获取智云课堂凭证")
    return Config(
        jwt=jwt,
        username="",
        student_id="",
        expires_at=expires_at,
        saved_at=int(time.time()),
        source="browser",
    )
