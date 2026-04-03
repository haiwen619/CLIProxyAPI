#!/usr/bin/env python3
"""
ChatGPT Auto-Register  —  Playwright 驱动
用法:
    python autoregister.py           # 注册 1 个账号
    python autoregister.py 5         # 注册 5 个账号
    python autoregister.py --open-only  # 仅打开登录页，不执行后续流程
    python autoregister.py --fresh   # 使用临时无痕 Profile 运行
    python autoregister.py --mail xxx@yyy.com  # 主动查询该邮箱最近邮件

    python autoregister.py --mail bstevens971@jpfwawf.shop

    --fresh 会创建独立临时 Profile 和临时 GM 数据文件，每次运行都是全新环境，结束后自动删除。
    python autoregister.py --fresh
    python autoregister.py 3 --fresh
    python autoregister.py --open-only --fresh

    python autoregister.py 2 --concurrency 2 --fresh


依赖:
    pip install playwright httpx
    # 可选：pip install patchright
    playwright install chromium
"""

import asyncio
import argparse
import datetime
import json
import random
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx
try:
    from patchright.async_api import async_playwright, Page
except ImportError:
    from playwright.async_api import async_playwright, Page

try:
    from xiaohei_mail import wait_for_code as _xh_wait_code, parse_account as _xh_parse_account
    _XIAOHEI_AVAILABLE = True
except ImportError:
    _XIAOHEI_AVAILABLE = False

# ═══════════════════════════ 配置区 ═══════════════════════════
NPCMAIL_API_KEY  = "sk-hvvr8yimqCKm"   # NPCmail 密钥
NPCMAIL_BASE     = "https://moemail.nanohajimi.mom"
GPTMAIL_API_KEY  = "sk-hvvr8yimqCKm"          # GPTMail 密钥
GPTMAIL_BASE     = "https://mail.chatgpt.org.uk"
EMAIL_PROVIDER   = "gptmail"    # "npcmail" | "gptmail" | "xiaohei"
# 小黑平台配置（EMAIL_PROVIDER="xiaohei" 时生效）
#   账号文件每行格式：email----password----verifyUrl[----...（其余字段忽略）]
XIAOHEI_ACCOUNTS_FILE = ""      # 小黑账号数据文件路径，留空则仅支持单账号模式
XIAOHEI_VERIFY_URL    = ""      # 单账号直接填写 verify_url，多账号从文件读取
HEADLESS         = False        # True = 无头后台；False = 可见浏览器
TIMEOUT_SECS     = 300          # 单账号最大等待秒数
HTTP_PROXY       = "http://127.0.0.1:6987"  # 本地代理，设为 "" 则不使用
MAIL_FETCH_SECS  = 15           # 主动查询邮件超时秒数
DEFAULT_CONCURRENCY = 10        # 本地默认并发上限
# ═════════════════════════════════════════════════════════════

ROOT       = Path(__file__).parent
USERSCRIPT = ROOT / "rpalogin.js"
LOGIN_URL  = "https://chatgpt.com/auth/login?next=/codex"
PROFILE_DIR = ROOT / ".chrome-profile"
GM_STORE_FILE = ROOT / ".tm-gm-store.json"
REQUESTS_LOG_DIR = ROOT / "requests_log"
# 只记录这两个域名下的请求（Sentinel 人机验证 + OpenAI Auth API）
_CAPTURE_DOMAINS = ("sentinel.openai.com", "auth.openai.com")


# ──────────────────────────────────────────────────────────────
#  小黑账号：email -> verify_url 映射
# ──────────────────────────────────────────────────────────────
def _load_xiaohei_verify_map() -> dict[str, str]:
    """
    从 XIAOHEI_ACCOUNTS_FILE 加载 email -> verify_url 映射。
    每行格式：email----password----verifyUrl[----...（其余字段忽略）]
    同时把 XIAOHEI_VERIFY_URL 单账号配置作为兜底写入（key 为空字符串）。
    """
    result: dict[str, str] = {}
    if XIAOHEI_VERIFY_URL:
        result[""] = XIAOHEI_VERIFY_URL.strip()

    path = XIAOHEI_ACCOUNTS_FILE
    if not path:
        return result
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = ROOT / path
    if not file_path.exists():
        print(f"[xiaohei] 账号文件不存在: {file_path}")
        return result
    try:
        for lineno, raw in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or "----" not in line:
                continue
            parts = [p.strip() for p in line.split("----")]
            if len(parts) < 3:
                print(f"[xiaohei] 第 {lineno} 行字段不足（需要 email----password----verifyUrl）")
                continue
            email_key = parts[0].lower()
            verify_url = parts[2]
            if email_key and verify_url.startswith("http"):
                result[email_key] = verify_url
            else:
                print(f"[xiaohei] 第 {lineno} 行数据无效（邮箱或 URL 格式错误）")
    except Exception as exc:
        print(f"[xiaohei] 读取账号文件失败: {exc}")
    return result


_XIAOHEI_VERIFY_MAP: dict[str, str] = _load_xiaohei_verify_map()


# ──────────────────────────────────────────────────────────────
#  请求记录器
# ──────────────────────────────────────────────────────────────
class RequestLogger:
    """收集注册过程中所有 HTTP 请求/响应，结束时写入 JSON 文件"""

    _BODY_LIMIT = 20 * 1024  # 单条响应体最多记录 20 KB

    def __init__(self):
        self._records: list[dict] = []
        self._started_at = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def log(self, record: dict) -> None:
        record.setdefault("_ts", datetime.datetime.now().isoformat())
        self._records.append(record)

    def save(self, label: str = "", log: Callable[[str], None] = print) -> Path:
        REQUESTS_LOG_DIR.mkdir(exist_ok=True)
        stem = f"requests_{self._started_at}"
        if label:
            stem += f"_{label}"
        path = REQUESTS_LOG_DIR / f"{stem}.json"
        path.write_text(
            json.dumps(self._records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"  [logger] 请求记录已保存: {path}  ({len(self._records)} 条)")
        return path

# ──────────────────────────────────────────────────────────────
#  指纹浏览器：UA / 分辨率池（参考 browser_captcha.py）
# ──────────────────────────────────────────────────────────────
_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768), (1536, 864),
    (1600, 900), (1280, 720), (1440, 900), (1680, 1050),
    (1280, 800), (1920, 1200),
]


