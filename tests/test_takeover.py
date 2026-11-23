"""Manual control keeps the exact failed page and never resumes a send."""
import ast
import asyncio
import base64
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.takeover import BrowserTakeovers, TakeoverError
import test_submission_state as submissions
import test_manual_actions as manuals
import test_auto_comments as automatic


class TakeoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = types.SimpleNamespace(url="https://tieba.baidu.com/p/123",
            viewport_size={"width": 1280, "height": 800}, is_closed=Mock(return_value=False),
            screenshot=AsyncMock(return_value=b"jpeg-frame"), reload=AsyncMock(), go_back=AsyncMock(),
            mouse=types.SimpleNamespace(click=AsyncMock(), move=AsyncMock(), down=AsyncMock(),
                                        up=AsyncMock(), wheel=AsyncMock()),
            keyboard=types.SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock()))
        self.context = types.SimpleNamespace(pages=[self.page], on=Mock(), remove_listener=Mock(), close=AsyncMock())
        self.browser = types.SimpleNamespace(persist_storage=AsyncMock(return_value="new-fingerprint"))
        self.store = BrowserTakeovers(self.browser)
        self.addAsyncCleanup(self.store.close_all)
        self.session = await self.store.retain(self.context, self.page, "tieba", "default",
                                               "fingerprint", job_id="job1")

    async def test_exact_context_and_page_are_retained_and_input_requires_claim(self):
        self.assertIs(self.store.get(self.session.id).page, self.page)
        self.assertIs(self.store.get(self.session.id).context, self.context)
        self.context.close.assert_not_awaited()
        with self.assertRaises(TakeoverError):
            await self.store.input(self.session.id, {"type": "click", "x": 1, "y": 2})
        await self.store.claim(self.session.id)
        await self.store.input(self.session.id, {"type": "click", "x": 100, "y": 200})
        self.page.mouse.click.assert_awaited_once_with(100.0, 200.0, button="left", click_count=1)

    async def test_frame_uses_live_viewport_without_files_or_credentials(self):
        result = await self.store.frame(self.session.id)
        self.assertEqual(base64.b64decode(result["image"].split(",")[1]), b"jpeg-frame")
        self.assertEqual((result["width"], result["height"]), (1280, 800))
        self.assertTrue(result["tabs"][0]["active"])
        self.assertNotIn("fingerprint", result)
        self.assertNotIn("context", result)
        self.assertNotIn("path", self.page.screenshot.await_args.kwargs)

    async def test_text_keys_wheel_and_navigation_use_browser_input(self):
        await self.store.claim(self.session.id)
        for payload in ({"type": "text", "text": "1234"}, {"type": "key", "key": "Enter"},
                        {"type": "wheel", "dy": 450}, {"type": "back"}, {"type": "reload"}):
            await self.store.input(self.session.id, payload)
        self.page.keyboard.insert_text.assert_awaited_once_with("1234")
        self.page.keyboard.press.assert_awaited_once_with("Enter")
        self.page.mouse.wheel.assert_awaited_once_with(0.0, 450.0)
        self.page.reload.assert_awaited_once()
        self.page.go_back.assert_awaited_once()

    async def test_invalid_coordinates_and_arbitrary_code_are_rejected_before_input(self):
        await self.store.claim(self.session.id)
        invalid = [{"type": "click", "x": value, "y": 1} for value in (True, float("nan"), -1, 1281, "5")]
        invalid += [{"type": "evaluate", "code": "alert(1)"}, {"type": "key", "key": "bad"},
                    {"type": "text", "text": "a" * 8193}, {"type": "tab", "index": True},
                    {"type": "drag", "points": [{"x": 1, "y": 1}]}]
        for payload in invalid:
            with self.subTest(payload=payload.get("type")), self.assertRaises(ValueError):
                await self.store.input(self.session.id, payload)
        self.page.mouse.click.assert_not_awaited()
        self.page.keyboard.press.assert_not_awaited()
        self.page.keyboard.insert_text.assert_not_awaited()

    async def test_failed_drag_always_releases_mouse(self):
        await self.store.claim(self.session.id)
        self.page.mouse.move.side_effect = [None, RuntimeError("movement failed")]
        with self.assertRaises(RuntimeError):
            await self.store.input(self.session.id, {"type": "drag", "points": [{"x": 1, "y": 2}, {"x": 200, "y": 2}]})
        self.page.mouse.down.assert_awaited_once()
        self.page.mouse.up.assert_awaited_once()

    async def test_popup_can_be_selected_and_controlled(self):
        other = types.SimpleNamespace(url="https://passport.baidu.com/", is_closed=lambda: False,
                                      viewport_size={"width": 1280, "height": 800})
        self.context.pages.append(other)
        self.context.on.call_args.args[1](other)
        self.assertIs(self.session.page, other)
        await self.store.claim(self.session.id)
        await self.store.input(self.session.id, {"type": "tab", "index": 0})
        self.assertIs(self.session.page, self.page)

    async def test_save_failure_keeps_browser_and_never_overwrites_newer_login(self):
        self.browser.persist_storage.side_effect = RuntimeError("login changed")
        with self.assertRaises(RuntimeError):
            await self.store.close(self.session.id)
        self.assertTrue(self.store.has_account("tieba", "default"))
        self.context.close.assert_not_awaited()
        self.browser.persist_storage.assert_awaited_once_with(self.context, "tieba", "default",
                                                               expected_fingerprint="fingerprint")

    async def test_input_from_stale_frame_cannot_click_new_popup(self):
        await self.store.claim(self.session.id)
        with self.assertRaises(TakeoverError):
            await self.store.input(self.session.id, {"type": "click", "x": 10, "y": 10, "page_id": "stale-page"})
        self.page.mouse.click.assert_not_awaited()

    async def test_closed_popup_returns_control_to_remaining_page(self):
        popup = types.SimpleNamespace(is_closed=lambda: True)
        self.session.page = popup
        self.assertIs(self.store.get(self.session.id).page, self.page)
        self.assertTrue(self.store.has_account("tieba", "default"))

    async def test_close_persists_state_then_releases_context_and_timer(self):
        result = await self.store.close(self.session.id)
        self.assertTrue(result["saved"])
        self.assertFalse(self.store.has_account("tieba", "default"))
        self.assertTrue(self.session.expiry.done())
        self.context.close.assert_awaited_once()

    async def test_unknown_id_and_duplicate_account_cannot_control_other_session(self):
        with self.assertRaises(TakeoverError):
            await self.store.frame("unknown")
        with self.assertRaises(TakeoverError):
            await self.store.retain(self.context, self.page, "tieba", "default", "fingerprint")
        self.assertEqual(len(self.store.sessions), 1)

    async def test_failed_and_successful_submissions_close_without_automatic_takeover(self):
        case = submissions.SubmissionStateTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addAsyncCleanup(case.manager.takeovers.close_all)
        case.button.click.side_effect = lambda **kw: case.hooks["response"](submissions.response(body=""))
        result = await case.manager.submit_tieba("default", case.page.url, "是啊。", job_id="failed-job")
        self.assertTrue(result["uncertain"])
        self.assertNotIn("takeover_id", result)
        self.assertFalse(case.manager.takeovers.has_account("tieba", "default"))
        case.context.close.assert_awaited_once()
        case.context.close.reset_mock()
        async def success(**kw):
            case.hooks["response"](submissions.response(body='{"error_code":0,"data":{"pid":"200"}}'))
            case.records = [{"id": "200", "text": "是啊。", "author_id": "42"}]
        case.button.click.side_effect = success
        self.assertTrue((await case.submit())["ok"])
        case.context.close.assert_awaited_once()

    async def test_retained_account_blocks_automatic_and_manual_sends(self):
        for fixture_type in (manuals.ManualActionsTests, automatic.AutomaticTiebaCommentTests):
            case = fixture_type(); case.setUp(); self.addCleanup(case.doCleanups)
            case.engine.browser.takeovers.has_account.return_value = True
            if hasattr(case, "actions"):
                with self.assertRaises(ValueError):
                    case.actions.start("comment", "takeover_test_job")
            else:
                self.assertEqual(case.engine._eligible_platforms(), [])
            case.engine.browser.submit_tieba.assert_not_awaited()
            case.writer.draft.assert_not_awaited()

    async def test_close_api_requires_matching_wait_and_does_not_send(self):
        source = Path(__file__).parents[1] / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CyberLifePlugin")
        method = next(n for n in cls.body if getattr(n, "name", "") == "api_takeover_close")
        req = types.SimpleNamespace(json=AsyncMock(return_value={"save": True, "processed": True}))
        env = {"request": req, "json_response": lambda x: x,
               "error_response": lambda msg, **kw: {"error": msg, **kw},
               "TakeoverError": TakeoverError, "ManualActionBusy": RuntimeError}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), env)
        actions = types.SimpleNamespace(verification={"default": {"job_id": "other-job"}}, clear_verification=Mock())
        plugin = types.SimpleNamespace(engine=types.SimpleNamespace(_busy=False),
            browser=types.SimpleNamespace(takeovers=self.store), manual_actions=actions)
        result = await env["api_takeover_close"](plugin, self.session.id)
        self.assertEqual(result["status_code"], 409)
        self.context.close.assert_not_awaited()
        actions.verification["default"]["job_id"] = "job1"
        result = await env["api_takeover_close"](plugin, self.session.id)
        self.assertTrue(result["released"])
        actions.clear_verification.assert_called_once_with("default", "job1")
        self.page.mouse.click.assert_not_awaited()
