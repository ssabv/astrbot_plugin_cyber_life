from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core.manual_actions import ManualActions, ManualActionBusy
from core.settings import SettingsStore
from core.tieba_actions import (TiebaSession, TiebaActionError, parse_inbox, parse_submission,
                               pb_bytes, pb_int, pb_str, fields, integer, message, string)
from core.writer import DraftResult


THREAD = {"tid": "123", "fid": "77", "forum": "大肥鱼", "title": "今天又加班", "body": "加班到现在，真累",
          "author_id": "99", "url": "https://tieba.baidu.com/p/123", "posts": [
              {"pid": "101", "floor": "1", "author_id": "99", "author": "楼主", "content": "加班到现在，真累"},
              {"pid": "102", "floor": "2", "author_id": "42", "author": "鱼", "content": "还没忙完啊"}]}
INCOMING = {"tid": "123", "pid": "103", "forum": "大肥鱼", "created_at": time.time(), "author_id": "99",
            "author": "群友", "portrait": "tb.fixture", "content": "是啊还有一堆", "quote_content": "通知引用未核对",
            "quote_pid": "102", "quote_user_id": "42", "thread_author_id": "99", "is_floor": False}


class ManualActionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        settings = SettingsStore({"writer_enabled": True, "platforms": ["tieba"], "tieba_forums": ["大肥鱼"]}, self.root)
        self.account = types.SimpleNamespace(name="default", status="logged_in")
        self.storage = self.root / "fixture.storage.json"
        self.storage.write_text("fixture-session", encoding="utf-8")
        self.stats = types.SimpleNamespace(seen_posts=[], comments=0, replies=0, next_comment_at="future", next_reply_at="future")
        self.writer = types.SimpleNamespace(draft=AsyncMock(return_value=DraftResult(ok=True, body="还没忙完啊。", provider_id="gemini")))
        self.engine = types.SimpleNamespace(settings=settings, accounts=types.SimpleNamespace(active_account=lambda platform: self.account),
                                            browser=types.SimpleNamespace(
                                                takeovers=types.SimpleNamespace(has_account=Mock(return_value=False)),
                                                storage_path=lambda *args: self.storage,
                                                submit_tieba=AsyncMock(return_value={
                                                    "ok": True, "id": "200", "verified": True,
                                                    "message": "页面已确认",
                                                }),
                                            ),
                                            writer=self.writer, _busy=False, history=[], save=Mock(), stats=lambda: self.stats)
        self.actions = ManualActions(self.engine, self.root)
        self.session = types.SimpleNamespace(identity=AsyncMock(return_value="42"), thread=AsyncMock(return_value=copy.deepcopy(THREAD)),
                                             latest_threads=AsyncMock(return_value=[{"tid": "123", "created_at": 100, "forum": "大肥鱼"}]),
                                             inbox=AsyncMock(return_value=[]), submit=AsyncMock(return_value="200"))
        # MagicMock supplies async context-manager special methods.
        from unittest.mock import MagicMock
        self.outer = MagicMock()
        self.outer.__aenter__.return_value = self.session
        self.outer.__aexit__.return_value = False
        self.session_patch = patch("core.manual_actions.TiebaSession", return_value=self.outer)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    async def run_kind(self, kind="comment", request_id="fixture_request_1"):
        self.actions.start(kind, request_id)
        await self.actions.run(request_id)
        return self.actions.jobs[request_id]

    async def test_comment_sends_once_and_keeps_automatic_quota_and_schedule(self):
        job = await self.run_kind()
        self.assertTrue(job["ok"])
        self.assertEqual(job["sent_pid"], "200")
        self.engine.browser.submit_tieba.assert_awaited_once()
        self.assertEqual((self.stats.comments, self.stats.replies, self.stats.next_comment_at, self.stats.next_reply_at), (0, 0, "future", "future"))
        self.assertEqual(self.actions.public()["manual_comments"], 1)
        self.assertEqual(self.writer.draft.await_args.kwargs["material"]["content"], THREAD["body"])
        self.assertTrue(self.engine.history[0]["manual"])
        self.assertIn("tieba:" + hashlib.sha1(THREAD["url"].encode()).hexdigest()[:16], self.stats.seen_posts)
        self.assertFalse(self.engine._busy)

    async def test_duplicate_http_request_does_not_start_second_job(self):
        job = await self.run_kind()
        same, created = self.actions.start("comment", job["id"])
        self.assertIs(same, job)
        self.assertFalse(created)
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_new_click_skips_already_submitted_target_after_reload(self):
        await self.run_kind()
        self.actions = ManualActions(self.engine, self.root)
        job = await self.run_kind(request_id="fixture_request_2")
        self.assertEqual(job["state"], "skipped")
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_manual_comment_skips_known_automatic_comment(self):
        self.engine.history.append({"kind": "comment", "platform": "tieba", "url": THREAD["url"] + "?fr=frs"})
        job = await self.run_kind()
        self.assertEqual(job["state"], "skipped")
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_latest_already_commented_post_skipped_then_next_selected(self):
        self.session.latest_threads.return_value = [{"tid": "999", "created_at": 200, "forum": "大肥鱼"},
                                                   {"tid": "123", "created_at": 100, "forum": "大肥鱼"}]
        self.engine.history.append({"kind": "comment", "platform": "tieba", "url": "https://tieba.baidu.com/p/999"})
        selected = await self.actions.prepare(self.session, "comment", "42")
        self.assertEqual((selected["thread"]["tid"], selected["created_at"]), ("123", 100))
        self.assertEqual(selected["selection_order"], "created_at_desc")
        self.session.thread.assert_awaited_once_with("123")
        self.session.latest_threads.assert_awaited_once_with(["大肥鱼"])
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_unavailable_latest_list_never_writes_or_falls_back(self):
        self.session.latest_threads.side_effect = TiebaActionError("latest feed unavailable")
        job = await self.run_kind()
        self.assertEqual(job["state"], "error")
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_busy_and_invalid_requests_rejected(self):
        self.actions.start("comment", "fixture_request_1")
        with self.assertRaises(ManualActionBusy):
            self.actions.start("reply", "fixture_request_2")
        with self.assertRaises(ValueError):
            self.actions.start("reply", "fixture_request_1")
        with self.assertRaises(ValueError):
            self.actions.start("post", "fixture_request_3")

    async def test_writer_failure_never_calls_submit(self):
        self.writer.draft.return_value = DraftResult(error="TimeoutError")
        job = await self.run_kind()
        self.assertEqual(job["state"], "error")
        self.engine.browser.submit_tieba.assert_not_awaited()
        self.assertFalse(self.actions.targets)

    async def test_changed_login_or_scope_never_calls_submit(self):
        async def changed(**kwargs):
            self.storage.write_text("changed-fixture")
            return DraftResult(ok=True, body="收到")
        self.writer.draft.side_effect = changed
        self.assertEqual((await self.run_kind())["state"], "error")
        self.engine.browser.submit_tieba.assert_not_awaited()
        self.writer.draft.side_effect = None
        async def scope(**kwargs):
            self.engine.settings.update({"tieba_forums": ["其他吧"]})
            return DraftResult(ok=True, body="收到")
        self.writer.draft.side_effect = scope
        self.assertEqual((await self.run_kind(request_id="fixture_request_2"))["state"], "error")
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_empty_inbox_skips_without_model(self):
        job = await self.run_kind("reply")
        self.assertEqual(job["state"], "skipped")
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_regular_reply_targets_incoming_post_with_verified_own_context(self):
        self.session.inbox.return_value = [copy.deepcopy(INCOMING)]
        job = await self.run_kind("reply")
        self.assertTrue(job["ok"])
        self.assertEqual(job["target"]["parent_pid"], "103")
        self.assertEqual(job["target"]["sub_pid"], "")
        material = self.writer.draft.await_args.kwargs["material"]
        self.assertEqual(material["incoming"], INCOMING["content"])
        self.assertEqual(material["own_comment"], "还没忙完啊")
        self.assertEqual(self.actions.public()["manual_replies"], 1)

    async def test_floor_reply_uses_real_parent_and_distinct_subcomment(self):
        incoming = {**INCOMING, "is_floor": True}
        self.session.inbox.return_value = [incoming]
        job = await self.run_kind("reply")
        self.assertEqual(job["target"]["parent_pid"], "102")
        self.assertEqual(job["target"]["sub_pid"], "103")

    async def test_unknown_floor_self_old_and_other_forum_are_skipped(self):
        unknown = {**INCOMING, "is_floor": True, "quote_pid": "999", "quote_user_id": "43"}
        self.session.inbox.return_value = [unknown, {**INCOMING, "author_id": "42"},
                                           {**INCOMING, "forum": "其他吧"}, {**INCOMING, "created_at": 1}]
        job = await self.run_kind("reply")
        self.assertEqual(job["state"], "skipped")
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()

    async def test_floor_reply_uses_verified_own_quote_pid_from_notification(self):
        incoming = {**INCOMING, "is_floor": True, "quote_pid": "999", "quote_user_id": "42"}
        self.session.inbox.return_value = [incoming]
        job = await self.run_kind("reply")
        self.assertTrue(job["ok"])
        self.assertEqual(job["target"]["parent_pid"], "999")
        self.assertEqual(job["target"]["sub_pid"], INCOMING["pid"])

    async def test_unconfirmed_send_blocks_target_across_restart(self):
        self.engine.browser.submit_tieba.side_effect = TiebaActionError("unknown", uncertain=True)
        job = await self.run_kind()
        self.assertEqual(job["state"], "uncertain")
        self.actions = ManualActions(self.engine, self.root)
        self.assertFalse(self.actions.verification)
        self.assertFalse(self.actions.is_waiting(self.account.name))
        job = await self.run_kind(request_id="fixture_request_2")
        self.assertEqual(job["state"], "skipped")
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_definite_rejection_does_not_retry_inside_job(self):
        self.engine.browser.submit_tieba.side_effect = TiebaActionError("captcha required")
        job = await self.run_kind()
        self.assertEqual(job["state"], "error")
        self.assertFalse(self.actions.targets)
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_recheck_confirmed_send_releases_only_matching_wait_and_never_resends(self):
        self.engine.browser.submit_tieba.side_effect = TiebaActionError("unknown", uncertain=True)
        job = await self.run_kind()
        self.engine.browser.confirm_tieba_sent = AsyncMock(return_value=True)
        self.engine.browser.takeovers.for_account = Mock(return_value=None)
        result = await self.actions.recheck(job["id"], "200")
        self.assertTrue(result["confirmed"])
        self.assertTrue(job["ok"])
        self.assertEqual(job["sent_pid"], "200")
        self.assertFalse(self.actions.verification)
        self.assertTrue(self.actions.targets)
        self.engine.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(len(self.engine.history), 1)

    async def test_recheck_missing_reply_keeps_target_without_pausing(self):
        self.engine.browser.submit_tieba.side_effect = TiebaActionError("unknown", uncertain=True)
        job = await self.run_kind()
        self.engine.browser.confirm_tieba_sent = AsyncMock(return_value=False)
        self.engine.browser.takeovers.for_account = Mock(return_value=None)
        result = await self.actions.recheck(job["id"], "200")
        self.assertFalse(result["confirmed"])
        self.assertFalse(self.actions.verification)
        self.assertEqual(job["state"], "uncertain")
        self.assertTrue(self.actions.targets)
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_old_wait_migrates_without_losing_unknown_target(self):
        self.engine.browser.submit_tieba.side_effect = TiebaActionError("unknown", uncertain=True)
        job = await self.run_kind()
        self.actions.verification[self.account.name] = {"job_id": job["id"], "uncertain": True}
        self.actions.save()
        restored = ManualActions(self.engine, self.root)
        self.assertFalse(restored.is_waiting(self.account.name))
        self.assertEqual(restored.targets[job["target_key"]], job["id"])
        self.assertIn("后续活动继续", restored.jobs[job["id"]]["message"])
        self.engine.browser.submit_tieba.assert_awaited_once()

    async def test_old_unsent_wait_releases_only_its_own_target(self):
        self.actions.jobs["old_job_fixture"] = {"id": "old_job_fixture", "state": "waiting_verification",
                                               "target_key": "target", "ok": False}
        self.actions.targets["target"] = "old_job_fixture"
        self.actions.targets["other_target"] = "other_job"
        self.actions.verification[self.account.name] = {"job_id": "old_job_fixture", "uncertain": False}
        self.actions.save()
        restored = ManualActions(self.engine, self.root)
        self.assertFalse(restored.verification)
        self.assertEqual(restored.jobs["old_job_fixture"]["state"], "error")
        self.assertEqual(restored.targets, {"other_target": "other_job"})

    async def test_cancellation_during_send_remains_uncertain(self):
        self.engine.browser.submit_tieba.side_effect = asyncio.CancelledError()
        self.actions.start("comment", "fixture_request_1")
        with self.assertRaises(asyncio.CancelledError):
            await self.actions.run("fixture_request_1")
        self.assertEqual(self.actions.jobs["fixture_request_1"]["state"], "uncertain")
        self.assertFalse(self.engine._busy)
        self.assertTrue(self.actions.targets)

    async def test_preview_is_read_only(self):
        before = self.actions.path.read_bytes()
        result = await self.actions.preview("comment")
        self.assertTrue(result["preview_only"])
        self.assertEqual(self.actions.path.read_bytes(), before)
        self.writer.draft.assert_not_awaited()
        self.engine.browser.submit_tieba.assert_not_awaited()


class TiebaProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_feed_uses_publication_time_and_excludes_pinned_ads_invalid_time(self):
        session = TiebaSession(None, "default")
        session.api = AsyncMock(return_value={"thread_list": [
            {"id": "1", "create_time": 100, "last_time_int": 9999},
            {"id": "2", "create_time": 200, "last_time_int": 200, "is_top": "0"},
            {"id": "3", "create_time": 900, "is_top": "1"},
            {"id": "4", "create_time": 800, "is_membertop": 1},
            {"id": "5", "create_time": 700, "is_tiebaplus_ad": 1},
            {"id": "6", "last_time_int": 9000},
            {"id": "7", "create_time": "bad"},
        ]})
        rows = await session.latest_threads(["大肥鱼"])
        self.assertEqual([row["tid"] for row in rows], ["2", "1"])
        self.assertEqual(session.api.await_args.args[1], {"kw": "大肥鱼", "pn": "1", "rn": "30", "tab_id": "503", "sort_type": "1"})

    async def test_latest_feed_merges_forums_before_limiting_and_rejects_malformed_list(self):
        session = TiebaSession(None, "default")
        session.api = AsyncMock(side_effect=[{"data": {"thread_list": [{"id": "1", "create_time": 100}]}},
                                             {"thread_list": [{"id": "2", "create_time": 200}]}])
        rows = await session.latest_threads(["大肥鱼", "另一个吧"], limit=1)
        self.assertEqual(rows, [{"tid": "2", "created_at": 200, "forum": "另一个吧"}])
        session.api = AsyncMock(return_value={"thread_list": "invalid"})
        with self.assertRaises(TiebaActionError):
            await session.latest_threads(["大肥鱼"])

    def test_inbox_shape_and_metadata(self):
        user = pb_int(2, 99) + pb_str(4, "群友") + pb_str(5, "portrait")
        row = pb_int(1, 123) + pb_int(2, 103) + pb_int(3, 100) + pb_str(5, "大肥鱼") + pb_str(6, "还没忙完") + pb_bytes(9, user)
        self.assertEqual(parse_inbox(pb_bytes(2, pb_bytes(2, row)))[0]["author_id"], "99")
        self.assertEqual(parse_inbox(pb_bytes(2, b"")), [])
        with self.assertRaises(ValueError):
            parse_inbox(b"not protobuf")

    def test_success_needs_positive_ack_and_conflicts_are_uncertain(self):
        self.assertEqual(parse_submission(pb_bytes(2, pb_str(3, "123"))), "123")
        for data in (pb_bytes(2, b""), pb_bytes(1, pb_int(1, 4)) + pb_bytes(2, pb_str(3, "123"))):
            with self.assertRaises(TiebaActionError) as error:
                parse_submission(data)
            self.assertTrue(error.exception.uncertain)
        with self.assertRaises(TiebaActionError) as error:
            parse_submission(pb_bytes(1, pb_int(1, 4)))
        self.assertFalse(error.exception.uncertain)

    async def test_submit_http_error_is_uncertain_without_retry(self):
        session = TiebaSession(None, "default")
        session.bduss = "fixture"
        session.identity = AsyncMock(return_value="42")
        session._request = AsyncMock(side_effect=[b'{"is_login":1,"tbs":"fixture"}', TiebaActionError("HTTP 503")])
        with self.assertRaises(TiebaActionError) as error:
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(session._request.await_count, 2)

    async def test_scope_and_identity_mismatch_block_before_submit(self):
        session = TiebaSession(None, "default")
        session.identity = AsyncMock(return_value="100")
        session._request = AsyncMock()
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["其他吧"])
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"])
        session._request.assert_not_awaited()

    async def test_floor_encode_keeps_parent_sub_and_author_ids(self):
        session = TiebaSession(None, "default")
        session.bduss = "fixture"
        session.identity = AsyncMock(return_value="42")
        session._request = AsyncMock(side_effect=[b'{"is_login":1,"tbs":"fixture"}', pb_bytes(2, pb_str(3, "200"))])
        target = {"parent_pid": "102", "sub_pid": "103", "author_id": "99", "author": "群友", "portrait": "tb.fixture"}
        self.assertEqual(await session.submit(copy.deepcopy(THREAD), "还没完啊", "42", ["大肥鱼"], target), "200")
        encoded = message(fields(session._request.await_args.kwargs["multipart"]), 1)
        self.assertEqual((string(encoded, 46), string(encoded, 50), string(encoded, 20)), ("102", "103", "99"))

    async def test_final_guard_blocks_change_during_read_only_preflight(self):
        session = TiebaSession(None, "default")
        session.bduss = "fixture"
        session.identity = AsyncMock(return_value="42")
        session._request = AsyncMock(return_value=b'{"is_login":1,"tbs":"fixture"}')
        def changed():
            raise TiebaActionError("scope changed")
        with self.assertRaises(TiebaActionError):
            await session.submit(copy.deepcopy(THREAD), "收到", "42", ["大肥鱼"], pre_send=changed)
        session._request.assert_awaited_once()