def _make_account_logger(index: int) -> Callable[[str], None]:
    prefix = f"[#{index + 1:02d}]"

    def _log(message: str) -> None:
        print(f"{prefix} {message}")

    return _log


def _launch_options() -> dict:
    proxy_opt = {"server": HTTP_PROXY} if HTTP_PROXY else None
    return {
        "headless": HEADLESS,
        "channel": "chrome",
        "locale": "zh-CN",
        "proxy": proxy_opt,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-quic",
            "--disable-features=UseDnsHttpsSvcb",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--no-first-run",
            "--disable-infobars",
            "--hide-scrollbars",
        ],
    }
# ──────────────────────────────────────────────────────────────
#  GM API 垫片（在浏览器页面内运行，localStorage 持久化）
# ──────────────────────────────────────────────────────────────
_GM_POLYFILL = r"""
(function () {
    const S = {};
    const lk = n => 'tm_gm_' + n;

    window.GM_setValue = (n, v) => {
        S[n] = v;
        try { localStorage.setItem(lk(n), JSON.stringify(v)); } catch (_) {}
        /* 同步到 Python 端，跨域导航后可恢复 */
        if (window.__pw_gm_set) {
            window.__pw_gm_set(lk(n), JSON.stringify(v)).catch(() => {});
        }
    };
    window.GM_getValue = (n, d) => {
        /* 优先内存 → localStorage（Python 端会在每次导航后回写） */
        if (S.hasOwnProperty(n)) return S[n];
        try {
            const r = localStorage.getItem(lk(n));
            if (r !== null) { S[n] = JSON.parse(r); return S[n]; }
        } catch (_) {}
        return d;
    };
    window.GM_deleteValue = n => {
        delete S[n];
        try { localStorage.removeItem(lk(n)); } catch (_) {}
        if (window.__pw_gm_set) {
            window.__pw_gm_set(lk(n), '__DELETED__').catch(() => {});
        }
    };
    window.GM_setClipboard = t => { try { navigator.clipboard.writeText(t); } catch (_) {} };
    window.GM_addStyle = css => {
        const s = document.createElement('style');
        s.textContent = css;
        const target = document.head || document.documentElement || document.body;
        if (target) { target.appendChild(s); }
        else { document.addEventListener('DOMContentLoaded', () => (document.head || document.documentElement).appendChild(s)); }
        return s;
    };
    window.GM_addElement = (tag, attrs) => {
        const el = document.createElement(tag);
        Object.assign(el, attrs || {});
        const target = document.head || document.documentElement || document.body;
        if (target) { target.appendChild(el); }
        else { document.addEventListener('DOMContentLoaded', () => (document.head || document.documentElement).appendChild(el)); }
        return el;
    };
    window.GM_cookie = {
        list: (d, cb) => cb(
            document.cookie.split(';').map(c => {
                const [n, ...v] = c.trim().split('=');
                return { name: n, value: v.join('='), domain: location.hostname };
            }), null),
        set: (d, cb) => {
            document.cookie = `${d.name}=${d.value || ''}` +
                (d.path   ? `;path=${d.path}`     : '') +
                (d.domain ? `;domain=${d.domain}` : '');
            if (cb) cb(null);
        },
        delete: (d, cb) => {
            document.cookie = `${d.name}=;expires=Thu,01 Jan 1970 00:00:00 GMT` +
                `;path=${d.path || '/'}`;
            if (cb) cb(null);
        }
    };
    /* GM_xmlhttpRequest — 通过 Patchright expose_function 桥接到 Python httpx */
    window.GM_xmlhttpRequest = opts => {
        const reqData = JSON.stringify({
            method: opts.method || 'GET',
            url: opts.url,
            headers: opts.headers || {},
            data: opts.data || null,
            timeout: opts.timeout || 15000,
        });

        /* __pw_http_request 由 Python 端 page.expose_function 注入，
           页面导航后可能有短暂延迟才可用，所以做重试 */
        function _tryBridge(retries) {
            const bridge = window.__pw_http_request;
            if (bridge) {
                bridge(reqData).then(resultJson => {
                    const r = JSON.parse(resultJson);
                    if (r.error) {
                        opts.onerror && opts.onerror({ error: r.error });
                    } else {
                        opts.onload && opts.onload(r);
                    }
                }).catch(err => {
                    opts.onerror && opts.onerror({ error: err.message });
                });
            } else if (retries > 0) {
                setTimeout(() => _tryBridge(retries - 1), 200);
            } else {
                console.warn('[GM_xmlhttpRequest] __pw_http_request 桥接不可用');
                opts.onerror && opts.onerror({ error: 'bridge not available' });
            }
        }
        _tryBridge(15);  /* 最多等 3 秒 (15 × 200ms) */
        return { abort: () => {} };
    };
    window.unsafeWindow = window;
})();
"""


