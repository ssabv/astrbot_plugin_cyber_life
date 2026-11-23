"""Verification failures cannot authorize a second submission or erase a login."""
import ast
import asyncio
import copy
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from PIL import Image, ImageDraw

from core.browser import BrowserError, BrowserManager
from core.captcha import (PUZZLE_BACKGROUND_SELECTORS, SLIDER_SELECTORS, CaptchaProbe,
                          CaptchaSolver, clean_code)
from core.manual_actions import ManualActions
from core.platforms.base import Post
from core.video_analysis import VideoInsight
from core.util import now_tz
from core.tieba_actions import (TiebaActionError, TiebaSession, parse_submission, pb_bytes,
                               pb_str, pb_int, safe_verification_url)
import test_manual_actions as manual_fixtures
import test_auto_comments as auto_fixtures


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = types.SimpleNamespace(bool=lambda key: True, int=lambda key: 3)
        self.manager = BrowserManager(self.settings, self.root)

    def fixture(self, automatic=False):
        case = auto_fixtures.AutomaticTiebaCommentTests() if automatic else manual_fixtures.ManualActionsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def page_fixture(self, before=None, after=None):
        editor = types.SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                       click=AsyncMock(), fill=AsyncMock(), input_value=AsyncMock(return_value="draft"))
        editor.first = editor
        button = types.SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
        button.first = button
        page = types.SimpleNamespace(url="https://example.invalid/fixture", goto=AsyncMock(),
            wait_for_timeout=AsyncMock(), evaluate=AsyncMock(), screenshot=AsyncMock(),
            locator=lambda key: editor if key == "#editor" else button)
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        normal = {"ok": True, "had": False, "solved": False}
        self.manager.solve_captcha = AsyncMock(side_effect=[before or normal, after or normal])
        return page, button

    async def post(self, page):
        return await self.manager.post_comment("xiaoheihe", "fixture", page.url, "draft",
            {"editor_selectors": ["#editor"], "submit_selectors": ["#send", "#alternate"]})

    def test_non_answers_are_not_guessed(self):
        for text in (None, " ", "看不清", "无法辨认", "无法识别", "I can help with many kinds of requests",
                     "验证码：aBc9", '前言\naBc9', '{"code":"无法辨认"}', '{"code":123}', '{"code":"aBc9","extra":1}'):
            self.assertEqual(clean_code(text), "", repr(text))
        self.assertEqual(clean_code('{"code":"aBc9"}'), "aBc9")
        self.assertEqual(clean_code("aBc9"), "aBc9")

    async def test_iframe_box_is_not_offset_twice(self):
        frame = types.SimpleNamespace(parent_frame=object(), frame_element=AsyncMock())
        box = {"x": 170, "y": 130, "width": 60, "height": 40}
        solver = CaptchaSolver(None, None, self.settings, self.root)
        self.assertEqual(await solver._abs_box(frame, box), box)
        frame.frame_element.assert_not_awaited()

    async def test_baidu_passmod_uses_puzzle_background_not_slider_track(self):
        settings = types.SimpleNamespace(bool=lambda key: True, int=lambda key: 3)
        solver = CaptchaSolver(None, None, settings, self.root)
        frame = object()
        probe = CaptchaProbe(found=True, kind="slider", label="滑块验证", frame=frame,
                             box={"x": 498, "y": 521.5, "width": 46, "height": 46})
        solver._find_visual = AsyncMock(side_effect=[
            ({"x": 495, "y": 340.5, "width": 290, "height": 160}, frame, None),
            ({"x": 495, "y": 340.5, "width": 52, "height": 160}, frame, None),
        ])
        solver._clip = AsyncMock(return_value={"x": 495, "y": 340.5, "width": 290, "height": 160})
        solver._grab_clip = AsyncMock(return_value=Path("captcha.png"))
        solver.llm = types.SimpleNamespace(complete_json=AsyncMock(return_value={"x": 0.71}))
        solver._drag = AsyncMock()
        ok, _ = await solver._solve_slider(object(), probe)
        self.assertTrue(ok)
        distance = solver._drag.await_args.args[2]
        self.assertAlmostEqual(distance, 495 + 0.71 * 290 - (495 + 26), places=3)

    def test_gap_ratio_finds_black_puzzle_hole(self):
        image = Image.new("RGB", (400, 200), (140, 150, 160))
        draw = ImageDraw.Draw(image)
        draw.rectangle((257, 57, 322, 142), fill=(245, 245, 245))
        draw.rectangle((260, 60, 319, 139), fill=(0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        solver = CaptchaSolver(None, None, self.settings, self.root)
        found = solver._background_gap_ratio(buffer.getvalue())
        self.assertIsNotNone(found)
        ratio, _ = found
        self.assertAlmostEqual(ratio, 289.5 / 400, places=3)

    def test_baidu_passmod_selectors_cover_new_widget(self):
        self.assertIn(".passMod_slide-btn", SLIDER_SELECTORS)
        self.assertIn(".passMod_puzzle-background", PUZZLE_BACKGROUND_SELECTORS)

    async def test_disabled_solver_still_reports_existing_verification(self):
        solver = CaptchaSolver(None, None, types.SimpleNamespace(bool=lambda key: False), self.root)
        solver.probe = AsyncMock(return_value=CaptchaProbe(found=True, kind="image"))
        result = await solver.solve(object())
        self.assertFalse(result["ok"])
        self.assertTrue(result["had"])

    async def test_disappearance_after_refresh_is_not_a_verified_result(self):
        solver = CaptchaSolver(None, None, self.settings, self.root)
        solver.probe = AsyncMock(side_effect=[CaptchaProbe(found=True, kind="image"), CaptchaProbe()])
        solver._solve_image = AsyncMock(return_value=(False, "no answer"))
        solver._refresh = AsyncMock()
        result = await solver.solve(object())
        self.assertFalse(result["ok"])
        self.assertFalse(result["solved"])

    async def test_unreadable_page_is_not_reported_as_no_captcha(self):
        solver = CaptchaSolver(None, None, self.settings, self.root)
        solver._find = AsyncMock(return_value=None)
        page = types.SimpleNamespace(is_closed=lambda: False, frames=[],
                                     locator=lambda key: types.SimpleNamespace(inner_text=AsyncMock(side_effect=TimeoutError)))
        probe = await solver.probe(page)
        self.assertTrue(probe.found)
        self.assertEqual(probe.kind, "unknown")

    async def test_no_generic_submit_or_enter_fallback(self):
        solver = CaptchaSolver(None, None, self.settings, self.root)
        locator = types.SimpleNamespace(count=AsyncMock(return_value=0))
        locator.first = locator
        frame = types.SimpleNamespace(locator=lambda key: locator)
        page = types.SimpleNamespace(frames=[frame], keyboard=types.SimpleNamespace(press=AsyncMock()))
        self.assertFalse(await solver._submit(page, CaptchaProbe()))
        page.keyboard.press.assert_not_awaited()

    async def test_failed_pre_submit_verification_never_clicks_send(self):
        page, button = self.page_fixture(before={"ok": False, "had": True, "solved": False})
        result = await self.post(page)
        self.assertFalse(result["ok"])
        self.assertTrue(result["verification_required"])
        button.click.assert_not_awaited()

    async def test_post_submit_verification_never_repeats_retained_draft(self):
        page, button = self.page_fixture(after={"ok": True, "had": True, "solved": True})
        result = await self.post(page)
        button.click.assert_awaited_once()
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])

    async def test_click_timeout_never_tries_another_send_button(self):
        page, button = self.page_fixture()
        button.click.side_effect = TimeoutError
        result = await self.post(page)
        button.click.assert_awaited_once()
        self.assertTrue(result["uncertain"])

    async def test_ordinary_page_still_submits_once(self):
        page, button = self.page_fixture()
        self.assertTrue((await self.post(page))["ok"])
        button.click.assert_awaited_once()

    async def test_tieba_submit_uses_page_editor_and_requires_visible_result(self):
        editor = types.SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        button = types.SimpleNamespace(click=AsyncMock())
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        )
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_submit = AsyncMock(return_value=button)
        self.manager._tieba_reply_records = AsyncMock(side_effect=[[], [
            {"id": "200", "text": "还没忙完啊。", "author_id": "42"}
        ]])
        self.manager._tieba_reply_identity = AsyncMock(return_value={"id": "42"})
        self.manager._tieba_post_ack = AsyncMock(return_value={"state": "ok", "id": "200"})
        result = await self.manager.submit_tieba("default", page.url, "还没忙完啊。",
                                                 allowed_forums=["大肥鱼"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "200")
        button.click.assert_awaited_once()
        self.manager._fill_tieba_editor.assert_awaited_once_with(page, editor, "还没忙完啊。")

    async def test_tieba_submit_captcha_failure_never_clicks_send(self):
        editor = types.SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        )
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.solve_captcha = AsyncMock(
            return_value={"ok": False, "had": True, "solved": False, "message": "验证未过"}
        )
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_submit = AsyncMock()
        result = await self.manager.submit_tieba("default", page.url, "测试",
                                                 allowed_forums=["大肥鱼"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["verification_required"])
        self.manager._tieba_submit.assert_not_awaited()

    async def test_tieba_submit_click_timeout_is_uncertain_and_not_repeated(self):
        editor = types.SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        button = types.SimpleNamespace(click=AsyncMock(side_effect=TimeoutError))
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        )
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_submit = AsyncMock(return_value=button)
        self.manager._text_count = AsyncMock(return_value=0)
        with self.assertRaises(BrowserError) as caught:
            await self.manager.submit_tieba("default", page.url, "测试",
                                            allowed_forums=["大肥鱼"])
        self.assertTrue(caught.exception.uncertain)
        button.click.assert_awaited_once()

    async def test_tieba_submit_without_visible_result_is_not_success(self):
        editor = types.SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        button = types.SimpleNamespace(click=AsyncMock())
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        )
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_submit = AsyncMock(return_value=button)
        self.manager._text_count = AsyncMock(return_value=0)
        self.manager._tieba_success_marker = AsyncMock(return_value=False)
        result = await self.manager.submit_tieba("default", page.url, "测试",
                                                 allowed_forums=["大肥鱼"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])

    async def test_mobile_fallback_carries_forum_word_and_thread_id(self):
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            title=AsyncMock(return_value="询问大肥鱼们"),
        )
        context = types.SimpleNamespace()
        editor = object()
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager._tieba_thread_text = AsyncMock(return_value=("询问大肥鱼们", "正文"))
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_short_page = AsyncMock(return_value=False)
        self.manager._tieba_same_thread = AsyncMock(return_value=True)
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        result, fingerprint = await self.manager._tieba_mobile_editor(
            page, context, "default", "fingerprint", page.url, {}
        )
        self.assertIs(result, editor)
        self.assertEqual(fingerprint, "fingerprint")
        first_url = page.goto.await_args_list[0].args[0]
        self.assertIn("word=%E5%A4%A7%E8%82%A5%E9%B1%BC", first_url)
        self.assertIn("tid=123", first_url)

    async def test_mobile_fallback_rejects_a_different_thread(self):
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            title=AsyncMock(return_value="别的帖子"),
        )
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager._tieba_thread_text = AsyncMock(return_value=("询问大肥鱼们", "正文"))
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_short_page = AsyncMock(return_value=False)
        self.manager._tieba_same_thread = AsyncMock(return_value=False)
        self.manager._tieba_editor = AsyncMock()
        self.manager._page_notice = AsyncMock(return_value="")
        self.manager._tieba_state = AsyncMock(return_value={})
        result, _ = await self.manager._tieba_mobile_editor(
            page, types.SimpleNamespace(), "default", "fingerprint", page.url, {}
        )
        self.assertIsNone(result)
        self.manager._tieba_editor.assert_not_awaited()

    async def test_comment_page_is_opened_with_show_comment_query(self):
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123?pn=0",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
        )
        editor = object()
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_editor = AsyncMock(return_value=editor)
        result, _ = await self.manager._tieba_comment_page(
            page, types.SimpleNamespace(), "default", "fingerprint", {}
        )
        self.assertIs(result, editor)
        target = page.goto.await_args.args[0]
        self.assertEqual(target, "https://tieba.baidu.com/p/123?pn=0&showComment=1&fr=pb")

    async def test_thread_reader_rejects_non_numeric_id_before_opening_browser(self):
        result = await self.manager.read_tieba_thread("default", "../etc/passwd")
        self.assertFalse(result["ok"])
        self.assertIn("编号无效", result["message"])

    async def test_reply_target_can_be_marked_from_its_visible_text(self):
        page = types.SimpleNamespace(evaluate=AsyncMock(return_value={"ok": True}))
        self.assertTrue(await self.manager._tieba_mark_reply_target(
            page, {"content": "什么 AI 我就是我啊 记性差是因为没睡好"}
        ))
        page.evaluate.assert_awaited_once()

    async def test_reply_target_expands_folded_comments_before_scrolling(self):
        page = types.SimpleNamespace()
        self.manager._tieba_sort_comments = AsyncMock(return_value=True)
        self.manager._tieba_mark_reply_target = AsyncMock(side_effect=[False, True])
        self.manager._tieba_expand_comments = AsyncMock(return_value=1)
        self.manager._tieba_scroll_comments = AsyncMock()
        self.assertTrue(await self.manager._tieba_load_reply_target(
            page, {"content": "什么 AI 我就是我啊 记性差是因为没睡好"}
        ))
        self.manager._tieba_expand_comments.assert_awaited_once()
        self.manager._tieba_scroll_comments.assert_not_awaited()

    async def test_reply_target_stops_when_virtual_list_has_no_new_items(self):
        page = types.SimpleNamespace(wait_for_timeout=AsyncMock())
        self.manager._tieba_sort_comments = AsyncMock(return_value=True)
        self.manager._tieba_mark_reply_target = AsyncMock(return_value=False)
        self.manager._tieba_expand_comments = AsyncMock(return_value=0)
        self.manager._tieba_scroll_comments = AsyncMock(
            return_value={"items": 2, "box": 0, "page": 0}
        )
        self.manager.solve_captcha = AsyncMock(return_value={"had": False})
        self.assertFalse(await self.manager._tieba_load_reply_target(
            page, {"content": "什么 AI 我就是我啊 记性差是因为没睡好"}
        ))
        self.assertEqual(self.manager._tieba_scroll_comments.await_count, 7)

    async def test_tieba_button_disabled_reads_rendered_state(self):
        button = types.SimpleNamespace(evaluate=AsyncMock(return_value=True))
        self.assertTrue(await self.manager._tieba_button_disabled(button))
        button.evaluate = AsyncMock(return_value=False)
        self.assertFalse(await self.manager._tieba_button_disabled(button))

    async def test_tieba_match_text_ignores_emoticon_markup_and_reply_prefix(self):
        candidates = self.manager._tieba_match_candidates(
            "回复  小鲸鱼ᵇᵒᵗ :好家伙，我这儿刚收竿呢#(笑眼)"
        )
        self.assertIn("回复小鲸鱼ᵇᵒᵗ:好家伙，我这儿刚收竿呢", candidates)
        self.assertIn("好家伙，我这儿刚收竿呢", candidates)

    async def test_mobile_fallback_uses_allowed_forum_when_page_lacks_one(self):
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            title=AsyncMock(return_value="询问大肥鱼们"),
        )
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager._tieba_thread_text = AsyncMock(return_value=("询问大肥鱼们", "正文"))
        self.manager._tieba_forum = AsyncMock(return_value="")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_short_page = AsyncMock(return_value=False)
        self.manager._tieba_same_thread = AsyncMock(return_value=True)
        self.manager._tieba_editor = AsyncMock(return_value=object())
        await self.manager._tieba_mobile_editor(
            page, types.SimpleNamespace(), "default", "fingerprint", page.url, {},
            allowed_forums=["大肥鱼"],
        )
        first_url = page.goto.await_args_list[0].args[0]
        self.assertIn("word=%E5%A4%A7%E8%82%A5%E9%B1%BC", first_url)

    async def test_mobile_fallback_is_used_when_desktop_has_no_editor(self):
        editor = types.SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        button = types.SimpleNamespace(click=AsyncMock())
        page = types.SimpleNamespace(
            url="https://tieba.baidu.com/p/123",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        )
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        self.manager.new_context = AsyncMock(return_value=context)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.solve_captcha = AsyncMock(return_value={"ok": True, "had": False})
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=None)
        self.manager._tieba_comment_page = AsyncMock(return_value=(None, "fingerprint"))
        self.manager._tieba_mobile_editor = AsyncMock(return_value=(editor, "fingerprint"))
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_submit = AsyncMock(return_value=button)
        self.manager._tieba_reply_records = AsyncMock(side_effect=[[], [
            {"id": "200", "text": "测试", "author_id": "42"}
        ]])
        self.manager._tieba_reply_identity = AsyncMock(return_value={"id": "42"})
        self.manager._tieba_post_ack = AsyncMock(return_value={"state": "ok", "id": "200"})
        result = await self.manager.submit_tieba("default", page.url, "测试",
                                                 allowed_forums=["大肥鱼"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.manager._tieba_mobile_editor.await_args.kwargs["allowed_forums"],
            ["大肥鱼"],
        )

    async def test_saved_login_cookies_are_added_to_a_context_without_storage(self):
        session = self.manager.data_dir / "sessions" / "tieba" / "default.json"
        session.parent.mkdir(parents=True)
        session.write_text('{"cookies": {"BDUSS": "secret", "STOKEN": "secret2"}}', encoding="utf-8")
        context = types.SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
        added = await self.manager.hydrate_cookies(context, "tieba", "default")
        self.assertEqual(added, 2)
        values = context.add_cookies.await_args.args[0]
        self.assertEqual({item["name"] for item in values}, {"BDUSS", "STOKEN"})
        self.assertTrue(all(item["domain"] == ".baidu.com" for item in values))
        self.assertNotIn("secret", str(self.manager._browser))

    async def test_saved_cookie_overwrites_a_stale_storage_cookie(self):
        session = self.manager.data_dir / "sessions" / "tieba" / "default.json"
        session.parent.mkdir(parents=True)
        session.write_text('{"cookies": {"BDUSS": "secret"}}', encoding="utf-8")
        context = types.SimpleNamespace(
            cookies=AsyncMock(return_value=[{"name": "BDUSS", "domain": ".baidu.com"}]),
            add_cookies=AsyncMock(),
        )
        self.assertEqual(await self.manager.hydrate_cookies(context, "tieba", "default"), 1)
        values = context.add_cookies.await_args.args[0]
        self.assertEqual(values, [{"name": "BDUSS", "value": "secret",
                                   "domain": ".baidu.com", "path": "/"}])

    async def test_changed_login_is_not_overwritten(self):
        path = self.manager.storage_path("tieba", "fixture")
        path.parent.mkdir(parents=True)
        path.write_text("original")
        original = self.manager.storage_fingerprint("tieba", "fixture")
        async def external_change():
            path.write_text("new-login")
            return {"cookies": []}
        with self.assertRaises(BrowserError):
            await self.manager.persist_storage(types.SimpleNamespace(storage_state=AsyncMock(side_effect=external_change)),
                "tieba", "fixture", expected_fingerprint=original)
        self.assertEqual(path.read_text(), "new-login")

    async def test_own_storage_update_returns_exact_written_fingerprint(self):
        result = await self.manager.persist_storage(types.SimpleNamespace(storage_state=AsyncMock(return_value={"cookies": []})),
                "tieba", "fixture", expected_fingerprint=None)
        self.assertEqual(result, self.manager.storage_fingerprint("tieba", "fixture"))

    def test_submission_preserves_actual_challenge(self):
        anti = pb_str(3, "1") + pb_str(4, "fixture-md5") + pb_str(6, "6") + pb_str(12, "https://seccaptcha.baidu.com/fixture")
        raw = pb_bytes(1, pb_int(1, 4)) + pb_bytes(2, pb_bytes(14, anti))
        with self.assertRaises(TiebaActionError) as caught:
            parse_submission(raw)
        self.assertTrue(caught.exception.verification_required)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.verification["url"], "https://seccaptcha.baidu.com/fixture")
        self.assertEqual(caught.exception.verification["md5"], "fixture-md5")

    def test_verification_link_cannot_point_to_an_arbitrary_site(self):
        for url in ("http://tieba.baidu.com/test", "https://tieba.baidu.com.evil.invalid/test", "https://u:p@tieba.baidu.com/test",
                    "https://127.0.0.1/test", "javascript:alert(1)", "https://tieba.baidu.com:8080/test"):
            self.assertEqual(safe_verification_url(url), "")

    def test_challenge_with_a_reply_id_remains_uncertain_and_paused(self):
        raw = pb_bytes(1, pb_int(1, 4)) + pb_bytes(2, pb_str(3, "123") + pb_bytes(14, pb_str(3, "1")))
        with self.assertRaises(TiebaActionError) as caught:
            parse_submission(raw)
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(caught.exception.verification_required)

    async def test_api_verification_does_not_open_homepage(self):
        session = TiebaSession(self.manager, "fixture")
        session.context = types.SimpleNamespace(new_page=AsyncMock())
        result = await session.pass_captcha({"type": "6"})
        self.assertFalse(result["solved"])
        session.context.new_page.assert_not_awaited()

    async def test_read_challenge_finishes_without_drafting_or_pausing(self):
        case = self.fixture()
        case.session.latest_threads.side_effect = TiebaActionError("验证", verification_required=True)
        job = await case.run_kind()
        self.assertEqual(job["state"], "error")
        case.writer.draft.assert_not_awaited()
        restored = ManualActions(case.engine, case.root)
        self.assertFalse(restored.is_waiting(case.account.name))
        self.assertTrue(restored.start("comment", "fixture_next_job")[1])

    async def test_submit_challenge_keeps_draft_and_never_retries_or_counts(self):
        case = self.fixture()
        case.engine.browser.submit_tieba.side_effect = TiebaActionError(
            "验证", verification_required=True
        )
        job = await case.run_kind()
        self.assertEqual(job["body"], "还没忙完啊。")
        self.assertEqual(job["state"], "error")
        self.assertFalse(case.actions.verification)
        self.assertFalse(case.actions.targets)
        case.engine.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(case.actions.public()["manual_comments"], 0)

    async def test_explicit_release_does_not_send_and_stale_release_is_rejected(self):
        case = self.fixture()
        case.engine.browser.submit_tieba.side_effect = TiebaActionError(
            "验证", verification_required=True
        )
        job = await case.run_kind()
        # Explicit recovery API remains compatible with an externally supplied
        # old record; new failures never create this wait.
        case.actions.verification[case.account.name] = {"job_id": job["id"], "uncertain": False}
        with self.assertRaises(ValueError):
            case.actions.clear_verification(case.account.name, "old_job_id")
        self.assertTrue(case.actions.is_waiting(case.account.name))
        case.actions.clear_verification(case.account.name, job["id"])
        self.assertFalse(case.actions.is_waiting(case.account.name))
        self.assertFalse(case.actions.targets)
        case.engine.browser.submit_tieba.assert_awaited_once()
        case.writer.draft.assert_awaited_once()

    async def test_uncertain_submission_reservation_survives_without_wait(self):
        case = self.fixture()
        case.engine.browser.submit_tieba.side_effect = TiebaActionError(
            "验证且结果未知", uncertain=True, verification_required=True
        )
        job = await case.run_kind()
        self.assertFalse(case.actions.is_waiting(case.account.name))
        self.assertTrue(case.actions.targets)
        self.assertFalse(job["ok"])

    async def test_automatic_future_rounds_continue_across_reload(self):
        case = self.fixture(automatic=True)
        case.engine.browser.submit_tieba.side_effect = TiebaActionError(
            "验证", verification_required=True
        )
        await case.engine.tick()
        self.assertEqual(case.engine.auto_comment["state"], "error")
        case.stats.next_comment_at = "2000-01-01T00:00:00+08:00"
        await case.engine.tick()
        restored = case.make_engine()
        restored.tieba_actions = ManualActions(restored, case.root)
        restored.stats().next_comment_at = "2000-01-01T00:00:00+08:00"
        await restored.tick()
        self.assertEqual(case.writer.draft.await_count, 3)
        self.assertEqual(case.engine.browser.submit_tieba.await_count, 3)
        self.assertNotEqual(restored.status()["auto_comment"]["gate"], "verification_required")
        self.assertFalse(restored.tieba_actions.verification)
        self.assertEqual(case.stats.comments, 0)

    async def test_browser_unknown_target_is_kept_without_account_wait(self):
        case = self.fixture(automatic=True)
        case.engine.record_browser_outcome("tieba", "default", "unknown", uncertain=True, url="https://tieba.baidu.com/p/123")
        self.assertTrue(case.engine.browser_uncertain_targets)
        self.assertFalse(case.engine.browser_verification)

    async def test_recorded_browser_failure_does_not_block_manual_action(self):
        case = self.fixture(automatic=True)
        case.engine.record_browser_outcome("tieba", "default", "failed")
        self.assertTrue(case.engine.tieba_actions.start("comment", "manual_next_fixture")[1])
        case.writer.draft.assert_not_awaited()
        case.engine.browser.submit_tieba.assert_not_awaited()

    async def test_browser_submission_interruption_is_reserved_before_request(self):
        case = self.fixture(automatic=True)
        case.engine._generate_comment = AsyncMock(return_value="draft")
        async def interrupted(post, text):
            restored = case.make_engine()
            self.assertFalse(restored.browser_verification)
            self.assertTrue(restored.browser_uncertain_targets)
            raise asyncio.CancelledError
        adapter = types.SimpleNamespace(comment=AsyncMock(side_effect=interrupted))
        post = Post(platform="tieba", id="123", title="日常", url="https://tieba.baidu.com/p/123")
        with self.assertRaises(asyncio.CancelledError):
            await case.engine._comment_on(post, adapter, VideoInsight(summary="日常"), case.stats, now_tz())
        self.assertTrue(case.engine.browser_uncertain_targets)

    async def test_legacy_browser_wait_migrates_and_keeps_unknown_target(self):
        case = self.fixture(automatic=True)
        case.engine.browser_verification["tieba:default"] = {"uncertain": True, "url": "https://tieba.baidu.com/p/123"}
        case.engine.save()
        restored = case.make_engine()
        self.assertFalse(restored.browser_verification)
        self.assertIn("tieba:default:https://tieba.baidu.com/p/123", restored.browser_uncertain_targets)
        self.assertIn("tieba", restored._eligible_platforms())

    async def test_native_release_requires_processed_flag_and_only_mutates_wait(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf8"))
        plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CyberLifePlugin")
        method = next(n for n in plugin.body if getattr(n, "name", "") == "api_verification_release")
        request = types.SimpleNamespace(json=AsyncMock(return_value={}))
        env = {"request": request, "json_response": lambda x: x,
               "error_response": lambda message, **kw: {"message": message, **kw},
               "ManualActionBusy": RuntimeError}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "release-api", "exec"), env)
        actions = types.SimpleNamespace(is_waiting=lambda name: True, clear_verification=Mock())
        target = types.SimpleNamespace(manual_actions=actions)
        self.assertEqual((await env["api_verification_release"](target))["status_code"], 400)
        actions.clear_verification.assert_not_called()
        request.json.return_value = {"processed": True, "platform": "tieba", "account_name": "default", "job_id": "fixture"}
        self.assertTrue((await env["api_verification_release"](target))["released"])
        actions.clear_verification.assert_called_once_with("default", "fixture")
