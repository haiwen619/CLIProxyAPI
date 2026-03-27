"""
Codex OAuth 批量登录工具
通过 CLIProxyAPI 管理 API + Playwright 实现 Codex OAuth 自动化批量登录。

每个账号的执行流程：
  1. GET  /v0/management/codex-auth-url?is_webui=true  → 获取授权 URL 和 state
  2. Playwright 打开授权 URL，自动填写账号密码，完成授权
  3. 服务端启动 localhost:1455 回调转发器，并完成 code → token 交换
  4. 轮询 /v0/management/get-auth-status              → 等待认证文件写入 auth-dir

使用方法：
  python codex_batch_login.py              # 使用默认的 config.json
  python codex_batch_login.py my.json      # 使用自定义配置文件

依赖安装：
  pip install -r requirements.txt
  playwright install chromium

可选（支持 TOTP/2FA 账号）：
  pip install pyotp
"""

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

import httpx
try:
    from patchright.async_api import BrowserContext, Page, async_playwright
except ImportError:
    from playwright.async_api import BrowserContext, Page, async_playwright


_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768), (1536, 864),
    (1600, 900), (1280, 720), (1440, 900), (1680, 1050),
]


def _browser_channel_candidates(preferred: str = "") -> list[str | None]:
    preferred = (preferred or "").strip().lower()
    if preferred in {"chromium", "builtin", "playwright"}:
        return [None]
    if preferred in {"chrome", "msedge"}:
        return [preferred, None]
    return ["chrome", "msedge", None]


def _proxy_bypass_rules() -> str:
    return "<-loopback>;localhost;127.0.0.1;::1"


def _launch_args(disable_system_proxy: bool) -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-quic",
        "--disable-features=UseDnsHttpsSvcb",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--disable-infobars",
        "--hide-scrollbars",
        f"--proxy-bypass-list={_proxy_bypass_rules()}",
    ]
    if disable_system_proxy:
        args.append("--no-proxy-server")
    if sys.platform.startswith("linux"):
        args.extend([
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ])
    return args

# ─────────────────────────────────────────────────────────────
#  管理 API 客户端
# ─────────────────────────────────────────────────────────────

class ManagementClient:
    """CLIProxyAPI 管理端点的封装客户端。"""

    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def get_codex_auth_url(self) -> dict:
        """
        向代理服务器发起一个新的 Codex OAuth 会话。
        返回格式：{"status": "ok", "url": "https://auth.openai.com/...", "state": "..."}

        默认开启 is_webui=true，与管理页原生 OAuth 登录保持一致，
        由服务器启动 1455 本地回调转发器并完成 code -> token 交换。
        """
        url = f"{self.base}/v0/management/codex-auth-url"
        print(f"    [D] GET {url}")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await client.get(url, headers=self.headers, params={"is_webui": "true"})
            print(f"    [D] 响应状态码：{r.status_code}")
            r.raise_for_status()
            return r.json()

    async def get_auth_status(self, state: str) -> dict:
        """轮询服务端 OAuth 会话状态。"""
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await client.get(
                f"{self.base}/v0/management/get-auth-status",
                headers=self.headers,
                params={"state": state},
            )
            r.raise_for_status()
            return r.json()

