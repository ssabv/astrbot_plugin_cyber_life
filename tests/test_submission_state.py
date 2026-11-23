"""A failed, unknown, or already successful send must not become a duplicate."""
import json
import tempfile
import types
import unittest
from urllib.parse import urlencode
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.browser import BrowserError, BrowserManager
from core.platforms.base import Post
from core.platforms.browser_sites import BrowserPlatform
import test_manual_actions as manual_fixtures


def response(code=None, *, body=None, status=200):
    return types.SimpleNamespace(status=status, url="https://tieba.baidu.com/c/c/post/add_pc",
        headers={"content-type": "application/json"},
        text=AsyncMock(return_value=json.dumps({"error_code": code}) if body is None else body))


class SubmissionStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = BrowserManager(types.SimpleNamespace(bool=lambda k: True,
                                                          int=lambda k: 3), Path(self.temp.name))
        self.hooks = {}
        self.page = types.SimpleNamespace(url="https://tieba.baidu.com/p/123",
            on=lambda event, callback: self.hooks.update({event: callback}),
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), screenshot=AsyncMock())
        self.context = types.SimpleNamespace(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.button = types.SimpleNamespace(click=AsyncMock())
        self.editor = object()
        self.manager.new_context = AsyncMock(return_value=self.context)
        self.manager.hydrate_cookies = AsyncMock(return_value=0)
        self.manager.storage_fingerprint = Mock(return_value="fingerprint")
        self.manager.persist_storage = AsyncMock(return_value="fingerprint")
        normal = {"ok": True, "had": False, "solved": False}
        self.normal = normal
        self.solved = {"ok": True, "had": True, "solved": True}
        self.manager.solve_captcha = AsyncMock(return_value=normal)
        self.manager._tieba_forum = AsyncMock(return_value="大肥鱼")
        self.manager._tieba_editor = AsyncMock(return_value=self.editor)
        self.manager._fill_tieba_editor = AsyncMock()
        self.manager._editor_contains = AsyncMock(return_value=True)
        self.manager._tieba_state = AsyncMock(return_value={"login": True})
        self.manager._tieba_submit = AsyncMock(return_value=self.button)
        self.manager._tieba_button_disabled = AsyncMock(return_value=False)
        self.manager._tieba_success_marker = AsyncMock(return_value=False)
        self.manager._tieba_load_reply_target = AsyncMock(return_value=True)
        self.records = []
        self.manager._tieba_reply_records = AsyncMock(side_effect=lambda page: self.records)
        self.manager._tieba_reply_identity = AsyncMock(return_value={"id": "42"})

    async def submit(self):
        return await self.manager.submit_tieba("default", self.page.url, "是啊。",
                                              allowed_forums=["大肥鱼"])

    def captcha_after_send(self):
        self.manager.solve_captcha.side_effect = [self.normal, self.normal, self.solved, self.normal]

    async def test_success_wins_over_older_or_newer_rejections(self):
        for codes in ([40, 0], [0, 40]):
            with self.subTest(codes=codes):
                self.assertEqual(await self.manager._tieba_post_result([response(code) for code in codes]), "ok")

    async def test_empty_malformed_and_http_errors_do_not_authorize_retry(self):
        for item in (response(body=""), response(body="<html>error</html>"),
                     response(body="{}"), response(0, status=502),
                     response(body='{"error_code":40,"data":{"pid":"200"}}')):
            with self.subTest(item=item):
                self.assertEqual(await self.manager._tieba_post_result([item]), "unknown")
        self.assertEqual(await self.manager._tieba_post_result([response(40), response(body="")]), "unknown")

    async def test_success_after_captcha_never_clicks_publish_again(self):
        self.captcha_after_send()
        async def click(**kw):
            self.hooks["response"](response(40))
            self.hooks["response"](response(body='{"error_code":0,"data":{"pid":"200"}}'))
            self.records = [{"id": "200", "text": "是啊。", "author_id": "42"}]
        self.button.click.side_effect = click
        result = await self.submit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "200")
        self.button.click.assert_awaited_once()

    async def test_empty_after_captcha_stays_uncertain_without_retry(self):
        self.captcha_after_send()
        self.button.click.side_effect = lambda **kw: self.hooks["response"](response(body=""))
        result = await self.submit()
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])
        self.button.click.assert_awaited_once()

    async def test_inflight_browser_retry_prevents_another_click(self):
        self.captcha_after_send()
        async def click(**kw):
            request = types.SimpleNamespace(url="https://tieba.baidu.com/c/c/post/add_pc",
                post_data="draft", method="POST")
            self.hooks["request"](request)
            self.hooks["request"](request)
            self.hooks["response"](response(40))
        self.button.click.side_effect = click
        result = await self.submit()
        self.assertTrue(result["uncertain"])
        self.button.click.assert_awaited_once()

    async def test_explicit_rejection_can_continue_once_after_captcha(self):
        self.captcha_after_send()
        async def click(**kw):
            if self.button.click.await_count == 1:
                self.hooks["response"](response(40))
            else:
                self.hooks["response"](response(body='{"error_code":0,"data":{"pid":"200"}}'))
                self.records = [{"id": "200", "text": "是啊。", "author_id": "42"}]
        self.button.click.side_effect = click
        result = await self.submit()
        self.assertTrue(result["ok"])
        self.assertEqual(self.button.click.await_count, 2)

    async def test_failed_send_cannot_be_confirmed_by_old_identical_reply(self):
        self.records = [{"id": "101", "text": "是啊。", "author_id": "42"}]
        self.button.click.side_effect = lambda **kw: self.hooks["response"](response(40))
        self.manager._text_count = AsyncMock(return_value=99)
        result = await self.submit()
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])
        self.manager._text_count.assert_not_awaited()
        self.button.click.assert_awaited_once()

    async def test_old_acknowledged_id_is_not_a_new_send(self):
        self.records = [{"id": "101", "text": "是啊。", "author_id": "42"}]
        self.button.click.side_effect = lambda **kw: self.hooks["response"](
            response(body='{"error_code":0,"data":{"pid":"101"}}'))
        result = await self.submit()
        self.assertFalse(result["ok"])
        self.button.click.assert_awaited_once()

    async def test_new_foreign_or_wrong_parent_reply_cannot_confirm_send(self):
        for record in ({"id": "200", "text": "是啊。", "author_id": "99", "parent_pid": "102"},
                       {"id": "200", "text": "是啊。", "author_id": "42", "parent_pid": "999"}):
            self.records = [record]
            pid = await self.manager._tieba_confirm_reply(self.page, "是啊。", set(),
                {"id": "42"}, {"parent_pid": "102"}, {"state": "ok", "id": "200"})
            self.assertEqual(pid, "")

    async def test_new_own_reply_with_correct_parent_confirms_send(self):
        self.records = [{"id": "200", "text": "是啊。", "author_id": "42", "parent_pid": "102"}]
        self.assertEqual(await self.manager._tieba_confirm_reply(self.page, "是啊。", {"101"},
            {"id": "42"}, {"parent_pid": "102"}, {"state": "ok", "id": "200"}), "200")

    def bound_response(self, *, tid="123", content="<p>是啊。</p>", parent="", pid="200"):
        item = response(body=json.dumps({"error_code": 0, "pid": pid}))
        item.request = types.SimpleNamespace(method="POST", url=item.url,
            post_data=urlencode({"tid": tid, "content": content, "quote_id": parent}))
        return item

    async def test_bound_success_ack_confirms_without_dom_or_another_click(self):
        self.captcha_after_send()
        item = self.bound_response()
        self.button.click.side_effect = lambda **kw: self.hooks["response"](item)
        result = await self.submit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "200")
        self.assertFalse(result["verification_required"])
        self.assertEqual(self.manager.takeovers.list(), [])
        self.button.click.assert_awaited_once()

    async def test_bound_success_wins_over_a_stale_captcha_overlay(self):
        self.manager.solve_captcha.side_effect = [self.normal, self.normal,
            {"had": True, "solved": False, "message": "旧的验证窗口"}]
        item = self.bound_response()
        self.button.click.side_effect = lambda **kw: self.hooks["response"](item)
        result = await self.submit()
        self.assertTrue(result["ok"])
        self.button.click.assert_awaited_once()

    async def test_unrelated_or_old_success_ack_does_not_confirm_missing_dom(self):
        for options in ({"tid": "999"}, {"content": "别的内容"}, {"pid": "101"}):
            with self.subTest(options=options):
                self.records = [{"id": "101", "text": "旧回复", "author_id": "42"}]
                self.button.click.side_effect = lambda **kw: self.hooks["response"](self.bound_response(**options))
                result = await self.submit()
                self.assertFalse(result["ok"])
                self.assertTrue(result["uncertain"])
                self.assertFalse(result["verification_required"])
                self.button.click.assert_awaited_once()
                await self.manager.takeovers.close_all()
                self.button.click.reset_mock()

    async def test_wrong_parent_request_does_not_get_bound_success(self):
        item = self.bound_response(parent="999")
        snap = await self.manager._tieba_submit_snapshot(item, self.page.url, "是啊。", {"parent_pid": "102"})
        self.assertFalse(snap["request_matches"])

    async def test_bound_ack_does_not_override_conflicting_author_or_parent(self):
        for record in ({"id": "200", "text": "是啊。", "author_id": "99", "parent_pid": "102"},
                       {"id": "200", "text": "是啊。", "author_id": "42", "parent_pid": "999"}):
            self.records = [record]
            self.assertEqual(await self.manager._tieba_confirm_reply(self.page, "是啊。", set(),
                {"id": "42"}, {"parent_pid": "102"}, {"state": "ok", "id": "200", "confirmed_id": "200"}), "")

    async def test_success_body_is_cached_before_reload_and_read_only_once(self):
        item = response(body='{"error_code":0,"data":{"pid":"200"}}')
        async def read_body():
            if self.page.goto.await_count > 1:
                raise RuntimeError("CDP response body no longer exists after navigation")
            return '{"error_code":0,"data":{"pid":"200"}}'
        item.text.side_effect = read_body
        self.button.click.side_effect = lambda **kw: self.hooks["response"](item)
        async def goto(*args, **kw):
            if self.page.goto.await_count > 1:
                self.records = [{"id": "200", "text": "是啊。", "author_id": "42"}]
        self.page.goto.side_effect = goto
        result = await self.submit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "200")
        self.assertEqual(result["network"][0]["read_error"], "")
        item.text.assert_awaited_once()
        self.button.click.assert_awaited_once()

    async def test_response_read_failure_is_logged_and_does_not_authorize_retry(self):
        self.captcha_after_send()
        item = response(body="")
        item.text.side_effect = RuntimeError("CDP response body unavailable")
        self.button.click.side_effect = lambda **kw: self.hooks["response"](item)
        result = await self.submit()
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["network"][0]["status"], 200)
        self.assertEqual(result["network"][0]["read_error"], "RuntimeError")
        item.text.assert_awaited_once()
        self.button.click.assert_awaited_once()

    async def test_truly_empty_response_is_distinct_from_read_failure(self):
        item = response(body="")
        self.button.click.side_effect = lambda **kw: self.hooks["response"](item)
        result = await self.submit()
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["network"][0]["body"], "")
        self.assertEqual(result["network"][0]["read_error"], "")
        item.text.assert_awaited_once()
        self.button.click.assert_awaited_once()

    async def test_unknown_browser_dict_reservation_survives_without_pausing_next_round(self):
        case = manual_fixtures.ManualActionsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.engine.browser.submit_tieba.return_value = {
            "ok": False, "uncertain": True, "verification_required": True, "message": "发送结果未知"}
        job = await case.run_kind()
        self.assertFalse(case.actions.verification)
        self.assertEqual(job["state"], "uncertain")
        self.assertNotIn("请在页面", job["message"])
        self.assertTrue(case.actions.targets)
        second = await case.run_kind(request_id="fixture_request_2")
        self.assertEqual(second["state"], "skipped")
        case.engine.browser.submit_tieba.assert_awaited_once()

    async def test_legacy_adapter_preserves_uncertainty_and_verification(self):
        self.manager.submit_tieba = AsyncMock(side_effect=BrowserError(
            "发送结果未知", uncertain=True, verification_required=True))
        accounts = types.SimpleNamespace(active_account=lambda key: types.SimpleNamespace(name="default"))
        adapter = BrowserPlatform("tieba", accounts, self.manager)
        adapter._allowed_forums = lambda: ["大肥鱼"]
        result = await adapter.comment(Post(platform="tieba", id="123", title="测试",
            url=self.page.url, extra={"tieba_forum": "大肥鱼"}), "是啊。")
        self.assertTrue(result.uncertain)
        self.assertTrue(result.verification_required)
        self.assertEqual(result.url, self.page.url)
