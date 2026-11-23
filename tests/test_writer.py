from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from core.activity import ActivityEngine, DayStats
from core.llm import LLMHelper
from core.platforms.base import CommentResult, Post, Reply
from core.settings import SettingsStore
from core.video_analysis import VideoInsight
from core.writer import DraftResult, DraftWriter
from core.writer_memory import MemoryEvent, recall_memory


class WriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.settings = SettingsStore({"writer_enabled": True, "writer_provider_id": "gemini",
                                       "writer_livingmemory_enabled": False}, self.folder)
        self.provider = SimpleNamespace(meta=lambda: SimpleNamespace(id="gemini"), text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text='{"body":"还没忙完啊。"}')))
        self.persona = {"name": "群友", "prompt": "你叫小鲸鱼。", "begin_dialogs": ["在？", "在。"]}
        self.context = SimpleNamespace(
            get_provider_by_id=Mock(return_value=self.provider),
            get_using_provider_async=AsyncMock(return_value=self.provider),
            get_using_provider=Mock(return_value=self.provider),
            get_config=Mock(return_value={}),
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
                                                get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="群友"))),
            persona_manager=SimpleNamespace(get_persona_v3_by_id=Mock(return_value=self.persona),
                resolve_selected_persona=AsyncMock(return_value=("群友", self.persona, None, False)),
                get_default_persona_v3=AsyncMock(return_value=self.persona)),
        )
        self.writer = DraftWriter(self.context, self.settings, lambda: "default:FriendMessage:123")
        self.material = {"title": "加班", "content": "今天又加班到这么晚"}

    async def draft(self, **kwargs):
        return await self.writer.draft(kind="comment", material=self.material, max_chars=25, **kwargs)

    async def test_selected_provider_persona_and_json(self):
        result = await self.draft()
        self.assertTrue(result.ok)
        self.context.get_provider_by_id.assert_called_once_with("gemini")
        self.context.get_using_provider_async.assert_not_awaited()
        args = self.provider.text_chat.call_args.kwargs
        self.assertIn("你叫小鲸鱼", args["system_prompt"])
        self.assertIn("今天又加班", args["prompt"])
        self.assertNotIn("contexts", args)
        self.assertNotIn("func_tool", args)
        self.assertEqual(result.persona_id, "群友")

    async def test_empty_provider_follows_real_session(self):
        self.settings.update({"writer_provider_id": ""})
        self.assertTrue((await self.draft()).ok)
        self.context.get_using_provider_async.assert_awaited_once_with("default:FriendMessage:123")

    async def test_missing_selected_provider_does_not_fall_back(self):
        self.context.get_provider_by_id.return_value = None
        result = await self.draft()
        self.assertEqual(result.error, "writer_provider_unavailable")
        self.context.get_using_provider_async.assert_not_awaited()
        self.provider.text_chat.assert_not_awaited()

    async def test_provider_error_skips_without_fallback(self):
        self.provider.text_chat.side_effect = TimeoutError()
        result = await self.draft()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "TimeoutError")
        self.provider.text_chat.assert_awaited_once()

    async def test_refusal_or_tool_json_never_becomes_a_comment(self):
        for output in ("I cannot help with that.", '{"name":"click","arguments":{}}', '{"body":123}'):
            self.provider.text_chat.return_value = SimpleNamespace(completion_text=output)
            result = await self.draft()
            self.assertFalse(result.ok)
            self.assertFalse(result.body)

    async def test_overlong_text_is_not_truncated_and_sent(self):
        self.provider.text_chat.return_value = SimpleNamespace(completion_text=json.dumps({"body": "字" * 26}))
        result = await self.draft()
        self.assertEqual(result.error, "invalid_writer_length")
        self.assertEqual(result.body, "")

    async def test_cancel_propagates(self):
        self.provider.text_chat.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.draft()

    async def test_explicit_persona_and_none(self):
        self.settings.update({"writer_persona_id": "群友"})
        self.assertTrue((await self.draft()).ok)
        self.context.persona_manager.get_persona_v3_by_id.assert_called_with("群友")
        self.settings.update({"writer_persona_id": "[%None]"})
        self.assertEqual((await self.draft()).persona_id, "")

    async def test_real_memory_in_input_but_not_returned(self):
        memory = {"status": "ready", "text": "真实召回材料", "count": 3}
        with patch("core.writer.recall_memory", AsyncMock(return_value=memory)) as recall:
            result = await self.draft()
        recall.assert_awaited_once()
        self.assertIn("真实召回材料", self.provider.text_chat.call_args.kwargs["prompt"])
        self.assertNotIn("text", result.memory)

    async def test_memory_without_source_does_not_guess_identity(self):
        self.settings.update({"writer_livingmemory_enabled": True})
        result = await recall_memory(self.context, self.settings, "", "查询")
        self.assertEqual(result["status"], "no_source_session")

    async def test_memory_excluded_by_source_profile(self):
        self.settings.update({"writer_livingmemory_enabled": True})
        self.context.get_config.return_value = {"plugin_set": ["other"]}
        result = await recall_memory(self.context, self.settings, "default:FriendMessage:123", "查询")
        self.assertEqual(result["status"], "excluded")

    async def test_analysis_and_vision_use_separate_providers(self):
        self.settings.update({"analysis_provider_id": "deepseek", "vision_provider_id": "hy3"})
        helper = LLMHelper(self.context, self.settings)
        await helper.complete("分析")
        self.context.get_provider_by_id.assert_called_with("deepseek")
        await helper.complete("识图", images=["image.png"])
        self.context.get_provider_by_id.assert_called_with("hy3")

    def test_source_identity_does_not_invent_group_sender(self):
        event = MemoryEvent("default:GroupMessage:999")
        self.assertEqual(event.get_sender_id(), "")
        self.assertEqual(event.get_group_id(), "999")
        self.assertEqual(MemoryEvent("webchat:FriendMessage:webchat!admin!cid").get_sender_id(), "admin")

    def test_native_configuration_beats_stale_model_override(self):
        self.settings.update({"writer_provider_id": "old"})
        reloaded = SettingsStore({"writer_provider_id": "new"}, self.folder)
        self.assertEqual(reloaded.str("writer_provider_id"), "new")
        self.assertEqual(SettingsStore({}, self.folder).str("writer_provider_id"), "old")

    def engine(self):
        return ActivityEngine(settings=self.settings, accounts=Mock(), mood=SimpleNamespace(style_hint=lambda: "平静"),
                              analyzer=Mock(), comfy=Mock(), llm=Mock(), browser=Mock(), data_dir=self.folder,
                              notify=AsyncMock(), writer=self.writer)

    async def test_comment_failure_never_sends_or_increments(self):
        engine = self.engine()
        self.provider.text_chat.side_effect = TimeoutError()
        adapter = SimpleNamespace(comments=AsyncMock(return_value=[]), comment=AsyncMock())
        stats = DayStats()
        await engine._comment_on(Post("tieba", "1", "测试"), adapter, VideoInsight(), stats,
                                 datetime.now(ZoneInfo("Asia/Shanghai")))
        adapter.comment.assert_not_awaited()
        self.assertEqual(stats.comments, 0)
        self.assertIn("本轮不发送", engine.last_error)

    async def test_comment_reads_context_first_and_records_original(self):
        engine = self.engine()
        adapter = SimpleNamespace(comments=AsyncMock(return_value=[Reply("2", "用户", "我也是")]),
                                  comment=AsyncMock(return_value=CommentResult(True, "root")))
        stats = DayStats()
        await engine._comment_on(Post("bilibili", "1", "测试", description="完整描述"), adapter,
                                 VideoInsight(), stats, datetime.now(ZoneInfo("Asia/Shanghai")))
        self.assertIn("我也是", self.provider.text_chat.call_args.kwargs["prompt"])
        self.assertEqual(engine.threads[0]["own_comment"], "还没忙完啊。")
        self.assertEqual(engine._post_from_thread(engine.threads[0]).description, "完整描述")
        self.assertEqual(stats.comments, 1)

    async def test_followup_includes_original_and_previous_exchange(self):
        engine = self.engine()
        thread = {"own_comment": "我还在加班", "exchanges": [{"incoming": "几点结束", "body": "还不知道"}]}
        text = await engine._generate_reply("群友", "忙完了吗", post=Post("bilibili", "1", "加班"), thread=thread)
        self.assertTrue(text)
        prompt = self.provider.text_chat.call_args.kwargs["prompt"]
        for content in ("我还在加班", "几点结束", "还不知道", "忙完了吗"):
            self.assertIn(content, prompt)

    async def test_followup_failure_does_not_send(self):
        engine = self.engine()
        self.provider.text_chat.side_effect = TimeoutError()
        engine.threads = [{"platform": "bilibili", "post": {"id": "1", "title": "测试"}, "root_id": "root"}]
        adapter = SimpleNamespace(replies=AsyncMock(return_value=[Reply("2", "用户", "你好")]), reply=AsyncMock())
        with patch("core.activity.make_platform", return_value=adapter):
            stats = DayStats()
            await engine._reply_round(stats, datetime.now(ZoneInfo("Asia/Shanghai")))
        adapter.reply.assert_not_awaited()
        self.assertEqual(stats.replies, 0)


if __name__ == "__main__":
    unittest.main()