async def _fill_openai_login(
    page: Page,
    email: str,
    password: str,
    totp_secret: str = "",
) -> None:
    """
    自动填写 OpenAI / auth.openai.com 登录表单。
    使用多个备用选择器，以兼容页面结构变化。
    """

    # ── 第一步：填写邮箱 ──
    # 实际页面：<input name="email" type="email" autocomplete="email" ...>
    email_selectors = [
        'input[name="email"]',
        'input[autocomplete="email"]',
        'input[type="email"]',
    ]
    filled = False
    for sel in email_selectors:
        try:
            print(f"    [D] 尝试邮箱选择器：{sel}")
            await page.wait_for_selector(sel, timeout=10_000, state="visible")
            await page.fill(sel, email)
            filled = True
            print(f"    [D] 邮箱已填写（{sel}）")
            break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("未找到邮箱输入框，页面结构可能已更新")

    # 填写邮箱后随机停顿，避免点击节奏过于机械
    await asyncio.sleep(random.uniform(1.0, 2.6))

    # 点击邮箱提交按钮，并等待页面跳转到密码页
    print("    [D] 点击提交（邮箱）…")
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
        await page.click('button[type="submit"][value="email"], button[type="submit"]')
    print(f"    [D] 跳转后页面 URL：{page.url}")

    # 等待密码框渲染稳定
    await asyncio.sleep(random.uniform(0.8, 1.5))

    # ── 第二步：填写密码 ──
    # 密码页 HTML 结构待确认，先用通用选择器兜底
    pwd_selectors = [
        'input[name="password"]',
        'input[type="password"]',
        'input[autocomplete="current-password"]',
    ]
    filled = False
    for sel in pwd_selectors:
        try:
            print(f"    [D] 尝试密码选择器：{sel}")
            await page.wait_for_selector(sel, timeout=10_000, state="visible")
            await page.fill(sel, password)
            filled = True
            print(f"    [D] 密码已填写（{sel}）")
            break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("未找到密码输入框")

    # 填写后停顿，模拟人工操作
    await asyncio.sleep(random.uniform(1.0, 2.0))

    # 点击密码提交按钮，等待后续跳转（可能是 2FA 或 OAuth 回调）
    print("    [D] 点击提交（密码）…")
    await page.click('button[type="submit"]')

    # ── 第三步：TOTP / 双重认证（可选）──
    if totp_secret.strip():
        try:
            import pyotp  # pip install pyotp

            code = pyotp.TOTP(totp_secret.strip()).now()
            await page.wait_for_selector(
                'input[name="code"], input[autocomplete="one-time-code"]',
                timeout=8_000,
            )
            await page.fill(
                'input[name="code"], input[autocomplete="one-time-code"]', code
            )
            await page.click('button[type="submit"]')
        except ImportError:
            print("    [!] 未安装 pyotp，跳过 TOTP（pip install pyotp）")
        except Exception:
            pass  # 部分账号不需要 2FA，忽略异常


