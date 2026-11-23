from __future__ import annotations

import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import astrbot.api
except ImportError:
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    sys.modules["astrbot.api"] = api

from core.browser import BrowserManager
from core.platforms.base import PlatformError, Post
from core.platforms.browser_sites import BrowserPlatform
from core.settings import SettingsStore
from core.tieba_scope import forum_name_from_url, forum_url, normalize_forums


class TiebaScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = types.SimpleNamespace(list=lambda key: ["大肥鱼"])
        self.browser = types.SimpleNamespace(
            settings=self.settings, browse=AsyncMock(), post_comment=AsyncMock(),
            submit_tieba=AsyncMock(return_value={"ok": True}),
        )
        accounts = types.SimpleNamespace(active_account=lambda platform: None)
        self.adapter = BrowserPlatform("tieba", accounts, self.browser)

    def test_names_and_url_round_trip(self):
        self.assertEqual(normalize_forums(" 大肥鱼，平板\n大肥鱼\n"), ["大肥鱼", "平板"])
        self.assertEqual(forum_name_from_url(forum_url("C++")), "C++")
        self.assertEqual(forum_name_from_url("https://other.test/f?kw=大肥鱼"), "")
        with self.assertRaises(ValueError):
            normalize_forums(["https://tieba.baidu.com/f?kw=大肥鱼"])

    async def test_feed_only_uses_configured_forum_and_rejects_external_links(self):
        self.browser.browse.return_value = {"url": forum_url("大肥鱼"), "cards": [
            {"title": "external", "url": "https://other.test/p/999"},
            {"title": "不是帖子", "url": "/f?kw=其他吧"},
            {"title": "测试帖", "url": "/p/123", "body": "实际内容"}]}
        posts = await self.adapter.feed()
        self.assertEqual(self.browser.browse.await_args.args[2], forum_url("大肥鱼"))
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].description, "实际内容")
        self.assertEqual(posts[0].extra["tieba_forum"], "大肥鱼")

    async def test_redirect_does_not_fall_back_to_homepage(self):
        self.browser.browse.return_value = {"url": "https://tieba.baidu.com/index.html", "cards": []}
        with self.assertRaises(PlatformError):
            await self.adapter.feed()
        self.browser.browse.assert_awaited_once()

    async def test_multiple_forums_select_only_from_allowed_list(self):
        self.settings.list = lambda key: ["大肥鱼", "平板"]
        self.browser.browse.return_value = {"url": forum_url("平板"), "cards": []}
        with patch("core.platforms.browser_sites.random.choice", return_value="平板") as choose:
            await self.adapter.feed()
        choose.assert_called_once_with(["大肥鱼", "平板"])
        self.assertEqual(self.browser.browse.await_args.args[2], forum_url("平板"))

    async def test_changed_scope_blocks_previously_selected_post(self):
        post = Post(platform="tieba", id="123", title="old", url="https://tieba.baidu.com/p/123",
                    extra={"tieba_forum": "平板"})
        result = await self.adapter.comment(post, "不应该发送")
        self.assertFalse(result.ok)
        self.browser.post_comment.assert_not_awaited()
        self.browser.submit_tieba.assert_not_awaited()

    async def test_allowed_post_passes_scope_to_actual_browser(self):
        self.browser.submit_tieba.return_value = {"ok": True}
        post = Post(platform="tieba", id="123", title="test", url="https://tieba.baidu.com/p/123",
                    extra={"tieba_forum": "大肥鱼"})
        self.assertTrue((await self.adapter.comment(post, "测试")).ok)
        self.assertEqual(self.browser.submit_tieba.await_args.kwargs["allowed_forums"], ["大肥鱼"])

    def test_native_configuration_wins_over_stale_saved_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            SettingsStore({}, root).update({"tieba_forums": ["平板"]})
            settings = SettingsStore({"tieba_forums": ["大肥鱼"]}, root)
            self.assertEqual(settings.list("tieba_forums"), ["大肥鱼"])

    async def browser_comment(self, actual_forum, actual_url="https://tieba.baidu.com/p/123"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager = BrowserManager(self.settings, Path(tmp.name))
        editor = types.SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                       click=AsyncMock(), fill=AsyncMock())
        editor.first = editor
        button = types.SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
        button.first = button
        missing = types.SimpleNamespace(count=AsyncMock(return_value=0))
        missing.first = missing
        page = types.SimpleNamespace(url=actual_url, goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
                                     evaluate=AsyncMock(side_effect=lambda script: actual_forum if "PageData" in script else None),
                                     locator=lambda selector: editor if selector == "textarea" else button if selector == "button" else missing)
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        manager.new_context = AsyncMock(return_value=context)
        manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False, "solved": False})
        result = await manager.post_comment("tieba", "test", "https://tieba.baidu.com/p/123", "测试",
            {"allowed_tieba_forums": ["大肥鱼"], "editor_selectors": ["textarea"], "submit_selectors": ["button"]})
        return result, editor, button

    async def test_real_page_forum_mismatch_or_missing_blocks_send(self):
        for forum in ("平板", ""):
            result, editor, button = await self.browser_comment(forum)
            self.assertFalse(result["ok"])
            editor.fill.assert_not_awaited()
            button.click.assert_not_awaited()

    async def test_real_page_redirect_blocks_send(self):
        result, editor, button = await self.browser_comment("大肥鱼", "https://passport.baidu.com/login")
        self.assertFalse(result["ok"])
        editor.fill.assert_not_awaited()
        button.click.assert_not_awaited()

    async def test_verified_forum_allows_submission(self):
        result, editor, button = await self.browser_comment("大肥鱼")
        self.assertTrue(result["ok"])
        editor.fill.assert_awaited_once_with("测试", timeout=4000)
        button.click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
