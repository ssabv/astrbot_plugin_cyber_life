"""Browser-driven platforms (Tieba, Xiaoheihe).

No stable public API is used here.  The adapter opens the site with the saved
Playwright storage state, scrapes feed cards, and comments through the page's
own input box.  All failures are reported instead of guessed.
"""

from __future__ import annotations

import hashlib
import random
from urllib.parse import urljoin

from astrbot.api import logger

from ..browser import BrowserManager, BrowserError
from .base import CommentResult, PlatformError, Post, Reply
from .specs import site_spec
from ..tieba_scope import forum_allowed, forum_name_from_url, forum_url, is_post_url, normalize_forums


class BrowserPlatform:
    def __init__(
        self,
        platform: str,
        accounts,
        browser: BrowserManager,
        account_name: str | None = None,
    ) -> None:
        self.platform = platform
        self.accounts = accounts
        self.browser = browser
        self.account_name = account_name or (
            accounts.active_account(platform).name if accounts.active_account(platform) else "default"
        )
        self.spec = site_spec(platform)
        if not self.spec:
            raise PlatformError(f"没有 {platform} 的站点配置")
        self.label = self.spec.get("label", platform)
        self.supports_reply = False
        self.last_feed = {}

    def _post_id(self, url: str) -> str:
        return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]

    def _allowed_forums(self) -> list[str]:
        if self.platform != "tieba":
            return []
        return normalize_forums(self.browser.settings.list("tieba_forums"))

    async def feed(self, limit: int = 10) -> list[Post]:
        allowed = self._allowed_forums()
        chosen = random.choice(allowed) if allowed else ""
        url_base = forum_url(chosen) if chosen else self.spec["feed_url"]
        selectors = dict(self.spec)
        if chosen:
            selectors.update({
                "card_selectors": [".frs-feed-list .feed-list-container .thread-card", "#thread_list .j_thread_list"],
                "card_title_selector": ".thread-title, a.j_th_tit",
                "card_link_selector": "a.thread-content-link, a.j_th_tit",
                "card_body_selector": ".thread-content, .threadlist_abs",
            })
        result = await self.browser.browse(
            self.platform,
            self.account_name,
            url_base,
            selectors,
        )
        self.last_feed = {key: result.get(key, "") for key in ("url", "title", "notice")}
        if chosen and not forum_allowed(forum_name_from_url(result.get("url", "")), [chosen]):
            raise PlatformError("指定贴吧页面发生跳转，本轮不浏览其他吧")
        posts: list[Post] = []
        seen = set()
        for card in result.get("cards", []):
            title = (card.get("title") or "").strip()
            href = (card.get("url") or "").strip()
            if not title or not href:
                continue
            url = urljoin(url_base, href)
            if self.platform == "tieba" and not is_post_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            posts.append(
                Post(
                    platform=self.platform,
                    id=self._post_id(url),
                    title=title[:180],
                    url=url,
                    description=(card.get("body") or "")[:400],
                    extra={"screenshot": result.get("screenshot", ""), "tieba_forum": chosen},
                )
            )
            if len(posts) >= limit:
                break
        return posts

    async def detail(self, post: Post) -> Post:
        return post

    async def comment(self, post: Post, text: str) -> CommentResult:
        try:
            allowed = self._allowed_forums()
            if allowed:
                if not is_post_url(post.url) or not forum_allowed(post.extra.get("tieba_forum", ""), allowed):
                    return CommentResult(ok=False, message="帖子不在允许的贴吧范围，本轮不发送")
            if self.platform == "tieba":
                result = await self.browser.submit_tieba(
                    self.account_name,
                    post.url,
                    text,
                    allowed_forums=allowed,
                )
            else:
                result = await self.browser.post_comment(
                    self.platform,
                    self.account_name,
                    post.url,
                    text,
                    dict(self.spec),
                )
        except BrowserError as exc:
            return CommentResult(ok=False, message=str(exc), url=post.url,
                                 uncertain=exc.uncertain,
                                 verification_required=exc.verification_required)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] {self.platform} 评论异常：{exc}")
            return CommentResult(ok=False, message=f"评论异常：{exc}")
        return CommentResult(
            ok=bool(result.get("ok")),
            id=str(result.get("id") or ""),
            uncertain=bool(result.get("uncertain")),
            verification_required=bool(result.get("verification_required")),
            message=str(result.get("message") or ""),
            url=post.url,
            screenshot=str(result.get("screenshot") or ""),
        )

    async def replies(self, post: Post, root_id: str) -> list[Reply]:
        return []

    async def reply(self, post: Post, root_id: str, parent_id: str, text: str) -> CommentResult:
        return CommentResult(ok=False, message=f"{self.label} 暂不支持自动回复评论")
