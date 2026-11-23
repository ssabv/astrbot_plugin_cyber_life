"""Tieba forum names and URL boundaries shared by settings and the browser."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit


def normalize_forums(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\r\n]+", value)
    if not isinstance(value, list):
        raise ValueError("允许的贴吧需要吧名列表")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("贴吧名称必须是文字")
        name = item.strip()
        if not name:
            continue
        if len(name) > 100 or "://" in name or any(ord(c) < 32 for c in name):
            raise ValueError("请填写贴吧名称，不要填写链接或控制字符")
        if name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    if len(result) > 50:
        raise ValueError("最多配置50个贴吧")
    return result


def forum_url(name: str) -> str:
    return "https://tieba.baidu.com/f?" + urlencode({"kw": name, "ie": "utf-8"})


def forum_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"tieba.baidu.com", "www.tieba.baidu.com"}:
        return ""
    if parsed.path.rstrip("/") != "/f":
        return ""
    return parse_qs(parsed.query).get("kw", [""])[0].strip()


def is_post_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme in {"http", "https"}
            and parsed.hostname in {"tieba.baidu.com", "www.tieba.baidu.com"}
            and re.fullmatch(r"/p/\d+/?", parsed.path) is not None)


def forum_allowed(name: str, allowed: list[str]) -> bool:
    return bool(name) and name.strip().casefold() in {item.casefold() for item in allowed}
