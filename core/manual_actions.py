"""Single manual Tieba comment/reply jobs, separate from automatic quotas."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path

from astrbot.api import logger

from .activity import UNSAFE_COMMENT_WORDS
from .tieba_actions import TiebaActionError, TiebaSession, positive
from .tieba_scope import forum_allowed, normalize_forums
from .util import atomic_write_json, now_tz, read_json, shorten


class ManualActionBusy(RuntimeError):
    pass


class ManualActions:
    def __init__(self, engine, data_dir: Path):
        self.engine = engine
        self.path = data_dir / "manual-actions.json"
        saved = read_json(self.path, {})
        self.jobs = saved.get("jobs", {}) if isinstance(saved, dict) else {}
        self.targets = saved.get("targets", {}) if isinstance(saved, dict) else {}
        old_waits = saved.get("verification", {}) if isinstance(saved, dict) else {}
        self.verification = {}
        # Old automatic waits must not block activity after upgrading. Preserve
        # reservations for anything that may already have been submitted.
        for waiting in (old_waits.values() if isinstance(old_waits, dict) else []):
            job_id = waiting.get("job_id")
            job = self.jobs.get(job_id, {})
            if not job or job.get("ok"):
                continue
            uncertain = bool(waiting.get("uncertain") or job.get("state") == "uncertain")
            target = job.get("target_key")
            if target and uncertain:
                self.targets.setdefault(target, job_id)
            elif target and self.targets.get(target) == job_id:
                self.targets.pop(target)
            job.update(state="uncertain" if uncertain else "error", ok=False, stage="结束",
                       message="旧等待已解除，后续活动继续" + ("；该目标不会自动重发" if uncertain else ""))
        if old_waits:
            self.engine.last_error = "旧等待已解除，后续活动继续；结果未知的目标不自动重发"
        self.current_id = ""
        for job in self.jobs.values():
            if job.get("state") in {"running", "submitting"}:
                job.update(state="uncertain" if job.get("target_key") else "error", ok=False,
                           message="上次任务被中断；如曾提交，请核对记录，不会自动重发")
            if job.get("source") == "automatic" and job.get("ok"):
                self.engine.account_auto_action(job)
        self.save()

    def save(self):
        atomic_write_json(self.path, {"jobs": self.jobs, "targets": self.targets, "verification": self.verification})

    def public(self):
        today = now_tz(self.engine.settings.str("timezone")).strftime("%Y-%m-%d")
        records = [job for job in self.jobs.values() if job.get("source") != "automatic"]
        success = [job for job in records if job.get("ok") and job.get("at", "").startswith(today)]
        current = self.jobs.get(self.current_id, {})
        if current.get("source") == "automatic":
            current = {}
        return {"current": dict(current or (records[-1] if records else {})),
                "manual_comments": sum(job["kind"] == "comment" for job in success),
                "manual_replies": sum(job["kind"] == "reply" for job in success),
                "verification": [{**{key: value.get(key, "") for key in ("job_id", "message", "url", "type", "at", "uncertain")},
                                  "platform": "tieba", "account_name": name} for name, value in self.verification.items()]}

    def start(self, kind: str, request_id: str, *, automatic: bool = False):
        if kind not in {"comment", "reply"} or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise ValueError("操作类型或请求编号无效")
        if request_id in self.jobs:
            if self.jobs[request_id]["kind"] != kind or (self.jobs[request_id].get("source") == "automatic") != automatic:
                raise ValueError("同一个请求编号不能用于不同操作")
            return self.jobs[request_id], False
        if (self.engine._busy and not automatic) or any(job.get("state") in {"running", "submitting"} for job in self.jobs.values()):
            raise ManualActionBusy("已有活动正在执行，请结束后再试")
        account = self.engine.accounts.active_account("tieba")
        if not account or account.status != "logged_in":
            raise ValueError("请先在账号页登录贴吧")
        if self.is_waiting(account.name) or f"tieba:{account.name}" in getattr(self.engine, "browser_verification", {}):
            raise ValueError("该账号正在等待验证处理，请先在概览恢复")
        if self.engine.browser.takeovers.has_account("tieba", account.name):
            raise ValueError("该账号的浏览器等待人工接管，请先结束接管")
        if "tieba" not in self.engine.settings.list("platforms"):
            raise ValueError("请先在设置中启用贴吧平台")
        if not self.engine.settings.bool("enabled"):
            raise ValueError("插件总开关已关闭")
        if not self.engine.settings.bool("writer_enabled"):
            raise ValueError("请先开启独立写稿；手动发送不会使用固定模板代替")
        if automatic:
            now = now_tz(self.engine.settings.str("timezone"))
            stats = self.engine.stats(now)
            due = self.engine._comment_due(stats, now) if kind == "comment" else (
                self.engine._reply_due(stats, now) and self.engine._reply_quota(stats))
            if not self.engine.running or not due:
                raise ValueError("自动操作当前未到时间、已关闭或额度已用完")
        self.engine._busy = True
        job = {"id": request_id, "kind": kind, "state": "running", "stage": "读取内容",
               "at": now_tz(self.engine.settings.str("timezone")).isoformat(timespec="seconds"),
               "ok": False, "message": "正在读取实际内容", "account_name": account.name,
               "source": "automatic" if automatic else "manual"}
        self.jobs[request_id] = job
        self.current_id = request_id
        try:
            self.save()
        except BaseException:
            self.engine._busy = False
            self.jobs.pop(request_id)
            raise
        return job, True

    def is_waiting(self, account_name):
        return account_name in self.verification

    async def recheck(self, request_id: str, pid: str) -> dict:
        """Confirm an existing reply, then repair only its matching wait record."""
        job = self.jobs.get(request_id)
        if not job or job.get("state") not in {"uncertain", "waiting_verification"} or not positive(pid):
            raise ValueError("请提供待核对任务及有效回复编号")
        waiting = self.verification.get(job["account_name"], {})
        if (waiting and waiting.get("job_id") != request_id) or self.engine._busy:
            raise ManualActionBusy("等待记录已变化或已有任务执行，请刷新后重试")
        account = self.engine.accounts.active_account("tieba")
        if not account or account.name != job["account_name"] or account.status != "logged_in":
            raise ValueError("请保持原账号登录后核对")
        self.engine._busy = True
        try:
            confirmed = await self.engine.browser.confirm_tieba_sent(
                account.name, job["thread"]["url"], job["body"], pid, job["uid"], job.get("target"))
            if not confirmed:
                return {"confirmed": False, "message": "未找到账号、正文和编号匹配的回复，保留原记录"}
            job.update(state="done", ok=True, verified=True, sent_pid=pid, stage="结束",
                       sent_at=job.get("sent_at") or job["at"],
                       message="已回查确认发送成功，没有再次发送")
            self.verification.pop(account.name, None)
            self.save()
            if job.get("source") == "automatic":
                self.engine.account_auto_action(job)
                setattr(self.engine, "auto_" + job["kind"], {
                    "at": job["at"], "platform": "tieba", "state": "done", "reason": "rechecked",
                    "message": job["message"], "pid": pid})
            if not any(row.get("rpid") == pid for row in self.engine.history):
                self.engine.history.append({"at": job["at"], "kind": job["kind"], "platform": "tieba",
                    "manual": job.get("source") != "automatic", "title": job["thread"]["title"],
                    "text": job["body"], "url": job["thread"]["url"], "rpid": pid,
                    "incoming": job.get("incoming", {}).get("content", "")})
            self.engine.last_error = ""
            self.engine.save()
            retained = self.engine.browser.takeovers.for_account("tieba", account.name)
            if retained and retained.job_id == request_id:
                await self.engine.browser.takeovers.close(retained.id)
            logger.info("[cyber-life] 已回查确认发送 job_id=%s pid=%s，没有再次发送", request_id, pid)
            return {"confirmed": True, "pid": pid, "message": job["message"]}
        finally:
            self.engine._busy = False

    def clear_verification(self, account_name, job_id):
        waiting = self.verification.get(account_name)
        if not waiting or waiting.get("job_id") != job_id:
            raise ValueError("验证记录已变化，请刷新后重试")
        if self.engine._busy or any(job.get("state") in {"running", "submitting"} for job in self.jobs.values()):
            raise ManualActionBusy("已有任务正在执行，请稍后恢复")
        account = self.engine.accounts.active_account("tieba")
        if not account or account.name != account_name or account.status != "logged_in":
            raise ValueError("请先登录并选中该贴吧账号")
        job = self.jobs.get(job_id, {})
        if not waiting.get("uncertain") and self.targets.get(job.get("target_key")) == job_id:
            self.targets.pop(job["target_key"])
        job.update(state="verification_released", stage="结束", message="已手动解除验证暂停；按原计划重新检查，未立即发送")
        self.verification.pop(account_name)
        self.save()
        self.engine.last_error = ""
        self.engine.save()

    def scope(self):
        allowed = normalize_forums(self.engine.settings.list("tieba_forums"))
        if not allowed:
            raise TiebaActionError("请先填写允许的贴吧，再执行手动评论或回复")
        return allowed

    def key(self, uid, kind, tid, pid=""):
        return f"tieba:{uid}:{kind}:{tid}:{pid}"

    def own_records(self, uid, tid):
        return [job for job in self.jobs.values() if job.get("ok") and job.get("uid") == uid
                and job.get("thread", {}).get("tid") == tid]

    async def prepare(self, session, kind, uid):
        allowed = self.scope()
        skipped = []
        if kind == "comment":
            posts = await session.latest_threads(allowed)
            for post in posts:
                tid = post["tid"]
                key = self.key(uid, kind, tid)
                already_commented = any(event.get("kind") == "comment" and event.get("platform") == "tieba"
                                        and event.get("url", "").partition("/p/")[2].split("?")[0].rstrip("/") == tid
                                        for event in self.engine.history)
                if not positive(tid) or key in self.targets or already_commented:
                    continue
                thread = await session.thread(tid)
                if not forum_allowed(thread["forum"], allowed) or thread["author_id"] == uid:
                    continue
                content = thread["title"] + thread["body"]
                if not thread["title"] or any(word in content for word in UNSAFE_COMMENT_WORDS):
                    continue
                image_summary = ""
                analyzer = getattr(self.engine, "analyzer", None)
                if thread.get("images") and analyzer and hasattr(analyzer, "analyze_images"):
                    try:
                        insight = await analyzer.analyze_images(
                            title=thread["title"], body=thread["body"], images=thread["images"])
                        image_summary = insight.summary
                    except Exception as exc:  # noqa: BLE001
                        logger.info("[cyber-life] 贴吧图片识别跳过 error_type=%s", type(exc).__name__)
                seen_key = "tieba:" + hashlib.sha1(thread["url"].encode("utf-8")).hexdigest()[:16]
                return {"key": key, "thread": thread, "target": None, "seen_key": seen_key,
                        "selection_order": "created_at_desc", "created_at": post["created_at"],
                        "material": {"platform": "tieba", "title": thread["title"], "content": thread["body"],
                                     "image_summary": image_summary,
                                     "comments": thread["posts"][1:6]}}
            return {"skip": "没有找到允许范围内尚未评论的合适帖子"}
        inbox = await session.inbox()
        for item in sorted(inbox, key=lambda row: row["created_at"], reverse=True):
            tid, pid = item["tid"], item["pid"]
            key = self.key(uid, kind, tid, pid)
            if key in self.targets or item["author_id"] == uid or not forum_allowed(item["forum"], allowed):
                continue
            if not str(item.get("content") or "").strip():
                continue
            if not 0 <= time.time() - item["created_at"] <= 7 * 86400:
                continue
            thread = await session.thread(tid, anchor=item["quote_pid"] if positive(item["quote_pid"]) else "")
            if not forum_allowed(thread["forum"], allowed) or not thread["title"]:
                continue
            records = self.own_records(uid, tid)
            own = next((row for row in thread["posts"] if row["pid"] == item["quote_pid"] and row["author_id"] == uid), {})
            known = next((job for job in records if job.get("sent_pid") == item["quote_pid"]), {})
            if item["quote_user_id"] != uid and item["thread_author_id"] != uid and not own and not known:
                skipped.append("无法核对消息是否回复本账号")
                continue
            parent = pid
            if item["is_floor"]:
                # A reply notification carries the quoted post's pid.  When
                # that post was written by this account (quote_user_id), the
                # pid is already the parent floor even if the read API did
                # not return floor posts for this page.
                parent = ((known.get("target") or {}).get("parent_pid")
                          or (item["quote_pid"]
                              if (own or known or item["quote_user_id"] == uid) else ""))
                if not positive(parent):
                    skipped.append("楼中楼的父楼无法定位")
                    continue
            target = {"parent_pid": parent, "sub_pid": pid if item["is_floor"] else "",
                      "author_id": item["author_id"], "author": item["author"], "portrait": item["portrait"],
                      "content": item["content"], "quote_content": item.get("quote_content") or ""}
            if target["sub_pid"] and (not target["author"] or not target["portrait"]):
                skipped.append("楼中楼缺少作者信息")
                continue
            actual_incoming = next((row for row in thread["posts"] if row["pid"] == pid and row["author_id"] == item["author_id"]), {})
            return {"key": key, "thread": thread, "target": target, "incoming": item,
                    "material": {"platform": "tieba", "title": thread["title"], "content": thread["body"],
                                 "incoming": actual_incoming.get("content") or item["content"], "author": item["author"],
                                 "incoming_is_notification_preview": not bool(actual_incoming.get("content")),
                                 "own_comment": own.get("content") or known.get("body", ""),
                                 "notification_quote_unverified": item["quote_content"],
                                 "previous_exchanges": [{"incoming": job.get("incoming", {}).get("content", ""),
                                                         "body": job.get("body", "")} for job in records[-5:]]}}
        return {"skip": f"没有需要回复的新消息（读取{len(inbox)}条）", "skipped_reasons": sorted(set(skipped))}

    async def preview(self, kind):
        account = self.engine.accounts.active_account("tieba")
        if not account or account.status != "logged_in":
            raise TiebaActionError("请先登录贴吧")
        async with TiebaSession(self.engine.browser, account.name) as session:
            uid = await session.identity()
            result = await self.prepare(session, kind, uid)
        return {**result, "uid": uid, "preview_only": True}

    async def run(self, request_id):
        job = self.jobs[request_id]
        automatic = job.get("source") == "automatic"
        counter = "comments" if job["kind"] == "comment" else "replies"
        def enabled():
            if not self.engine.settings.bool("enabled") or "tieba" not in self.engine.settings.list("platforms"):
                raise TiebaActionError("插件或贴吧已停用，本次未发送")
            if automatic and (not self.engine.running or not self.engine.settings.bool(f"{job['kind']}_enabled")
                              or not self.engine.settings.bool("writer_enabled")
                              or not self.engine.settings.can_call(now_tz(self.engine.settings.str("timezone")))
                              or getattr(self.engine.stats(), counter) >= self.engine.settings.int(f"max_{counter}_per_day")):
                raise TiebaActionError("自动操作已停用或额度用完，本次未发送")
        submitted = False
        try:
            enabled()
            account = self.engine.accounts.active_account("tieba")
            path = self.engine.browser.storage_path("tieba", account.name)
            fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            async with TiebaSession(self.engine.browser, account.name) as session:
                uid = await session.identity()
                job["uid"] = uid
                selection = await self.prepare(session, job["kind"], uid)
                if selection.get("skip"):
                    job.update(state="skipped", message=selection["skip"], skipped_reasons=selection.get("skipped_reasons", []))
                    return
                thread_info = {key: selection["thread"][key] for key in ("tid", "fid", "forum", "title", "body", "url")}
                job.update(thread=thread_info, target=selection.get("target"), incoming=selection.get("incoming", {}), seen_key=selection.get("seen_key", ""),
                           stage="Gemini 写稿", message="已选定一条，正在写稿")
                self.save()
                limit = min(400, max(8, self.engine.settings.int("comment_max_chars" if job["kind"] == "comment" else "reply_max_chars")))
                result = await self.engine.writer.draft(kind=job["kind"], material=selection["material"], max_chars=limit)
                if not result.ok:
                    job.update(state="error", message=f"写稿失败（{result.error}），本次未发送")
                    return
                current = self.engine.accounts.active_account("tieba")
                if (not current or current.status != "logged_in" or current.name != account.name
                        or hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint):
                    raise TiebaActionError("登录账号或登录状态已变化，本次未发送")
                if not forum_allowed(selection["thread"]["forum"], self.scope()):
                    raise TiebaActionError("允许的贴吧已变化，本次未发送")
                job.update(body=result.body, writer_provider=result.provider_id, target_key=selection["key"],
                           state="submitting", stage="提交一次", message="正在提交一次并核对回复编号")
                self.targets[selection["key"]] = request_id
                self.save()
                submitted = True
                def pre_send():
                    enabled()
                    current = self.engine.accounts.active_account("tieba")
                    if (not current or current.status != "logged_in" or current.name != account.name
                            or hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint
                            or not forum_allowed(selection["thread"]["forum"], self.scope())):
                        raise TiebaActionError("账号或允许的贴吧已变化，本次未发送")
                pre_send()
                # Write through the real Tieba page.  The mobile protobuf write
                # path returns a type=6 challenge that must be replayed as a
                # token; this browser path solves the same captcha in place and
                # verifies the new reply on the page instead.
                outcome = await self.engine.browser.submit_tieba(
                    account.name,
                    selection["thread"]["url"],
                    result.body,
                    target=selection.get("target"),
                    allowed_forums=self.scope(),
                    author_id=uid,
                    job_id=request_id,
                )
                if not outcome.get("ok"):
                    if outcome.get("takeover_id"):
                        job["takeover_id"] = outcome["takeover_id"]
                    if outcome.get("network"):
                        job["network"] = outcome["network"]
                        self.save()
                    message = str(outcome.get("message") or "浏览器发送未完成")
                    if outcome.get("verification_required"):
                        raise TiebaActionError(
                            message,
                            uncertain=bool(outcome.get("uncertain")),
                            verification_required=True,
                            verification={"url": selection["thread"]["url"], "type": "browser",
                                          "message": message},
                        )
                    raise TiebaActionError(message, uncertain=bool(outcome.get("uncertain")))
                pid = str(outcome.get("id") or "")
                job.update(state="done", ok=True, sent_pid=pid, message="评论成功" if job["kind"] == "comment" else "回复成功",
                           verified=bool(outcome.get("ok")), submission_ack=outcome.get("ack", {}),
                           sent_at=now_tz(self.engine.settings.str("timezone")).isoformat(timespec="seconds"))
                self.save()
                if automatic:
                    self.engine.account_auto_action(job)
                self.engine.last_action = f"{'自动' if automatic else '手动'}{'评论' if job['kind'] == 'comment' else '回复'}了《{shorten(job['thread']['title'],36)}》"
                self.engine.last_action_at = job["at"]
                self.engine.last_error = ""
                self.engine.history.append({"at": job["at"], "kind": job["kind"], "platform": "tieba", "manual": not automatic,
                                            "title": job["thread"]["title"], "text": result.body, "url": job["thread"]["url"],
                                            "rpid": pid, "to": job["incoming"].get("author", ""),
                                            "incoming": job["incoming"].get("content", "")})
                stats = self.engine.stats()
                post_id = hashlib.sha1(job["thread"]["url"].encode()).hexdigest()[:16]
                if "tieba:" + post_id not in stats.seen_posts:
                    stats.seen_posts.append("tieba:" + post_id)
                if job.get("seen_key") and job["seen_key"] not in stats.seen_posts:
                    stats.seen_posts.append(job["seen_key"])
        except asyncio.CancelledError:
            job.update(state="uncertain" if submitted else "error", message="任务已中断；曾提交的目标不会自动重发")
            raise
        except Exception as exc:
            if job.get("ok") and positive(job.get("sent_pid")):
                job.update(state="done", message="已确认发送，但活动记录更新失败，请保留回复编号")
                return
            uncertain = submitted and (not isinstance(exc, TiebaActionError) or exc.uncertain)
            message = str(exc) if isinstance(exc, TiebaActionError) else f"执行失败（{type(exc).__name__}），请查看日志"
            job.update(state="uncertain" if uncertain else "error",
                       message=message + "；本轮结束，后续活动继续" + ("，该目标不自动重发" if uncertain else ""))
            self.engine.last_error = job["message"]
            if submitted and not uncertain:
                self.targets.pop(job.get("target_key", ""), None)
        finally:
            if job["state"] not in {"running", "submitting"}:
                job["stage"] = "结束"
            if not automatic:
                self.engine._busy = False
            self.save()
            self.engine.save()
            log = logger.info if job.get("ok") or job["state"] == "skipped" else logger.warning
            log("[cyber-life] action source=%s kind=%s state=%s request_id=%s tid=%s pid=%s message=%s",
                "automatic" if automatic else "manual",
                job["kind"], job["state"], job["id"], job.get("thread", {}).get("tid", ""),
                job.get("sent_pid", ""), job.get("message", ""))
