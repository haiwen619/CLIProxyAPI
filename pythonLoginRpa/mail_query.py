#!/usr/bin/env python3
"""
mail_query.py — 独立邮件查询模块
支持 GPTMail 和 NPCMail 两种提供商。

用法（命令行）:
    python mail_query.py xxx@yyy.com
    python mail_query.py xxx@yyy.com --provider npcmail
    python mail_query.py xxx@yyy.com --json

用法（库调用）:
    import asyncio
    from mail_query import fetch_latest_email_info

    info = asyncio.run(fetch_latest_email_info("xxx@yyy.com"))
    print(info["verification_code"])
"""

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import quote

import httpx

# ═══════════════════════════ 配置区 ═══════════════════════════
NPCMAIL_API_KEY = "sk-hvvr8yimqCKm"
NPCMAIL_BASE    = "https://moemail.nanohajimi.mom"

GPTMAIL_API_KEY = "sk-hvvr8yimqCKm"
GPTMAIL_BASE    = "https://mail.chatgpt.org.uk"

EMAIL_PROVIDER  = "gptmail"          # "npcmail" | "gptmail"
HTTP_PROXY      = "http://127.0.0.1:6987"   # 本地代理，设为 "" 则不使用
MAIL_FETCH_SECS = 15                 # 请求超时秒数
# ═════════════════════════════════════════════════════════════


# ──────────────────────────────────────────────────────────────
#  内部辅助
# ──────────────────────────────────────────────────────────────
def _extract_verification_code(*texts: str) -> str:
    """从若干段文本中提取第一个 6 位（或 4~8 位）数字验证码。"""
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


def _preview_text(text: str, max_len: int = 160) -> str:
    cleaned = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return cleaned if len(cleaned) <= max_len else cleaned[:max_len] + "..."