# ──────────────────────────────────────────────────────────────
#  辅助：构建 GM 预填脚本 & 载入 userscript
# ──────────────────────────────────────────────────────────────
def _gm_config_values() -> dict:
    return {
        "tm_npcmail_apikey":    NPCMAIL_API_KEY,
        "tm_email_provider":    EMAIL_PROVIDER,
        "gptmail_api_key":      GPTMAIL_API_KEY,
        "tm_auto_redirect_team": False,
        "tm_reg_prefix":        "",
        "tm_reg_domain":        "",
        "tm_use_cd":            False,
        "tm_team_url":          "",
        "tm_team_key":          "",
        "tm_sync_url":          "",
        "tm_sync_apikey":       "",
        # 小黑平台：单账号 verify_url 直接写入 GM，
        # 多账号场景由 Python 侧在取到注册邮箱后按 email 查表
        "tm_xiaohei_verify_url": XIAOHEI_VERIFY_URL,
    }


def _gm_config_script() -> str:
    """在每次导航前向 localStorage 写入配置，确保 userscript 读到正确值"""
    values = _gm_config_values()
    lines = ["(function(){"]
    for k, v in values.items():
        lines.append(
            f"  try{{localStorage.setItem('tm_gm_{k}',JSON.stringify({json.dumps(v)}));}}catch(_){{}}"
        )
    lines.append("})();")
    return "\n".join(lines)


def _load_userscript() -> str:
    """剥离 ==UserScript== 头部，包裹在 DOMContentLoaded 中模拟 @run-at document-idle"""
    raw = USERSCRIPT.read_text(encoding="utf-8")
    marker = "// ==/UserScript=="
    if marker in raw:
        raw = raw.split(marker, 1)[1].strip()
    # 原脚本期望 DOM 完全就绪（@run-at document-idle），
    # 但 add_init_script 在文档创建时就执行，此时 head/body 尚不存在。
    # 用 DOMContentLoaded 延迟执行以模拟 Tampermonkey 行为。
    return (
        "if (document.readyState === 'loading') {\n"
        "  document.addEventListener('DOMContentLoaded', function() {\n"
        f"    {raw}\n"
        "  });\n"
        "} else {\n"
        f"  {raw}\n"
        "}"
    )


def _default_gm_store() -> dict[str, str]:
    return {
        f"tm_gm_{k}": json.dumps(v)
        for k, v in _gm_config_values().items()
    }


