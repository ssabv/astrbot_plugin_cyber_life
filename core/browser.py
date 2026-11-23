"""Playwright browser wrapper used for QR login and sites without a usable API.

Playwright is imported lazily so the plugin still loads (and shows a clear
dependency warning in its settings page) when the browser dependency has not
been installed yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from html import unescape
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .util import atomic_write_json, now_tz, read_json, safe_filename
from .tieba_scope import forum_allowed, forum_name_from_url, is_post_url
from .takeover import BrowserTakeovers


class BrowserError(RuntimeError):
    def __init__(self, message, *, verification_required=False, uncertain=False):
        super().__init__(message)
        self.verification_required = verification_required
        self.uncertain = uncertain


@dataclass
class BrowserLoginHandle:
    platform: str
    name: str
    context: Any
    page: Any
    screenshot: Path
    created_at: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    async def cookies(self) -> dict[str, str]:
        try:
            items = await self.context.cookies()
        except Exception:  # noqa: BLE001
            return {}
        return {item.get("name", ""): item.get("value", "") for item in items if item.get("name")}

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception:  # noqa: BLE001
            pass


class BrowserManager:
    COOKIE_DOMAINS = {
        "tieba": ".baidu.com",
        "bilibili": ".bilibili.com",
        "xiaoheihe": ".xiaoheihe.cn",
    }

    def __init__(self, settings: Any, data_dir: Path) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self.shots_dir = data_dir / "media" / "login"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        # 由 main.py 注入的验证码识别器（core.captcha.CaptchaSolver）。
        # 不在这里 import，避免 browser ← captcha ← browser 的循环依赖。
        self.captcha: Any = None
        self._last_expand_trace: list[dict[str, Any]] = []
        self.takeovers = BrowserTakeovers(self)

    # ------------------------------------------------------------- lifecycle
    @staticmethod
    def installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def local_executables(self) -> list[str]:
        """Find existing Chromium binaries without downloading or sharing sessions."""
        roots = [Path.home() / ".cache" / "ms-playwright"]
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if configured and configured != "0":
            roots.insert(0, Path(configured))
        roots.extend(
            self.data_dir.parent / plugin / "browsers"
            for plugin in (
                "astrbot_plugin_qzone_tools",
                "astrbot_plugin_media_parser",
                "astrbot_plugin_cyber_life",
            )
        )
        paths: list[str] = []
        layouts = (
            ("chromium-*", "chrome-linux64/chrome"),
            ("chromium-*", "chrome-linux/chrome"),
            ("chromium_headless_shell-*", "chrome-headless-shell-linux64/chrome-headless-shell"),
            ("chromium_headless_shell-*", "chrome-linux/headless_shell"),
        )
        for root in dict.fromkeys(roots):
            for pattern, relative in layouts:
                for folder in sorted(root.glob(pattern), reverse=True):
                    path = folder / relative
                    if path.is_file() and os.access(path, os.X_OK):
                        paths.append(str(path))
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"):
            path = shutil.which(name)
            if path:
                paths.append(path)
        return list(dict.fromkeys(paths))

    async def install_chromium(self) -> list[str]:
        """Install full Chromium for unified headless mode into persistent data."""
        target = self.data_dir / "browsers"
        target.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(target)}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "--no-shell", "chromium",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            detail = output.decode("utf-8", errors="replace")[-1200:]
            raise BrowserError(f"Chromium 安装失败（退出码 {process.returncode}）：{detail}")
        paths = self.local_executables()
        if not paths:
            raise BrowserError("安装结束，但没有找到可执行的 Chromium")
        return paths

    def remove_old_headless_shell(self) -> dict[str, Any]:
        """Remove the unused shell matching our selected full Chromium revision."""
        root = (self.data_dir / "browsers").resolve(strict=True)
        executable = Path(self.settings.str("browser_executable")).resolve(strict=True)
        folder = executable.parents[1]
        revision = folder.name.removeprefix("chromium-")
        if (not folder.name.startswith("chromium-") or not revision.isdigit()
                or folder.parent != root or executable.name != "chrome"
                or not executable.is_file() or not os.access(executable, os.X_OK)):
            raise BrowserError("请先选择本插件目录里的完整 Chromium，再清理旧无头运行时")
        target = root / f"chromium_headless_shell-{revision}"
        if target.is_symlink():
            raise BrowserError("旧运行时目录是符号链接，停止清理")
        if not target.exists():
            return {"removed": False, "bytes_removed": 0}
        if not target.is_dir() or target.resolve(strict=True).parent != root:
            raise BrowserError("旧运行时目录超出本插件浏览器缓存，停止清理")
        size = sum(path.stat().st_size for path in target.rglob("*")
                   if path.is_file() and not path.is_symlink())
        shutil.rmtree(target)
        return {"removed": True, "path": str(target), "bytes_removed": size}

    async def _ensure(self):
        if self._browser is not None:
            return self._browser
        async with self._lock:
            if self._browser is not None:
                return self._browser
            if not self.settings.bool("browser_enabled"):
                raise BrowserError("内置浏览器已在设置中关闭")
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(
                    "Playwright 未安装。请在插件目录执行 pip install -r requirements.txt"
                ) from exc
            self._playwright = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "headless": self.settings.bool("browser_headless"),
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            channel = self.settings.str("browser_channel").strip()
            executable = self.settings.str("browser_executable").strip()
            errors = []
            attempts = []
            if executable:
                attempts.append({"executable_path": executable})
            if channel:
                attempts.append({"channel": channel})
            for candidate in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ):
                if os.path.exists(candidate):
                    attempts.append({"executable_path": candidate})
            attempts.append({})
            attempts.extend({"executable_path": path} for path in self.local_executables()
                            if path != executable)
            for extra in attempts:
                try:
                    self._browser = await self._playwright.chromium.launch(**kwargs, **extra)
                    if extra.get("executable_path"):
                        logger.info(f"[cyber-life] 使用已有浏览器：{extra['executable_path']}")
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{extra}: {exc}")
            if self._browser is None:
                await self._playwright.stop()
                self._playwright = None
                raise BrowserError(
                    "无法启动浏览器。请安装 Edge/Chrome，或在设置里填写 browser_channel / browser_executable。"
                    f" 详情：{'; '.join(errors[-2:])}"
                )
            return self._browser

    async def new_context(self, storage_path: Path | None = None, *, storage: bool = True):
        browser = await self._ensure()
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.int("browser_viewport_width"),
                "height": self.settings.int("browser_viewport_height"),
            },
            "locale": "zh-CN",
            "timezone_id": self.settings.str("timezone") or "Asia/Shanghai",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
            ),
            "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        }
        if storage and storage_path and storage_path.exists():
            kwargs["storage_state"] = str(storage_path)
        context = await browser.new_context(**kwargs)
        context.set_default_timeout(self.settings.int("browser_timeout_seconds") * 1000)
        try:
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception:  # noqa: BLE001
            pass
        return context

    def session_cookies(self, platform: str, name: str) -> dict[str, str]:
        """Read cookies saved by the account store without exposing their values."""
        path = self.data_dir / "sessions" / safe_filename(platform) / (
            safe_filename(name, "default") + ".json"
        )
        data = read_json(path, {})
        return dict(data.get("cookies") or {}) if isinstance(data, dict) else {}

    async def hydrate_cookies(self, context: Any, platform: str, name: str) -> int:
        """Put saved login cookies into a browser context that lacks a storage file.

        QR login saves both a Playwright storage state and the account cookie
        jar.  Older imports only kept the latter, which made the page render as
        a logged-out visitor even though the account itself was still valid.
        """
        domain = self.COOKIE_DOMAINS.get(platform)
        cookies = self.session_cookies(platform, name)
        if not domain or not cookies:
            return 0
        additions = [
            {"name": key, "value": value, "domain": domain, "path": "/"}
            for key, value in cookies.items()
            if value
        ]
        if additions:
            try:
                await context.add_cookies(additions)
            except Exception:  # noqa: BLE001
                return 0
        return len(additions)

    async def close(self) -> None:
        await self.takeovers.close_all()
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playwright = None

    # -------------------------------------------------------------- captcha
    async def solve_captcha(self, page: Any, platform: str = "", reason: str = "") -> dict[str, Any]:
        """把当前页面交给验证码识别器；没注入或已关闭时安全跳过。"""
        solver = self.captcha
        if solver is None:
            return {"ok": False, "solved": False, "had": False, "skipped": True,
                    "message": "未启用验证码识别"}
        try:
            return await solver.solve(page, platform=platform, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 验证码识别异常（{platform}/{reason}）：{exc}")
            return {"ok": False, "solved": False, "had": False,
                    "message": f"验证码识别异常：{exc}"}

    async def pass_site_captcha(self, platform: str, name: str, url: str) -> dict[str, Any]:
        """单独开一个带登录态的页面去过验证，成功后把新 Cookie 写回去。"""
        fingerprint = self.storage_fingerprint(platform, name)
        context = await self.new_context(self.storage_path(platform, name))
        await self.hydrate_cookies(context, platform, name)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2200)
            result = await self.solve_captcha(page, platform, "单独过验证")
            if result.get("solved"):
                await self.persist_storage(context, platform, name, expected_fingerprint=fingerprint)
            result["url"] = page.url
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "solved": False, "had": False,
                    "message": f"打开验证页失败：{exc}", "url": url}
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    def storage_fingerprint(self, platform: str, name: str) -> str | None:
        path = self.storage_path(platform, name)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    async def persist_storage(self, context: Any, platform: str, name: str, *, expected_fingerprint: str | None) -> str:
        """Keep a concurrent login change; never silently overwrite or ignore errors."""
        state = await context.storage_state()
        if self.storage_fingerprint(platform, name) != expected_fingerprint:
            raise BrowserError("登录状态已被其他操作更新，本次不覆盖，请重新检查账号")
        atomic_write_json(self.storage_path(platform, name), state)
        return self.storage_fingerprint(platform, name)

    # ----------------------------------------------------------- login pages
    async def begin_login(self, platform: str, name: str, spec: dict[str, Any]) -> BrowserLoginHandle:
        context = await self.new_context(storage=False)
        page = await context.new_page()
        try:
            await page.goto(spec["login_url"], wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            await context.close()
            raise BrowserError(f"打开登录页失败：{exc}") from exc
        await page.wait_for_timeout(1800)
        for action in spec.get("prep", []):
            try:
                selector = action.get("selector")
                if not selector:
                    continue
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(timeout=5000)
                await page.wait_for_timeout(int(action.get("wait_ms", 800)))
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(1200)
        # 登录页常先弹一道人机校验，过了才能看到二维码
        captcha = await self.solve_captcha(page, platform, "登录页")
        if captcha.get("solved"):
            await page.wait_for_timeout(1200)
        target = self.shots_dir / f"{platform}-{name}-{int(now_tz().timestamp())}.png"
        await self._screenshot_qr(page, spec.get("qr_selectors", []), target)
        return BrowserLoginHandle(
            platform=platform,
            name=name,
            context=context,
            page=page,
            screenshot=target,
            created_at=now_tz().isoformat(timespec="seconds"),
            notes={"captcha": captcha},
        )

    async def _screenshot_qr(self, page, selectors: list[str], target: Path) -> None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.wait_for(state="visible", timeout=4000)
                box = await locator.bounding_box()
                if not box or box.get("width", 0) < 80 or box.get("height", 0) < 80:
                    continue
                # add a little breathing room around the code
                clip = {
                    "x": max(0, box["x"] - 18),
                    "y": max(0, box["y"] - 18),
                    "width": box["width"] + 36,
                    "height": box["height"] + 36,
                }
                await page.screenshot(path=str(target), clip=clip, type="png")
                return
            except Exception:  # noqa: BLE001
                continue
        await page.screenshot(path=str(target), type="png")

    @staticmethod
    async def is_logged_in(handle: BrowserLoginHandle, spec: dict[str, Any]) -> bool:
        cookies = await handle.cookies()
        required = [name for name in spec.get("cookie_names", []) if name]
        if required and any(name in cookies for name in required):
            return True
        selectors = spec.get("logged_in_selectors", [])
        for selector in selectors:
            try:
                locator = handle.page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=1000):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def save_storage(self, handle: BrowserLoginHandle, target: Path) -> None:
        try:
            state = await handle.context.storage_state()
            atomic_write_json(target, state)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"保存浏览器登录态失败：{exc}") from exc

    # ------------------------------------------------------------- activity
    async def browse(self, platform: str, name: str, url: str, selectors: dict[str, Any]) -> dict[str, Any]:
        """Open a feed page, scrape cards and take a viewport screenshot."""
        fingerprint = self.storage_fingerprint(platform, name)
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        captcha: dict[str, Any] = {}
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            # 百度/小黑盒会在信息流前插一道验证页，过了再重新打开目标页
            captcha = await self.solve_captcha(page, platform, "打开信息流")
            if not captcha.get("ok") or (captcha.get("had") and not captcha.get("solved")):
                raise BrowserError(captcha.get("message") or "页面验证未确认", verification_required=True)
            if captcha.get("solved"):
                await self.persist_storage(context, platform, name, expected_fingerprint=fingerprint)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2200)
            cards = []
            for selector in selectors.get("card_selectors", []):
                try:
                    items = await page.locator(selector).all()
                except Exception:  # noqa: BLE001
                    continue
                for item in items[:24]:
                    try:
                        title = (await item.inner_text(timeout=1500)).strip()
                        title_selector = selectors.get("card_title_selector")
                        if title_selector:
                            node = item.locator(title_selector).first
                            if not await node.count():
                                continue
                            title = (await node.inner_text(timeout=1200)).strip() or title
                        href = await item.get_attribute("href", timeout=1000)
                        if selectors.get("card_link_selector"):
                            href = await item.locator(selectors["card_link_selector"]).first.get_attribute("href", timeout=1000)
                        body = ""
                        body_selector = selectors.get("card_body_selector")
                        if body_selector:
                            node = item.locator(body_selector).first
                            if await node.count():
                                body = (await node.inner_text(timeout=1200)).strip()
                        if not title:
                            continue
                        cards.append({"title": title[:200], "url": href or "", "body": body[:400]})
                    except Exception:  # noqa: BLE001
                        continue
                if cards:
                    break
            shot = self.data_dir / "media" / "browse" / f"{platform}-{int(now_tz().timestamp())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), type="png")
            notice = ""
            if not cards:
                try:
                    notice = (await page.locator("body").inner_text(timeout=2000)).strip()[:600]
                except Exception:
                    pass
            return {"url": page.url, "title": await page.title(), "cards": cards,
                    "screenshot": str(shot), "notice": notice, "captcha": captcha}
        finally:
            await context.close()

    def storage_path(self, platform: str, name: str) -> Path:
        return self.data_dir / "sessions" / platform / f"{name}.storage.json"

    async def post_comment(
        self,
        platform: str,
        name: str,
        url: str,
        text: str,
        selectors: dict[str, Any],
    ) -> dict[str, Any]:
        """Best-effort DOM commenting. Returns ok/message/screenshot."""
        fingerprint = self.storage_fingerprint(platform, name)
        clicked = False
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        shot_dir = self.data_dir / "media" / "comments"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / f"{platform}-{int(now_tz().timestamp())}.png"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2200)
            allowed = selectors.get("allowed_tieba_forums", []) if platform == "tieba" else []
            if allowed:
                if not is_post_url(page.url):
                    return {"ok": False, "message": "帖子页面发生跳转，本轮不发送"}
                forum = await page.evaluate("() => String(window.PageData?.forum?.name || '')")
                if not forum:
                    breadcrumb = page.locator("a.card_title_fname[href*='/f?kw='], #thread_theme_5 a[href*='/f?kw='], .bread_crumb a[href*='/f?kw=']").first
                    if await breadcrumb.count():
                        href = await breadcrumb.get_attribute("href", timeout=1000)
                        forum = forum_name_from_url(urljoin(page.url, href or ""))
                if not forum_allowed(forum, allowed):
                    return {"ok": False, "message": "无法确认帖子属于允许的贴吧，本轮不发送"}
            for selector in selectors.get("comment_open_selectors", []):
                try:
                    opener = page.locator(selector).first
                    if await opener.count() and await opener.is_visible(timeout=1000):
                        await opener.click(timeout=4000)
                        await page.wait_for_timeout(800)
                        break
                except Exception:  # noqa: BLE001
                    continue
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                pass
            editor = None
            for selector in selectors.get("editor_selectors", []):
                try:
                    locator = page.locator(selector).first
                    if await locator.count() and await locator.is_visible(timeout=1200):
                        editor = locator
                        break
                except Exception:  # noqa: BLE001
                    continue
            if editor is None:
                await page.screenshot(path=str(shot), type="png")
                return {"ok": False, "message": "没有找到评论输入框（页面结构可能变了）", "screenshot": str(shot)}
            await editor.click(timeout=5000)
            await page.wait_for_timeout(400)
            try:
                await editor.fill(text, timeout=4000)
            except Exception:  # noqa: BLE001
                await page.keyboard.type(text, delay=35)
            await page.wait_for_timeout(600)
            before = await self.solve_captcha(page, platform, "提交前")
            if not before.get("ok") or (before.get("had") and not before.get("solved")):
                return {"ok": False, "uncertain": False, "verification_required": True,
                        "message": before.get("message") or "验证未确认，本次不提交", "captcha": before}
            if before.get("solved"):
                fingerprint = await self.persist_storage(context, platform, name, expected_fingerprint=fingerprint)
            for selector in selectors.get("submit_selectors", []):
                try:
                    button = page.locator(selector).first
                    if await button.count() and await button.is_visible(timeout=1000):
                        if self.storage_fingerprint(platform, name) != fingerprint:
                            raise BrowserError("登录状态已变化，本次不提交")
                        # A timeout after invoking click can still mean the request was sent.
                        clicked = True
                        await button.click(timeout=5000)
                        break
                except BrowserError:
                    raise
                except Exception:
                    if clicked:
                        raise
                    continue
            await page.wait_for_timeout(1700)
            captcha = await self.solve_captcha(page, platform, "提交评论") if clicked else {}
            await page.screenshot(path=str(shot), type="png")
            blocked = clicked and (captcha.get("had") or not captcha.get("ok"))
            return {"ok": clicked and not blocked, "uncertain": bool(blocked),
                    "verification_required": bool(blocked),
                    "message": "已点击发送，结果未确认，请核对记录；不会自动补发" if blocked else (
                        "已提交评论" if clicked else "没有找到发送按钮，请检查选择器"),
                    "screenshot": str(shot), "url": page.url, "captcha": captcha}
        except Exception as exc:  # noqa: BLE001
            try:
                await page.screenshot(path=str(shot), type="png")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "uncertain": clicked, "message": f"浏览器评论失败：{exc}", "screenshot": str(shot)}
        finally:
            await context.close()

    # --------------------------------------------------------- tieba in-page
    async def submit_tieba(
        self,
        name: str,
        url: str,
        text: str,
        *,
        target: dict[str, Any] | None = None,
        allowed_forums: list[str] | None = None,
        author_id: str = "",
        job_id: str = "",
    ) -> dict[str, Any]:
        """Write a Tieba comment through the page itself, including its captcha.

        This path never uses the mobile write API and never carries a captured
        verification token. The browser must see the editor and the result in
        the same page; a click alone is not treated as success.
        """
        if not text.strip():
            return {"ok": False, "message": "正文为空，本次不发送"}
        if self.takeovers.has_account("tieba", name):
            raise BrowserError("该账号正在等待人工接管，请先结束浏览器接管")
        from .platforms.specs import site_spec
        spec = site_spec("tieba") or {}
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        hydrated = await self.hydrate_cookies(context, "tieba", name)
        if hydrated:
            logger.info("[cyber-life] 贴吧浏览器补入登录 Cookie 数量=%d", hydrated)
        page = await context.new_page()
        shot_dir = self.data_dir / "media" / "comments"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / f"tieba-{int(now_tz().timestamp())}.png"
        clicked = False
        captured: list[Any] = []
        response_tasks: list[asyncio.Task] = []
        captured_requests: list[dict[str, Any]] = []
        action = "回复" if target else "评论"
        def note_request(request: Any) -> None:
            try:
                if "/c/c/post/add_pc" in request.url:
                    data = request.post_data or ""
                    captured_requests.append({
                        "method": request.method,
                        "url": request.url[:180],
                        "post_bytes": len(data),
                        "has_text": text in data,
                    })
            except Exception:  # noqa: BLE001
                pass
        def note_response(response: Any) -> None:
            try:
                if "/c/c/post/add_pc" in response.url:
                    # Read while the CDP response body still exists. Keeping
                    # Response objects until a reload can lose a valid ACK.
                    task = asyncio.create_task(
                        self._tieba_submit_snapshot(response, url, text, target))
                    captured.append(task)
                    response_tasks.append(task)
            except Exception:  # noqa: BLE001
                pass
        try:
            page.on("request", note_request)
            page.on("response", note_response)
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2200)
            captcha = await self.solve_captcha(page, "tieba", "打开帖子")
            if captcha.get("had") and not captcha.get("solved"):
                return {"ok": False, "uncertain": False, "verification_required": True,
                        "message": captcha.get("message") or "帖子页验证未通过", "captcha": captcha}
            if captcha.get("solved"):
                fingerprint = await self.persist_storage(
                    context, "tieba", name, expected_fingerprint=fingerprint
                )
            if not is_post_url(page.url):
                return {"ok": False, "message": "帖子页面发生跳转，本次不发送"}
            forum = await self._tieba_forum(page)
            if allowed_forums and not forum_allowed(forum, allowed_forums):
                return {"ok": False, "message": "无法确认帖子属于允许的贴吧，本次不发送"}
            editor = None
            if target:
                editor = await self._tieba_open_reply_target(page, target, spec)
                if editor is None:
                    await page.screenshot(path=str(shot), type="png")
                    return {"ok": False, "message": "没有找到要回复的楼层，本次不发送",
                            "screenshot": str(shot)}
            else:
                editor = await self._tieba_editor(page, spec)
            if editor is None and not target:
                editor, fingerprint = await self._tieba_comment_page(
                    page, context, name, fingerprint, spec
                )
            if editor is None and not target:
                editor, fingerprint = await self._tieba_mobile_editor(
                    page, context, name, fingerprint, url, spec,
                    allowed_forums=allowed_forums,
                )
            if editor is None:
                await page.screenshot(path=str(shot), type="png")
                notice = await self._page_notice(page)
                state = await self._tieba_state(page)
                if state and not state.get("login"):
                    return {"ok": False, "login_required": True, "state": state,
                            "message": "贴吧登录态已失效，请在账号页重新扫码登录后再试",
                            "screenshot": str(shot), "notice": notice}
                return {"ok": False, "message": "没有找到评论输入框（页面结构可能变了）",
                        "screenshot": str(shot), "notice": notice, "state": state}
            logger.info("[cyber-life] 贴吧输入框已就绪 page=%s state=%s",
                        str(page.url)[:120], await self._tieba_state(page))
            await self._fill_tieba_editor(page, editor, text)
            if not await self._editor_contains(editor, text):
                await page.screenshot(path=str(shot), type="png")
                return {"ok": False, "message": "评论框没有接受正文，本次不发送",
                        "screenshot": str(shot)}
            before = await self.solve_captcha(page, "tieba", "提交前")
            if before.get("had") and not before.get("solved"):
                return {"ok": False, "uncertain": False, "verification_required": True,
                        "message": before.get("message") or "提交前验证未通过，本次不发送",
                        "captcha": before}
            if before.get("solved"):
                fingerprint = await self.persist_storage(
                    context, "tieba", name, expected_fingerprint=fingerprint
                )
            button = await self._tieba_submit(page, spec)
            js_target = None
            keyboard_submit = False
            if button is None:
                js_target = await self._tieba_js_submit_target(page)
                if not js_target.get("ok"):
                    editor_html = await self._tieba_editor_html(page)
                    logger.info("[cyber-life] 贴吧无发送按钮，尝试键盘提交 editor=%s",
                                editor_html)
                    keyboard_submit = True
            if self.storage_fingerprint("tieba", name) != fingerprint:
                raise BrowserError("登录状态已变化，本次不发送")
            identity = await self._tieba_reply_identity(page)
            if author_id:
                identity["id"] = author_id
            before_records = await self._tieba_reply_records(page)
            before_ids = None if before_records is None else {
                record["id"] for record in before_records if record.get("id")
            }
            # Ignore responses from page initialization; only this send counts.
            captured.clear()
            captured_requests.clear()
            if button is not None:
                disabled = await self._tieba_button_disabled(button)
                for _ in range(8):
                    if not disabled:
                        break
                    await page.wait_for_timeout(250)
                    disabled = await self._tieba_button_disabled(button)
                if disabled:
                    raise BrowserError("发送按钮仍不可用，本次未提交")
                clicked = True
                try:
                    await button.click(timeout=5000)
                except Exception:
                    # A click timeout can still have submitted; never press it again.
                    raise BrowserError("点击发送后结果未确认，不会自动补发", uncertain=True)
            elif keyboard_submit:
                clicked = True
                await editor.click(timeout=5000)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(4000)
            else:
                clicked = True
                clicked_js = await self._tieba_js_submit_click(page)
                if not clicked_js.get("clicked"):
                    raise BrowserError("发送按钮已消失，本次未提交")
            await page.wait_for_timeout(2200)
            after = await self.solve_captcha(page, "tieba", f"提交{action}")
            if after.get("had") and not after.get("solved"):
                confirmed = await self._tieba_confirm_reply(
                    page, text, before_ids, identity, target, await self._tieba_post_ack(captured))
                if not confirmed:
                    return {"ok": False, "uncertain": True, "verification_required": True,
                            "message": after.get("message") or "提交后验证未通过，结果未确认",
                            "captcha": after, "screenshot": str(shot)}
            if after.get("solved"):
                fingerprint = await self.persist_storage(
                    context, "tieba", name, expected_fingerprint=fingerprint
                )
                # Continue only an acknowledged rejection. Empty, pending, or
                # successful responses never authorize a second click.
                await page.wait_for_timeout(1600)
                posted = await self._tieba_post_result(captured)
                if (posted == "failed" and len(captured) >= len(captured_requests)
                        and not await self._tieba_success_marker(page)):
                    logger.info("[cyber-life] 滑块通过后提交明确被拒绝，检查是否可继续提交")
                    retry_button = await self._tieba_submit(page, spec)
                    if (retry_button is not None
                            and not await self._tieba_button_disabled(retry_button)
                            and await self._editor_contains(editor, text)
                            and self.storage_fingerprint("tieba", name) == fingerprint
                            and await self._tieba_post_result(captured) == "failed"
                            and len(captured) >= len(captured_requests)):
                        try:
                            await retry_button.click(timeout=5000)
                            logger.info("[cyber-life] 滑块通过后补点一次发送")
                        except Exception:  # noqa: BLE001
                            logger.info("[cyber-life] 滑块通过后补点发送失败")
                        await page.wait_for_timeout(2500)
                        retry_captcha = await self.solve_captcha(page, "tieba", f"提交{action}（验证后继续）")
                        if retry_captcha.get("had") and not retry_captcha.get("solved"):
                            return {"ok": False, "uncertain": True, "verification_required": True,
                                    "message": retry_captcha.get("message") or "补发验证未通过，结果未确认",
                                    "captcha": retry_captcha, "screenshot": str(shot)}
            sent_pid = ""
            for _ in range(12):
                ack = await self._tieba_post_ack(captured)
                sent_pid = await self._tieba_confirm_reply(
                    page, text, before_ids, identity, target, ack
                )
                if sent_pid:
                    break
                await page.wait_for_timeout(700)
            if not sent_pid:
                # A captcha can replace the comment DOM after a successful
                # submit. Reloading and re-reading is safe: never click again.
                try:
                    match = re.search(r"/p/(\d+)", url)
                    if match:
                        await page.goto(
                            f"https://tieba.baidu.com/p/{match.group(1)}?showComment=1&fr=pb",
                            wait_until="domcontentloaded", timeout=45000,
                        )
                        await page.wait_for_timeout(2600)
                        again = await self.solve_captcha(page, "tieba", "提交后核对")
                        if not (again.get("had") and not again.get("solved")):
                            await self._tieba_load_reply_target(
                                page, {"content": text}, allow_scroll=True
                            )
                            sent_pid = await self._tieba_confirm_reply(
                                page, text, before_ids, identity, target,
                                await self._tieba_post_ack(captured),
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.info("[cyber-life] 贴吧提交后核对失败 error_type=%s",
                                type(exc).__name__)
            await page.screenshot(path=str(shot), type="png")
            network = []
            for response in captured[-3:]:
                snapshot = await self._tieba_response_snapshot(response)
                network.append({**snapshot, "body": snapshot["body"][:600]})
            if network:
                ack = await self._tieba_post_ack(captured)
                logger.info("[cyber-life] 贴吧提交响应 action=%s ack=%s pid=%s responses=%s",
                            action, ack["state"], ack["id"], str(network)[:900])
            if captured_requests:
                logger.info("[cyber-life] 贴吧提交请求 %s", str(captured_requests)[:900])
            if not sent_pid:
                return {"ok": False, "uncertain": True, "verification_required": False,
                        "message": "发送结果尚未确认，本轮结束；后续活动继续，该目标不自动重发",
                        "screenshot": str(shot), "captcha": after, "network": network,
                        "requests": captured_requests}
            return {"ok": True, "uncertain": False, "verification_required": False,
                    "message": "已确认发送", "screenshot": str(shot),
                    "id": sent_pid, "ack": await self._tieba_post_ack(captured), "network": network,
                    "requests": captured_requests}
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                await page.screenshot(path=str(shot), type="png")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "uncertain": clicked,
                    "message": f"浏览器评论失败：{exc}", "screenshot": str(shot)}
        finally:
            try:
                page.remove_listener("request", note_request)
                page.remove_listener("response", note_response)
            except Exception:  # noqa: BLE001
                pass
            for task in response_tasks:
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
            await context.close()

    async def _tieba_comment_page(self, page: Any, context: Any, name: str,
                                  fingerprint: str | None, spec: dict[str, Any]):
        """Open Tieba's dedicated comment page, which may still mount an editor."""
        parsed = urlsplit(page.url)
        if not re.fullmatch(r"/p/\d+/?", parsed.path):
            return None, fingerprint
        query = parse_qs(parsed.query)
        query["showComment"] = ["1"]
        query["fr"] = ["pb"]
        target_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"),
                                 urlencode(query, doseq=True), ""))
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2600)
            captcha = await self.solve_captcha(page, "tieba", "回复页")
            if captcha.get("had") and not captcha.get("solved"):
                logger.info("[cyber-life] 贴吧回复页验证未过")
                return None, fingerprint
            if captcha.get("solved"):
                fingerprint = await self.persist_storage(
                    context, "tieba", name, expected_fingerprint=fingerprint
                )
            editor = await self._tieba_editor(page, spec)
            if editor is not None:
                logger.info("[cyber-life] 贴吧回复页输入框已找到")
                return editor, fingerprint
            logger.info("[cyber-life] 贴吧回复页无输入框 state=%s outline=%s html=%s",
                        await self._tieba_state(page),
                        await self._tieba_reply_outline(page),
                        await self._tieba_content_html(page))
        except Exception as exc:  # noqa: BLE001
            logger.info("[cyber-life] 贴吧回复页打开失败 error_type=%s",
                        type(exc).__name__)
        return None, fingerprint

    async def _tieba_content_html(self, page: Any) -> str:
        try:
            value = await page.evaluate(
                """() => {
                    const node = document.querySelector(
                        '.center-content, .content-container, #thread_theme_5'
                    );
                    return node ? String(node.outerHTML || '').slice(0, 1800) : '';
                }"""
            )
            return str(value or "").replace("\n", " ")[:1800]
        except Exception:  # noqa: BLE001
            return ""

    async def _tieba_mobile_editor(self, page: Any, context: Any, name: str,
                                   fingerprint: str | None, url: str,
                                   spec: dict[str, Any],
                                   allowed_forums: list[str] | None = None):
        """Desktop Tieba may say “去APP回复”; try its mobile web post page.

        The mobile route needs both the forum word and the thread id.  A bare
        ``tid`` is redirected to the mobile home page, so every candidate
        carries the forum name and is checked against the thread title before
        its editor is trusted.
        """
        match = re.search(r"/p/(\d+)", url)
        if not match:
            return None, fingerprint
        tid = match.group(1)
        title, first_post = await self._tieba_thread_text(page)
        forum = await self._tieba_forum(page)
        if not forum:
            forum = forum_name_from_url(url)
        if not forum and allowed_forums:
            forum = allowed_forums[0]
        if not forum:
            logger.info("[cyber-life] 贴吧手机版缺少吧名，无法打开帖子")
            return None, fingerprint
        mobile_ua = (
            "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        )
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride", {
                "userAgent": mobile_ua,
                "platform": "Android",
                "acceptLanguage": "zh-CN,zh;q=0.9",
            })
            await page.set_viewport_size({"width": 390, "height": 844})
            logger.info("[cyber-life] 贴吧手机版已切换移动 UA")
        except Exception as exc:  # noqa: BLE001
            logger.info("[cyber-life] 贴吧手机版切换 UA 失败 error_type=%s",
                        type(exc).__name__)
        word = quote(forum or "")
        candidates = (
            f"https://tieba.baidu.com/mo/q/m?word={word}&tid={tid}&pn=0",
            f"https://tieba.baidu.com/mo/q/m?kw={word}&tid={tid}&pn=0&lp=5028&is_share=1",
            f"https://tieba.baidu.com/mo/q/m?tn=bdFBW&word={word}&tid={tid}&pn=0&",
            f"https://tieba.baidu.com/mo/q/m?kw={word}&tid={tid}&pn=0&",
        )
        for target_url in candidates:
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2600)
                if await self._tieba_short_page(page):
                    await page.wait_for_timeout(4000)
                captcha = await self.solve_captcha(page, "tieba", "手机版帖子")
                if captcha.get("had") and not captcha.get("solved"):
                    logger.info("[cyber-life] 贴吧手机版验证未过 path=%s",
                                target_url.split("?")[0])
                    continue
                if captcha.get("solved"):
                    fingerprint = await self.persist_storage(
                        context, "tieba", name, expected_fingerprint=fingerprint
                    )
                if not await self._tieba_same_thread(page, tid, title, first_post):
                    logger.info("[cyber-life] 贴吧手机版不是目标帖子 path=%s url_tid=%s",
                                target_url.split("?")[0],
                                "yes" if f"tid={tid}" in page.url else "no")
                    continue
                editor = await self._tieba_editor(page, spec, mobile=True)
                if editor is not None:
                    logger.info("[cyber-life] 贴吧手机版输入框已找到 path=%s",
                                target_url.split("?")[0])
                    return editor, fingerprint
                state = await self._tieba_state(page)
                notice = await self._page_notice(page)
                logger.info("[cyber-life] 贴吧手机版无输入框 path=%s title=%s state=%s text=%s",
                            target_url.split("?")[0], str(await page.title())[:60],
                            state, notice[:240].replace("\n", " | "))
            except Exception as exc:  # noqa: BLE001
                logger.info("[cyber-life] 贴吧手机版打开失败 path=%s error_type=%s",
                            target_url.split("?")[0], type(exc).__name__)
        return None, fingerprint

    async def _tieba_thread_text(self, page: Any) -> tuple[str, str]:
        try:
            values = await page.evaluate(
                """() => {
                    const data = window.PageData || {};
                    const thread = data.thread || {};
                    return [String(thread.title || ''), String(thread.first_post_content || '')];
                }"""
            )
            if isinstance(values, list) and len(values) == 2:
                return str(values[0] or ""), str(values[1] or "")
        except Exception:  # noqa: BLE001
            pass
        return "", ""

    async def _tieba_short_page(self, page: Any) -> bool:
        try:
            text = (await page.locator("body").inner_text(timeout=2000)).strip()
        except Exception:  # noqa: BLE001
            return True
        return len(text) < 80

    async def _tieba_same_thread(self, page: Any, tid: str, title: str,
                                 first_post: str) -> bool:
        if f"tid={tid}" in page.url or f"/p/{tid}" in page.url:
            return True
        try:
            current = await page.evaluate(
                "() => String(window.PageData?.thread?.tid || '')"
            )
            if str(current or "") == tid:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            text = await page.locator("body").inner_text(timeout=2500)
        except Exception:  # noqa: BLE001
            return False
        probes = [probe for probe in (title.strip(), first_post.strip()[:24]) if len(probe) >= 4]
        return bool(probes) and any(probe in text for probe in probes)

    async def _tieba_state(self, page: Any) -> dict[str, Any]:
        """Describe the page without exposing cookie values."""
        try:
            state = await page.evaluate(
                """() => {
                    const data = window.PageData || {};
                    const user = data.user || {};
                    const domLogin = !!document.querySelector(
                        '.userinfo, .u_menu_username, a[href*="logout"], .user_name, .user_level'
                    );
                    return {
                        login: !!(user.is_login || user.name || domLogin),
                        bduss: /(?:^|;\\s*)BDUSS=/.test(document.cookie),
                        dom: domLogin,
                        ta: document.querySelectorAll('textarea').length,
                        ce: document.querySelectorAll('[contenteditable="true"]').length,
                        ue: document.querySelectorAll('#ueditor_replace, .edui-body-container, .poster_component').length,
                        reply: document.querySelectorAll('a.p_reply, .p_reply, a[href*="reply"]').length,
                    };
                }"""
            )
            if isinstance(state, dict):
                return state
        except Exception:  # noqa: BLE001
            pass
        return {}

    async def _tieba_forum(self, page: Any) -> str:
        try:
            forum = await page.evaluate("() => String(window.PageData?.forum?.name || '')")
        except Exception:  # noqa: BLE001
            forum = ""
        if forum:
            return forum
        try:
            breadcrumb = page.locator(
                "a.card_title_fname[href*='/f?kw='], #thread_theme_5 a[href*='/f?kw='], "
                ".bread_crumb a[href*='/f?kw='], a[href*='/f?kw=']"
            ).first
            if await breadcrumb.count():
                href = await breadcrumb.get_attribute("href", timeout=1000)
                return forum_name_from_url(urljoin(page.url, href or ""))
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def read_tieba_thread(self, name: str, tid: str, *, target: str = "",
                                comment_view: bool = False, pn: int = 0) -> dict[str, Any]:
        """Read a thread with the real account, without writing anything."""
        if not re.fullmatch(r"\d{1,24}", tid):
            return {"ok": False, "message": "帖子编号无效"}
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        await self.hydrate_cookies(context, "tieba", name)
        page = await context.new_page()
        try:
            page_url = f"https://tieba.baidu.com/p/{tid}"
            query = []
            if comment_view:
                query.extend([("showComment", "1"), ("fr", "pb")])
            if pn > 0:
                query.append(("pn", str(pn)))
            if query:
                page_url += "?" + urlencode(query)
            await page.goto(page_url,
                            wait_until="domcontentloaded", timeout=45000,
                            referer="https://tieba.baidu.com/f?kw=%E5%A4%A7%E8%82%A5%E9%B1%BC")
            await page.wait_for_timeout(3500)
            for _ in range(3):
                if "数据获取失败" not in (await self._page_notice(page)):
                    break
                try:
                    reload_button = page.locator("text=重新加载").first
                    if await reload_button.count() and await reload_button.is_visible(timeout=800):
                        await reload_button.click(timeout=3000)
                        await page.wait_for_timeout(2800)
                except Exception:  # noqa: BLE001
                    break
            captcha = await self.solve_captcha(page, "tieba", "读取帖子")
            if captcha.get("had") and not captcha.get("solved"):
                return {"ok": False, "verification_required": True,
                        "message": captcha.get("message") or "帖子页验证未通过"}
            if captcha.get("solved"):
                await self.persist_storage(
                    context, "tieba", name, expected_fingerprint=fingerprint
                )
            try:
                text = (await page.locator("body").inner_text(timeout=5000)).strip()
            except Exception:  # noqa: BLE001
                text = await self._page_notice(page)
            result = {"ok": True, "url": page.url, "title": str(await page.title())[:120],
                      "text": text[:12000], "state": await self._tieba_state(page)}
            if target:
                trace = []
                found = await self._tieba_load_reply_target_pages(
                    page, {"content": target}, trace=trace
                )
                if not found:
                    found = await self._tieba_load_reply_target(
                        page, {"content": target}, allow_scroll=True
                    )
                selected = ""
                if found:
                    selected = str(await page.evaluate(
                        "() => document.querySelector('[data-cyber-target=\"1\"]')?.innerText || ''"
                    ))
                result.update({"target_found": found, "target_text": selected[:300],
                               "target_trace": trace})
                try:
                    result["target_page_text"] = (
                        await page.locator("body").inner_text(timeout=5000)
                    )[:12000]
                    result["target_lzl"] = await page.evaluate(
                        """() => [...document.querySelectorAll('.pb-lzl-item')]
                            .slice(0, 40)
                            .map((node) => String(node.innerText || '').slice(0, 220))"""
                    )
                except Exception:  # noqa: BLE001
                    pass
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取帖子失败：{exc}"}
        finally:
            await context.close()

    async def _tieba_load_reply_target_pages(self, page: Any, target: dict[str, Any],
                                             *, max_pages: int = 30,
                                             trace: list | None = None) -> bool:
        """Walk Tieba's floor pages and expand nested replies without scrolling."""
        text = str(target.get("content") or "").strip()
        sub_pid = str(target.get("sub_pid") or "")
        parent_pid = str(target.get("parent_pid") or "")
        if len(text) < 4 and not sub_pid.isdigit() and not parent_pid.isdigit():
            return False
        match = re.search(r"/p/(\d+)", str(page.url))
        if not match:
            return False
        tid = match.group(1)
        previous = None
        for pn in range(1, max_pages + 1):
            url = f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb&pn={pn}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2400)
                captcha = await self.solve_captcha(page, "tieba", f"第{pn}页")
                if captcha.get("had") and not captcha.get("solved"):
                    return False
                for _ in range(3):
                    if await self._tieba_mark_reply_target(page, target):
                        if trace is not None:
                            trace.append({"pn": pn, "found": True, "round": _})
                        return True
                    expanded = await self._tieba_expand_comments(page)
                    if await self._tieba_mark_reply_target(page, target):
                        if trace is not None:
                            trace.append({"pn": pn, "found": True, "round": _,
                                          "expanded": expanded})
                        return True
                    if expanded == 0:
                        break
                    await page.wait_for_timeout(700)
                for _ in range(6):
                    if await self._tieba_mark_reply_target(page, target):
                        if trace is not None:
                            trace.append({"pn": pn, "found": True, "scroll": _})
                        return True
                    await self._tieba_scroll_comments(page)
                    await page.wait_for_timeout(800)
                    if await self._tieba_mark_reply_target(page, target):
                        if trace is not None:
                            trace.append({"pn": pn, "found": True, "scroll": _})
                        return True
                    expanded = await self._tieba_expand_comments(page)
                    if await self._tieba_mark_reply_target(page, target):
                        if trace is not None:
                            trace.append({"pn": pn, "found": True, "scroll": _,
                                          "expanded": expanded})
                        return True
                signature = await page.evaluate(
                    """() => ({
                        first: document.querySelector('.pb-comment-item')?.getAttribute('data-id') || '',
                        last: [...document.querySelectorAll('.pb-comment-item')].slice(-1)[0]?.getAttribute('data-id') || '',
                        items: document.querySelectorAll('.pb-comment-item, .pb-lzl-item').length,
                        body: String(document.body?.innerText || '').length,
                        expanders: document.querySelectorAll('.show-more-lzl').length,
                        parent: !!document.querySelector('[data-id="' + %s + '"]'),
                        sub: !!document.querySelector('[data-id="' + %s + '"]')
                    })"""
                    % (json.dumps(parent_pid), json.dumps(sub_pid))
                )
                if trace is not None:
                    trace.append({"pn": pn, "state": signature})
                if pn > 1 and signature == previous:
                    break
                previous = signature
            except Exception as exc:  # noqa: BLE001
                logger.info("[cyber-life] 贴吧第%s页加载失败 error_type=%s", pn,
                            type(exc).__name__)
                break
        return False

    async def probe_tieba_target(self, name: str, tid: str, *, target: str,
                                 parent_pid: str = "",
                                 sub_pid: str = "") -> dict[str, Any]:
        """Read-only probe: does a direct comment URL expose the target reply?"""
        if not re.fullmatch(r"\d{1,24}", tid):
            return {"ok": False, "message": "帖子编号无效"}
        if not target.strip():
            return {"ok": False, "message": "请提供目标正文"}
        base = f"https://tieba.baidu.com/p/{tid}"
        query = []
        if parent_pid.isdigit():
            query.append(("pid", parent_pid))
        if sub_pid.isdigit():
            query.append(("cid", sub_pid))
        encoded = "&".join(f"{key}={value}" for key, value in query)
        variants = [
            ("direct_pid_cid", base + ("?" + encoded if encoded else "")),
            ("direct_pid_cid_fragment", base + ("?" + encoded if encoded else "") + "#" + sub_pid),
            ("comment_pid_cid", base + "?showComment=1&fr=pb" + (("&" + encoded) if encoded else "")),
            ("direct_sub_pid", base + "?pid=" + sub_pid),
            ("comment_parent_pid", base + "?showComment=1&fr=pb&pid=" + parent_pid),
            ("direct_cid", base + "?cid=" + sub_pid),
            ("mobile_pid", "https://tieba.baidu.com/mo/q/m?word=%E5%A4%A7%E8%82%A5%E9%B1%BC&tid="
             + tid + "&pid=" + sub_pid + "&pn=0"),
        ]
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        await self.hydrate_cookies(context, "tieba", name)
        page = await context.new_page()
        results = []
        try:
            for label, url in variants:
                row = {"label": label, "url": url.split("#")[0][-80:]}
                try:
                    if label.startswith("mobile"):
                        try:
                            cdp = await context.new_cdp_session(page)
                            await cdp.send("Network.setUserAgentOverride", {
                                "userAgent": (
                                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Mobile Safari/537.36"
                                ),
                                "platform": "Android",
                                "acceptLanguage": "zh-CN,zh;q=0.9",
                            })
                            await page.set_viewport_size({"width": 390, "height": 844})
                        except Exception:  # noqa: BLE001
                            pass
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3200)
                    captcha = await self.solve_captcha(page, "tieba", "探测目标")
                    row["captcha"] = bool(captcha.get("had"))
                    row["solved"] = bool(captcha.get("solved"))
                    if captcha.get("had") and not captcha.get("solved"):
                        row["error"] = "verification"
                        results.append(row)
                        continue
                    if captcha.get("solved"):
                        fingerprint = await self.persist_storage(
                            context, "tieba", name, expected_fingerprint=fingerprint
                        )
                    row["final_url"] = page.url[:120]
                    row["title"] = str(await page.title())[:80]
                    row["counts"] = await page.evaluate(
                        """() => ({
                            comments: document.querySelectorAll('.pb-comment-item').length,
                            replies: document.querySelectorAll('.pb-lzl-item').length,
                            ids: document.querySelectorAll('[data-id]').length,
                            body: String(document.body?.innerText || '').length
                        })"""
                    )
                    row["body"] = str(await page.evaluate(
                        "() => String(document.body?.innerText || '').slice(0, 700)"
                    ))
                    row["target_counts"] = await page.evaluate(
                        """(text) => ({
                            target: String(document.body?.innerText || '').includes(text),
                            prefix: String(document.body?.innerText || '').includes('好家伙'),
                            sub_pid: String(document.body?.innerHTML || '').includes('154150663558'),
                            parent_pid: String(document.body?.innerHTML || '').includes('154150652759')
                        })""",
                        target,
                    )
                    row["id_nodes"] = await page.evaluate(
                        """() => [...document.querySelectorAll('[data-id]')].slice(0, 12).map((node) => ({
                            id: String(node.getAttribute('data-id') || ''),
                            cls: String(node.className || '').slice(0, 80),
                            text: String(node.innerText || '').slice(0, 90)
                        }))"""
                    )
                    row["found"] = await self._tieba_mark_reply_target(
                        page, {"content": target, "parent_pid": parent_pid, "sub_pid": sub_pid}
                    )
                    if row["found"]:
                        row["target"] = str(await page.evaluate(
                            "() => document.querySelector('[data-cyber-target=\"1\"]')?.innerText || ''"
                        ))[:220]
                    results.append(row)
                except Exception as exc:  # noqa: BLE001
                    row["error"] = type(exc).__name__
                    results.append(row)
            flow = await self._probe_tieba_comment_flow(page, tid, target, parent_pid, sub_pid)
            return {"ok": True, "results": results, "flow": flow}
        finally:
            await context.close()

    async def probe_tieba_expanders(self, name: str, tid: str, *,
                                    pn: int = 1, text: str = "",
                                    sub_pid: str = "") -> dict[str, Any]:
        """Open one comment page and report fold labels plus click results."""
        if not re.fullmatch(r"\d{1,24}", tid):
            return {"ok": False, "message": "帖子编号无效"}
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        await self.hydrate_cookies(context, "tieba", name)
        page = await context.new_page()
        try:
            url = f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb"
            if pn > 0:
                url += f"&pn={pn}"
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3200)
            captcha = await self.solve_captcha(page, "tieba", "探测展开")
            if captcha.get("had") and not captcha.get("solved"):
                return {"ok": False, "message": captcha.get("message") or "验证未通过"}
            if captcha.get("solved"):
                await self.persist_storage(context, "tieba", name,
                                           expected_fingerprint=fingerprint)
            before = await self._tieba_expand_state(page)
            candidates = await self._tieba_expand_candidates(page)
            expanded = await self._tieba_expand_comments(page)
            first_trace = list(self._last_expand_trace)
            await page.wait_for_timeout(1800)
            expanded += await self._tieba_expand_comments(page)
            second_trace = list(self._last_expand_trace)
            after = await self._tieba_expand_state(page)
            body = str(await page.evaluate(
                "() => String(document.body?.innerText || '')"
            ))
            needle = re.sub(r"\s+", "", text)
            lzls = await page.evaluate(
                """() => [...document.querySelectorAll('.pb-lzl-item, .pb-comment-item')]
                    .slice(0, 200)
                    .map((node) => String(node.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200))"""
            )
            return {"ok": True, "url": page.url[:160], "before": before,
                    "candidates": candidates, "expanded": expanded,
                    "after": after, "trace": first_trace + second_trace,
                    "body": body[:2500],
                    "lzls": lzls,
                    "checks": {
                        "body_compact": bool(needle) and needle in re.sub(r"\s+", "", body),
                        "html_sub": bool(sub_pid) and sub_pid in str(
                            await page.evaluate("() => document.documentElement.innerHTML")
                        ),
                        "body_len": len(body),
                    }}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"探测展开失败：{exc}"}
        finally:
            await context.close()

    async def probe_tieba_scroll_target(self, name: str, tid: str, *,
                                        text: str, parent_pid: str = "",
                                        sub_pid: str = "", label: str = "正序",
                                        steps: int = 40) -> dict[str, Any]:
        """Read-only: sort the comment view and scroll until the target renders."""
        if not re.fullmatch(r"\d{1,24}", tid):
            return {"ok": False, "message": "帖子编号无效"}
        if not text.strip() and not sub_pid.isdigit():
            return {"ok": False, "message": "请提供目标正文"}
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        await self.hydrate_cookies(context, "tieba", name)
        page = await context.new_page()
        target = {"content": text, "parent_pid": parent_pid, "sub_pid": sub_pid}
        trace = []
        try:
            await page.goto(f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3200)
            captcha = await self.solve_captcha(page, "tieba", "探测目标")
            if captcha.get("had") and not captcha.get("solved"):
                return {"ok": False, "message": captcha.get("message") or "验证未通过"}
            if captcha.get("solved"):
                await self.persist_storage(context, "tieba", name,
                                           expected_fingerprint=fingerprint)
            sorted_ok = await self._tieba_sort_comments(page, label)
            await page.wait_for_timeout(1800)
            found = False
            stable = 0
            last = None
            needle = re.sub(r"\s+", "", text)
            all_expand: list[dict[str, Any]] = []
            html_seen = False
            for step in range(max(1, min(steps, 80))):
                found = await self._tieba_mark_reply_target(page, target)
                expanded = 0
                if not found:
                    expanded = await self._tieba_expand_comments(page)
                    all_expand.extend(self._last_expand_trace)
                    await page.wait_for_timeout(1200)
                    found = await self._tieba_mark_reply_target(page, target)
                probe = await page.evaluate(
                    """(args) => {
                        const compact = (value) => String(value || '').replace(/\\s+/g, '');
                        return {
                            body: compact(document.body?.innerText || '').includes(args.text),
                            sub: !!args.sub && String(document.documentElement?.innerHTML || '')
                                .includes(args.sub),
                            lzl: [...document.querySelectorAll('.pb-lzl-item')]
                                .some((node) => compact(node.innerText).includes(args.text))
                        };
                    }""",
                    {"text": needle, "sub": sub_pid},
                )
                html_seen = html_seen or bool((probe or {}).get("sub"))
                state = await self._tieba_scroll_comments(page)
                items = int(state.get("items") or 0)
                signature = (str(state.get("first") or ""), str(state.get("last") or ""),
                             items, int(state.get("top") or 0))
                trace.append({"step": step, "expanded": expanded, "items": items,
                              "first": signature[0], "last": signature[1],
                              "top": signature[3], "found": found, **probe})
                if found:
                    break
                if signature == last and expanded == 0:
                    stable += 1
                else:
                    stable = 0
                last = signature
                if stable >= 8:
                    break
                await page.wait_for_timeout(900)
                captcha = await self.solve_captcha(page, "tieba", "滚动目标")
                if captcha.get("had") and not captcha.get("solved"):
                    trace.append({"step": step, "verification": True})
                    break
            selected = ""
            if found:
                selected = str(await page.evaluate(
                    "() => document.querySelector('[data-cyber-target=\"1\"]')?.innerText || ''"
                ))
            checks = await page.evaluate(
                """(args) => ({
                    body_text: String(document.body?.innerText || '')
                        .replace(/\\s+/g, '').includes(args.text),
                    html_sub: !!args.sub && String(document.documentElement?.innerHTML || '').includes(args.sub),
                    html_parent: !!args.parent && String(document.documentElement?.innerHTML || '').includes(args.parent),
                    body: String(document.body?.innerText || '').length
                })""",
                {"text": needle,
                 "sub": sub_pid, "parent": parent_pid},
            )
            checks["html_sub_seen"] = html_seen
            return {"ok": True, "sorted": sorted_ok, "found": found,
                    "target_text": selected[:300], "trace": trace,
                    "checks": checks, "expand_trace": all_expand[-30:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"滚动探测失败：{exc}"}
        finally:
            await context.close()

    async def probe_tieba_reply_submit(self, name: str, tid: str, *,
                                       text: str, parent_pid: str = "",
                                       sub_pid: str = "",
                                       body: str = "测试输入，不发送",
                                       verify: bool = False) -> dict[str, Any]:
        """Dry-run a reply; the submit request is aborted, so nothing posts."""
        if not re.fullmatch(r"\d{1,24}", tid):
            return {"ok": False, "message": "帖子编号无效"}
        if not text.strip() and not sub_pid.isdigit():
            return {"ok": False, "message": "请提供目标正文"}
        if not body.strip():
            return {"ok": False, "message": "请提供测试正文"}
        from .platforms.specs import site_spec
        spec = site_spec("tieba") or {}
        fingerprint = self.storage_fingerprint("tieba", name)
        context = await self.new_context(self.storage_path("tieba", name))
        await self.hydrate_cookies(context, "tieba", name)
        page = await context.new_page()
        target = {"content": text, "parent_pid": parent_pid, "sub_pid": sub_pid}
        requests: list[dict[str, Any]] = []
        try:
            await page.goto(f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3200)
            captcha = await self.solve_captcha(page, "tieba", "回复预演")
            if captcha.get("had") and not captcha.get("solved"):
                return {"ok": False, "message": captcha.get("message") or "验证未通过"}
            if captcha.get("solved"):
                await self.persist_storage(context, "tieba", name,
                                           expected_fingerprint=fingerprint)
            found = await self._tieba_load_reply_target_pages(page, target)
            if not found:
                await page.goto(f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb",
                                wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2800)
                await self._tieba_sort_comments(page, "正序")
                await page.wait_for_timeout(1600)
                found = await self._tieba_load_reply_target(page, target, allow_scroll=True)
            if not found:
                return {"ok": True, "found": False, "stage": "target",
                        "expand_trace": self._last_expand_trace[-20:]}
            reply = page.locator("[data-cyber-reply='1']").first
            if not await reply.count():
                return {"ok": True, "found": True, "stage": "reply-button"}
            await reply.click(timeout=5000)
            editor = None
            for _ in range(5):
                await page.wait_for_timeout(900)
                if await self._tieba_mark_editor_for_target(page):
                    editor = page.locator("[data-cyber-editor='1']").first
                    break
            if editor is None:
                return {"ok": True, "found": True, "stage": "editor",
                        "state": await self._tieba_state(page)}
            await self._fill_tieba_editor(page, editor, body)
            accepted = await self._editor_contains(editor, body)
            button = await self._tieba_submit(page, spec)
            button_info: dict[str, Any] = {"found": bool(button)}
            if button is not None:
                button_info.update({
                    "disabled": await self._tieba_button_disabled(button),
                    "html": str(await button.evaluate(
                        "(node) => String(node.outerHTML || '').slice(0, 800)"
                    )),
                })
            async def gate(route, request):
                if request.method == "POST":
                    requests.append({
                        "url": request.url[:180],
                        "type": request.resource_type,
                        "post_bytes": len(request.post_data or ""),
                    })
                    if "/c/c/post/add_pc" in request.url:
                        await route.abort()
                    else:
                        await route.continue_()
                else:
                    await route.continue_()
            await page.route("**/*", gate)
            clicked = False
            if button is not None and not button_info.get("disabled"):
                await button.click(timeout=5000)
                clicked = True
            await page.wait_for_timeout(4500)
            captcha = None
            if verify:
                captcha = await self.solve_captcha(page, "tieba", "回复预演")
            await page.unroute("**/*", gate)
            return {"ok": True, "found": True, "stage": "clicked" if clicked else "button-disabled",
                    "accepted": accepted, "clicked": clicked, "requests": requests,
                    "button": button_info,
                    "captcha": captcha,
                    "editor_kept": await self._editor_contains(editor, body),
                    "notice": (await self._page_notice(page))[:700],
                    "state": await self._tieba_state(page)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"回复预演失败：{exc}"}
        finally:
            await context.close()

    async def _probe_tieba_comment_flow(self, page: Any, tid: str, target: str,
                                        parent_pid: str, sub_pid: str) -> dict[str, Any]:
        def snapshot(extra: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"extra": extra or {}}

        try:
            await page.goto(f"https://tieba.baidu.com/p/{tid}?showComment=1&fr=pb",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3200)
            await self.solve_captcha(page, "tieba", "探测评论流")
            flow = {}
            flow["before"] = await self._probe_tieba_snapshot(page)
            await self._tieba_sort_comments(page)
            await page.wait_for_timeout(1600)
            flow["after_sort"] = await self._probe_tieba_snapshot(page)
            expanded = await self._tieba_expand_comments(page)
            await page.wait_for_timeout(900)
            flow["after_expand"] = await self._probe_tieba_snapshot(page, {"expanded": expanded})
            flow["expand_trace"] = list(self._last_expand_trace)
            for _ in range(4):
                await self._tieba_scroll_comments(page)
                await page.wait_for_timeout(900)
            flow["after_scroll"] = await self._probe_tieba_snapshot(page)
            flow["found"] = await self._tieba_mark_reply_target(
                page, {"content": target, "parent_pid": parent_pid, "sub_pid": sub_pid}
            )
            return flow
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__}

    async def _probe_tieba_snapshot(self, page: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            data = await page.evaluate(
                """() => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const comments = [...document.querySelectorAll('.pb-comment-item')];
                    const replies = [...document.querySelectorAll('.pb-lzl-item')];
                    return {
                        body: String(document.body?.innerText || ''),
                        counts: {comments: comments.length, replies: replies.length},
                        comments: comments.slice(0, 14).map((node) => clean(node.innerText).slice(0, 150)),
                        replies: replies.slice(0, 14).map((node) => clean(node.innerText).slice(0, 150)),
                        expanders: [...document.querySelectorAll('*')]
                            .filter((node) => node.children.length === 0
                                && /^展开\\s*\\d+\\s*条回复/.test(clean(node.innerText)))
                            .slice(0, 20)
                            .map((node) => ({
                                tag: String(node.tagName || ''),
                                cls: String(node.className || '').slice(0, 100),
                                text: clean(node.innerText).slice(0, 80)
                            })),
                        floor_replies: comments.slice(0, 14).map((node) => ({
                            id: String(node.getAttribute('data-id') || ''),
                            text: clean(node.innerText).slice(0, 80),
                            replies: node.querySelectorAll('.pb-lzl-item').length,
                            expanders: [...node.querySelectorAll('*')]
                                .filter((child) => child.children.length === 0
                                    && /^展开\\s*\\d+\\s*条回复/.test(clean(child.innerText)))
                                .length
                        })),
                        pagination: [...document.querySelectorAll('[class*="page"], [class*="pagination"]')]
                            .slice(0, 12).map((node) => ({
                                cls: String(node.className || '').slice(0, 90),
                                text: clean(node.innerText).slice(0, 100)
                            }))
                    };
                }"""
            )
            body = str((data or {}).get("body") or "")
            return {
                "head": body[:500],
                "counts": (data or {}).get("counts"),
                "comments": (data or {}).get("comments"),
                "replies": (data or {}).get("replies"),
                "expanders": (data or {}).get("expanders"),
                "floor_replies": (data or {}).get("floor_replies"),
                "pagination": (data or {}).get("pagination"),
                "extra": extra or {},
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__, "extra": extra or {}}

    async def _tieba_load_reply_target(self, page: Any, target: dict[str, Any],
                                       *, allow_scroll: bool = True) -> bool:
        """Expand and scroll Tieba's virtual comment list until the target exists."""
        text = str(target.get("content") or "").strip()
        sub_pid = str(target.get("sub_pid") or "")
        parent_pid = str(target.get("parent_pid") or "")
        if len(text) < 4 and not sub_pid.isdigit() and not parent_pid.isdigit():
            return False
        if not await self._tieba_sort_comments(page):
            logger.info("[cyber-life] 贴吧评论排序入口未命中，按当前顺序加载")
        signature = None
        stalled = 0
        for _ in range(40):
            if await self._tieba_mark_reply_target(page, target):
                return True
            expanded = await self._tieba_expand_comments(page)
            if await self._tieba_mark_reply_target(page, target):
                return True
            if not allow_scroll:
                break
            state = await self._tieba_scroll_comments(page)
            current = (str(state.get("first") or ""), str(state.get("last") or ""),
                       int(state.get("items") or 0), int(state.get("top") or 0))
            if expanded == 0 and current == signature:
                stalled += 1
                if stalled >= 6:
                    break
            else:
                stalled = 0
            signature = current
            await page.wait_for_timeout(900)
            captcha = await self.solve_captcha(page, "tieba", "加载回复")
            if captcha.get("had") and not captcha.get("solved"):
                return False
        return await self._tieba_mark_reply_target(page, target)

    async def _tieba_sort_comments(self, page: Any, label: str = "倒序") -> bool:
        try:
            return bool(await page.evaluate(
                """(label) => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return !!rect.width && !!rect.height && style.visibility !== 'hidden';
                    };
                    const nodes = [...document.querySelectorAll('div,span,button,a')]
                        .filter((node) => clean(node.innerText) === label && visible(node));
                    if (!nodes.length) return false;
                    const node = nodes[0];
                    node.scrollIntoView({block: 'center'});
                    node.click();
                    return true;
                }""", label
            ))
        except Exception:  # noqa: BLE001
            return False

    async def _tieba_expand_state(self, page: Any) -> dict[str, int]:
        """Count the nodes that change when a folded reply list opens."""
        try:
            value = await page.evaluate(
                """() => ({
                    body: String(document.body?.innerText || '').length,
                    comments: document.querySelectorAll('.pb-comment-item').length,
                    replies: document.querySelectorAll('.pb-lzl-item').length,
                })"""
            )
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _tieba_expand_candidates(self, page: Any) -> list[dict[str, Any]]:
        """Find every folded reply label by its text, independent of CSS class."""
        try:
            value = await page.evaluate(
                """() => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return !!rect.width && !!rect.height
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || 1) > 0;
                    };
                    const pattern = /^(展开|查看全部|查看更多)\\s*(\\d+\\s*条)?\\s*(回复|评论)|^(展开|查看)更多(回复|评论)/;
                    document.querySelectorAll('[data-cyber-expand]').forEach((node) => {
                        node.removeAttribute('data-cyber-expand');
                    });
                    const all = [...document.querySelectorAll('div,span,a,button,p,li')]
                        .filter((node) => {
                            const text = clean(node.innerText);
                            return text.length <= 24 && pattern.test(text) && visible(node);
                        });
                    const deepest = all.filter((node) => {
                        const parent = node.parentElement;
                        return !parent || !all.includes(parent)
                            || clean(parent.innerText) !== clean(node.innerText);
                    });
                    const rows = [];
                    deepest.slice(0, 30).forEach((node, index) => {
                        const rect = node.getBoundingClientRect();
                        const id = 'e' + index;
                        node.setAttribute('data-cyber-expand', id);
                        rows.push({
                            id, tag: String(node.tagName || ''),
                            cls: String(node.className || '').slice(0, 160),
                            text: clean(node.innerText).slice(0, 60),
                            html: String(node.outerHTML || '').slice(0, 600),
                            w: Math.round(rect.width), h: Math.round(rect.height)
                        });
                    });
                    return rows;
                }"""
            )
            return value if isinstance(value, list) else []
        except Exception:  # noqa: BLE001
            return []

    async def _tieba_expand_changed(self, page: Any, selector: str,
                                    before: dict[str, int]) -> bool:
        after = await self._tieba_expand_state(page)
        try:
            label = (await page.locator(selector).first.inner_text(timeout=800)).strip()
        except Exception:  # noqa: BLE001
            label = ""
        if label and not re.search(r"展开|查看全部|查看更多", label):
            return True
        if not label:
            return True
        if int(after.get("body") or 0) >= int(before.get("body") or 0) + 40:
            return True
        if int(after.get("replies") or 0) > int(before.get("replies") or 0):
            return True
        return False

    async def _tieba_click_expander(self, page: Any, item: dict[str, Any]) -> bool:
        """Open one folded list, preferring a real mouse click over DOM events."""
        eid = str(item.get("id") or "")
        if not eid:
            return False
        selector = f'[data-cyber-expand="{eid}"]'
        before = await self._tieba_expand_state(page)
        trace: dict[str, Any] = {
            "id": eid, "tag": item.get("tag"), "cls": item.get("cls"),
            "text": item.get("text"), "tried": [], "ok": False,
        }
        opened = False
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                return False
            await locator.scroll_into_view_if_needed(timeout=1800)
            await page.wait_for_timeout(300)
            box = await locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                await page.mouse.move(x, y)
                await page.wait_for_timeout(120)
                await page.mouse.click(x, y)
                trace["tried"].append("mouse")
                await page.wait_for_timeout(1600)
                opened = await self._tieba_expand_changed(page, selector, before)
            if not opened:
                await page.evaluate(
                    """(selector) => {
                        const node = document.querySelector(selector);
                        if (!node) return;
                        const targets = [node, node.parentElement,
                            node.parentElement?.parentElement].filter(Boolean);
                        const opts = {bubbles: true, cancelable: true, composed: true, view: window};
                        for (const target of targets) {
                            target.dispatchEvent(new PointerEvent('pointerdown', opts));
                            target.dispatchEvent(new MouseEvent('mousedown', opts));
                            target.dispatchEvent(new PointerEvent('pointerup', opts));
                            target.dispatchEvent(new MouseEvent('mouseup', opts));
                            target.click();
                        }
                    }""", selector)
                trace["tried"].append("dispatch")
                await page.wait_for_timeout(1600)
                opened = await self._tieba_expand_changed(page, selector, before)
            if not opened:
                try:
                    await locator.click(timeout=2500, force=True)
                    trace["tried"].append("locator")
                    await page.wait_for_timeout(1600)
                    opened = await self._tieba_expand_changed(page, selector, before)
                except Exception as exc:  # noqa: BLE001
                    trace["error"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            trace["error"] = type(exc).__name__
        finally:
            trace["ok"] = opened
            trace.setdefault("before", before)
            try:
                trace["after"] = await self._tieba_expand_state(page)
            except Exception:  # noqa: BLE001
                trace["after"] = {}
            self._last_expand_trace.append(trace)
        return opened

    async def _tieba_expand_comments(self, page: Any) -> int:
        expanded = 0
        self._last_expand_trace = []
        for _ in range(6):
            candidates = await self._tieba_expand_candidates(page)
            if not candidates:
                break
            opened = 0
            for item in candidates:
                if await self._tieba_click_expander(page, item):
                    expanded += 1
                    opened += 1
                    await page.wait_for_timeout(900)
                    # The list re-renders after opening one fold; indices move.
                    break
            if opened == 0:
                break
        self._last_expand_trace = self._last_expand_trace[-40:]
        return expanded

    async def _tieba_scroll_comments(self, page: Any) -> dict[str, int]:
        try:
            state = await page.evaluate(
                """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        return !!rect.width && !!rect.height;
                    };
                    const boxes = [...document.querySelectorAll(
                        '.pc-pb-box.styled-scrollbar, .styled-scrollbar, .pb-comment-list'
                    )].filter(visible);
                    const box = boxes.find((node) => node.scrollHeight > node.clientHeight + 80);
                    if (box) {
                        box.scrollTop += Math.max(420, box.clientHeight * 0.8);
                    }
                    window.scrollBy(0, Math.max(620, window.innerHeight * 0.8));
                    return {
                        items: document.querySelectorAll('.pb-comment-item, .pb-lzl-item').length,
                        chars: String(document.body?.innerText || '').length,
                        box: box ? box.scrollHeight : 0,
                        page: document.body.scrollHeight,
                        first: document.querySelector('.pb-comment-item')?.getAttribute('data-id') || '',
                        last: [...document.querySelectorAll('.pb-comment-item')].slice(-1)[0]?.getAttribute('data-id') || '',
                        top: Math.max(
                            Number(window.scrollY || 0),
                            Number(document.documentElement?.scrollTop || 0),
                            Number(box?.scrollTop || 0)
                        )
                    };
                }"""
            )
            try:
                await page.mouse.move(360, 520)
                await page.mouse.wheel(0, 850)
            except Exception:  # noqa: BLE001
                pass
            return state if isinstance(state, dict) else {"items": 0, "chars": 0, "box": 0, "page": 0}
        except Exception:  # noqa: BLE001
            return {"items": 0, "chars": 0, "box": 0, "page": 0}

    async def _tieba_mark_reply_target(self, page: Any, target: dict[str, Any]) -> bool:
        text = str(target.get("content") or "").strip()
        sub_pid = str(target.get("sub_pid") or "")
        parent_pid = str(target.get("parent_pid") or "")
        texts = self._tieba_match_candidates(text)
        try:
            value = await page.evaluate(
                """(args) => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const compact = (value) => String(value || '').replace(/\\s+/g, '');
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return !!rect.width && !!rect.height && style.visibility !== 'hidden';
                    };
                    document.querySelectorAll('[data-cyber-target], [data-cyber-reply]')
                        .forEach((node) => {
                            node.removeAttribute('data-cyber-target');
                            node.removeAttribute('data-cyber-reply');
                        });
                    const mark = (item, reply, kind) => {
                        item.setAttribute('data-cyber-target', '1');
                        reply.setAttribute('data-cyber-reply', '1');
                        reply.scrollIntoView({block: 'center'});
                        return {ok: true, kind, text: clean(item.innerText).slice(0, 160)};
                    };
                    const texts = (args.texts || []).map(compact)
                        .filter((text) => text.length >= 4);
                    for (const text of texts) {
                        const items = [...document.querySelectorAll('.pb-lzl-item, .pb-comment-item')]
                            .filter((node) => visible(node) && compact(node.innerText).includes(text))
                            .sort((a, b) => compact(a.innerText).length - compact(b.innerText).length);
                        for (const item of items) {
                            const reply = [...item.querySelectorAll('.reply, .reply-container')]
                                .find((node) => visible(node) && clean(node.innerText) === '回复');
                            if (reply) return mark(item, reply, 'text');
                        }
                    }
                    const sub = String(args.sub_pid || '');
                    if (sub) {
                        const exact = document.querySelector('[data-id="' + sub + '"]');
                        if (exact && visible(exact)) {
                            const item = exact.closest('.pb-lzl-item, .pb-comment-item') || exact;
                            const reply = [...item.querySelectorAll('.reply, .reply-container')]
                                .find((node) => visible(node) && clean(node.innerText) === '回复');
                            if (reply) return mark(item, reply, 'sub_pid');
                        }
                    }
                    const parent = String(args.parent_pid || '');
                    if (parent && !sub) {
                        const exact = document.querySelector('[data-id="' + parent + '"]');
                        if (exact && visible(exact)) {
                            const item = exact.closest('.pb-comment-item') || exact;
                            const reply = [...item.querySelectorAll('.reply, .reply-container')]
                                .find((node) => visible(node) && clean(node.innerText) === '回复');
                            if (reply) return mark(item, reply, 'parent_pid');
                        }
                    }
                    return {ok: false, items: document.querySelectorAll(
                        '.pb-comment-item, .pb-lzl-item'
                    ).length};
                }""",
                {"texts": texts, "sub_pid": sub_pid, "parent_pid": parent_pid},
            )
            return bool(isinstance(value, dict) and value.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def _tieba_match_candidates(self, text: str) -> list[str]:
        """Strip rendered emoticon markup and keep stable matching fragments."""
        cleaned = re.sub(r"#\([^)]{0,20}\)", "", text)
        cleaned = re.sub(r"\[[^\]]{1,16}\]", "", cleaned)
        cleaned = re.sub(r"[\u200b-\u200d\ufeff]", "", cleaned)
        compact = re.sub(r"\s+", "", cleaned)
        candidates = [compact]
        if len(compact) > 36:
            candidates.append(compact[:36])
        unquoted = re.sub(r"^回复[^:：]{0,40}[:：]", "", compact)
        if unquoted and unquoted != compact:
            candidates.append(unquoted[:36])
        return list(dict.fromkeys(item for item in candidates if len(item) >= 4))

    async def _page_notice(self, page: Any) -> str:
        try:
            return (await page.locator("body").inner_text(timeout=2000)).strip()[:500]
        except Exception:  # noqa: BLE001
            return ""

    async def _tieba_open_reply_target(self, page: Any, target: dict[str, Any],
                                       spec: dict[str, Any]):
        if not await self._tieba_comment_view(page):
            return None
        found = await self._tieba_load_reply_target_pages(page, target)
        if not found:
            # The page walk finishes on the last duplicate page. Reopen the
            # top of the comment view and search chronologically, where the
            # virtual list can render floors that later pages never mount.
            match = re.search(r"/p/(\d+)", str(page.url))
            if match:
                try:
                    await page.goto(
                        f"https://tieba.baidu.com/p/{match.group(1)}?showComment=1&fr=pb",
                        wait_until="domcontentloaded", timeout=45000,
                    )
                    await page.wait_for_timeout(2800)
                    captcha = await self.solve_captcha(page, "tieba", "回复目标")
                    if not (captcha.get("had") and not captcha.get("solved")):
                        await self._tieba_sort_comments(page, "正序")
                        await page.wait_for_timeout(1600)
                        found = await self._tieba_load_reply_target(
                            page, target, allow_scroll=True
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.info("[cyber-life] 贴吧目标页重开失败 error_type=%s",
                                type(exc).__name__)
        if not found:
            return None
        reply = page.locator("[data-cyber-reply='1']").first
        try:
            if not await reply.count() or not await reply.is_visible(timeout=1000):
                return None
            await reply.click(timeout=5000)
        except Exception:  # noqa: BLE001
            return None
        for _ in range(4):
            await page.wait_for_timeout(900)
            if await self._tieba_mark_editor_for_target(page):
                return page.locator("[data-cyber-editor='1']").first
        logger.info("[cyber-life] 贴吧目标回复已点开但输入框未就绪 state=%s",
                    await self._tieba_state(page))
        return None

    async def _tieba_comment_view(self, page: Any) -> bool:
        """Open the comment view before looking for a specific floor reply."""
        parsed = urlsplit(page.url)
        if not re.fullmatch(r"/p/\d+/?", parsed.path):
            return True
        query = parse_qs(parsed.query)
        if query.get("showComment") == ["1"] and query.get("fr") == ["pb"]:
            return True
        query["showComment"] = ["1"]
        query["fr"] = ["pb"]
        target_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"),
                                 urlencode(query, doseq=True), ""))
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2600)
            captcha = await self.solve_captcha(page, "tieba", "回复页")
            if captcha.get("had") and not captcha.get("solved"):
                logger.info("[cyber-life] 贴吧评论页验证未过")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("[cyber-life] 贴吧评论页打开失败 error_type=%s",
                        type(exc).__name__)
            return False

    async def _tieba_mark_editor_for_target(self, page: Any) -> bool:
        try:
            value = await page.evaluate(
                """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return !!rect.width && !!rect.height && style.visibility !== 'hidden';
                    };
                    const item = document.querySelector('[data-cyber-target="1"]');
                    const selectors = [
                        '.ql-editor', '.edui-body-container', '#ueditor_replace',
                        'textarea', '[contenteditable="true"]', '[role="textbox"]'
                    ].join(',');
                    const editors = [...document.querySelectorAll(selectors)].filter(visible);
                    document.querySelectorAll('[data-cyber-editor]').forEach((node) => {
                        node.removeAttribute('data-cyber-editor');
                    });
                    if (!editors.length) return false;
                    const inside = item
                        ? editors.filter((node) => item.contains(node))
                        : [];
                    if (inside.length) {
                        inside[inside.length - 1].setAttribute('data-cyber-editor', '1');
                        return true;
                    }
                    if (editors.length === 1) {
                        editors[0].setAttribute('data-cyber-editor', '1');
                        return true;
                    }
                    const anchor = item ? item.getBoundingClientRect() : null;
                    const distance = (node) => {
                        const rect = node.getBoundingClientRect();
                        if (!anchor) return rect.top;
                        return Math.abs(rect.top - anchor.top) + Math.abs(rect.left - anchor.left) / 10;
                    };
                    editors.sort((a, b) => distance(a) - distance(b));
                    editors[0].setAttribute('data-cyber-editor', '1');
                    return true;
                }"""
            )
            return bool(value)
        except Exception:  # noqa: BLE001
            return False

    async def _tieba_editor(self, page: Any, spec: dict[str, Any], *, mobile: bool = False):
        selectors = list(spec.get("editor_selectors") or []) + [
            "#ueditor_replace",
            ".reply_textarea textarea",
            ".edui-body-container",
            ".ql-editor",
            "textarea",
            "[role='textbox']",
            "textarea[placeholder*='回复']",
            "textarea[placeholder*='评论']",
            "div[contenteditable='true']",
        ]

        async def find_editor():
            frames = [page]
            try:
                frames.extend(frame for frame in page.frames if frame is not page.main_frame)
            except Exception:  # noqa: BLE001
                pass
            for frame in frames:
                for selector in dict.fromkeys(selectors):
                    try:
                        locator = frame.locator(selector).first
                        if await locator.count() and await locator.is_visible(timeout=900):
                            return locator
                    except Exception:  # noqa: BLE001
                        continue
            return None

        editor = await find_editor()
        if editor is not None:
            return editor
        # Some routes mount the editor only after scrolling to the reply area.
        try:
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
        editor = await find_editor()
        if editor is not None:
            return editor
        # 新版页面先给一个“回复/发表回复”入口，点开后才挂载输入框。
        openers = list(spec.get("comment_open_selectors") or []) + [
            "a.comment-link-zone",
            "a[href*='showComment=1']",
            ".pc-pb-reply-box a",
            ".pc-pb-reply-box",
            ".core_title_btns a:has-text('回复')",
            "a:has-text('发表回复')",
            "button:has-text('发表回复')",
            "button:has-text('回复')",
            "a:has-text('回复')",
            ".p_reply a",
            "a.p_reply",
            "[data-pid] a:has-text('回复')",
            "#quick_reply",
            ".poster_reply",
            ".reply_btn",
            "[class*='reply_bar']",
            "[class*='reply-bar']",
            "[class*='reply']:has-text('回复')",
        ]
        for selector in dict.fromkeys(openers):
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=700):
                    before_url = page.url
                    try:
                        await locator.scroll_into_view_if_needed(timeout=1500)
                    except Exception:  # noqa: BLE001
                        pass
                    await locator.click(timeout=3500)
                    await page.wait_for_timeout(1200)
                    if page.url != before_url:
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:  # noqa: BLE001
                            pass
                        await page.wait_for_timeout(1200)
                    editor = await find_editor()
                    if editor is not None:
                        return editor
            except Exception:  # noqa: BLE001
                continue
        notice = await self._page_notice(page)
        logger.info("[cyber-life] 贴吧评论框探测未命中 page=%s state=%s reply_outline=%s text=%s",
                    str(getattr(page, "url", ""))[:100], await self._tieba_state(page),
                    await self._tieba_reply_outline(page),
                    notice[:260].replace("\n", " | "))
        return None

    async def _tieba_reply_outline(self, page: Any) -> list[dict[str, Any]]:
        try:
            value = await page.evaluate(
                """() => {
                    const hits = [];
                    document.querySelectorAll('a,button,div,span').forEach((node) => {
                        const text = (node.innerText || '').trim();
                        if ((text === '回复' || text.includes('去APP回复')) && node.children.length <= 1) {
                            const rect = node.getBoundingClientRect();
                            hits.push({
                                tag: node.tagName,
                                cls: String(node.className || '').slice(0, 60),
                                txt: text.slice(0, 30),
                                href: node.getAttribute('href') || '',
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                            });
                        }
                    });
                    return hits.slice(-12);
                }"""
            )
            return value if isinstance(value, list) else []
        except Exception:  # noqa: BLE001
            return []

    async def _fill_tieba_editor(self, page: Any, editor: Any, text: str) -> None:
        await editor.click(timeout=5000)
        await page.wait_for_timeout(250)
        try:
            await editor.fill(text, timeout=3000)
        except Exception:  # noqa: BLE001
            try:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
            except Exception:  # noqa: BLE001
                pass
            await page.keyboard.insert_text(text)
        await page.wait_for_timeout(450)

    async def _editor_contains(self, editor: Any, text: str) -> bool:
        try:
            value = await editor.input_value(timeout=1200)
        except Exception:  # noqa: BLE001
            try:
                value = await editor.inner_text(timeout=1200)
            except Exception:  # noqa: BLE001
                return False
        return text.strip() in (value or "")

    async def _tieba_submit(self, page: Any, spec: dict[str, Any]):
        # The reply editor is mounted inside the target item; its own publish
        # button is the only one that belongs to this reply.  Prefer it before
        # falling back to a page-wide selector, which can hit the main comment
        # box at the bottom of the thread.
        try:
            item = page.locator('[data-cyber-target="1"]').first
            if await item.count():
                local = item.locator(
                    ".publish-btn, [class*='publish-btn'], button, [role='button']"
                )
                total = await local.count()
                for index in range(min(total, 8)):
                    node = local.nth(index)
                    try:
                        label = (await node.inner_text(timeout=600)).strip()
                        cls = str(await node.get_attribute("class", timeout=600) or "")
                        if (await node.is_visible(timeout=600)
                                and ("发布" in label or "发送" in label
                                     or "publish" in cls)):
                            return node
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        selectors = list(spec.get("submit_selectors") or []) + [
            ".poster_submit",
            ".reply-btn",
            ".send-btn",
            ".submit-btn",
            ".publish-btn",
            ".pc-focus-reply-box .publish-btn",
            "[class*='publish-btn']",
            ".comment-submit",
            ".comment-btn",
            ".reply-submit",
            "input[type='submit']",
            "input[type='button']",
            "input[value*='发表']",
            "input[value*='发送']",
            "button:has-text('发表')",
            "button:has-text('发送')",
            "button:has-text('发布')",
            "button:has-text('评论')",
            "a:has-text('发送')",
            "a:has-text('发布')",
            "span:has-text('发表')",
            "div[role='button']:has-text('发表')",
            ".edui-button-body:has-text('发表')",
            "[class*='submit']:has-text('发表')",
            "[class*='comment'] button",
            "[class*='comment'] [role='button']",
        ]
        found = []
        for selector in dict.fromkeys(selectors):
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(min(count, 6)):
                    item = locator.nth(index)
                    if await item.is_visible(timeout=600):
                        found.append(item)
            except Exception:  # noqa: BLE001
                continue
        if found:
            return found[-1]
        logger.info("[cyber-life] 贴吧发送按钮未命中 page=%s state=%s buttons=%s html=%s",
                    str(getattr(page, "url", ""))[:100], await self._tieba_state(page),
                    await self._tieba_button_outline(page),
                    await self._tieba_content_html(page))
        return None

    async def _tieba_button_disabled(self, button: Any) -> bool:
        try:
            return bool(await button.evaluate(
                """(node) => node.classList.contains('button-wrapper-disabled')
                    || node.getAttribute('aria-disabled') === 'true'
                    || node.hasAttribute('disabled')"""
            ))
        except Exception:  # noqa: BLE001
            return False

    async def _tieba_button_outline(self, page: Any) -> list[dict[str, Any]]:
        try:
            value = await page.evaluate(
                """() => {
                    const hits = [];
                    document.querySelectorAll('button,input,a,span,div[role="button"]').forEach((node) => {
                        const text = String(node.innerText || node.value || '').trim();
                        const cls = String(node.className || '');
                        if (!text && !/submit|send|comment|reply|publish|post/i.test(cls)) return;
                        const rect = node.getBoundingClientRect();
                        if (!rect.width || !rect.height) return;
                        hits.push({
                            tag: node.tagName,
                            type: node.getAttribute('type') || '',
                            cls: cls.slice(0, 60),
                            txt: text.slice(0, 24),
                            w: Math.round(rect.width),
                            h: Math.round(rect.height),
                        });
                    });
                    return hits.slice(-24);
                }"""
            )
            return value if isinstance(value, list) else []
        except Exception:  # noqa: BLE001
            return []

    JS_SUBMIT_CANDIDATES = """() => {
        const editor = document.querySelector('[contenteditable="true"], textarea');
        if (!editor) return {ok: false, count: 0, outline: []};
        let root = editor.closest('form, [class*="comment"], [class*="reply"]');
        if (!root) {
            root = editor.parentElement || editor;
            for (let i = 0; i < 4 && root.parentElement; i += 1) root = root.parentElement;
        }
        const visible = [];
        root.querySelectorAll('button,a,div,span,i,em').forEach((node) => {
            if (node === editor || node.isContentEditable) return;
            const text = String(node.innerText || node.value || '').trim();
            const cls = String(node.className || '');
            const aria = String(node.getAttribute('aria-label') || '')
                + String(node.getAttribute('title') || '');
            if (text === '回复' || cls.includes('comment-link-zone')) return;
            const wanted = ['发送', '发表', '发布', '提交'].includes(text)
                || /send|submit|publish|post-btn/i.test(cls)
                || /发送|发表|发布|提交/.test(aria);
            if (!wanted) return;
            const rect = node.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            visible.push({node, text, cls, rect});
        });
        return {
            ok: visible.length > 0,
            count: visible.length,
            outline: visible.slice(-8).map((item) => ({
                tag: item.node.tagName,
                cls: item.cls.slice(0, 60),
                txt: item.text.slice(0, 24),
                w: Math.round(item.rect.width),
                h: Math.round(item.rect.height),
            })),
        };
    }"""

    async def _tieba_js_submit_target(self, page: Any) -> dict[str, Any]:
        try:
            value = await page.evaluate(self.JS_SUBMIT_CANDIDATES)
            return value if isinstance(value, dict) else {"ok": False, "outline": []}
        except Exception:  # noqa: BLE001
            return {"ok": False, "outline": []}

    async def _tieba_js_submit_click(self, page: Any) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """() => {
                    const editor = document.querySelector('[contenteditable="true"], textarea');
                    if (!editor) return {clicked: false};
                    let root = editor.closest('form, [class*="comment"], [class*="reply"]');
                    if (!root) {
                        root = editor.parentElement || editor;
                        for (let i = 0; i < 4 && root.parentElement; i += 1) root = root.parentElement;
                    }
                    const visible = [];
                    root.querySelectorAll('button,a,div,span,i,em').forEach((node) => {
                        if (node === editor || node.isContentEditable) return;
                        const text = String(node.innerText || node.value || '').trim();
                        const cls = String(node.className || '');
                        const aria = String(node.getAttribute('aria-label') || '')
                            + String(node.getAttribute('title') || '');
                        if (text === '回复' || cls.includes('comment-link-zone')) return;
                        const wanted = ['发送', '发表', '发布', '提交'].includes(text)
                            || /send|submit|publish|post-btn/i.test(cls)
                            || /发送|发表|发布|提交/.test(aria);
                        if (!wanted) return;
                        const rect = node.getBoundingClientRect();
                        if (!rect.width || !rect.height) return;
                        visible.push(node);
                    });
                    if (!visible.length) return {clicked: false};
                    const target = visible[visible.length - 1];
                    target.scrollIntoView({block: 'center'});
                    target.click();
                    return {clicked: true, text: String(target.innerText || '').trim().slice(0, 24)};
                }"""
            )
        except Exception:  # noqa: BLE001
            return {"clicked": False}

    async def _tieba_editor_html(self, page: Any) -> str:
        try:
            value = await page.evaluate(
                """() => {
                    const editor = document.querySelector('[contenteditable="true"], textarea');
                    if (!editor) return '';
                    let root = editor.closest('form, [class*="comment"], [class*="reply"]');
                    if (!root) {
                        root = editor.parentElement || editor;
                        for (let i = 0; i < 4 && root.parentElement; i += 1) root = root.parentElement;
                    }
                    return String(root.outerHTML || '').slice(0, 2400);
                }"""
            )
            return str(value or "").replace("\n", " ")[:2400]
        except Exception:  # noqa: BLE001
            return ""

    async def _text_count(self, page: Any, text: str) -> int:
        needle = text.strip()
        if not needle:
            return 0
        try:
            body = await page.locator("body").inner_text(timeout=2500)
        except Exception:  # noqa: BLE001
            return 0
        return body.count(needle)

    async def _tieba_reply_visible(self, page: Any, text: str) -> bool:
        """True only when the reply is already rendered as a comment node.

        Body text also contains the editor draft, so a plain text count would
        wrongly report success before the server accepted the comment.
        """
        needle = text.strip()
        if not needle:
            return False
        try:
            return bool(await page.evaluate(
                """(text) => {
                    const wanted = String(text).replace(/\\s+/g, '');
                    const nodes = Array.from(document.querySelectorAll(
                        '.pb-comment-item, .pb-lzl-item, [data-pid]'
                    ));
                    return nodes.some((node) => {
                        if (node.querySelector('textarea, [contenteditable="true"]')) return false;
                        return String(node.innerText || '').replace(/\\s+/g, '').includes(wanted);
                    });
                }""",
                needle,
            ))
        except Exception:  # noqa: BLE001
            return False

    async def _tieba_post_result(self, responses: list[Any]) -> str:
        return (await self._tieba_post_ack(responses))["state"]

    async def confirm_tieba_sent(self, name: str, url: str, text: str, pid: str,
                                author_id: str, target: dict[str, Any] | None) -> bool:
        """Read an existing reply without filling an editor or sending again."""
        retained = self.takeovers.for_account("tieba", name)
        context = retained.context if retained else await self.new_context(self.storage_path("tieba", name))
        if retained and retained.controlled:
            raise BrowserError("请先结束人工操作，再核对发送结果")
        page = retained.page if retained else await context.new_page()
        try:
            identity = await self._tieba_reply_identity(page)
            identity["id"] = author_id
            check_ack = {"state": "ok", "id": pid}
            if await self._tieba_confirm_reply(page, text, set(), identity, target, check_ack):
                return True
            await page.goto(url + "?showComment=1&fr=pb", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2600)
            identity = await self._tieba_reply_identity(page)
            identity["id"] = author_id
            await self._tieba_load_reply_target_pages(page, {"content": text, "sub_pid": pid}, max_pages=5)
            return bool(await self._tieba_confirm_reply(page, text, set(), identity, target, check_ack))
        finally:
            if not retained:
                await context.close()

    async def _tieba_submit_snapshot(self, response: Any, url: str, text: str,
                                     target: dict[str, Any] | None) -> dict[str, Any]:
        """Bind the success response to this page's actual draft and destination."""
        snapshot = await self._tieba_response_snapshot(response)
        snapshot["request_matches"] = False
        try:
            request = response.request
            parts = urlsplit(request.url)
            form = parse_qs(request.post_data or "", keep_blank_values=True)
            tid = re.search(r"/p/(\d+)", url)
            wanted = re.sub(r"\s+", "", text)
            content = str((form.get("content") or [""])[0])
            actual = re.sub(r"\s+", "", content)
            if actual != wanted:
                actual = re.sub(r"\s+", "", unescape(re.sub(
                    r"</?(?:p|br|div|span|b|strong|i|em)\b[^>]*>", "", content,
                    flags=re.IGNORECASE)))
            parent = str((target or {}).get("parent_pid") or "")
            sub = str((target or {}).get("sub_pid") or "")
            supplied_parent = str((form.get("quote_id") or form.get("pid") or [""])[0])
            snapshot["request_matches"] = bool(
                request.method == "POST" and parts.scheme == "https"
                and parts.hostname == "tieba.baidu.com" and parts.path == "/c/c/post/add_pc"
                and tid and (form.get("tid") or [""])[0] == tid.group(1)
                and wanted and actual == wanted
                and (not parent or supplied_parent in {parent, sub} - {""}))
        except (AttributeError, TypeError, ValueError):
            pass
        return snapshot

    async def _tieba_response_snapshot(self, response: Any) -> dict[str, Any]:
        """Cache bodies before navigation; a read failure is not an empty ACK."""
        snapshot = {"status": 0, "content_type": "", "body": "", "read_error": ""}
        if isinstance(response, asyncio.Future):
            try:
                return await asyncio.wait_for(asyncio.shield(response), timeout=3)
            except Exception as exc:  # noqa: BLE001
                snapshot["read_error"] = type(exc).__name__
                return snapshot
        try:
            snapshot["status"] = response.status
            snapshot["content_type"] = response.headers.get("content-type", "")
            snapshot["body"] = await asyncio.wait_for(response.text(), timeout=10) or ""
        except Exception as exc:  # noqa: BLE001
            snapshot["read_error"] = type(exc).__name__
        return snapshot

    async def _tieba_post_ack(self, responses: list[Any]) -> dict[str, str]:
        """Success wins over earlier rejections; ambiguous writes stay unknown."""
        failed = False
        unknown = not responses
        success = False
        sent_pid = ""
        confirmed_pid = ""
        for response in responses:
            snapshot = await self._tieba_response_snapshot(response)
            if snapshot["read_error"]:
                unknown = True
                continue
            try:
                body = snapshot["body"].strip()
                data = json.loads(body)
            except Exception:  # noqa: BLE001
                unknown = True
                continue
            if not isinstance(data, dict) or snapshot["status"] != 200:
                unknown = True
                continue
            code = data.get("error_code", data.get("errno", data.get("no", "")))
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            pid = str(data.get("pid") or data.get("post_id")
                      or nested.get("pid") or nested.get("post_id") or "")
            if str(code) == "0":
                success = True
                if pid.isdigit() and int(pid) > 0:
                    sent_pid = pid
                    if snapshot.get("request_matches"):
                        confirmed_pid = pid
            elif str(code).lstrip("-").isdigit() and not (pid.isdigit() and int(pid) > 0):
                failed = True
            else:
                unknown = True
        return {"state": "ok" if success else "unknown" if unknown else "failed" if failed else "unknown",
                "id": confirmed_pid or sent_pid, "confirmed_id": confirmed_pid}

    async def _tieba_reply_identity(self, page: Any) -> dict[str, str]:
        try:
            value = await page.evaluate("""() => {
                const user = window.PageData?.user || {};
                return {id: String(user.user_id || user.id || ''),
                    name: String(user.name_show || user.name || ''),
                    portrait: String(user.portrait || '')};
            }""")
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _tieba_reply_records(self, page: Any) -> list[dict[str, str]] | None:
        """Read reply nodes, including modern Vue and legacy Tieba pages."""
        try:
            value = await page.evaluate("""() => {
                const selector = '.pb-comment-item, .pb-lzl-item, .l_post, .lzl_single_post';
                const compact = (value) => String(value || '').replace(/\\s+/g, '');
                const numeric = (value) => /^\\d+$/.test(String(value || '')) ? String(value) : '';
                const metadata = (node) => {
                    let field = {};
                    try { field = JSON.parse(node.getAttribute('data-field') || '{}'); } catch {}
                    const props = node.__vueParentComponent?.props || {};
                    const info = props.post || props.comment || props.item || props.data
                        || props.postInfo || props.commentInfo || props;
                    return {id: numeric(node.getAttribute('data-id') || node.getAttribute('data-pid')
                            || node.getAttribute('data-post-id') || field.content?.post_id
                            || info.post_id || info.pid || info.id),
                        author: numeric(field.author?.user_id || info.author?.user_id
                            || info.author_id || info.user_id)};
                };
                return [...document.querySelectorAll(selector)].map((node) => {
                    const own = (query) => [...node.querySelectorAll(query)]
                        .find((child) => child.closest(selector) === node);
                    const content = own('.comment-content, .pb-rich-text, .d_post_content, .lzl_content_main');
                    const clone = (content || node).cloneNode(true);
                    clone.querySelectorAll(selector + ', textarea, [contenteditable="true"], .reply-input, .reply-area')
                        .forEach((child) => child.remove());
                    const name = own('.head-name, .p_author_name, .lzl_p_p a');
                    const profile = own('a[href*="/home/main"]');
                    let portrait = '';
                    try { portrait = new URL(profile?.href || '', location.href).searchParams.get('id') || ''; } catch {}
                    const parent = node.classList.contains('pb-lzl-item')
                        ? node.parentElement?.closest('.pb-comment-item')
                        : node.classList.contains('lzl_single_post') ? node.closest('.l_post') : null;
                    const data = metadata(node);
                    return {id: data.id, author_id: data.author,
                        author_name: compact(name?.textContent), portrait,
                        parent_pid: parent ? metadata(parent).id : '',
                        nested: !!parent || node.matches('.pb-lzl-item, .lzl_single_post'),
                        text: compact(clone.textContent)};
                });
            }""")
            return value if isinstance(value, list) else None
        except Exception:  # noqa: BLE001
            return None

    async def _tieba_confirm_reply(self, page: Any, text: str, before_ids: set[str] | None,
                                   identity: dict[str, str], target: dict[str, Any] | None,
                                   ack: dict[str, str]) -> str:
        # Body text, an editor draft, and an old toast cannot confirm a write.
        if ack.get("state") != "ok":
            return ""
        wanted = re.sub(r"\s+", "", text)
        acknowledged = ack.get("id", "")
        parent = str((target or {}).get("parent_pid") or "")
        acknowledged_node = False
        for record in await self._tieba_reply_records(page) or []:
            pid = str(record.get("id") or "")
            if acknowledged and pid == acknowledged:
                acknowledged_node = True
            if not pid.isdigit() or int(pid) <= 0 or pid in (before_ids or set()):
                continue
            exact_ack = bool(acknowledged) and pid == acknowledged
            if acknowledged and not exact_ack:
                continue
            if before_ids is None and not exact_ack:
                continue
            if not wanted or wanted not in re.sub(r"\s+", "", str(record.get("text") or "")):
                continue
            if not target and record.get("nested"):
                continue
            if parent and record.get("parent_pid") and str(record["parent_pid"]) != parent:
                continue
            if parent and not record.get("parent_pid") and not exact_ack:
                continue
            own = False
            for expected, actual in ((identity.get("id"), record.get("author_id")),
                                     (identity.get("portrait"), record.get("portrait")),
                                     (identity.get("name"), record.get("author_name"))):
                if expected and actual:
                    if re.sub(r"\s+", "", str(expected)) != re.sub(r"\s+", "", str(actual)):
                        break
                    own = True
            else:
                if own or exact_ack:
                    return pid
        # Vue can omit/unmount the newly published floor. A complete ACK for
        # this exact request confirms delivery even before the DOM renders it.
        confirmed = str(ack.get("confirmed_id") or "")
        if (not acknowledged_node and confirmed == acknowledged and confirmed.isdigit()
                and int(confirmed) > 0 and confirmed not in (before_ids or set())):
            logger.info("[cyber-life] 贴吧服务器已确认发送 pid=%s，页面暂未显示新回复", confirmed)
            return confirmed
        return ""

    async def _tieba_success_marker(self, page: Any) -> bool:
        for selector in ("text=发表成功", "text=回复成功", "text=评论成功",
                         ".poster_success", ".alert_success", ".success-tip"):
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=500):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _tieba_new_id(self, page: Any, text: str) -> str:
        try:
            return str(await page.evaluate(
                """(text) => {
                    const compact = (value) => String(value || '').replace(/\\s+/g, '');
                    const wanted = compact(text);
                    const nodes = Array.from(document.querySelectorAll(
                        '.pb-comment-item, .pb-lzl-item, [data-pid], [data-post-id], [data-id]'
                    ));
                    for (const node of nodes) {
                        if (compact(node.innerText).includes(wanted)) {
                            const id = node.getAttribute('data-id')
                                || node.getAttribute('data-pid')
                                || node.getAttribute('data-post-id') || '';
                            if (id) return id;
                        }
                    }
                    return '';
                }""",
                text,
            ) or "")
        except Exception:  # noqa: BLE001
            return ""


    async def screenshot(self, platform: str, name: str, url: str) -> Path:
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1800)
            target = self.data_dir / "media" / "shots" / f"{platform}-{int(now_tz().timestamp())}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target), type="png")
            return target
        finally:
            await context.close()