async def _handle_email_verification(
    page,
    email: str,
    mail_provider: str = "",
    proxy_url: str = "",
    max_wait: int = 90,
) -> None:
    """
    检测到 auth.openai.com/email-verification 页时，
    轮询邮件获取验证码并自动填入提交。
    """
    try:
        from mail_query import fetch_latest_email_info
    except ImportError:
        raise RuntimeError("缺少 mail_query.py，无法自动获取邮件验证码")

    print("    [D] 检测到邮件验证步骤，开始轮询验证码…")
    code = ""
    for attempt in range(max_wait // 5):
        await asyncio.sleep(5)
        info = await fetch_latest_email_info(
            email,
            provider=mail_provider or None,
            proxy=proxy_url or None,
        )
        code = info.get("verification_code", "")
        if code:
            print(f"    [D] 获取到验证码：{code}")
            break
        print(f"    [D] 等待验证码…（已等待 {(attempt + 1) * 5}s）")

    if not code:
        raise RuntimeError(f"等待邮件验证码超时（{max_wait}s）")

    code_selectors = [
        'input[autocomplete="one-time-code"]',
        'input[name="code"]',
        'input[type="text"]',
    ]
    filled = False
    for sel in code_selectors:
        try:
            await page.wait_for_selector(sel, timeout=5_000, state="visible")
            await page.fill(sel, code)
            filled = True
            print(f"    [D] 验证码已填入（{sel}）")
            break
        except Exception:
            continue

    if not filled:
        raise RuntimeError("未找到验证码输入框，页面结构可能已更新")

    await page.click('button[type="submit"]')
    print("    [D] 验证码已提交")


async def browser_login_and_complete(
    client: ManagementClient,
    state: str,
    auth_url: str,
    email: str,
    password: str,
    totp_secret: str = "",
    headless: bool = True,
    timeout_sec: int = 60,
    proxy_url: str = "",
    mail_provider: str = "",
    browser_channel: str = "",
    inherit_system_proxy: bool = False,
) -> bool:
    """
    在 Chromium 浏览器中打开授权 URL，自动完成登录，
    然后等待服务端完成 OAuth 回调处理。

    登录成功时返回 True，失败返回 False。
    """
    base_w, base_h = random.choice(_RESOLUTIONS)
    viewport = {"width": base_w, "height": base_h - random.randint(0, 80)}

    print(f"    [D] 启动 Chromium，headless={headless}，分辨率={viewport['width']}x{viewport['height']}")
    async with async_playwright() as pw:
        proxy_settings = (
            {"server": proxy_url, "bypass": "localhost,127.0.0.1,::1"}
            if proxy_url else None
        )
        disable_system_proxy = not proxy_url and not inherit_system_proxy
        temp_profile = tempfile.TemporaryDirectory(prefix="codex-login-profile-")
        context: BrowserContext | None = None
        selected_channel = "chromium"
        try:
            launch_options = {
                "headless": headless,
                "proxy": proxy_settings,
                "viewport": viewport,
                "locale": "zh-CN",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "args": _launch_args(disable_system_proxy),
            }

            launch_errors = []
            for candidate in _browser_channel_candidates(browser_channel):
                try:
                    options = dict(launch_options)
                    if candidate:
                        options["channel"] = candidate
                    context = await pw.chromium.launch_persistent_context(
                        user_data_dir=temp_profile.name,
                        **options,
                    )
                    selected_channel = candidate or "chromium"
                    break
                except Exception as launch_exc:
                    label = candidate or "chromium"
                    launch_errors.append(f"{label}: {launch_exc}")

            if context is None:
                raise RuntimeError("；".join(launch_errors) or "无法启动浏览器上下文")

            print(f"    [D] 浏览器已启动，channel={selected_channel}，profile={temp_profile.name}")
            if proxy_settings:
                print(f"    [D] 使用显式代理：{proxy_settings['server']}（已绕过 localhost/127.0.0.1）")
            elif disable_system_proxy:
                print("    [D] 未配置显式代理，已禁用系统代理继承")
            else:
                print("    [D] 未配置显式代理，沿用系统网络环境")

            page = context.pages[0] if context.pages else await context.new_page()
            print("    [D] 页面已创建，等待服务端 OAuth 状态…")

            print(f"    [D] 正在打开授权 URL…")
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=30_000)
            print(f"    [D] 页面加载完成（domcontentloaded），当前 URL：{page.url}")
            print("    [D] 开始填写登录表单…")
            await _fill_openai_login(page, email, password, totp_secret)
            print("    [D] 表单填写完毕，等待服务端完成 OAuth 回调…")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout_sec
            last_url = ""
            last_page_count = len(context.pages)
            email_verification_handled = False
            last_status_check = 0.0
            while True:
                if loop.time() > deadline:
                    # 超时后截图，方便排查页面卡在哪一步
                    shot = Path(__file__).parent / f"debug_{email.split('@')[0]}.png"
                    await page.screenshot(path=str(shot))
                    print(f"    [!] 等待超时，调试截图已保存：{shot.name}")
                    print(f"    [D] 超时时当前页面 URL：{page.url}")
                    return False
                if len(context.pages) != last_page_count:
                    last_page_count = len(context.pages)
                    print(f"    [D] 浏览器页签数量变化：{last_page_count}")
                    latest_page = next((p for p in reversed(context.pages) if not p.is_closed()), None)
                    if latest_page is not None and latest_page is not page:
                        page = latest_page
                        print(f"    [D] 已切换到最新页签，当前 URL：{page.url}")
                current_url = page.url
                if current_url != last_url:
                    print(f"    [D] 页面跳转 → {current_url}")
                    last_url = current_url
                    if "email-verification" in current_url and not email_verification_handled:
                        email_verification_handled = True
                        try:
                            await _handle_email_verification(page, email, mail_provider, proxy_url)
                        except Exception as ve:
                            print(f"    [!] 邮件验证失败：{ve}")
                            break
                    elif "codex/consent" in current_url:
                        try:
                            continue_button = page.locator(
                                'button[data-dd-action-name="Continue"], button[type="submit"]'
                            ).first
                            await continue_button.wait_for(timeout=5_000, state="visible")
                            await asyncio.sleep(random.uniform(0.8, 1.5))
                            await continue_button.click()
                            print("    [D] 已点击授权同意按钮")
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                                print(f"    [D] 点击同意后页面 URL：{page.url}")
                            except Exception:
                                print(f"    [D] 点击同意后仍在等待导航，当前 URL：{page.url}")
                        except Exception as ce:
                            print(f"    [!] 点击同意按钮失败：{ce}")
                if loop.time() - last_status_check >= 1.0:
                    last_status_check = loop.time()
                    try:
                        auth_status = await client.get_auth_status(state)
                    except Exception as status_exc:
                        print(f"    [D] OAuth 状态查询失败：{status_exc}")
                    else:
                        status = auth_status.get("status", "")
                        if status == "ok":
                            print("    [+] 服务端已完成 OAuth 回调处理")
                            return True
                        if status == "error":
                            print(f"    [!] 服务端 OAuth 回调失败：{auth_status.get('error', '未知错误')}")
                            return False
                await asyncio.sleep(0.4)

        except Exception as exc:
            print(f"    [!] 浏览器异常：{type(exc).__name__}: {exc}")
            shot = Path(__file__).parent / f"debug_{email.split('@')[0]}.png"
            try:
                await page.screenshot(path=str(shot))
                print(f"    [D] 异常截图已保存：{shot.name}")
            except Exception:
                pass
        finally:
            if context is not None:
                await context.close()
            temp_profile.cleanup()
            print("    [D] 浏览器已关闭")

    return False


