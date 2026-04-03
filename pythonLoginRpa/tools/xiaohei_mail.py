"""
xiaohei_mail.py — 小黑邮箱账号解析 & 验证码获取工具

账号数据格式（每行一条，----分隔）：
  email----password----verifyUrl----email----password----uuid----token

用法：
    from xiaohei_mail import parse_account, fetch_code

    acc = parse_account("KristineFarley9052@outlook.com----busk447120----https://xiaoheiapi.top/m/...")
    code = await fetch_code(acc["verify_url"])   # 返回 "281242" 或 None
"""

import re
import asyncio
from typing import Optional
from html.parser import HTMLParser

try:
    import httpx
    _HTTP_BACKEND = "httpx"
except ImportError:
    import urllib.request
    _HTTP_BACKEND = "urllib"


# ──────────────────────────────────────────────────────────────
#  数据解析
# ──────────────────────────────────────────────────────────────

def parse_account(line: str) -> dict:
    """
    解析一行账号数据，返回字段字典。

    必填字段：email, password, verify_url
    可选字段：uuid, token
    """
    parts = [p.strip() for p in line.split("----")]
    if len(parts) < 3:
        raise ValueError(f"账号数据至少需要 3 个字段（email/password/verifyUrl），实际: {line!r}")

    email      = parts[0]
    password   = parts[1]
    verify_url = parts[2]

    if not email or "@" not in email:
        raise ValueError(f"邮箱格式不正确: {email!r}")
    if not verify_url.startswith("http"):
        raise ValueError(f"验证码 URL 格式不正确: {verify_url!r}")

    return {
        "email":      email,
        "password":   password,
        "verify_url": verify_url,
        "uuid":       parts[5] if len(parts) > 5 else "",
        "token":      parts[6] if len(parts) > 6 else "",
    }


def load_accounts(path: str) -> list[dict]:
    """
    从文件逐行读取账号，返回解析后的列表（忽略空行和注释行）。
    """
    accounts = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "----" not in line:
                continue
            try:
                accounts.append(parse_account(line))
            except ValueError as e:
                print(f"[xiaohei_mail] 第 {lineno} 行解析失败: {e}")
    return accounts


# ──────────────────────────────────────────────────────────────
#  HTML 解析（提取验证码）
# ──────────────────────────────────────────────────────────────

class _MenloParser(HTMLParser):
    """提取 <p style="font-family: Menlo..."> 内的 6 位数字"""

    def __init__(self):
        super().__init__()
        self._in_target = False
        self._depth     = 0
        self.code: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if self.code:
            return
        if tag == "p":
            style = dict(attrs).get("style", "")
            if "Menlo" in style or "Monaco" in style or "Lucida Console" in style:
                self._in_target = True
                self._depth = 1

    def handle_endtag(self, tag):
        if self._in_target and tag == "p":
            self._depth -= 1
            if self._depth <= 0:
                self._in_target = False

    def handle_data(self, data):
        if self._in_target and not self.code:
            m = re.search(r"\b(\d{6})\b", data)
            if m:
                self.code = m.group(1)


def _extract_code_from_html(html: str) -> Optional[str]:
    """
    从小黑邮箱 HTML 页面提取 6 位验证码。

    优先从 <p style="font-family: Menlo..."> 提取，
    找不到则在全文中查找第一个 6 位数字（兜底）。
    """
    parser = _MenloParser()
    parser.feed(html)
    if parser.code:
        return parser.code

    # 兜底：全文搜索第一个独立的 6 位数字
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", html)
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────
#  网络请求（同步 & 异步）
# ──────────────────────────────────────────────────────────────

_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _build_httpx_kwargs(timeout: int, proxy: str) -> dict:
    kwargs = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _preview_html(html: str, max_len: int = 160) -> str:
    cleaned = re.sub(r"\s+", " ", html or "").strip()
    return cleaned if len(cleaned) <= max_len else cleaned[:max_len] + "..."