# ──────────────────────────────────────────────────────────────
#  公开接口
# ──────────────────────────────────────────────────────────────
async def fetch_latest_email_info(
    email: str,
    provider: str | None = None,
    *,
    api_key: str | None = None,
    proxy: str | None = None,
    timeout: float | None = None,
) -> dict:
    """
    查询指定邮箱的最新一封邮件。

    参数:
        email     — 邮箱地址
        provider  — "gptmail"（默认）或 "npcmail"
        api_key   — 覆盖配置区的 API Key（可选）
        proxy     — HTTP 代理，如 "http://127.0.0.1:7890"；None 则用全局配置
        timeout   — 请求超时秒数；None 则用全局配置

    返回 dict:
        ok                bool    是否成功
        provider          str
        email             str
        mail_id           str
        subject           str
        from              str     发件人
        created_at        str
        content           str     纯文本正文
        html_content      str     HTML 正文
        verification_code str     提取到的 6 位验证码（未找到则为空串）
        error             str     失败时的错误描述
    """
    provider = (provider or EMAIL_PROVIDER or "gptmail").strip().lower()
    _proxy   = proxy if proxy is not None else (HTTP_PROXY or None)
    _timeout = timeout if timeout is not None else MAIL_FETCH_SECS

    info: dict = {
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
        async with httpx.AsyncClient(timeout=_timeout, proxy=_proxy) as client:

            # ── GPTMail ──────────────────────────────────────────
            if provider == "gptmail":
                key     = api_key or GPTMAIL_API_KEY
                headers = {"X-API-Key": key, "Content-Type": "application/json"}

                inbox_url = f"{GPTMAIL_BASE}/api/emails?email={quote(email, safe='')}"
                resp = await client.get(inbox_url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()

                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(payload.get("error") or "GPTMail 返回 success=false")

                data  = payload.get("data") if isinstance(payload, dict) else payload
                mails = _to_gptmail_array(data)
                if not mails:
                    info["error"] = "该邮箱暂无邮件"
                    return info

                latest  = mails[0]
                mail_id = str(latest.get("id") or "")
                detail  = latest

                if mail_id:
                    detail_url  = f"{GPTMAIL_BASE}/api/email/{quote(mail_id, safe='')}"
                    det_resp    = await client.get(detail_url, headers=headers)
                    det_resp.raise_for_status()
                    det_payload = det_resp.json()
                    if isinstance(det_payload, dict) and det_payload.get("success") is False:
                        raise RuntimeError(det_payload.get("error") or "GPTMail 详情接口返回 success=false")
                    det_data = det_payload.get("data") if isinstance(det_payload, dict) else det_payload
                    if isinstance(det_data, dict):
                        detail = det_data

                subject  = str(detail.get("subject")  or latest.get("subject")  or "")
                from_addr = str(
                    detail.get("from_address") or detail.get("from")
                    or latest.get("from_address") or latest.get("from") or ""
                )
                created_at = str(
                    detail.get("created_at") or detail.get("received_at")
                    or latest.get("created_at") or latest.get("received_at") or ""
                )
                content      = str(detail.get("content")      or latest.get("content")      or "")
                html_content = str(detail.get("html_content") or latest.get("html_content") or "")
                code = _extract_verification_code(content, html_content, subject)

                info.update({
                    "ok": True, "mail_id": mail_id, "subject": subject,
                    "from": from_addr, "created_at": created_at,
                    "content": content, "html_content": html_content,
                    "verification_code": code, "error": "",
                })
                return info

            # ── NPCMail ──────────────────────────────────────────
            if provider == "npcmail":
                key     = api_key or NPCMAIL_API_KEY
                headers = {"X-API-Key": key, "Content-Type": "application/json"}

                inbox_url = f"{NPCMAIL_BASE}/api/public/emails/{quote(email, safe='')}/messages"
                resp = await client.get(inbox_url, headers=headers)
                resp.raise_for_status()
                mails = _to_npcmail_array(resp.json())
                if not mails:
                    info["error"] = "该邮箱暂无邮件"
                    return info

                latest     = mails[0]
                subject    = str(latest.get("subject") or "")
                from_addr  = str(latest.get("sender") or latest.get("from") or "")
                created_at = str(latest.get("received_at") or latest.get("created_at") or "")
                content      = str(latest.get("body") or latest.get("text") or "")
                html_content = str(latest.get("html") or "")
                code = _extract_verification_code(content, html_content, subject)

                info.update({
                    "ok": True, "mail_id": str(latest.get("id") or ""),
                    "subject": subject, "from": from_addr, "created_at": created_at,
                    "content": content, "html_content": html_content,
                    "verification_code": code, "error": "",
                })
                return info

            info["error"] = f"不支持的邮箱提供商: {provider}"
            return info

    except Exception as exc:
        info["error"] = str(exc)
        return info


# ──────────────────────────────────────────────────────────────
#  命令行入口
# ──────────────────────────────────────────────────────────────
async def _main_async() -> None:
    parser = argparse.ArgumentParser(description="查询临时邮箱最新邮件及验证码")
    parser.add_argument("email", help="邮箱地址，如 abc@example.com")
    parser.add_argument(
        "--provider", default=EMAIL_PROVIDER,
        choices=["gptmail", "npcmail"],
        help=f"邮件提供商（默认 {EMAIL_PROVIDER}）",
    )
    parser.add_argument("--api-key", default="", help="覆盖配置中的 API Key")
    parser.add_argument("--proxy",   default="", help="HTTP 代理，如 http://127.0.0.1:7890")
    parser.add_argument("--json",    action="store_true", help="以 JSON 格式输出完整结果")
    args = parser.parse_args()

    info = await fetch_latest_email_info(
        args.email,
        args.provider,
        api_key=args.api_key or None,
        proxy=args.proxy or None,
    )

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    print("=" * 52)
    print(f"  邮箱: {args.email}  |  提供商: {info['provider']}")
    print("=" * 52)
    if not info["ok"]:
        print(f"\n  ✗ 查询失败: {info['error']}")
        sys.exit(1)

    print(f"\n  ✓ 查询成功")
    print(f"  主题  : {info['subject'] or '(无主题)'}")
    print(f"  发件人: {info['from'] or '(未知)'}")
    print(f"  时间  : {info['created_at'] or '(未知)'}")
    if info["verification_code"]:
        print(f"  验证码: {info['verification_code']}")
    else:
        print(f"  摘要  : {_preview_text(info['content'] or info['html_content'])}")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