# ─────────────────────────────────────────────────────────────
#  单账号登录流程
# ─────────────────────────────────────────────────────────────

async def login_one_account(
    client: ManagementClient,
    email: str,
    password: str,
    totp_secret: str = "",
    headless: bool = True,
    proxy_url: str = "",
    mail_provider: str = "",
    browser_channel: str = "",
    inherit_system_proxy: bool = False,
) -> bool:
    print(f"[*] {email}")

    try:
        # 第 1 步：向代理服务器申请 Codex 授权 URL
        data = await client.get_codex_auth_url()
        auth_url = data.get("url", "")
        if not auth_url:
            print(f"    [!] 未获取到授权 URL，响应内容：{data}")
            return False
        state_preview = data.get("state", "")[:8]
        print(f"    [>] 授权 URL 已获取（state={state_preview}...）")

        state = data.get("state", "").strip()
        if not state:
            print(f"    [!] 未获取到 OAuth state，响应内容：{data}")
            return False

        # 第 2 步：浏览器自动化登录，服务端在后台处理本地回调
        ok = await browser_login_and_complete(
            client=client,
            state=state,
            auth_url=auth_url,
            email=email,
            password=password,
            totp_secret=totp_secret,
            headless=headless,
            proxy_url=proxy_url,
            mail_provider=mail_provider,
            browser_channel=browser_channel,
            inherit_system_proxy=inherit_system_proxy,
        )
        if ok:
            print("    [+] 登录成功，凭证已保存")
            return True

        print("    [!] 登录失败，服务端未完成 OAuth 回调")
        return False

    except httpx.HTTPStatusError as exc:
        print(f"    [!] HTTP 错误 {exc.response.status_code}：{exc.response.text[:200]}")
        return False
    except Exception as exc:
        print(f"    [!] 未知异常：{type(exc).__name__}: {exc}")
        return False


# ─────────────────────────────────────────────────────────────
#  批量登录主流程
# ─────────────────────────────────────────────────────────────

async def batch_login(config: dict) -> None:
    client = ManagementClient(
        base_url=config["mgmt_url"],
        api_key=config["mgmt_key"],
    )
    headless = config.get("headless", True)
    proxy_url = config.get("proxy_url", "")
    mail_provider = config.get("mail_provider", "")
    browser_channel = config.get("browser_channel", "")
    inherit_system_proxy = bool(config.get("inherit_system_proxy", False))
    delay = config.get("delay_between_accounts", 3)

    accounts = config.get("accounts", [])
    if not accounts:
        print("配置文件中没有账号信息。")
        return

    print(f"Codex 批量登录 — 共 {len(accounts)} 个账号，无头模式={headless}\n")

    success_list, fail_list = [], []

    for idx, acc in enumerate(accounts):
        email = acc.get("email", "").strip()
        password = acc.get("password", "").strip()
        totp = acc.get("totp_secret", "").strip()

        if not email or not password:
            print(f"[!] 跳过第 {idx + 1} 条记录：缺少 email 或 password")
            continue

        ok = await login_one_account(
            client,
            email,
            password,
            totp,
            headless,
            proxy_url,
            mail_provider,
            browser_channel=browser_channel,
            inherit_system_proxy=inherit_system_proxy,
        )
        (success_list if ok else fail_list).append(email)

        # 每个账号之间暂停，避免触发频率限制
        if idx < len(accounts) - 1 and delay > 0:
            print(f"    [~] 等待 {delay}s 后继续下一个账号…")
            await asyncio.sleep(delay)

    print(f"\n{'=' * 48}")
    print(f"完成：成功 {len(success_list)} 个，失败 {len(fail_list)} 个")
    if fail_list:
        print("失败账号：")
        for e in fail_list:
            print(f"  - {e}")


# ─────────────────────────────────────────────────────────────
#  程序入口
# ─────────────────────────────────────────────────────────────

def main() -> None:
    config_name = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    config_path = Path(__file__).parent / config_name

    if not config_path.exists():
        print(f"配置文件不存在：{config_path}")
        print("请将 config.example.json 复制为 config.json 并填写相关信息。")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as fh:
        config = json.load(fh)

    asyncio.run(batch_login(config))


if __name__ == "__main__":
    main()
