"""Exercise the real scheduler and shared sender without publishing."""
import copy
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from core.activity import ActivityEngine
from core.manual_actions import ManualActions
from core.platforms.base import Post
from core.settings import SettingsStore
from core.tieba_actions import TiebaActionError
from core.tieba_actions import image_urls
from core.video_analysis import VideoAnalyzer, VideoInsight
from core.writer import DraftResult
from core.util import now_tz


class AutomaticTiebaCommentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore({"platforms": ["tieba"], "tieba_forums": ["大肥鱼"],
                                       "writer_enabled": True, "comment_probability": 1,
                                       "mood_enabled": False, "notify_actions": False, "max_draws_per_day": 0}, self.root)
        self.storage = self.root / "fixture.storage.json"
        self.storage.write_text("fixture", encoding="utf-8")
        self.account = types.SimpleNamespace(name="default", status="logged_in")
        self.accounts = types.SimpleNamespace(active_account=lambda platform: self.account if platform == "tieba" else None)
        self.writer = types.SimpleNamespace(draft=AsyncMock(return_value=DraftResult(ok=True, body="还没忙完啊", provider_id="gemini")))
        self.browser = types.SimpleNamespace(
            takeovers=types.SimpleNamespace(has_account=Mock(return_value=False)),
            storage_path=lambda *args: self.storage,
            submit_tieba=AsyncMock(return_value={
                "ok": True, "id": "200", "verified": True, "message": "页面已确认",
            }),
        )
        self.engine = self.make_engine()
        self.engine.tieba_actions = ManualActions(self.engine, self.root)
        self.stats = self.engine.stats()
        self.stats.next_comment_at = "2000-01-01T00:00:00+08:00"
        self.stats.next_watch_at = "2099-01-01T00:00:00+08:00"
        self.engine._watch_round = AsyncMock()
        self.thread = {"tid": "123", "fid": "77", "forum": "大肥鱼", "title": "今天加班",
                       "body": "还没弄完", "author_id": "99", "url": "https://tieba.baidu.com/p/123", "posts": []}
        self.session = types.SimpleNamespace(identity=AsyncMock(return_value="42"),
                                             latest_threads=AsyncMock(return_value=[{"tid": "123", "created_at": 100}]),
                                             thread=AsyncMock(return_value=self.thread), submit=AsyncMock(return_value="200"))
        outer = MagicMock()
        outer.__aenter__.return_value = self.session
        outer.__aexit__.return_value = False
        mocked = patch("core.manual_actions.TiebaSession", return_value=outer)
        mocked.start()
        self.addCleanup(mocked.stop)

    def make_engine(self):
        return ActivityEngine(settings=self.settings, accounts=self.accounts, mood=None, analyzer=None,
                              comfy=None, llm=None, browser=self.browser, data_dir=self.root,
                              notify=AsyncMock(), writer=self.writer)

    async def test_due_comment_executes_without_browse_due_and_counts_once_after_reload(self):
        await self.engine.tick()
        self.engine._watch_round.assert_not_awaited()
        self.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(self.stats.comments, 1)
        self.assertFalse(self.engine.history[-1]["manual"])
        self.assertEqual(self.engine.tieba_actions.public()["manual_comments"], 0)
        self.assertEqual(self.engine.tieba_actions.public()["current"], {})
        self.assertFalse(self.engine._busy)
        self.assertEqual(self.engine.auto_comment["state"], "done")
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        self.assertEqual(restored.stats().comments, 1)
        self.assertNotEqual(restored.stats().next_comment_at, "2000-01-01T00:00:00+08:00")

    async def test_reconcile_acknowledged_send_after_activity_save_gap(self):
        await self.engine.tick()
        job = next(iter(self.engine.tieba_actions.jobs.values()))
        self.stats.comments = 0
        self.stats.auto_comment_jobs = []
        self.engine.save()
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        self.assertEqual(restored.stats().comments, 1)
        restored.account_auto_action(job)
        self.assertEqual(restored.stats().comments, 1)

    async def test_probability_miss_schedules_next_without_model_or_send(self):
        self.settings.update({"comment_probability": 0})
        await self.engine.tick()
        self.writer.draft.assert_not_awaited()
        self.browser.submit_tieba.assert_not_awaited()
        self.assertEqual(self.engine.auto_comment["reason"], "probability")
        self.assertNotEqual(self.stats.next_comment_at, "2000-01-01T00:00:00+08:00")

    async def test_not_due_disabled_budget_or_quota_prevents_comment(self):
        cases = ({"next": "2099-01-01T00:00:00+08:00"}, {"settings": {"comment_enabled": False}},
                 {"settings": {"auto_run": False}}, {"settings": {"max_comments_per_day": 0}})
        for case in cases:
            with self.subTest(case=case):
                self.settings.update({"comment_enabled": True, "auto_run": True, "max_comments_per_day": 8,
                                      "daily_watch_minutes": 60})
                self.settings.update(case.get("settings", {}))
                self.stats.next_comment_at = case.get("next", "2000-01-01T00:00:00+08:00")
                await self.engine.tick()
        self.writer.draft.assert_not_awaited()
        self.browser.submit_tieba.assert_not_awaited()

    async def test_comment_ignores_browse_budget_and_switch(self):
        cases = ({"daily_watch_minutes": 0}, {"browse_enabled": False})
        for case in cases:
            with self.subTest(case=case):
                self.settings.update(case)
                self.stats.next_comment_at = "2000-01-01T00:00:00+08:00"
                await self.engine.tick()
        self.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(self.stats.comments, 1)
        self.engine._watch_round.assert_not_awaited()
        self.assertEqual(self.engine._tieba_comment_status(self.stats, now_tz("Asia/Shanghai"))["gate"], "waiting")

    async def test_tieba_image_urls_are_bounded_and_upgraded_to_https(self):
        content = [
            {"type": "0", "text": "看图"},
            {"type": "2", "origin_src": "http://tiebapic.baidu.com/forum/a.jpg?size=1"},
            {"type": "2", "src": "//imgsrc.baidu.com/forum/b.jpg"},
            {"type": "2", "src": "http://localhost/secret"},
            {"type": "2", "src": "http://user:pass@tiebapic.baidu.com/leak.jpg"},
        ] * 3
        urls = image_urls(content)
        self.assertEqual(urls, ["https://tiebapic.baidu.com/forum/a.jpg?size=1",
                                "https://imgsrc.baidu.com/forum/b.jpg"])

    async def test_tieba_images_are_described_by_vision_provider(self):
        self.llm = types.SimpleNamespace(
            available=True,
            complete_json=AsyncMock(return_value={"summary": "一只大肥鱼", "topics": ["日常"],
                                                   "sentiment": 0.4, "comment_hint": "很肥"}),
        )
        analyzer = VideoAnalyzer(self.settings, self.llm, self.root)
        insight = await analyzer.analyze_images(title="看图", body="帮我看看",
                                                images=["http://tiebapic.baidu.com/a.jpg"])
        self.llm.complete_json.assert_awaited_once()
        self.assertEqual(self.llm.complete_json.await_args.kwargs["images"],
                         ["https://tiebapic.baidu.com/a.jpg"])
        self.assertEqual(insight.summary, "一只大肥鱼")
        self.assertEqual(insight.source, "vision")

    async def test_tieba_images_are_cached_across_calls(self):
        self.llm = types.SimpleNamespace(
            available=True,
            complete_json=AsyncMock(return_value={"summary": "一只大肥鱼"}),
        )
        analyzer = VideoAnalyzer(self.settings, self.llm, self.root)
        first = await analyzer.analyze_images(title="看图", body="帮我看看",
                                              images=["http://tiebapic.baidu.com/a.jpg"])
        second = await analyzer.analyze_images(title="看图", body="帮我看看",
                                               images=["https://tiebapic.baidu.com/a.jpg"])
        self.llm.complete_json.assert_awaited_once()
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.summary, "一只大肥鱼")

    async def test_tieba_comment_material_includes_image_summary(self):
        self.engine.analyzer = types.SimpleNamespace(analyze_images=AsyncMock(
            return_value=VideoInsight(ok=True, summary="一只大肥鱼")))
        self.session.thread = AsyncMock(return_value={**self.thread, "images": ["https://tiebapic.baidu.com/a.jpg"]})
        preview = await self.engine.tieba_actions.preview("comment")
        self.assertEqual(preview["material"]["image_summary"], "一只大肥鱼")
        self.assertNotIn("images", preview["material"])

    async def test_tieba_reply_reads_body_context_and_target_images(self):
        self.prepare_reply()
        async def describe_images(*, title, body, images):
            return VideoInsight(ok=True, summary=body or title, frames=len(images))
        self.engine.analyzer = types.SimpleNamespace(analyze_images=AsyncMock(side_effect=describe_images))
        self.thread["images"] = ["https://tiebapic.baidu.com/body.jpg"]
        self.thread["posts"] = [
            {"pid": "102", "floor": "2", "author_id": "42", "author": "群友",
             "content": "还没忙完啊", "images": ["https://tiebapic.baidu.com/own.jpg"]},
            {"pid": "103", "floor": "3", "author_id": "99", "author": "别人",
             "content": "是啊还有一堆", "images": ["https://tiebapic.baidu.com/target.jpg"]},
            {"pid": "104", "floor": "4", "author_id": "100", "author": "其他",
             "content": "别的楼层", "images": ["https://tiebapic.baidu.com/other.jpg"]},
        ]
        await self.engine.tick()
        self.assertEqual(self.engine.analyzer.analyze_images.await_count, 3)
        self.assertEqual([call.kwargs["images"] for call in self.engine.analyzer.analyze_images.await_args_list],
                         [["https://tiebapic.baidu.com/body.jpg"],
                          ["https://tiebapic.baidu.com/own.jpg", "https://tiebapic.baidu.com/target.jpg"],
                          ["https://tiebapic.baidu.com/target.jpg"]])
        material = self.writer.draft.await_args.kwargs["material"]
        self.assertEqual(material["image_summary"], self.thread["body"])
        self.assertEqual(material["context_image_summary"], "还没忙完啊\n是啊还有一堆")
        self.assertEqual(material["incoming_image_summary"], "是啊还有一堆")

    async def test_tieba_reply_fetches_current_floor_and_target_by_pid(self):
        self.prepare_reply()
        current = {"pid": "103", "floor": "48", "author_id": "99", "author": "别人",
                   "content": "是啊还有一堆", "images": ["https://tiebapic.baidu.com/target.jpg"]}
        current_floor = {"pid": "102", "floor": "2", "author_id": "42", "author": "群友",
                         "content": "还没忙完啊", "images": ["https://tiebapic.baidu.com/floor2.jpg"]}
        calls = []

        async def read_thread(tid, anchor=""):
            calls.append(anchor)
            return {**self.thread, "posts": [current] if anchor == "103" else (
                [current_floor] if anchor == "102" else [])}

        self.session.thread = AsyncMock(side_effect=read_thread)
        async def describe_images(*, title, body, images):
            return VideoInsight(ok=True, summary=body or title, frames=len(images))
        self.engine.analyzer = types.SimpleNamespace(analyze_images=AsyncMock(side_effect=describe_images))

        await self.engine.tick()

        self.assertEqual(calls, ["103", "102"])
        material = self.writer.draft.await_args.kwargs["material"]
        self.assertEqual(material["incoming_image_summary"], "是啊还有一堆")
        self.assertEqual(material["context_image_summary"], "还没忙完啊\n是啊还有一堆")

    async def test_writer_failure_keeps_quota_and_records_reason(self):
        self.writer.draft.return_value = DraftResult(error="TimeoutError")
        await self.engine.tick()
        self.assertEqual(self.stats.comments, 0)
        self.browser.submit_tieba.assert_not_awaited()
        self.assertIn("TimeoutError", self.engine.auto_comment["message"])

    async def test_switch_off_during_draft_prevents_submission(self):
        async def disabled(**kwargs):
            self.settings.update({"comment_enabled": False})
            return DraftResult(ok=True, body="收到", provider_id="gemini")
        self.writer.draft.side_effect = disabled
        await self.engine.tick()
        self.assertEqual(self.stats.comments, 0)
        self.browser.submit_tieba.assert_not_awaited()
        self.assertFalse(self.engine.tieba_actions.targets)

    async def test_uncertain_submit_is_not_repeated_at_next_interval(self):
        self.browser.submit_tieba.side_effect = TiebaActionError("unknown acknowledgement", uncertain=True)
        await self.engine.tick()
        self.assertEqual(self.engine.auto_comment["state"], "uncertain")
        self.stats.next_comment_at = "2000-01-01T00:00:00+08:00"
        await self.engine.tick()
        self.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(self.stats.comments, 0)

    async def test_unknown_comment_continues_with_other_post_after_reload(self):
        self.browser.submit_tieba.side_effect = [
            {"ok": False, "uncertain": True, "message": "发送结果未知"},
            {"ok": True, "id": "201", "message": "已确认发送"},
        ]
        await self.engine.tick()
        old_key = next(iter(self.engine.tieba_actions.targets))
        other = {**self.thread, "tid": "124", "url": "https://tieba.baidu.com/p/124"}
        self.session.latest_threads.return_value.append({"tid": "124", "created_at": 90})
        self.session.thread.side_effect = lambda tid: other if tid == "124" else self.thread
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        restored.stats().next_comment_at = "2000-01-01T00:00:00+08:00"
        await restored.tick()
        self.assertEqual(self.browser.submit_tieba.await_count, 2)
        self.assertEqual(self.browser.submit_tieba.await_args.args[1], other["url"])
        self.assertIn(old_key, restored.tieba_actions.targets)
        self.assertFalse(restored.tieba_actions.verification)
        self.assertEqual(restored.stats().comments, 1)

    def test_old_automatic_takeover_notice_is_replaced_without_changing_schedule(self):
        self.engine.auto_reply = {"state": "uncertain", "reason": "uncertain",
                                  "message": "发送结果尚未确认，已保留浏览器供核对；不会自动重发"}
        self.engine.save()
        restored = self.make_engine()
        self.assertNotIn("已保留浏览器", restored.auto_reply["message"])
        self.assertIn("后续活动继续", restored.auto_reply["message"])
        self.assertEqual(restored.auto_reply["state"], "uncertain")
        self.assertEqual(restored.stats().next_comment_at, self.stats.next_comment_at)

    async def test_watch_does_not_start_second_comment_when_independent_mode_enabled(self):
        del self.engine._watch_round
        self.stats.next_watch_at = "2000-01-01T00:00:00+08:00"
        adapter = types.SimpleNamespace(feed=AsyncMock(return_value=[Post("tieba", "fixture", "测试", url=self.thread["url"])]))
        self.engine.analyzer = types.SimpleNamespace(analyze=AsyncMock(return_value=VideoInsight(ok=True, summary="读到内容")))
        self.engine._comment_on = AsyncMock()
        with patch("core.activity.make_platform", return_value=adapter):
            await self.engine._watch_round(self.stats, __import__("core.util", fromlist=["now_tz"]).now_tz("Asia/Shanghai"))
        self.engine._comment_on.assert_not_awaited()
        self.writer.draft.assert_not_awaited()

    def prepare_reply(self):
        self.stats.next_comment_at = "2099-01-01T00:00:00+08:00"
        self.stats.next_reply_at = "2000-01-01T00:00:00+08:00"
        self.thread["posts"] = [{"pid": "102", "floor": "2", "author_id": "42", "content": "还没忙完啊"}]
        self.incoming = {"tid": "123", "pid": "103", "forum": "大肥鱼", "created_at": now_tz().timestamp(),
                         "author_id": "99", "author": "群友", "portrait": "tb.fixture", "content": "是啊还有一堆",
                         "quote_pid": "102", "quote_user_id": "42", "quote_content": "还没忙完啊",
                         "thread_author_id": "99", "is_floor": False}
        self.session.inbox = AsyncMock(return_value=[self.incoming])

    async def test_tieba_only_inbox_without_tracked_bilibili_threads_is_checked_and_counted(self):
        self.prepare_reply()
        self.assertFalse(self.engine.threads)
        await self.engine.tick()
        self.session.inbox.assert_awaited_once()
        self.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(self.browser.submit_tieba.await_args.kwargs["target"]["parent_pid"], "103")
        self.assertEqual((self.stats.comments, self.stats.replies), (0, 1))
        self.assertEqual(self.writer.draft.await_args.kwargs["kind"], "reply")
        material = self.writer.draft.await_args.kwargs["material"]
        self.assertEqual(material["own_comment"], "还没忙完啊")
        self.assertEqual(material["content"], self.thread["body"])
        self.assertEqual(self.engine.tieba_actions.public()["manual_replies"], 0)
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        self.assertEqual(restored.stats().replies, 1)
        self.assertEqual(restored.stats().comments, 0)

    async def test_reply_empty_self_old_other_forum_and_already_replied_are_skipped_without_writer(self):
        self.prepare_reply()
        self.session.inbox.return_value = []
        await self.engine.tick()
        self.assertEqual(self.engine.auto_reply["state"], "skipped")
        self.assertNotEqual(self.stats.next_reply_at, "2000-01-01T00:00:00+08:00")
        self.stats.next_reply_at = "2000-01-01T00:00:00+08:00"
        self.session.inbox.return_value = [{**self.incoming, "author_id": "42"},
                                          {**self.incoming, "forum": "其他吧"},
                                          {**self.incoming, "content": "  "},
                                          {**self.incoming, "created_at": 1}]
        await self.engine.tick()
        self.writer.draft.assert_not_awaited()
        self.browser.submit_tieba.assert_not_awaited()

    async def test_reply_writes_once_then_skips_same_message_at_next_interval(self):
        self.prepare_reply()
        await self.engine.tick()
        self.stats.next_reply_at = "2000-01-01T00:00:00+08:00"
        await self.engine.tick()
        self.browser.submit_tieba.assert_awaited_once()
        self.writer.draft.assert_awaited_once()
        self.assertEqual(self.stats.replies, 1)

    async def test_reply_newest_message_is_selected_before_older_notification(self):
        self.prepare_reply()
        newest = {**self.incoming, "pid": "104", "content": "刚发的新消息"}
        self.session.inbox.return_value = [{**self.incoming, "created_at": self.incoming["created_at"] - 60}, newest]
        await self.engine.tick()
        self.assertEqual(self.browser.submit_tieba.await_args.kwargs["target"]["parent_pid"], "104")
        self.assertEqual(self.writer.draft.await_args.kwargs["material"]["incoming"], "刚发的新消息")

    async def test_reply_reconcile_success_after_activity_save_gap_counts_reply_once(self):
        self.prepare_reply()
        await self.engine.tick()
        self.stats.replies = 0
        self.stats.auto_reply_jobs = []
        self.engine.save()
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        self.assertEqual((restored.stats().comments, restored.stats().replies), (0, 1))
        job = next(iter(restored.tieba_actions.jobs.values()))
        restored.account_auto_action(job)
        self.assertEqual(restored.stats().replies, 1)

    async def test_reply_ignores_comment_probability_but_obeys_reply_switch_and_limit(self):
        self.prepare_reply()
        self.settings.update({"comment_probability": 0, "reply_enabled": False})
        await self.engine.tick()
        self.session.inbox.assert_not_awaited()
        self.settings.update({"reply_enabled": True, "max_replies_per_day": 0})
        await self.engine.tick()
        self.session.inbox.assert_not_awaited()
        self.settings.update({"max_replies_per_day": 1})
        await self.engine.tick()
        self.browser.submit_tieba.assert_awaited_once()

    async def test_reply_uncertain_write_is_not_repeated_after_reload(self):
        self.prepare_reply()
        self.browser.submit_tieba.side_effect = TiebaActionError("unknown", uncertain=True)
        await self.engine.tick()
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        restored.stats().next_reply_at = "2000-01-01T00:00:00+08:00"
        await restored.tick()
        self.browser.submit_tieba.assert_awaited_once()
        self.assertEqual(restored.stats().replies, 0)

    async def test_unknown_reply_continues_with_other_notification_after_reload(self):
        self.prepare_reply()
        self.browser.submit_tieba.side_effect = [
            {"ok": False, "uncertain": True, "verification_required": True, "message": "结果未知"},
            {"ok": True, "id": "201", "message": "已确认发送"},
        ]
        await self.engine.tick()
        old_key = next(iter(self.engine.tieba_actions.targets))
        self.session.inbox.return_value.append({**self.incoming, "pid": "104", "content": "另一条消息"})
        restored = self.make_engine()
        restored.tieba_actions = ManualActions(restored, self.root)
        restored.stats().next_reply_at = "2000-01-01T00:00:00+08:00"
        await restored.tick()
        self.assertEqual(self.browser.submit_tieba.await_count, 2)
        self.assertEqual(self.browser.submit_tieba.await_args.kwargs["target"]["parent_pid"], "104")
        self.assertIn(old_key, restored.tieba_actions.targets)
        self.assertFalse(restored.tieba_actions.verification)
        self.assertEqual(restored.stats().replies, 1)

    async def test_reply_writer_failure_and_switch_off_do_not_count_or_send(self):
        self.prepare_reply()
        self.writer.draft.return_value = DraftResult(error="TimeoutError")
        await self.engine.tick()
        self.browser.submit_tieba.assert_not_awaited()
        self.assertIn("TimeoutError", self.engine.auto_reply["message"])
        self.stats.next_reply_at = "2000-01-01T00:00:00+08:00"
        async def disabled(**kwargs):
            self.settings.update({"reply_enabled": False})
            return DraftResult(ok=True, body="收到", provider_id="gemini")
        self.writer.draft.side_effect = disabled
        await self.engine.tick()
        self.assertEqual(self.stats.replies, 0)
        self.browser.submit_tieba.assert_not_awaited()
        self.assertFalse(self.engine.tieba_actions.targets)

    async def test_mixed_platforms_alternate_reply_checks(self):
        self.prepare_reply()
        self.settings.update({"platforms": ["tieba", "bilibili"]})
        self.accounts.active_account = lambda platform: self.account
        self.engine.threads = [{"platform": "bilibili", "root_id": "333", "post": {"platform": "bilibili", "id": "fixture"}}]
        self.session.inbox.return_value = []
        adapter = types.SimpleNamespace(replies=AsyncMock(return_value=[]))
        with patch("core.activity.make_platform", return_value=adapter):
            await self.engine.tick()
            self.stats.next_reply_at = "2000-01-01T00:00:00+08:00"
            await self.engine.tick()
        self.session.inbox.assert_awaited_once()
        adapter.replies.assert_awaited_once()