def fetch_code_sync_details(verify_url: str, timeout: int = 15, proxy: str = "") -> dict:
    result = {
        "ok": False,
        "code": None,
        "status_code": 0,
        "final_url": verify_url,
        "body_preview": "",
        "error": "",
    }
    try:
        if _HTTP_BACKEND == "httpx":
            with httpx.Client(**_build_httpx_kwargs(timeout, proxy)) as client:
                r = client.get(verify_url, headers=_HEADERS)
                html = r.text
                code = _extract_code_from_html(html)
                result.update({
                    "ok": True,
                    "code": code,
                    "status_code": r.status_code,
                    "final_url": str(r.url),
                    "body_preview": _preview_html(html),
                })
                return result
        req = urllib.request.Request(verify_url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
            final_url = getattr(resp, "geturl", lambda: verify_url)()
        code = _extract_code_from_html(html)
        result.update({
            "ok": True,
            "code": code,
            "status_code": status,
            "final_url": final_url,
            "body_preview": _preview_html(html),
        })
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


async def fetch_code_details(verify_url: str, timeout: int = 15, proxy: str = "") -> dict:
    try:
        if _HTTP_BACKEND == "httpx":
            async with httpx.AsyncClient(**_build_httpx_kwargs(timeout, proxy)) as client:
                r = await client.get(verify_url, headers=_HEADERS)
                html = r.text
                code = _extract_code_from_html(html)
                return {
                    "ok": True,
                    "code": code,
                    "status_code": r.status_code,
                    "final_url": str(r.url),
                    "body_preview": _preview_html(html),
                    "error": "",
                }
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: fetch_code_sync_details(verify_url, timeout, proxy)
        )
    except Exception as e:
        return {
            "ok": False,
            "code": None,
            "status_code": 0,
            "final_url": verify_url,
            "body_preview": "",
            "error": str(e),
        }


def fetch_code_sync(verify_url: str, timeout: int = 15, proxy: str = "") -> Optional[str]:
    """
    同步版本：从 verify_url 获取验证码。

    :param verify_url: 小黑 API 地址，如 https://xiaoheiapi.top/m/xxx/email%40outlook.com
    :param timeout:    请求超时秒数
    :param proxy:      HTTP 代理，如 "http://127.0.0.1:6987"，留空不使用
    :return:           6 位验证码字符串，或 None（未找到）
    """
    detail = fetch_code_sync_details(verify_url, timeout=timeout, proxy=proxy)
    if not detail.get("ok") and detail.get("error"):
        print(f"[xiaohei_mail] fetch_code_sync 失败: {detail['error']}")
    return detail.get("code")


async def fetch_code(verify_url: str, timeout: int = 15, proxy: str = "") -> Optional[str]:
    """
    异步版本：从 verify_url 获取验证码（依赖 httpx[asyncio]）。

    :param verify_url: 小黑 API 地址
    :param timeout:    请求超时秒数
    :param proxy:      HTTP 代理
    :return:           6 位验证码字符串，或 None
    """
    detail = await fetch_code_details(verify_url, timeout=timeout, proxy=proxy)
    if not detail.get("ok") and detail.get("error"):
        print(f"[xiaohei_mail] fetch_code 失败: {detail['error']}")
    return detail.get("code")


async def wait_for_code(
    verify_url: str,
    max_retries: int = 30,
    interval: float = 3.0,
    timeout: int = 15,
    proxy: str = "",
    progress=None,
) -> Optional[str]:
    """
    轮询等待验证码出现（邮件可能有延迟）。

    :param verify_url:  小黑 API 地址
    :param max_retries: 最多重试次数，默认 30 次
    :param interval:    每次间隔秒数，默认 3s
    :param timeout:     单次请求超时秒数
    :param proxy:       HTTP 代理
    :return:            6 位验证码，或 None（超时）
    """
    for i in range(max_retries):
        detail = await fetch_code_details(verify_url, timeout=timeout, proxy=proxy)
        code = detail.get("code")
        if progress is not None:
            try:
                progress({
                    "attempt": i + 1,
                    "max_retries": max_retries,
                    "interval": interval,
                    **detail,
                })
            except Exception:
                pass
        if code:
            return code
        print(f"[xiaohei_mail] 第 {i+1}/{max_retries} 次未获取到验证码，{interval}s 后重试...")
        await asyncio.sleep(interval)
    return None


# ──────────────────────────────────────────────────────────────
#  CLI 快速测试
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python xiaohei_mail.py <verifyUrl 或 账号数据行>")
        sys.exit(1)

    arg = sys.argv[1]
    if "----" in arg:
        acc = parse_account(arg)
        url = acc["verify_url"]
        print(f"邮箱: {acc['email']}")
        print(f"密码: {acc['password']}")
        print(f"URL:  {url}")
    else:
        url = arg

    proxy = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"正在请求验证码...")
    code = asyncio.run(fetch_code(url, proxy=proxy))
    print(f"验证码: {code}" if code else "未获取到验证码")