def _load_persisted_gm_store(gm_store_file: Path) -> dict[str, str]:
    store = _default_gm_store()
    if not gm_store_file.exists():
        return store

    try:
        raw = json.loads(gm_store_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [gm-store] 读取失败，改用默认值: {exc}")
        return store

    if not isinstance(raw, dict):
        return store

    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            store[key] = value
    return store


def _save_persisted_gm_store(store: dict[str, str], gm_store_file: Path) -> None:
    try:
        gm_store_file.write_text(
            json.dumps(store, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"  [gm-store] 写入失败: {exc}")




# ──────────────────────────────────────────────────────────────
#  Cloudflare Turnstile 自动处理
# ──────────────────────────────────────────────────────────────
async def _handle_turnstile(page: Page, timeout: int = 30_000, log: Callable[[str], None] = print) -> bool:
    """检测并点击 Cloudflare Turnstile 验证框，等待通过"""
    deadline = time.time() + timeout / 1000
    clicked = False

    while time.time() < deadline:
        # Turnstile iframe 内的 checkbox
        for frame in page.frames:
            try:
                cb = await frame.query_selector("input[type='checkbox']")
                if cb and not clicked:
                    box = await cb.bounding_box()
                    if box:
                        log("  → 检测到 Turnstile 验证框，点击中...")
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        clicked = True
            except Exception:
                pass

        # 检查是否已经通过（页面跳转离开 challenge 或 checkbox 消失）
        try:
            challenge = await page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if not challenge:
                if clicked:
                    log("  → Turnstile 验证已通过")
                return True
        except Exception:
            pass

        await page.wait_for_timeout(1_000)

    log("  [!] Turnstile 验证超时")
    return False


# ──────────────────────────────────────────────────────────────
#  触发自动注册：FAB → panel → [data-act="register"]
# ──────────────────────────────────────────────────────────────
async def _trigger_registration(page: Page) -> bool:
    # 等待悬浮按钮出现
    try:
        await page.wait_for_selector(".tm-fab", timeout=20_000)
    except Exception:
        print("  [!] FAB 未出现，userscript 可能未载入")
        return False

    # 打开面板
    await page.click(".tm-fab")
    await page.wait_for_timeout(700)

    # 点击「自动注册」按钮（data-act="register"）
    clicked = await page.evaluate("""
        () => {
            const btn = document.querySelector('[data-act="register"]');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    return bool(clicked)


# ──────────────────────────────────────────────────────────────
#  监听注册进度，返回 {'ok': bool, 'email': str, 'password': str}
# ──────────────────────────────────────────────────────────────
async def _wait_for_result(
    page: Page,
    timeout: int,
    dialog_msgs: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    deadline    = time.time() + timeout
    last_status = ""

    while time.time() < deadline:
        # 检查是否有 alert 弹窗报错（如"创建邮箱失败"）
        if dialog_msgs:
            for msg in dialog_msgs:
                if "失败" in msg or "error" in msg.lower() or "终止" in msg:
                    return {"ok": False, "email": "", "password": "",
                            "error": f"JS alert: {msg}"}
        try:
            res = await page.evaluate("""
                () => {
                    // 成功弹窗
                    const modal = document.getElementById('tm-modal-success');
                    if (modal && modal.classList.contains('show')) {
                        const email = document.getElementById('tm-success-email');
                        const pwd   = document.getElementById('tm-success-password');
                        return {
                            done: true, ok: true,
                            email:    email ? email.textContent.trim() : '',
                            password: pwd   ? pwd.textContent.trim()   : ''
                        };
                    }
                    // 状态条
                    const sbar   = document.querySelector('.tm-sbar');
                    const status = sbar ? sbar.textContent.replace('✕','').trim() : '';
                    // error toast
                    const errEl  = document.querySelector('.tm-toast.error.show');
                    const err    = errEl ? errEl.textContent.trim() : '';
                    return { done: false, status, err };
                }
            """)

            if res.get("done"):
                return {
                    "ok":       True,
                    "email":    res.get("email", ""),
                    "password": res.get("password", ""),
                }

            status = res.get("status", "")
            if status and status != last_status:
                log(f"  >> {status}")
                last_status = status

            err = res.get("err", "")
            if err:
                log(f"  [!] {err}")

        except Exception:
            pass

        await asyncio.sleep(1.5)

    return {"ok": False, "email": "", "password": "", "error": "timeout"}


async def _detect_final_error_state(page: Page) -> tuple[bool, str]:
    """注册流程结束后，检测是否落在 OpenAI 错误页（如 unsupported_email）。"""
    try:
        state = await page.evaluate(
            """
            () => {
                const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';
                const h1 = document.querySelector('h1');
                const h2 = document.querySelector('h2');
                const title = (h1 && h1.textContent) || (h2 && h2.textContent) || '';
                return {
                    url: location.href || '',
                    title: (title || '').trim(),
                    text: (bodyText || '').replace(/\s+/g, ' ').trim().slice(0, 6000),
                };
            }
            """
        )
    except Exception as exc:
        return False, f"页面检测异常: {exc}"

    url = str(state.get("url") or "")
    title = str(state.get("title") or "")
    text = str(state.get("text") or "")
    merged = f"{url}\n{title}\n{text}".lower()

    error_markers = [
        "unsupported_email",
        "验证过程中出错",
        "糟糕，出错了",
        "something went wrong",
        "an error occurred",
        "we ran into an issue",
    ]

    for marker in error_markers:
        if marker in merged:
            return True, marker

    # 兜底：错误页常见组合词（避免误判，至少同时命中“出错/错误 + 重试”）
    if (("出错" in merged or "错误" in merged) and "重试" in merged) or (
        ("error" in merged or "failed" in merged) and "try again" in merged
    ):
        return True, "检测到错误页文案"

    return False, ""


def _extract_verification_code(*texts: str) -> str:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return ""

    m = re.search(r"\b(\d{6})\b", joined)
    if m:
        return m.group(1)

    m = re.search(r"\b(\d{4,8})\b", joined)
    return m.group(1) if m else ""


def _to_gptmail_array(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        if isinstance(data.get("emails"), list):
            return [x for x in data["emails"] if isinstance(x, dict)]
        inner = data.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict) and isinstance(inner.get("emails"), list):
            return [x for x in inner["emails"] if isinstance(x, dict)]

    return []


def _to_npcmail_array(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        if isinstance(data.get("messages"), list):
            return [x for x in data["messages"] if isinstance(x, dict)]
        if isinstance(data.get("data"), list):
            return [x for x in data["data"] if isinstance(x, dict)]

    return []


async def _fetch_latest_email_info(email: str, provider: str | None = None, *, verify_url: str = "") -> dict:
    provider = (provider or EMAIL_PROVIDER or "gptmail").strip().lower()
    proxy = HTTP_PROXY or None

    info = {
        "ok": False,
        "provider": provider,
        "email": email,
        "mail_id": "",
        "subject": "",
        "from": "",
        "created_at": "",
        "content": "",
        "html_content": "",
        "verification_code": "",
        "error": "",
    }

    try:
        async with httpx.AsyncClient(timeout=MAIL_FETCH_SECS, proxy=proxy) as client:
            if provider == "gptmail":
                headers = {"X-API-Key": GPTMAIL_API_KEY, "Content-Type": "application/json"}
                inbox_url = f"{GPTMAIL_BASE}/api/emails?email={quote(email, safe='')}"
                resp = await client.get(inbox_url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()

                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(payload.get("error") or "GPTMail 返回 success=false")

                data = payload.get("data") if isinstance(payload, dict) else payload
                mails = _to_gptmail_array(data)
                if not mails:
                    info["error"] = "该邮箱暂无邮件"
                    return info

                latest = mails[0]
                mail_id = str(latest.get("id") or "")
                detail = latest

                if mail_id:
                    detail_url = f"{GPTMAIL_BASE}/api/email/{quote(mail_id, safe='')}"
                    det_resp = await client.get(detail_url, headers=headers)
                    det_resp.raise_for_status()
                    det_payload = det_resp.json()
                    if isinstance(det_payload, dict) and det_payload.get("success") is False:
                        raise RuntimeError(det_payload.get("error") or "GPTMail 详情接口返回 success=false")
                    det_data = det_payload.get("data") if isinstance(det_payload, dict) else det_payload
                    if isinstance(det_data, dict):
                        detail = det_data

                subject = str(detail.get("subject") or latest.get("subject") or "")
                from_addr = str(
                    detail.get("from_address")
                    or detail.get("from")
                    or latest.get("from_address")
                    or latest.get("from")
                    or ""
                )
                created_at = str(
                    detail.get("created_at")
                    or detail.get("received_at")
                    or latest.get("created_at")
                    or latest.get("received_at")
                    or ""
                )
                content = str(detail.get("content") or latest.get("content") or "")
                html_content = str(detail.get("html_content") or latest.get("html_content") or "")
                code = _extract_verification_code(content, html_content, subject)

                info.update({
                    "ok": True,
                    "mail_id": mail_id,
                    "subject": subject,
                    "from": from_addr,
                    "created_at": created_at,
                    "content": content,
                    "html_content": html_content,
                    "verification_code": code,
                    "error": "",
                })
                return info

            if provider == "npcmail":
                headers = {"X-API-Key": NPCMAIL_API_KEY, "Content-Type": "application/json"}
                inbox_url = f"{NPCMAIL_BASE}/api/public/emails/{quote(email, safe='')}/messages"
                resp = await client.get(inbox_url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                mails = _to_npcmail_array(payload)
                if not mails:
                    info["error"] = "该邮箱暂无邮件"
                    return info

                latest = mails[0]
                subject = str(latest.get("subject") or "")
                from_addr = str(latest.get("sender") or latest.get("from") or "")
                created_at = str(latest.get("received_at") or latest.get("created_at") or "")
                content = str(latest.get("body") or latest.get("text") or "")
                html_content = str(latest.get("html") or "")
                code = _extract_verification_code(content, html_content, subject)

                info.update({
                    "ok": True,
                    "mail_id": str(latest.get("id") or ""),
                    "subject": subject,
                    "from": from_addr,
                    "created_at": created_at,
                    "content": content,
                    "html_content": html_content,
                    "verification_code": code,
                    "error": "",
                })
                return info

            if provider == "xiaohei":
                url = verify_url or _XIAOHEI_VERIFY_MAP.get(email.strip().lower(), "") or _XIAOHEI_VERIFY_MAP.get("", "")
                if not url:
                    info["error"] = f"小黑账号 {email} 未配置 verify_url，请在 XIAOHEI_ACCOUNTS_FILE 或 XIAOHEI_VERIFY_URL 中设置"
                    return info
                if not _XIAOHEI_AVAILABLE:
                    info["error"] = "xiaohei_mail 模块不可用，请确认 xiaohei_mail.py 在同目录"
                    return info
                code = await _xh_wait_code(
                    url,
                    max_retries=20,
                    interval=3.0,
                    timeout=15,
                    proxy=HTTP_PROXY or "",
                )
                if code:
                    info.update({
                        "ok": True,
                        "subject": "Microsoft 验证码邮件（小黑平台）",
                        "from": "Microsoft",
                        "verification_code": code,
                        "content": f"验证码: {code}",
                        "error": "",
                    })
                else:
                    info["error"] = "小黑平台轮询超时，未获取到验证码"
                return info

            info["error"] = f"不支持的邮箱提供商: {provider}"
            return info
    except Exception as exc:
        info["error"] = str(exc)
        return info


def _preview_text(text: str, max_len: int = 160) -> str:
    cleaned = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return cleaned if len(cleaned) <= max_len else cleaned[:max_len] + "..."


# ──────────────────────────────────────────────────────────────
#  初始化持久化浏览器 context
# ──────────────────────────────────────────────────────────────
async def _configure_context(context) -> None:
    await context.add_init_script(script=_GM_POLYFILL)
    await context.add_init_script(script=_gm_config_script())
    await context.add_init_script(script=_load_userscript())


# ──────────────────────────────────────────────────────────────
#  创建已注入 GM / userscript / 桥接能力的页面
# ──────────────────────────────────────────────────────────────
async def _create_scripted_page(
    context,
    viewport: dict,
    gm_store_file: Path,
    logger: RequestLogger | None = None,
    log: Callable[[str], None] = print,
) -> tuple:
    # API 代理：通过 expose_function 桥接 JS→Python（绕过 CORS / Service Worker）
    # 域名→API Key 映射，确保 localStorage.clear() 后仍使用正确密钥
    _api_key_map = {
        "mail.chatgpt.org.uk":    GPTMAIL_API_KEY,
        "moemail.nanohajimi.mom": NPCMAIL_API_KEY,
    }

    async def _pw_http_request(req_json: str) -> str:
        """在 Python 层发送 HTTP 请求，结果返回给浏览器 JS"""
        opts = json.loads(req_json)
        url = opts["url"]
        log(f"  [bridge] {opts.get('method','GET')} {url}")
        if logger:
            logger.log({
                "type": "bridge_request",
                "method": opts.get("method", "GET"),
                "url": url,
                "headers": opts.get("headers", {}),
                "data": opts.get("data"),
            })
        try:
            headers = {k: v for k, v in opts.get("headers", {}).items()}
            # 注入正确的 API Key（JS 端 localStorage 被清除后会丢失密钥）
            for domain, key in _api_key_map.items():
                if domain in url:
                    headers["X-API-Key"] = key
                    break

            proxy = HTTP_PROXY or None
            async with httpx.AsyncClient(timeout=opts.get("timeout", 15000) / 1000, proxy=proxy) as client:
                resp = await client.request(
                    method  = opts.get("method", "GET"),
                    url     = url,
                    headers = headers,
                    content = (opts["data"].encode() if opts.get("data") else b""),
                )
            if logger:
                logger.log({
                    "type": "bridge_response",
                    "url": url,
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp.text[:RequestLogger._BODY_LIMIT],
                })
            return json.dumps({
                "status":          resp.status_code,
                "statusText":      resp.reason_phrase,
                "responseText":    resp.text,
                "response":        resp.text,
                "responseHeaders": "\r\n".join(f"{k}: {v}" for k, v in resp.headers.items()),
            })
        except Exception as e:
            log(f"  [bridge-err] {e}")
            if logger:
                logger.log({"type": "bridge_error", "url": url, "error": str(e)})
            return json.dumps({"error": str(e)})

    page = await context.new_page()
    await page.set_viewport_size(viewport)
    await page.expose_function("__pw_http_request", _pw_http_request)

    # Python 端保存所有 GM_setValue 的值，导航到新域名后回写到 localStorage
    _gm_store = _load_persisted_gm_store(gm_store_file)
    _save_persisted_gm_store(_gm_store, gm_store_file)

    async def _pw_gm_set(key: str, value: str) -> None:
        if value == "__DELETED__":
            _gm_store.pop(key, None)
        else:
            _gm_store[key] = value
        _save_persisted_gm_store(_gm_store, gm_store_file)

    await page.expose_function("__pw_gm_set", _pw_gm_set)

    async def _on_load(page_obj):
        if not _gm_store:
            return
        pairs = [
            f"try{{localStorage.setItem({json.dumps(k)},{json.dumps(v)});}}catch(_){{}}"
            for k, v in _gm_store.items()
        ]
        try:
            await page_obj.evaluate("()=>{" + "".join(pairs) + "}")
        except Exception:
            pass

    page.on("load", lambda: asyncio.ensure_future(_on_load(page)))

    _dialog_msgs: list[str] = []

    async def _on_dialog(dialog):
        msg = dialog.message
        _dialog_msgs.append(msg)
        log(f"  [dialog:{dialog.type}] {msg}")
        await dialog.dismiss()

    page.on("dialog", _on_dialog)
    page.on("console", lambda m: (
        log(f"  [JS] {m.text}")
        if m.type in ("log", "info") and m.text.strip()
        else None
    ))
    page.on("pageerror", lambda e: log(f"  [JS-ERR] {e}"))

    # 浏览器网络请求记录（仅 Sentinel + OpenAI Auth）
    if logger:
        async def _on_request(request):
            if not any(d in request.url for d in _CAPTURE_DOMAINS):
                return
            try:
                buf = request.post_data_buffer
                if buf is None:
                    post_data = None
                else:
                    try:
                        post_data = buf.decode("utf-8")
                    except UnicodeDecodeError:
                        import base64
                        post_data = f"<binary base64>{base64.b64encode(buf).decode()}"
            except Exception:
                post_data = None
            logger.log({
                "type": "browser_request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
                "post_data": post_data,
            })

        async def _on_response(response):
            if not any(d in response.url for d in _CAPTURE_DOMAINS):
                return
            record: dict = {
                "type": "browser_response",
                "status": response.status,
                "url": response.url,
                "headers": dict(response.headers),
            }
            try:
                # 只对 XHR/Fetch 请求读取响应体，跳过文档导航等资源
                # 避免在页面跳转时 body() 调用与导航冲突
                req_type = response.request.resource_type
                if req_type in ("xhr", "fetch", "other"):
                    ctype = response.headers.get("content-type", "")
                    if any(t in ctype for t in ("text", "json", "javascript", "xml")):
                        body = await response.body()
                        record["body"] = body.decode("utf-8", errors="replace")[:RequestLogger._BODY_LIMIT]
            except Exception:
                pass
            logger.log(record)

        page.on("request", lambda r: asyncio.ensure_future(_on_request(r)))
        page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

    return page, _dialog_msgs


# ──────────────────────────────────────────────────────────────
#  保持浏览器打开，直到用户手动关闭页面/窗口
# ──────────────────────────────────────────────────────────────
async def _wait_for_manual_close(page: Page, log: Callable[[str], None] = print) -> None:
    log("  [!] 浏览器将保持打开状态，手动关闭页面或浏览器窗口即可结束")
    while not page.is_closed():
        await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────
#  注册成功后退出登录（清除 session cookies）
# ──────────────────────────────────────────────────────────────
_SESSION_COOKIE_NAMES = frozenset([
    "__Secure-next-auth.session-token",
    "__Secure-next-auth.session-token.0",
    "__Secure-next-auth.session-token.1",
    "__Secure-next-auth.session-token.2",
])

async def _logout_session(context, log: Callable[[str], None] = print) -> None:
    """删除 ChatGPT session cookies，使当前登录态失效，为下次注册做准备"""
    log("  → 退出登录（清除 session cookies）...")
    try:
        all_cookies = await context.cookies()
        kept = [c for c in all_cookies if c["name"] not in _SESSION_COOKIE_NAMES]
        await context.clear_cookies()
        if kept:
            await context.add_cookies(kept)
        removed = len(all_cookies) - len(kept)
        log(f"  → 已退出登录（清除 {removed} 条 session cookie）")
    except Exception as exc:
        log(f"  [!] 退出登录失败: {exc}")


# ──────────────────────────────────────────────────────────────
#  仅打开登录页，不执行后续自动注册
# ──────────────────────────────────────────────────────────────
async def _open_login_only(
    browser,
    gm_store_file: Path,
    log: Callable[[str], None] = print,
) -> None:
    page, _ = await _create_scripted_page(
        browser,
        {"width": 1440, "height": 900},
        gm_store_file,
        log=log,
    )

    try:
        log("  → 仅打开登录页模式")
        log(f"  → 打开: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

        # setInterval 需约 1500ms 才会创建 .tm-fab，等最多 5s
        fab_found = False
        try:
            await page.wait_for_selector(".tm-fab", timeout=5_000)
            fab_found = True
        except Exception:
            pass

        if not fab_found:
            # 备用：add_init_script 在某些持久化 Profile 或重定向场景下不会重注入，
            # 改为通过 evaluate 直接在当前页面执行，完全绕过 CSP 限制
            log("  → add_init_script 未生效，直接注入 rpalogin.js...")
            try:
                await page.evaluate(_GM_POLYFILL)
                await page.evaluate(_gm_config_script())
                await page.evaluate(f"(function(){{{_load_userscript()}}})()")
                await page.wait_for_selector(".tm-fab", timeout=5_000)
                fab_found = True
            except Exception as exc:
                log(f"  [!] 直接注入失败: {exc}")

        if fab_found:
            log("  → rpalogin.js 已加载")
        else:
            log("  [!] 未检测到 .tm-fab，rpalogin.js 可能未成功显示")

        log("  → 已打开登录页，不执行后续自动化")
        await _wait_for_manual_close(page, log=log)
    finally:
        if not page.is_closed():
            await page.close()


# ──────────────────────────────────────────────────────────────
#  单账号注册流程
# ──────────────────────────────────────────────────────────────
async def _register_one(
    context,
    index: int,
    gm_store_file: Path,
    *,
    keep_failed_open: bool = True,
) -> dict:
    log = _make_account_logger(index)
    sep = "─" * 52
    log("")
    log(sep)
    log(f"  注册账号 #{index + 1}")
    log(sep)

    # 每次注册随机分辨率
    base_w, base_h = random.choice(_RESOLUTIONS)
    viewport = {"width": base_w, "height": base_h - random.randint(0, 80)}

    logger = RequestLogger()

    # 复用持久化浏览器 profile，保留缓存 / 登录态 / 脚本历史数据
    page, _dialog_msgs = await _create_scripted_page(
        context,
        viewport,
        gm_store_file,
        logger,
        log=log,
    )

    result: dict = {"ok": False, "email": "", "password": "", "error": "unknown"}
    try:
        log(f"  → 打开 {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)

        # 检测 Cloudflare Turnstile 验证（如有则自动点击）
        cf_iframe = await page.query_selector("iframe[src*='challenges.cloudflare.com']")
        if cf_iframe:
            await _handle_turnstile(page, timeout=30_000, log=log)
            await page.wait_for_timeout(3_000)

        log("  → 点击「自动注册」...")
        ok = await _trigger_registration(page)
        if not ok:
            log("  [!] 首次未找到按钮，3s 后重试 ...")
            await page.wait_for_timeout(3_000)
            ok = await _trigger_registration(page)

        if not ok:
            result = {"ok": False, "error": "无法触发注册按钮，userscript 未载入"}
        else:
            log(f"  → 等待注册完成（最多 {TIMEOUT_SECS}s）...")
            result = await _wait_for_result(page, TIMEOUT_SECS, _dialog_msgs, log=log)
            if result.get("ok") and result.get("email"):
                # 注册成功弹窗出现后，再做一次最终页面健康检查：
                # 若落入 OpenAI 错误页（如 unsupported_email），按失败处理并抛弃结果。
                await page.wait_for_timeout(2_000)
                has_error, reason = await _detect_final_error_state(page)
                if has_error:
                    result = {
                        "ok": False,
                        "email": "",
                        "password": "",
                        "error": f"注册后落入错误页: {reason}",
                    }
                    log(f"  [!] 注册后页面异常，判定失败: {reason}")
                else:
                    log("  → 主动查询最近邮件...")
                    _vurl = _XIAOHEI_VERIFY_MAP.get(result["email"].strip().lower(), "") if EMAIL_PROVIDER == "xiaohei" else ""
                    latest_mail = await _fetch_latest_email_info(result["email"], EMAIL_PROVIDER, verify_url=_vurl)
                    result["latest_mail"] = latest_mail
                    if latest_mail.get("ok"):
                        subject = latest_mail.get("subject") or "(无主题)"
                        log(f"  → 最近邮件主题: {subject}")
                        code = latest_mail.get("verification_code") or ""
                        if code:
                            log(f"  → 最近邮件验证码: {code}")
                        else:
                            snippet = _preview_text(
                                latest_mail.get("content") or latest_mail.get("html_content") or ""
                            )
                            if snippet:
                                log(f"  → 最近邮件摘要: {snippet}")
                    else:
                        log(f"  [!] 最近邮件查询失败: {latest_mail.get('error', '')}")

    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        log(f"  [exception] {exc}")

    # 结束后保持浏览器打开，让用户手动操作/查看
    if keep_failed_open:
        if result.get("ok"):
            log("")
            log("  ✓ 注册成功，浏览器保持打开，你可以手动查看")
        else:
            log("")
            log("  [!] 注册失败，浏览器保持打开，你可以手动操作")
        await _wait_for_manual_close(page, log=log)

    if not page.is_closed():
        await page.close()

    # 注册成功后退出登录（页面关闭后再清除 cookie，避免干扰注册中的页面刷新）
    if result.get("ok"):
        await _logout_session(context, log=log)

    label = f"account{index + 1}"
    if result.get("email"):
        safe = re.sub(r"[^\w@.-]", "_", result["email"])
        label = f"account{index + 1}_{safe}"
    logger.save(label, log=log)

    return result


async def _run_one_account(
    pw,
    index: int,
    *,
    use_temp_profile: bool,
    keep_failed_open: bool,
) -> dict:
    temp_profile = None
    context = None
    log = _make_account_logger(index)

    try:
        if use_temp_profile:
            temp_profile = tempfile.TemporaryDirectory(
                dir=str(ROOT),
                prefix=f"fresh-profile-{index + 1:02d}-",
            )
            profile_dir = Path(temp_profile.name)
            gm_store_file = profile_dir / ".tm-gm-store.json"
        else:
            PROFILE_DIR.mkdir(exist_ok=True)
            profile_dir = PROFILE_DIR
            gm_store_file = GM_STORE_FILE

        log(f"  浏览器目录: {profile_dir}")
        log(f"  GM 数据文件: {gm_store_file}")

        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **_launch_options(),
        )
        await _configure_context(context)
        return await _register_one(
            context,
            index,
            gm_store_file,
            keep_failed_open=keep_failed_open,
        )
    except Exception as exc:
        log(f"  [exception] 启动失败: {exc}")
        return {"ok": False, "email": "", "password": "", "error": str(exc)}
    finally:
        if context is not None:
            await context.close()
        if temp_profile is not None:
            temp_profile.cleanup()


async def _run_batch(
    pw,
    count: int,
    *,
    concurrency: int,
    use_temp_profile: bool,
) -> list[dict]:
    results: list[dict | None] = [None] * count
    semaphore = asyncio.Semaphore(concurrency)

    async def _worker(index: int) -> None:
        async with semaphore:
            if concurrency > 1:
                await asyncio.sleep(random.uniform(0.0, 1.5))
            result = await _run_one_account(
                pw,
                index,
                use_temp_profile=use_temp_profile,
                keep_failed_open=(concurrency == 1),
            )
            results[index] = result
            log = _make_account_logger(index)
            if result.get("ok"):
                log(f"  ✓  邮箱: {result['email']}  密码: {result['password']}")
            else:
                log(f"  ✗  失败: {result.get('error', '')}")

    await asyncio.gather(*(_worker(i) for i in range(count)))
    return [r or {"ok": False, "email": "", "password": "", "error": "unknown"} for r in results]


# ──────────────────────────────────────────────────────────────
#  主入口
# ──────────────────────────────────────────────────────────────
async def _main(
    count: int,
    open_only: bool = False,
    fresh: bool = False,
    concurrency: int = 1,
) -> None:
    if not USERSCRIPT.exists():
        print(f"[!] 找不到 userscript: {USERSCRIPT}")
        sys.exit(1)

    count = max(1, count)
    concurrency = max(1, min(concurrency, count))
    use_temp_profile = fresh or concurrency > 1

    print("=" * 52)
    print(f"  ChatGPT Auto-Register")
    print(f"  账号数: {count}  |  邮箱: {EMAIL_PROVIDER}  |  无头: {HEADLESS}")
    if EMAIL_PROVIDER == "xiaohei":
        account_count = len([k for k in _XIAOHEI_VERIFY_MAP if k])
        single_url = _XIAOHEI_VERIFY_MAP.get("", "")
        if account_count:
            print(f"  小黑账号文件: {XIAOHEI_ACCOUNTS_FILE}（已加载 {account_count} 条）")
        elif single_url:
            print(f"  小黑 verify_url: {single_url[:60]}...")
        else:
            print("  [!] 小黑平台未配置 verify_url，请设置 XIAOHEI_VERIFY_URL 或 XIAOHEI_ACCOUNTS_FILE")
    if concurrency > 1:
        print(f"  并发: {concurrency}（本地上限默认 {DEFAULT_CONCURRENCY}）")
        print("  模式: concurrent 并发独立 profile")
    else:
        print(f"  模式: {'fresh 临时无痕' if fresh else 'persistent 默认持久化'}")
    if concurrency == 1 and not use_temp_profile:
        PROFILE_DIR.mkdir(exist_ok=True)
        print(f"  浏览器目录: {PROFILE_DIR}")
        print(f"  GM 数据文件: {GM_STORE_FILE}")
    elif concurrency == 1 and use_temp_profile:
        print("  浏览器目录: 运行时创建临时目录")
        print("  GM 数据文件: 跟随临时 profile 自动创建")
    else:
        print("  浏览器目录: 每个任务独立临时目录")
        print("  GM 数据文件: 每个任务独立临时文件")
    print("=" * 52)

    results: list[dict] = []

    async with async_playwright() as pw:
        if open_only:
            temp_profile = None
            context = None
            try:
                if use_temp_profile:
                    temp_profile = tempfile.TemporaryDirectory(
                        dir=str(ROOT),
                        prefix="fresh-profile-open-",
                    )
                    profile_dir = Path(temp_profile.name)
                    gm_store_file = profile_dir / ".tm-gm-store.json"
                else:
                    profile_dir = PROFILE_DIR
                    gm_store_file = GM_STORE_FILE

                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    **_launch_options(),
                )
                await _configure_context(context)
                await _open_login_only(context, gm_store_file)
            finally:
                if context is not None:
                    await context.close()
                if temp_profile is not None:
                    temp_profile.cleanup()
            return

        results = await _run_batch(
            pw,
            count,
            concurrency=concurrency,
            use_temp_profile=use_temp_profile,
        )

    # 汇总
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n{'═' * 52}")
    print(f"  结果汇总: {ok_count}/{count} 成功")
    print("═" * 52)
    for i, r in enumerate(results):
        mark = "✓" if r["ok"] else "✗"
        info = f"{r['email']}  /  {r['password']}" if r["ok"] else r.get("error", "")
        print(f"  {mark}  #{i + 1}  {info}")


async def _query_email_and_print(email: str) -> None:
    print("=" * 52)
    print("  邮件主动查询")
    print(f"  邮箱: {email}  |  提供商: {EMAIL_PROVIDER}")
    print("=" * 52)

    info = await _fetch_latest_email_info(email, EMAIL_PROVIDER)
    if not info.get("ok"):
        print(f"\n  ✗ 查询失败: {info.get('error', '')}")
        return

    print("\n  ✓ 查询成功")
    print(f"  主题: {info.get('subject') or '(无主题)'}")
    print(f"  发件人: {info.get('from') or '(未知)'}")
    print(f"  时间: {info.get('created_at') or '(未知)'}")
    if info.get("verification_code"):
        print(f"  验证码: {info.get('verification_code')}")
    else:
        print(f"  摘要: {_preview_text(info.get('content') or info.get('html_content') or '')}")


def _parse_args() -> tuple[int, bool, str, bool, int]:
    parser = argparse.ArgumentParser(description="ChatGPT 自动注册 / 按邮箱查询最近邮件")
    parser.add_argument("count", nargs="?", type=int, default=1, help="注册账号数量，默认 1")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不执行自动注册")
    parser.add_argument("--mail", dest="query_email", default="", help="按邮箱查询最近邮件并退出")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="使用临时独立 profile 与临时 GM 数据文件，适合并发或无痕运行",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"最大并发数，默认 {DEFAULT_CONCURRENCY}",
    )
    args = parser.parse_args(sys.argv[1:])
    return args.count, args.open_only, (args.query_email or "").strip(), args.fresh, args.concurrency


def main() -> None:
    count, open_only, query_email, fresh, concurrency = _parse_args()
    if query_email:
        asyncio.run(_query_email_and_print(query_email))
        return
    asyncio.run(_main(count, open_only=open_only, fresh=fresh, concurrency=concurrency))


if __name__ == "__main__":
    main()
