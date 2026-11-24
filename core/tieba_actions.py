"""Bounded Tieba reads and one acknowledged submission using the browser login.

Field numbers follow the verified Tieba app protocol. A rejected submission may
continue once with success data from its original challenge. Unknown writes
are never repeated. Authentication and verification tokens are not logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astrbot.api import logger

from .tieba_scope import forum_allowed, normalize_forums


class TiebaActionError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, verification_required: bool = False, verification: dict | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.verification_required = bool(verification_required or verification)
        self.verification = verification or {}


def positive(value: Any) -> bool:
    return str(value).isdigit() and int(value) > 0


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def pb_int(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def pb_bytes(field: int, value: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(value)) + value


def pb_str(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode("utf-8"))


def fields(payload: bytes) -> list:
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("oversized protobuf")
    offset = 0
    result = []
    def read_int():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(payload):
                raise ValueError("truncated protobuf")
            byte = payload[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError("invalid protobuf varint")
    while offset < len(payload):
        tag = read_int()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid field number")
        if wire == 0:
            value = read_int()
        elif wire in (1, 2, 5):
            size = read_int() if wire == 2 else (8 if wire == 1 else 4)
            if offset + size > len(payload):
                raise ValueError("truncated protobuf field")
            value = payload[offset:offset + size]
            offset += size
        else:
            raise ValueError("unsupported protobuf wire")
        result.append((number, wire, value))
    return result


def integer(items, number):
    return next((value for n, wire, value in items if n == number and wire == 0), 0)


def string(items, number):
    return next((value.decode("utf-8") for n, wire, value in items if n == number and wire == 2), "")


def message(items, number):
    return next((fields(value) for n, wire, value in items if n == number and wire == 2), [])


def text_content(value):
    if isinstance(value, str):
        return value[:12000]
    if not isinstance(value, list):
        return ""
    return "".join(str(item.get("text") or "") for item in value
                   if isinstance(item, dict) and str(item.get("type", 0)) in {"0", "4"})[:12000]


def public_image_url(value: str) -> str:
    value = str(value or "")
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or not hostname
                or parsed.username or parsed.password or len(value) > 2048
                or hostname.endswith(".local") or hostname == "localhost"
                or any(c.isspace() or ord(c) < 32 for c in value)):
            return ""
        if parsed.scheme == "http":
            value = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
        return value
    except (ValueError, TypeError):
        return ""


def image_urls(value) -> list[str]:
    """Read bounded Tieba picture fragments without changing text handling."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:100]:
        has_image_field = any(key in item for key in (
            "origin_src", "big_src", "big_cdn_src", "cdn_src", "src", "c",
            "big_pic", "small_pic", "origin_pic", "pic_url",
        )) if isinstance(item, dict) else False
        if not isinstance(item, dict) or (str(item.get("type", "2")) not in {"2", "3"} and not has_image_field):
            continue
        raw = str(item.get("origin_src") or item.get("big_src") or item.get("big_cdn_src")
                  or item.get("cdn_src") or item.get("src") or item.get("c")
                  or item.get("big_pic") or item.get("small_pic") or item.get("origin_pic")
                  or item.get("pic_url") or "")
        url = public_image_url(raw)
        path = urlsplit(url).path.lower()
        if url and "/tb/editor/images/" in path and "image_emoticon" in path:
            continue
        if url and url not in result:
            result.append(url)
        if len(result) >= 6:
            break
    return result


def safe_verification_url(value: str) -> str:
    value = str(value or "")
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        allowed = {"tieba.baidu.com", "c.tieba.baidu.com", "wappass.baidu.com", "seccaptcha.baidu.com", "passport.baidu.com", "verify.baidu.com", "vcode.baidu.com"}
        if (parsed.scheme == "https" and parsed.hostname in allowed and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None and len(value) <= 8192
                and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in value)):
            return value
    except (ValueError, TypeError):
        pass
    return ""


def challenge_url(value: str) -> str:
    """Upgrade legacy Baidu challenge URLs locally; never make an HTTP request."""
    value = str(value or "")
    if len(value) > 8192 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "http" and parsed.port in {None, 80}
                and parsed.username is None and parsed.password is None):
            value = urlunsplit(("https", parsed.hostname or "", parsed.path, parsed.query, parsed.fragment))
        return safe_verification_url(value)
    except (ValueError, TypeError):
        return ""


