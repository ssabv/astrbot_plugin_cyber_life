"""Read-only recall through LivingMemory's existing access rules and formatters."""
from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

from astrbot.api import logger


class MemoryEvent:
    """Identity of an existing source session; never grants tool/admin permissions."""

    def __init__(self, umo: str) -> None:
        self.unified_msg_origin = umo
        self.adapter, self.kind, self.session = umo.split(":", 2)

    def get_platform_id(self):
        return self.adapter

    def get_platform_name(self):
        return self.adapter

    def get_sender_id(self):
        if self.kind == "GroupMessage":
            return ""  # A group origin does not identify a particular sender.
        if self.adapter == "webchat" and self.session.startswith("webchat!"):
            return self.session.split("!", 2)[1]
        return self.session

    def get_sender_name(self):
        return self.get_sender_id()

    def get_group_id(self):
        return self.session if self.kind == "GroupMessage" else ""

    def is_private_chat(self):
        return self.kind == "FriendMessage"


async def recall_memory(context: Any, settings: Any, umo: str, query: str) -> dict:
    result = {"status": "disabled", "count": 0, "profile_injected": False, "chars": 0, "text": ""}
    if not settings.bool("writer_livingmemory_enabled"):
        return result
    start = time.monotonic()
    try:
        if not umo:
            result["status"] = "no_source_session"
            return result
        event = MemoryEvent(umo)
        selected = context.get_config(umo=umo).get("plugin_set", ["*"])
        if "*" not in selected and "astrbot_plugin_livingmemory" not in selected:
            result["status"] = "excluded"
            return result
        meta = context.get_registered_star("astrbot_plugin_livingmemory")
        lm = getattr(meta, "star_cls", None)
        handler = getattr(lm, "event_handler", None)
        if not getattr(meta, "activated", False) or handler is None:
            result["status"] = "unavailable"
            return result
        package = type(lm).__module__.rpartition(".")[0]
        scope_module = importlib.import_module(package + ".core.memory_scope")
        utils = importlib.import_module(package + ".core.utils")
        config, engine = lm.config_manager, handler.memory_engine

        async def recall():
            with scope_module.adapter_access(config, event):
                access = await scope_module.resolve_event_memory_access(config, context, event)
                if not access.allowed:
                    result["status"] = "excluded"
                    return
                k = int(config.get("recall_engine.top_k", 5))
                if k <= 0:
                    return
                memories = await engine.search_memories(
                    query=query, k=k, session_id=access.scope, persona_id=access.persona_id,
                    extra_scopes=[access.channel_scope] if access.channel_scope else None,
                )
                rows = [{"id": getattr(m, "doc_id", None), "content": m.content,
                         "score": m.final_score, "metadata": m.metadata,
                         "timestamp": m.metadata.get("create_time")} for m in memories or []]
                text = utils.format_memories_for_injection(rows) if rows else ""
                profile = await handler._memory_recall._load_profile_injection_text(access.scope, access.persona_id)
                if text and profile:
                    text = utils.attach_profile_to_memory_injection(text, profile)
                elif profile:
                    text = utils.format_profile_block_for_injection(profile)
                result.update(status="ready" if text else "empty", count=len(rows),
                              profile_injected=bool(profile), chars=len(text), text=text)

        await asyncio.wait_for(recall(), timeout=settings.int("writer_memory_timeout"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # A recall failure must not invent memories.
        result.update(status="unavailable", error_type=type(exc).__name__)
    finally:
        result["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        logger.info("[cyber-life] writer_memory %s", " ".join(
            f"{key}={result.get(key)}" for key in
            ("status", "count", "profile_injected", "chars", "elapsed_ms", "error_type")))
    return result
