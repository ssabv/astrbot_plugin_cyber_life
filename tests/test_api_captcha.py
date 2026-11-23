"""API challenges use real verification data; unknown writes never repeat."""
import asyncio
import copy
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

from core.tieba_actions import (TiebaActionError, TiebaSession, challenge_url,
    fields, message, parse_submission, pb_bytes, pb_int, pb_str, string,
    verification_from_json, web_verification_data)
from test_manual_actions import THREAD

URL = "https://wappass.baidu.com/static/captcha/fixture?token=fixture"
SUCCESS_URL = "https://seccaptcha.baidu.com/v1/webapi/verint/verify/fixture"
BODY = b'{"code":0,"data":{"pass":true,"token":"fixture-success"}}'


def challenge(pid="", url=URL):
    anti = pb_str(3, "1") + pb_str(4, "fixture-md5") + pb_str(6, "6") + pb_str(12, url)
    return pb_bytes(1, pb_int(1, 4)) + pb_bytes(2, pb_str(3, pid) + pb_bytes(14, anti))


class ApiCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def session(self, responses):
        session = TiebaSession(None, "fixture")
        session.bduss = "fixture"
        session.identity = AsyncMock(return_value="42")
        session._request = AsyncMock(side_effect=[b'{"is_login":1,"tbs":"fixture"}', *responses])
        session.pass_captcha = AsyncMock(return_value={"solved": True, "vcode": '{"pass":true,"token":"fixture-success"}'})
        return session

    def page_session(self, *, acknowledged=True, solved=True):
        events = {}
        page = types.SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            route=AsyncMock(), on=lambda name, handler: events.update({name: handler}),
            remove_listener=Mock(), close=AsyncMock(), main_frame=object())
        response = types.SimpleNamespace(url=SUCCESS_URL, status=200, body=AsyncMock(return_value=BODY))
        async def solve(*args):
            if acknowledged:
                events["response"](response)
                await asyncio.sleep(0)
            return {"solved": solved, "message": "fixture failure"}
        browser = types.SimpleNamespace(settings=types.SimpleNamespace(bool=lambda name: True),
                                        solve_captcha=AsyncMock(side_effect=solve))
        session = TiebaSession(browser, "fixture")
        session.context = types.SimpleNamespace(new_page=AsyncMock(return_value=page))
        return session, page

    def test_legacy_url_is_upgraded_without_altering_challenge(self):
        raw = "http://wappass.baidu.com:80/static/captcha/fixture?a=a%2Fb&b=1"
        self.assertEqual(challenge_url(raw), "https://wappass.baidu.com/static/captcha/fixture?a=a%2Fb&b=1")
        with self.assertRaises(TiebaActionError) as caught:
            parse_submission(challenge(url=raw))
        self.assertEqual(caught.exception.verification["url"], challenge_url(raw))
        self.assertEqual(verification_from_json({"data":{"vcode_pic_url":raw}})["url"], challenge_url(raw))

    def test_url_does_not_upgrade_untrusted_or_malformed_addresses(self):
        for raw in ("http://127.0.0.1/x", "http://u:p@wappass.baidu.com/x", "http://wappass.baidu.com:81/x",
                    "http://wappass.baidu.com.evil.invalid/x", "http://wappass.baidu.com/x\n", "http://wappass.baidu.com\\x"):
            self.assertEqual(challenge_url(raw), "")

    def test_extra_endpoint_is_preserved(self):
        extra = pb_str(3, URL)
        anti = pb_str(3, "1") + pb_str(4, "fixture-md5") + pb_str(6, "6") + pb_bytes(13, extra)
        with self.assertRaises(TiebaActionError) as caught:
            parse_submission(pb_bytes(2, pb_bytes(14, anti)))
        self.assertEqual(caught.exception.verification["url"], URL)

    def test_success_data_requires_official_positive_acknowledgement(self):
        self.assertEqual(json.loads(web_verification_data(SUCCESS_URL, 200, BODY))["token"], "fixture-success")
        self.assertTrue(web_verification_data(SUCCESS_URL, 200, b'callback('+BODY+b');'))
        bad = (b'{"code":0,"data":{"pass":false}}', b'{"code":false,"data":{"pass":true}}',
               b'{"code":1,"data":{"pass":true}}', b'{"code":0,"data":{"pass":true,"cookie":"secret"}}',
               b'{"code":0,"data":{"pass":"1"}}', b'{}', b'not-json')
        for body in bad:
            self.assertEqual(web_verification_data(SUCCESS_URL, 200, body), "")
        for url in ("https://evil.invalid/v1/webapi/verint/verify/", "https://seccaptcha.baidu.com/other", "http://seccaptcha.baidu.com/v1/webapi/verint/verify/x"):
            self.assertEqual(web_verification_data(url, 200, BODY), "")
        self.assertEqual(web_verification_data(SUCCESS_URL, 403, BODY), "")

    async def test_exact_challenge_page_is_sent_to_existing_solver(self):
        session, page = self.page_session()
        result = await session.pass_captcha({"url":URL,"type":"6"})
        self.assertTrue(result["solved"])
        page.goto.assert_awaited_once_with(URL, wait_until="domcontentloaded", timeout=45000)
        session.browser.solve_captcha.assert_awaited_once_with(page, "tieba", "接口原挑战")
        page.close.assert_awaited_once()
        self.assertEqual(json.loads(result["vcode"])["token"], "fixture-success")

    async def test_widget_disappearance_without_server_proof_does_not_authorize_submit(self):
        session, page = self.page_session(acknowledged=False)
        self.assertFalse((await session.pass_captcha({"url":URL,"type":"6"}))["solved"])
        page.close.assert_awaited_once()

    async def test_no_url_and_disabled_solver_do_not_open_homepage(self):
        session, _ = self.page_session()
        self.assertFalse((await session.pass_captcha({"type":"6"}))["solved"])
        session.context.new_page.assert_not_awaited()
        session.browser.settings.bool = lambda name: False
        self.assertTrue((await session.pass_captcha({"url":URL,"type":"6"}))["skipped"])
        session.context.new_page.assert_not_awaited()

    async def test_navigation_failure_closes_page_and_reports_failure(self):
        session, page = self.page_session()
        page.goto.side_effect = TimeoutError
        self.assertFalse((await session.pass_captcha({"url":URL,"type":"6"}))["solved"])
        page.close.assert_awaited_once()
        session.browser.solve_captcha.assert_not_awaited()

    async def test_verified_challenge_continues_same_body_and_target_once(self):
        session = self.session([challenge(), pb_bytes(2,pb_str(3,"200"))])
        target = {"parent_pid":"102","sub_pid":"103","author_id":"99","author":"群友","portrait":"tb.fixture"}
        self.assertEqual(await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"], target), "200")
        calls = [call for call in session._request.await_args_list if "multipart" in call.kwargs]
        self.assertEqual(len(calls), 2)
        first, second = [message(fields(call.kwargs["multipart"]),1) for call in calls]
        for n in (19,26,30,45,46,49,50,20):
            self.assertEqual(string(first,n),string(second,n))
        self.assertEqual((string(second,14),string(second,15)),("fixture-md5","6"))
        self.assertTrue(json.loads(string(second,13))["pass"])
        session.pass_captcha.assert_awaited_once()

    async def test_failed_solver_preserves_challenge_without_a_second_write(self):
        session = self.session([challenge()])
        session.pass_captcha.return_value = {"solved":False,"reason":"recognition_failed","message":"未识别"}
        with self.assertRaises(TiebaActionError) as caught:
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        self.assertTrue(caught.exception.verification_required)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.verification["url"],URL)
        self.assertEqual(session._request.await_count,2)

    async def test_second_challenge_is_not_solved_or_retried_again(self):
        session = self.session([challenge(),challenge()])
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        session.pass_captcha.assert_awaited_once()
        self.assertEqual(session._request.await_count,3)

    async def test_uncertain_write_and_conflicting_ack_never_invoke_solver(self):
        for response in (challenge(pid="200"),pb_bytes(2,b""),TimeoutError()):
            session = self.session([response])
            with self.assertRaises(TiebaActionError) as caught:
                await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
            self.assertTrue(caught.exception.uncertain)
            session.pass_captcha.assert_not_awaited()
            self.assertEqual(session._request.await_count,2)

    async def test_account_or_permission_change_blocks_continuation(self):
        session = self.session([challenge()])
        session.identity.side_effect = ["42","99"]
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        self.assertEqual(session._request.await_count,2)
        session = self.session([challenge()])
        guard = Mock(side_effect=[None,TiebaActionError("permission changed")])
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"],pre_send=guard)
        self.assertEqual(session._request.await_count,2)

    async def test_missing_md5_never_allows_continuation(self):
        session = self.session([])
        anti = pb_str(3,"1")+pb_str(6,"6")+pb_str(12,URL)
        session._request.side_effect = [b'{"is_login":1,"tbs":"fixture"}',pb_bytes(2,pb_bytes(14,anti))]
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        self.assertEqual(session._request.await_count,2)