def web_verification_data(url: str, status: int, body: bytes) -> str:
    """Accept only the official widget's bounded successful response."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "seccaptcha.baidu.com"
            or parsed.port not in {None, 443} or parsed.username is not None or parsed.password is not None
            or not parsed.path.startswith("/v1/webapi/verint/verify/") or status != 200
            or len(body) > 32768):
        return ""
    try:
        text = body.decode("utf-8").strip()
        match = re.fullmatch(r"[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?", text, re.S)
        value = json.loads(match.group(1) if match else text)
        if not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0:
            return ""
        data = value.get("data")
        if not isinstance(data, dict) or data.get("pass") not in (True, 1) or type(data.get("pass")) not in (bool, int):
            return ""
        nodes = [0]
        forbidden = {"bduss", "stoken", "ptoken", "cookie", "authorization", "password"}
        def check(item, depth=0):
            nodes[0] += 1
            if depth > 6 or nodes[0] > 256:
                raise ValueError("oversized success data")
            if isinstance(item, dict):
                for key, sub in item.items():
                    if not isinstance(key, str) or len(key) > 80 or key.lower() in forbidden:
                        raise ValueError("invalid success data")
                    check(sub, depth + 1)
            elif isinstance(item, list):
                for sub in item:
                    check(sub, depth + 1)
            elif isinstance(item, str):
                if len(item) > 8192 or any(ord(c) < 32 for c in item):
                    raise ValueError("invalid success string")
        check(data)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return encoded if len(encoded.encode("utf-8")) <= 16384 else ""
    except (ValueError, TypeError, UnicodeError):
        return ""


def verification_from_json(raw: dict) -> dict:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    source = {**raw, **data, **info}
    return {"url": challenge_url(source.get("vcode_pic_url") or source.get("vcode_url") or source.get("verify_url") or ""),
            "type": str(source.get("vcode_type") or "")[:32], "md5": str(source.get("vcode_md5") or "")[:128]}


def check_error(code, text="", *, verification: dict | None = None):
    if str(code or 0) == "0":
        return
    if str(code) in {"4", "40", "220034", "9012", "9014"} or re.search("验证|验证码|captcha", text, re.I):
        raise TiebaActionError("百度要求验证，账号已暂停，处理后请在概览恢复", verification_required=True, verification=verification)
    if str(code) in {"1", "2", "3"} or re.search("登录|login", text, re.I):
        raise TiebaActionError("贴吧登录状态无效，请重新扫码登录")
    raise TiebaActionError(f"贴吧接口拒绝请求（错误码 {code}）")


def parse_inbox(payload: bytes) -> list[dict]:
    outer = fields(payload)
    error = message(outer, 1)
    check_error(integer(error, 1), string(error, 2))
    if not any(n == 2 and wire == 2 for n, wire, _ in outer):
        raise ValueError("missing inbox response")
    result = []
    for number, wire, raw in message(outer, 2):
        if number != 2 or wire != 2:
            continue
        row = fields(raw)
        user = message(row, 9)
        item = {"tid": str(integer(row, 1)), "pid": str(integer(row, 2)),
                "created_at": integer(row, 3), "forum": string(row, 5), "content": string(row, 6),
                "is_floor": bool(integer(row, 7)), "quote_content": string(row, 8),
                "quote_pid": str(integer(row, 14)), "author_id": str(integer(user, 2)),
                "author": string(user, 4) or string(user, 3), "portrait": string(user, 5),
                "quote_user_id": str(integer(message(row, 15), 2)),
                "thread_author_id": str(integer(message(row, 25), 2))}
        if all(positive(item[key]) for key in ("tid", "pid", "author_id")) and item["content"].strip():
            result.append(item)
    return result


def parse_submission(payload: bytes) -> str:
    outer = fields(payload)
    error = message(outer, 1)
    data = message(outer, 2)
    code, error_text = integer(error, 1), string(error, 2)
    pid = string(data, 3)
    anti = message(data, 14)
    vcode = message(data, 17)
    extra = message(vcode, 4) or message(anti, 13)
    challenge = string(anti, 3) not in {"", "0"} or bool(vcode)
    verification = {"url": challenge_url(string(vcode, 2) or string(anti, 12) or string(extra, 3) or string(extra, 5)),
                    "type": string(vcode, 3) or string(anti, 6),
                    "md5": string(vcode, 1) or string(anti, 4)}
    if positive(pid) and (code or challenge):
        required = challenge or str(code) in {"4", "40", "220034", "9012", "9014"} or bool(re.search("验证|captcha", error_text, re.I))
        raise TiebaActionError("发送结果同时含回复编号和错误，请人工核对，禁止重复发送", uncertain=True,
                              verification_required=required, verification=verification if required else None)
    check_error(code, error_text, verification=verification if challenge else None)
    if challenge:
        raise TiebaActionError("百度要求验证码，账号已暂停，处理后请在概览恢复", verification_required=True, verification=verification)
    if not positive(pid):
        raise TiebaActionError("未取得贴吧回复编号，请人工核对，禁止重复发送", uncertain=True)
    return str(int(pid))


def parse_floor_comments(payload: bytes, *, limit: int = 8) -> list[dict]:
    """Parse one floor's bounded sub-comments from the verified floor response."""
    outer = fields(payload)
    error = message(outer, 1)
    check_error(integer(error, 1), string(error, 2))
    data = message(outer, 2)
    rows = []
    for number, wire, value in data:
        if number != 4 or wire != 2:
            continue
        item = fields(value)
        pid = str(integer(item, 1))
        if not positive(pid):
            continue
        author = message(item, 7)
        text_parts = []
        for fragment_no, fragment_wire, fragment_value in item:
            if fragment_no != 2 or fragment_wire != 2:
                continue
            fragment = fields(fragment_value)
            if str(integer(fragment, 1)) in {"0", "4"}:
                text_parts.append(string(fragment, 2))
        rows.append({"pid": pid, "floor": str(integer(item, 6)),
                     "author_id": str(integer(author, 2) or integer(item, 4)),
                     "author": string(author, 4) or string(author, 3),
                     "portrait": string(author, 5),
                     "content": "".join(text_parts)[:12000]})
        if len(rows) >= max(1, min(limit, 20)):
            break
    return rows


