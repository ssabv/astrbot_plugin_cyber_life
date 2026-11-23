"""Authenticated WebUI control of a retained Playwright page.

No desktop server, extra port, browser debugging endpoint or user-supplied
JavaScript is exposed. Input goes through the existing browser instance.
"""
from __future__ import annotations

import asyncio
import base64
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


class TakeoverError(RuntimeError):
    pass


@dataclass
class TakeoverSession:
    id: str
    context: Any
    page: Any
    platform: str
    account_name: str
    fingerprint: str | None
    job_id: str = ""
    original: bool = True
    controlled: bool = False
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry: asyncio.Task | None = None
    popup_handler: Any = None


class BrowserTakeovers:
    IDLE_SECONDS = 30 * 60
    MAX_SESSIONS = 3

    def __init__(self, browser: Any):
        self.browser = browser
        self.sessions: dict[str, TakeoverSession] = {}

    @staticmethod
    def _closed(page: Any) -> bool:
        check = getattr(page, "is_closed", None)
        return bool(check()) if callable(check) else False

    def for_account(self, platform: str, name: str) -> TakeoverSession | None:
        return next((s for s in self.sessions.values()
                     if s.platform == platform and s.account_name == name
                     and self._available(s)), None)

    def _available(self, session: TakeoverSession) -> bool:
        if not self._closed(session.page):
            return True
        pages = [p for p in session.context.pages if not self._closed(p)]
        if pages:
            session.page = pages[-1]
            return True
        return False

    def has_account(self, platform: str, name: str) -> bool:
        return self.for_account(platform, name) is not None

    def get(self, session_id: str) -> TakeoverSession:
        session = self.sessions.get(session_id)
        if session is None or not self._available(session):
            raise TakeoverError("浏览器现场已关闭或过期，请重新打开")
        return session

    @staticmethod
    def public(session: TakeoverSession) -> dict[str, Any]:
        return {"id": session.id, "platform": session.platform,
                "account_name": session.account_name, "job_id": session.job_id,
                "original": session.original, "controlled": session.controlled,
                "url": str(session.page.url)[:2000]}

    def list(self) -> list[dict[str, Any]]:
        return [self.public(s) for s in self.sessions.values() if self._available(s)]

    async def retain(self, context: Any, page: Any, platform: str, name: str,
                     fingerprint: str | None, *, job_id: str = "",
                     original: bool = True) -> TakeoverSession:
        existing = self.for_account(platform, name)
        if existing:
            raise TakeoverError("该账号已有保留的浏览器，请先结束接管")
        if len(self.sessions) >= self.MAX_SESSIONS:
            raise TakeoverError("最多保留三个浏览器，请先关闭其他接管页面")
        session = TakeoverSession(secrets.token_urlsafe(24), context, page, platform,
                                  name, fingerprint, job_id=job_id, original=original)
        self.sessions[session.id] = session
        def popup(new_page: Any) -> None:
            session.page = new_page
        session.popup_handler = popup
        try:
            context.on("page", popup)
        except AttributeError:
            pass
        session.expiry = asyncio.create_task(self._expire(session.id))
        return session

    async def _expire(self, session_id: str) -> None:
        while session_id in self.sessions:
            await asyncio.sleep(60)
            session = self.sessions.get(session_id)
            if session and (not self._available(session)
                            or time.monotonic() - session.touched > self.IDLE_SECONDS):
                await self.close(session_id, save=False)
                return

    async def claim(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        session.controlled = True
        session.touched = time.monotonic()
        return self.public(session)

    async def frame(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        async with session.lock:
            self.get(session_id)
            session.touched = time.monotonic()
            page = session.page
            payload = await page.screenshot(type="jpeg", quality=65,
                                                    full_page=False, timeout=15000)
            size = page.viewport_size or {"width": 1280, "height": 800}
            pages = [p for p in session.context.pages if not self._closed(p)]
            return {**self.public(session), "url": str(page.url)[:2000],
                    "page_id": str(id(page)), **size,
                    "image": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
                    "tabs": [{"index": i, "url": str(p.url)[:2000],
                              "active": p is page} for i, p in enumerate(pages)]}

    @staticmethod
    def _number(value: Any, low: float, high: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("坐标或滚动距离无效")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError("坐标或滚动距离超出范围")
        return float(value)

    async def input(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.get(session_id)
        if not session.controlled:
            raise TakeoverError("请先点击接管浏览器")
        async with session.lock:
            self.get(session_id)
            page = session.page
            if "page_id" in payload and payload["page_id"] != str(id(page)):
                raise TakeoverError("浏览器页面已切换，请等画面刷新后再操作")
            size = page.viewport_size or {"width": 1280, "height": 800}
            def point(p: dict[str, Any]) -> tuple[float, float]:
                if not isinstance(p, dict):
                    raise ValueError("坐标无效")
                return (self._number(p.get("x"), 0, size["width"]),
                        self._number(p.get("y"), 0, size["height"]))
            kind = payload.get("type")
            if kind == "click":
                x, y = point(payload)
                button = payload.get("button", "left")
                count = payload.get("count", 1)
                if button not in {"left", "right", "middle"} or count not in {1, 2}:
                    raise ValueError("点击参数无效")
                await page.mouse.click(x, y, button=button, click_count=count)
            elif kind == "drag":
                points = payload.get("points")
                if not isinstance(points, list) or not 2 <= len(points) <= 100:
                    raise ValueError("拖动轨迹无效")
                path = [point(p) for p in points]
                await page.mouse.move(*path[0])
                await page.mouse.down()
                try:
                    for x, y in path[1:]:
                        await page.mouse.move(x, y)
                        await asyncio.sleep(0.015)
                finally:
                    await page.mouse.up()
            elif kind == "wheel":
                dx = self._number(payload.get("dx", 0), -3000, 3000)
                dy = self._number(payload.get("dy", 0), -3000, 3000)
                await page.mouse.move(size["width"] / 2, size["height"] / 2)
                await page.mouse.wheel(dx, dy)
            elif kind == "text":
                text = payload.get("text")
                if not isinstance(text, str) or not 1 <= len(text) <= 8192:
                    raise ValueError("输入内容须为1到8192个字符")
                await page.keyboard.insert_text(text)
            elif kind == "key":
                key = payload.get("key")
                if key not in {"Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowLeft",
                               "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
                               "PageUp", "PageDown", "Space", "Control+A", "Shift+Enter", "Shift+Tab"}:
                    raise ValueError("不支持这个按键")
                await page.keyboard.press(key)
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            elif kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=45000)
            elif kind == "tab":
                index = payload.get("index")
                pages = [p for p in session.context.pages if not self._closed(p)]
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(pages):
                    raise ValueError("浏览器标签页已变化，请刷新")
                session.page = pages[index]
            else:
                raise ValueError("不支持这个浏览器操作")
            session.touched = time.monotonic()
            return {"ok": True}

    async def close(self, session_id: str, *, save: bool = True) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise TakeoverError("浏览器现场已关闭")
        async with session.lock:
            if session_id not in self.sessions:
                raise TakeoverError("浏览器现场已关闭")
            if save:
                session.fingerprint = await self.browser.persist_storage(
                    session.context, session.platform, session.account_name,
                    expected_fingerprint=session.fingerprint)
            self.sessions.pop(session_id)
            try:
                session.context.remove_listener("page", session.popup_handler)
            except AttributeError:
                pass
            if session.expiry and session.expiry is not asyncio.current_task():
                session.expiry.cancel()
                await asyncio.gather(session.expiry, return_exceptions=True)
            await session.context.close()
            return {"closed": True, "saved": save}

    async def close_all(self) -> None:
        for session_id in list(self.sessions):
            try:
                await self.close(session_id, save=False)
            except Exception:
                pass