class TiebaSession:
    def __init__(self, browser, account_name: str):
        self.browser, self.account_name = browser, account_name
        self.context = None
        self.bduss = ""

    async def __aenter__(self):
        self.context = await self.browser.new_context(self.browser.storage_path("tieba", self.account_name))
        try:
            cookies = await self.context.cookies(["https://tieba.baidu.com"])
            self.bduss = next((item["value"] for item in cookies if item["name"] == "BDUSS"), "")
            if not self.bduss:
                raise TiebaActionError("浏览器登录里缺少 BDUSS，请重新扫码并确认贴吧登录")
            return self
        except BaseException:
            await self.context.close()
            raise

    async def __aexit__(self, *args):
        await self.context.close()
        self.bduss = ""

    async def pass_captcha(self, verification: dict | None = None) -> dict:
        """Open this API challenge in the same account context, not the homepage."""
        verification = verification or {}
        url = challenge_url(verification.get("url", ""))
        kind = str(verification.get("type", ""))
        logger.info("[cyber-life] 接口验证码识别准备 type=%s has_url=%s", kind if kind.isdigit() else "unknown", bool(url))
        if not url:
            logger.warning("[cyber-life] 接口验证码识别未启动 reason=missing_challenge_url")
            return {"ok": False, "solved": False, "had": True, "message": "接口未提供可用验证链接，无法截图识别", "reason": "missing_challenge_url"}
        settings = getattr(self.browser, "settings", None)
        if settings is not None and not settings.bool("captcha_auto_solve"):
            return {"ok": False, "solved": False, "had": True, "skipped": True, "message": "验证码自动识别已关闭", "reason": "disabled"}
        page = None
        pending = set()
        success = ""
        async def capture(response):
            nonlocal success
            try:
                if urlsplit(response.url).hostname != "seccaptcha.baidu.com":
                    return
                result = web_verification_data(response.url, response.status, await asyncio.wait_for(response.body(), 8))
                if result:
                    success = result
            except Exception:
                pass
        def observe(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
        async def navigate(route, request):
            if request.is_navigation_request() and request.frame == page.main_frame and not safe_verification_url(request.url):
                await route.abort()
            else:
                await route.continue_()
        try:
            page = await self.context.new_page()
            await page.route("**/*", navigate)
            page.on("response", observe)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2200)
            logger.info("[cyber-life] 接口验证码开始识别 type=%s", kind if kind.isdigit() else "unknown")
            result = await asyncio.wait_for(self.browser.solve_captcha(page, "tieba", "接口原挑战"), 180)
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            if kind == "6" and success:
                logger.info("[cyber-life] 接口验证码已取得百度成功凭据 type=6")
                return {"ok": True, "solved": True, "had": True, "vcode": success, "message": "已取得百度验证成功凭据"}
            logger.warning("[cyber-life] 接口验证码未通过 reason=%s", "no_success_data" if result.get("solved") else "recognition_failed")
            return {"ok": False, "solved": False, "had": True,
                    "message": "未取得百度验证成功凭据" if result.get("solved") else result.get("message", "验证码识别未通过"),
                    "reason": "no_success_data" if result.get("solved") else "recognition_failed"}
        except Exception as exc:
            logger.warning("[cyber-life] 接口验证码识别异常 error_type=%s", type(exc).__name__)
            return {"ok": False, "solved": False, "had": True, "message": f"接口验证码识别失败（{type(exc).__name__}）", "reason": type(exc).__name__}
        finally:
            if page is not None:
                page.remove_listener("response", observe)
                try:
                    await page.close()
                except Exception:
                    pass
            for task in list(pending):
                task.cancel()
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)

    async def _request(self, url, *, form=None, multipart=None):
        # URLs are fixed below. Session material never travels over HTTP or redirects.
        kwargs = {"timeout": 25000, "max_redirects": 0, "max_retries": 0,
                  "headers": {"Cookie": "BDUSS=" + self.bduss, "User-Agent": "aiotieba/4.8.1"}}
        if multipart is not None:
            kwargs.update(multipart={"data": {"name": "file", "mimeType": "application/octet-stream", "buffer": multipart}})
            kwargs["headers"]["x_bd_data_type"] = "protobuf"
        elif form is not None:
            kwargs["form"] = form
        response = await (self.context.request.post(url, **kwargs) if form is not None or multipart is not None
                          else self.context.request.get(url, **kwargs))
        if response.status != 200:
            raise TiebaActionError(f"贴吧接口 HTTP {response.status}")
        payload = await response.body()
        if len(payload) > 2 * 1024 * 1024:
            raise TiebaActionError("贴吧响应过大，停止处理")
        return payload

    async def identity(self) -> str:
        response = json.loads(await self._request("https://tieba.baidu.com/mo/q/sync?client_type=2&back=0"))
        check_error(response.get("no", 0), str(response.get("error") or response.get("error_msg") or ""),
                    verification=verification_from_json(response))
        data = response.get("data") or {}
        user = data.get("user") or {}
        uid = str(user.get("id") or user.get("uid") or data.get("user_id") or "")
        if str(response.get("no")) != "0" or not positive(uid):
            raise TiebaActionError("贴吧未确认登录身份，请重新扫码登录")
        return uid

    async def api(self, path: str, params: dict) -> dict:
        payload = {"_client_id": f"wappc_{int(time.time()*1000)}_0", "_client_type": "2",
                   "_client_version": "12.20.1.0", "_phone_imei": "000000000000000",
                   "from": "tieba", "BDUSS": self.bduss, **params}
        payload = {key: str(value) for key, value in payload.items()}
        signed = "".join(f"{key}={payload[key]}" for key in sorted(payload)) + "tiebaclient!!!"
        payload["sign"] = hashlib.md5(signed.encode("utf-8")).hexdigest()
        response = json.loads(await self._request("https://c.tieba.baidu.com" + path, form=payload))
        check_error(response.get("error_code", response.get("errno", 0)), str(response.get("error_msg") or ""), verification=verification_from_json(response))
        return response

    async def inbox(self) -> list[dict]:
        common = pb_int(1, 2) + pb_str(2, "22.12.1.0") + pb_str(10, self.bduss)
        payload = pb_bytes(1, pb_str(1, "1") + pb_bytes(3, common))
        raw = await self._request("https://tiebac.baidu.com/c/u/feed/replyme?cmd=303007&format=protobuf", multipart=payload)
        try:
            return parse_inbox(raw)
        except (ValueError, UnicodeError):
            raise TiebaActionError("贴吧消息接口格式无法识别，本次不发送") from None

    async def floor_comments(self, tid: str, parent_pid: str, *, limit: int = 8) -> list[dict]:
        """Read one floor's sub-comments; the caller decides how much context to use."""
        if not positive(tid) or not positive(parent_pid):
            raise TiebaActionError("楼层编号无效")
        common = pb_int(1, 2) + pb_str(2, "22.12.1.0") + pb_str(10, self.bduss)
        payload = pb_bytes(1, pb_int(1, int(tid)) + pb_int(2, int(parent_pid))
                           + pb_int(4, 1) + pb_bytes(9, common))
        raw = await self._request("https://tiebac.baidu.com/c/f/pb/floor?cmd=302002", multipart=payload)
        try:
            return parse_floor_comments(raw, limit=limit)
        except (ValueError, UnicodeError):
            raise TiebaActionError("贴吧楼中楼接口格式无法识别，本次不发送") from None

    async def latest_threads(self, forums: list[str], limit: int = 30) -> list[dict]:
        """Read the new-post tab and order by publication, never reply activity."""
        candidates = {}
        for name in normalize_forums(forums):
            raw = await self.api("/c/f/frs/page", {"kw": name, "pn": "1", "rn": "30",
                                                   "tab_id": "503", "sort_type": "1"})
            data = raw.get("data") or raw
            rows = data.get("thread_list", [])
            if not isinstance(rows, list):
                raise TiebaActionError("贴吧最新帖子列表格式无法识别，本次不发送")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if any(str(row.get(flag) or "0").lower() not in {"0", "false"}
                       for flag in ("is_top", "is_membertop", "top_thread_set_time", "is_tiebaplus_ad")):
                    continue
                tid = str(row.get("id") or row.get("tid") or "")
                try:
                    published = int(row.get("create_time") or 0)
                except (TypeError, ValueError):
                    continue
                if not positive(tid) or not 0 < published <= time.time() + 300:
                    continue
                candidates[tid] = {"tid": tid, "created_at": published, "forum": name}
        return sorted(candidates.values(), key=lambda row: (row["created_at"], int(row["tid"])),
                      reverse=True)[:max(1, min(limit, 30))]

    async def thread(self, tid: str, *, anchor: str = "") -> dict:
        if not positive(tid) or (anchor and not positive(anchor)):
            raise TiebaActionError("帖子编号无效")
        params = {"kz": tid, "pn": "1", "rn": "30"}
        if anchor:
            params["pid"] = anchor
        raw = await self.api("/c/f/pb/page", params)
        data = raw.get("data") or raw
        thread = data.get("thread") or {}
        forum = data.get("forum") or thread.get("forum") or {}
        if not isinstance(forum, dict):
            forum = {}
        users = {str(user.get("id") or ""): user for user in data.get("user_list", []) if isinstance(user, dict)}
        posts = []
        for row in data.get("post_list", []):
            if not isinstance(row, dict):
                continue
            user = row.get("author") or row.get("user") or users.get(str(row.get("author_id") or ""), {})
            if not isinstance(user, dict):
                user = {}
            post_images = image_urls(row.get("content"))
            posts.append({"pid": str(row.get("id") or row.get("pid") or ""), "floor": str(row.get("floor") or ""),
                          "author_id": str(user.get("id") or row.get("author_id") or ""),
                          "author": str(user.get("name_show") or user.get("name") or ""),
                          "content": text_content(row.get("content")), "images": post_images})
        origin = thread.get("origin_thread_info") or {}
        main = next((row for row in posts if row["floor"] == "1"), {})
        body = text_content(origin.get("content")) or main.get("content", "")
        images = image_urls(origin.get("content")) + image_urls(origin.get("media")) + main.get("images", [])
        images = list(dict.fromkeys(url for url in images if url))
        images = images[:6]
        return {"tid": tid, "title": str(thread.get("title") or origin.get("title") or ""),
                "body": text_content(origin.get("content")) or main.get("content", ""),
                "images": images,
                "forum": str(thread.get("forum_name") or thread.get("fname") or forum.get("name") or ""),
                "fid": str(thread.get("fid") or thread.get("forum_id") or forum.get("id") or ""),
                "author_id": str(thread.get("author_id") or main.get("author_id") or ""), "posts": posts,
                "url": "https://tieba.baidu.com/p/" + tid}

    async def submit(self, thread: dict, body: str, uid: str, allowed: list[str], target: dict | None = None, *, pre_send=None) -> str:
        if not forum_allowed(thread.get("forum", ""), allowed) or not all(positive(thread.get(key)) for key in ("tid", "fid")):
            raise TiebaActionError("无法确认帖子属于允许的贴吧，本次不发送")
        if not body.strip() or len(body) > 400:
            raise TiebaActionError("正文为空或超过400字，本次不发送")
        if await self.identity() != uid:
            raise TiebaActionError("登录账号已变化，本次不发送")
        if target:
            if not all(positive(target.get(key)) for key in ("parent_pid", "author_id")):
                raise TiebaActionError("回复目标不完整，本次不发送")
            if target.get("sub_pid"):
                if not positive(target["sub_pid"]) or not target.get("author") or not target.get("portrait"):
                    raise TiebaActionError("楼中楼目标不完整，本次不发送")
                if any(char in target["author"] + target["portrait"] for char in "(),\r\n"):
                    raise TiebaActionError("楼中楼目标名称无法安全处理，本次不发送")
                body = f"回复 #(reply, {target['portrait'].split('?')[0]}, {target['author']}) :{body}"
        tbs = None
        for host in ("tieba.baidu.com", "c.tieba.baidu.com"):
            try:
                raw = json.loads(await self._request(f"https://{host}/dc/common/tbs"))
            except TiebaActionError:
                continue
            if str(raw.get("is_login")) == "1" and raw.get("tbs"):
                tbs = str(raw["tbs"])
                break
        if not tbs:
            raise TiebaActionError("未取得登录校验值，请重新登录")
        common = pb_int(1, 2) + pb_str(2, "22.12.1.0") + pb_str(10, self.bduss) + pb_str(11, tbs)
        values = [(4, tbs), (19, body), (26, thread["fid"]), (30, thread["forum"]),
                  (45, thread["tid"]), (48, "0"), (6, "1"), (18, "1"), (16, "12")]
        if target:
            values += [(46, target["parent_pid"]), (49, target["parent_pid"]), (20, target["author_id"])]
            if target.get("sub_pid"):
                values.append((50, target["sub_pid"]))
        else:
            values.append((55, "13"))
        proof = []
        for attempt in range(2):
            if attempt and await self.identity() != uid:
                raise TiebaActionError("验证码处理期间登录账号已变化，本次不继续发送")
            payload = pb_bytes(1, pb_bytes(1, common) + b"".join(pb_str(n, str(value)) for n, value in [*values, *proof]))
            if pre_send is not None:
                pre_send()
            try:
                result = await self._request("https://tiebac.baidu.com/c/c/post/add?cmd=309731&format=protobuf", multipart=payload)
            except Exception:
                raise TiebaActionError("发送结果未确认，请人工核对，禁止重复发送", uncertain=True) from None
            try:
                return parse_submission(result)
            except TiebaActionError as exc:
                if attempt or exc.uncertain or not exc.verification_required:
                    raise
                challenge = exc.verification
                solved = await self.pass_captcha(challenge)
                if (not solved.get("solved") or challenge.get("type") != "6"
                        or not challenge.get("md5") or not solved.get("vcode")):
                    detail = dict(challenge)
                    detail["auto_attempted"] = not solved.get("skipped", False)
                    detail["auto_reason"] = solved.get("reason", "missing_submission_proof")
                    raise TiebaActionError(str(exc) + "；" + solved.get("message", "未取得可回传验证凭据"),
                                          verification_required=True, verification=detail) from None
                proof = [(13, solved["vcode"]), (14, challenge["md5"]), (15, challenge["type"])]
                logger.info("[cyber-life] 接口验证已通过，携带凭据继续原草稿一次")
            except Exception:
                raise TiebaActionError("发送响应格式无法识别，请人工核对，禁止重复发送", uncertain=True) from None
