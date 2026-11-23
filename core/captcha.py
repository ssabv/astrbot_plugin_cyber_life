"""Browser verification handling for character images and slider controls.

Unsupported controls or unknown results stop the operation. Disappearance of a
control is not proof that an API write succeeded; those writes wait separately.
"""

from __future__ import annotations

import io
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from astrbot.api import logger

# 局部容器：图片型验证码
IMAGE_SELECTORS = (
    "#Tangram__1__captcha",
    "#TANGRAM__PSP_4__captcha",
    ".tang-pass-captcha:not(:has(.tang-pass-qrcode))",
    ".pass-captcha img",
    ".vcode-img",
    "#captchaImg",
    "#captcha-img",
    "img[src*='captcha']",
    "img[src*='vcode']",
    "img[src*='verifyCode']",
    "img[src*='verify_code']",
    "[class*='captcha'] img",
    "[class*='captcha-img']",
    "[class*='verify-img']",
    "[class*='captcha'] canvas",
)

# 滑块验证码的手柄（只收强特征类名，避免把轮播按钮当成滑块）
SLIDER_SELECTORS = (
    ".passMod_slide-btn",
    ".geetest_slider_button",
    ".geetest_btn",
    ".nc_iconfont.btn_slide",
    ".verify-move-block",
    "[class*='slider-btn']",
    "[class*='sliderBtn']",
    "[class*='slide-button']",
    "[class*='slide_btn']",
    "[class*='drag-btn']",
    "[class*='dragBtn']",
    "[class*='btn_slide']",
)

# 滑块的轨道容器（用来算拖动距离）
SLIDER_TRACK_SELECTORS = (
    ".passMod_slide-control",
    ".geetest_slider",
    ".geetest_slider_track",
    ".nc_scale",
    ".nc-container",
    "[class*='slider-track']",
    "[class*='sliderTrack']",
    "[class*='slide-track']",
    "[class*='verify-bar']",
)

# 百度新版拼图：背景图和可拖动的拼图块是两张独立图片。
PUZZLE_BACKGROUND_SELECTORS = (
    ".passMod_puzzle-background",
    ".passMod_puzzle-context img:not(.passMod_puzzle-block)",
)

PUZZLE_BLOCK_SELECTORS = (
    ".passMod_puzzle-block",
)

CAPTCHA_SUCCESS_SELECTORS = (
    ".passMod_verify-success",
    ".passMod_dialog-success",
)

CAPTCHA_INPUT_SELECTORS = (
    "#TANGRAM__PSP_4__captchaInput",
    ".tang-pass-captcha input",
    "input[placeholder*='验证码']",
    "input[placeholder*='图片']",
    "input[placeholder*='字符']",
    "input[name*='captcha']",
    "input[id*='captcha']",
    "input[name*='vcode']",
    "input[id*='vcode']",
    "input[name*='verifyCode']",
    "input[class*='captcha']",
)

SUBMIT_SELECTORS = (
    "#TANGRAM__PSP_4__submit",
    ".tang-pass-captcha button",
    "[class*='captcha'] input[type='submit']",
    "[class*='captcha'] button",
)

REFRESH_SELECTORS = (
    ".passMod_dialog-footer-refresh",
    "a:has-text('换一张')",
    "button:has-text('换一张')",
    ".tang-pass-captcha-refresh",
    "[class*='captcha'] [class*='refresh']",
    "[class*='captcha'] [class*='change']",
    "[aria-label*='换一张']",
)

UNSUPPORTED_SELECTORS = (
    "[data-captcha-type='click']", "[data-captcha-type='rotate']",
    ".geetest_click", ".geetest_rotate",
)

# 整页拦截（百度安全验证这类独立验证页）。只收强特征，
# 「请输入验证码」这种普通登录页文案不能用来判定，否则会误闯。
PAGE_URL_MARKS = (
    "wappass.baidu.com/static/captcha",
    "verify.baidu.com",
    "captcha.qq.com",
    "sec-captcha",
    "/security/verify",
    "geetest.com/validate",
)
PAGE_TEXT_MARKS = (
    "百度安全验证",
    "请完成下方验证",
    "检测到异常访问",
    "访问过于频繁",
    "拖动下方滑块",
    "请拖动滑块",
    "人机身份验证",
)

IMAGE_PROMPT = (
    "这是一张网页验证码截图。请只输出图片中的字符，保持原样的大小写和数字。"
    '只输出 JSON：{"code":"识别的字符"}，不要解释或猜测。'
    '如果不是字符验证码或无法辨认，输出 {"code":""}。'
)

IMAGE_SYSTEM = '只输出 JSON 对象，code 是字符串；不能辨认时 code 为空。'

SLIDER_PROMPT = (
    "这是一张滑块验证码截图。图中左侧是手柄，右侧背景图里有一个凹槽缺口。"
    "请以图片宽度为 1，给出缺口中心点的横向比例。\n"
    '只输出 JSON：{"x": 0.63}\n'
    "看不到缺口时输出 {\"x\": 0}。不要输出别的字段。"
)

SLIDER_BACKGROUND_PROMPT = (
    "这是一张滑块拼图验证码的背景图。图中有一个纯黑或很深的拼图缺口。"
    "请以图片宽度为 1，给出缺口中心点的横向比例。\n"
    '只输出 JSON：{"x": 0.52}\n'
    "看不到缺口时输出 {\"x\": 0}。不要输出别的字段。"
)

CLICK_PROMPT = (
    "这是一张点选式验证码截图，可能要求按顺序点击若干目标。"
    "请以图片宽高为 1，返回需要点击的位置，按点击顺序排列。\n"
    '只输出 JSON：{"points": [{"x": 0.32, "y": 0.45}]}\n'
    "最多 5 个点；看不清就返回 {\"points\": []}。"
)

_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,8}$|^[\u4e00-\u9fa5]{2,6}$")


@dataclass
class CaptchaProbe:
    """一次探测的结果：有没有、什么类型、在哪、怎么给人描述。"""

    found: bool = False
    kind: str = ""  # image / slider / click / page / unknown
    label: str = ""
    box: dict[str, float] | None = None
    frame: Any = None
    shot: Path | None = None
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "kind": self.kind,
            "label": self.label,
            "box": self.box,
        }


def clean_code(text: str | None) -> str:
    """Accept a complete answer, never extract a guess from explanatory text."""
    if not isinstance(text, str) or not text.strip():
        return ""
    value = text.strip()
    if value.startswith("{"):
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return ""
        if not isinstance(data, dict) or set(data) != {"code"} or not isinstance(data["code"], str):
            return ""
        value = data["code"].strip()
    rejected = {"UNKNOWN", "ERROR", "FAILED", "NONE", "NULL", "看不清", "看不清楚",
                "无法辨认", "无法识别", "不能识别", "无法确定", "不知道", "不确定"}
    return value if value.upper() not in rejected and _CODE_RE.fullmatch(value) else ""


class CaptchaSolver:
    def __init__(self, browser: Any, llm: Any, settings: Any, data_dir: Path) -> None:
        self.browser = browser
        self.llm = llm
        self.settings = settings
        self.shots_dir = data_dir / "media" / "captcha"
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- 对外入口
    @property
    def enabled(self) -> bool:
        return bool(self.settings.bool("captcha_auto_solve"))

    async def solve(self, page: Any, *, platform: str = "", reason: str = "") -> dict[str, Any]:
        """检测并尝试过验证码。返回 ok/solved/had/kind/attempts/message/screenshot。"""
        if not self.enabled:
            probe = await self.probe(page)
            return {"ok": not probe.found, "solved": False, "had": probe.found, "skipped": True,
                    "message": "验证码自动识别已关闭" if probe.found else "没有发现验证码"}
        attempts = max(1, min(6, self.settings.int("captcha_max_attempts") or 3))
        last_message = ""
        kind = ""
        for index in range(attempts):
            probe = await self.probe(page)
            if not probe.found:
                if index == 0:
                    return {"ok": True, "solved": False, "had": False,
                            "message": "没有发现验证码"}
                return {"ok": False, "solved": False, "had": True, "kind": kind,
                        "attempts": index, "message": "验证控件消失，但未确认验证结果，需要核对"}
            kind = probe.kind
            if probe.kind == "unknown":
                # 只有输入框、没有可识别的图形：模型帮不上忙，直接交给人
                shot = await self._grab(page, probe, pad=10)
                return {"ok": False, "solved": False, "had": True, "kind": kind,
                        "attempts": 1, "message": probe.label or "验证码需要人工处理",
                        "screenshot": str(shot) if shot else ""}
            try:
                if probe.kind == "image":
                    ok, detail = await self._solve_image(page, probe)
                elif probe.kind == "slider":
                    ok, detail = await self._solve_slider(page, probe)
                elif probe.kind == "click":
                    ok, detail = await self._solve_click(page, probe)
                elif probe.kind == "page":
                    ok, detail = await self._solve_page(page, probe)
                else:
                    ok, detail = False, probe.label or "无法识别的验证码"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[cyber-life] 验证码处理异常（{platform}/{reason}）：{exc}")
                ok, detail = False, f"处理异常：{exc}"
            last_message = detail
            logger.info(
                f"[cyber-life] 验证码第 {index + 1}/{attempts} 次"
                f"（{platform}/{reason}，{kind_label(kind)}）：{detail}"
            )
            if ok:
                passed = False
                rejected = ""
                after = None
                deadline = time.monotonic() + 6.0
                while time.monotonic() < deadline:
                    await page.wait_for_timeout(500)
                    if await self._has_success_marker(page):
                        passed = True
                        break
                    rejected = await self._slider_message(page)
                    if rejected:
                        break
                    after = await self.probe(page)
                    if not after.found:
                        passed = True
                        break
                if passed:
                    logger.info(f"[cyber-life] 已通过{kind_label(kind)}（{platform}/{reason}，第 {index + 1} 次）")
                    return {"ok": True, "solved": True, "had": True, "kind": kind,
                            "attempts": index + 1, "message": detail}
                if rejected:
                    last_message = f"提交后仍被拦截（{rejected}）"
                else:
                    last_message = f"提交后仍被拦截（{(after.label if after else '') or '验证未通过'}）"
            if index + 1 < attempts:
                await self._refresh(page, probe)
        shot = await self._save_page_shot(page, platform)
        logger.warning(f"[cyber-life] 验证码自动识别失败（{platform}/{reason}）：{last_message}")
        return {"ok": False, "solved": False, "had": True, "kind": kind,
                "attempts": attempts, "message": last_message or "验证码没通过",
                "screenshot": str(shot) if shot else ""}

    # --------------------------------------------------------------- 探测
    async def probe(self, page: Any) -> CaptchaProbe:
        if page.is_closed():
            return CaptchaProbe(found=True, kind="unknown", label="页面已关闭，验证状态未知")
        frames = list(page.frames)
        unsupported = await self._find(frames, UNSUPPORTED_SELECTORS, min_w=20, min_h=16)
        if unsupported:
            return CaptchaProbe(found=True, kind="unknown", label="点选或旋转验证需要人工处理",
                                box=unsupported[0], frame=unsupported[1])
        # 1) 滑块：先判，避免把滑块背景里的图片当成图片验证码
        slider = await self._find(frames, SLIDER_SELECTORS, min_w=20, min_h=16)
        if slider:
            return CaptchaProbe(found=True, kind="slider", label="滑块验证",
                                box=slider[0], frame=slider[1])
        # 2) 图片验证码
        image = await self._find(frames, IMAGE_SELECTORS, min_w=48, min_h=20)
        if image:
            return CaptchaProbe(found=True, kind="image", label="图片验证码",
                                box=image[0], frame=image[1])
        # 3) 整页拦截
        text = ""
        try:
            text = (await page.locator("body").inner_text(timeout=1500))[:2000]
        except Exception:  # noqa: BLE001
            return CaptchaProbe(found=True, kind="unknown", label="无法读取页面，验证状态未知")
        url = str(getattr(page, "url", "") or "").lower()
        if any(mark in url for mark in PAGE_URL_MARKS) or (
            text and any(mark in text for mark in PAGE_TEXT_MARKS)
        ):
            return CaptchaProbe(found=True, kind="page", label="整页安全验证",
                                notes=[text[:400]])
        # 4) 有验证码输入框但没图（短信/邮箱/点选）
        field_hit = await self._find(frames, CAPTCHA_INPUT_SELECTORS, min_w=40, min_h=10)
        if field_hit:
            return CaptchaProbe(found=True, kind="unknown", label="验证码输入框（无图片可识别）",
                                box=field_hit[0], frame=field_hit[1])
        return CaptchaProbe()

    async def _find(self, frames: list[Any], selectors: tuple[str, ...],
                    *, min_w: int = 0, min_h: int = 0) -> tuple[dict[str, float], Any] | None:
        for frame in frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count():
                        continue
                    if not await locator.is_visible(timeout=600):
                        continue
                    box = await locator.bounding_box()
                    if not box:
                        continue
                    if box.get("width", 0) < min_w or box.get("height", 0) < min_h:
                        continue
                    return await self._abs_box(frame, box), frame
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _abs_box(self, frame: Any, box: dict[str, float]) -> dict[str, float]:
        """Playwright bounding boxes already use main-frame viewport coordinates."""
        return {
            "x": float(box["x"]),
            "y": float(box["y"]),
            "width": float(box["width"]),
            "height": float(box["height"]),
        }

    # --------------------------------------------------------------- 图片型
    async def _solve_image(self, page: Any, probe: CaptchaProbe) -> tuple[bool, str]:
        shot = await self._grab(page, probe, pad=10)
        if not shot:
            return False, "截不到验证码图片"
        answer = await self.llm.complete(
            IMAGE_PROMPT, system=IMAGE_SYSTEM, images=[shot], timeout=60
        )
        code = clean_code(answer)
        if not code:
            return False, f"视觉模型没读出验证码（返回：{(answer or '空')[:40]}）"
        if not await self._fill(page, probe, code):
            return False, "没找到验证码输入框"
        if not await self._submit(page, probe):
            return False, "没有找到验证码专用确认按钮，停止提交"
        return True, "已填写验证码并点击验证按钮"

    def _ordered_frames(self, page: Any, probe: CaptchaProbe) -> list[Any]:
        """验证码图层和输入框不一定在同一个 frame，先试原 frame 再兜全网。"""
        frames = list(page.frames)
        if probe.frame is not None and probe.frame in frames:
            frames.remove(probe.frame)
            frames.insert(0, probe.frame)
        elif probe.frame is not None:
            frames.insert(0, probe.frame)
        return frames

    async def _fill(self, page: Any, probe: CaptchaProbe, code: str) -> bool:
        for frame in self._ordered_frames(page, probe):
            for selector in CAPTCHA_INPUT_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count() or not await locator.is_visible(timeout=500):
                        continue
                    await locator.click(timeout=3000)
                    try:
                        await locator.fill(code, timeout=3000)
                    except Exception:  # noqa: BLE001
                        await page.keyboard.type(code, delay=60)
                    await page.wait_for_timeout(220)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def _submit(self, page: Any, probe: CaptchaProbe) -> bool:
        for frame in self._ordered_frames(page, probe):
            for selector in SUBMIT_SELECTORS:
                try:
                    button = frame.locator(selector).first
                    if not await button.count() or not await button.is_visible(timeout=400):
                        continue
                    await button.click(timeout=3000)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    # --------------------------------------------------------------- 滑块型
    async def _solve_slider(self, page: Any, probe: CaptchaProbe) -> tuple[bool, str]:
        if not self.settings.bool("captcha_slider"):
            return False, "滑块自动拖动已关闭"
        handle = probe.box or {}
        # 百度新版 passMod 拼图：缺口在上方背景图里，下面只是滑动条。
        # 必须截背景图，不能拿滑动条当拼图，否则模型永远看不到缺口。
        puzzle = await self._find_visual(page, probe, PUZZLE_BACKGROUND_SELECTORS,
                                         min_w=100, min_h=40)
        block = await self._find_visual(page, probe, PUZZLE_BLOCK_SELECTORS,
                                        min_w=12, min_h=20)
        track = None
        panel = None
        ratio = 0.0
        source = ""
        if puzzle:
            panel = puzzle[0]
            raw = await self._visual_bytes(page, puzzle[2])
            if raw:
                detected = self._background_gap_ratio(raw)
                if detected:
                    ratio, detail = detected
                    source = "像素定位"
                    logger.info("[cyber-life] 拼图缺口像素定位 %s", detail)
        else:
            track = await self._slider_track(page, probe)
            # 比例必须基于背景图宽度，所以截轨道；轨道找不到就按手柄向上/向右扩一块
            panel = track or {
                "x": max(0.0, float(handle.get("x", 0)) - 6),
                "y": max(0.0, float(handle.get("y", 0)) - 150),
                "width": float(handle.get("width", 40)) + 280,
                "height": 190.0,
            }
        clip = await self._clip(page, panel, pad=0)
        if not clip:
            return False, "截不到滑块图片"
        shot = await self._grab_clip(page, clip, "captcha")
        if not shot:
            return False, "截不到滑块图片"
        if ratio <= 0:
            # 背景图能拿到就送原图；拿不到才退回带手柄的截图。
            model_shot = shot
            model_prompt = SLIDER_PROMPT
            if puzzle and raw:
                saved = self._write(raw, "slider-bg")
                if saved:
                    model_shot = saved
                    model_prompt = SLIDER_BACKGROUND_PROMPT
            data = await self.llm.complete_json(
                model_prompt, images=[model_shot], timeout=60
            ) or {}
            try:
                ratio = float(data.get("x") or 0)
            except (TypeError, ValueError):
                ratio = 0.0
            source = "视觉模型" if ratio > 0 else ""
        if ratio <= 0.02 or ratio > 1.05:
            return False, "视觉模型没定位到缺口"
        gap_x = float(panel.get("x", clip["x"])) + ratio * float(
            panel.get("width", clip["width"])
        )
        if puzzle:
            # 拼图块从背景图左边缘开始；用它的实际宽度算中心，避免把
            # 46px 的滑动按钮宽度误当成拼图块宽度。
            block_box = block[0] if block else puzzle[0]
            block_width = float(block_box.get("width", 0) or 0)
            if block_width <= 0 or block_width > clip["width"] * 0.5:
                block_width = min(52.0, clip["width"] * 0.18)
            start_x = float(block_box.get("x", clip["x"])) + block_width / 2
        else:
            start_x = float(handle.get("x", 0)) + float(handle.get("width", 20)) / 2
        distance = gap_x - start_x
        if distance <= 6:
            return False, "算出的拖动距离太小"
        await self._drag(page, handle, distance)
        return True, f"已拖动 {distance:.0f}px（缺口 {ratio * 100:.0f}%，{source or '未知来源'}）"

    async def _find_visual(self, page: Any, probe: CaptchaProbe,
                           selectors: tuple[str, ...], *, min_w: int = 0,
                           min_h: int = 0) -> tuple[dict[str, float], Any, Any] | None:
        """Find an image-like control and wait briefly for it to finish loading."""
        frames = self._ordered_frames(page, probe)
        for frame in frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count():
                        continue
                    await locator.wait_for(state="visible", timeout=4000)
                    box = await locator.bounding_box()
                    if not box:
                        continue
                    if box.get("width", 0) < min_w or box.get("height", 0) < min_h:
                        continue
                    try:
                        await locator.evaluate(
                            "(img) => new Promise((resolve) => {"
                            "if (img.complete && img.naturalWidth) return resolve(true);"
                            "const done = () => resolve(true);"
                            "img.addEventListener('load', done, {once: true});"
                            "img.addEventListener('error', done, {once: true});"
                            "setTimeout(done, 4000);"
                            "})",
                            timeout=5000,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    box = await locator.bounding_box() or box
                    return await self._abs_box(frame, box), frame, locator
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _visual_bytes(self, page: Any, locator: Any) -> bytes | None:
        """Read the captcha image itself, without the draggable block on top."""
        try:
            src = await locator.get_attribute("src") or ""
        except Exception:  # noqa: BLE001
            return None
        if not src:
            return None
        try:
            if src.startswith("data:"):
                head, _, data = src.partition(",")
                if ";base64" not in head:
                    return None
                import base64

                return base64.b64decode(data)
            # Prefer the image already decoded by the page: no extra request,
            # and no chance of touching Tieba's write endpoints.
            try:
                rendered = await locator.evaluate(
                    "(img) => {"
                    "if (!img.complete || !img.naturalWidth) return '';"
                    "const canvas = document.createElement('canvas');"
                    "canvas.width = img.naturalWidth;"
                    "canvas.height = img.naturalHeight;"
                    "canvas.getContext('2d').drawImage(img, 0, 0);"
                    "try { return canvas.toDataURL('image/png').split(',')[1] || ''; }"
                    "catch (err) { return ''; }"
                    "}"
                )
                if rendered:
                    import base64

                    return base64.b64decode(rendered)
            except Exception:  # noqa: BLE001
                pass
            base = str(getattr(page, "url", "") or "")
            absolute = urljoin(base, src)
            request = getattr(page, "request", None)
            if request is None:
                request = getattr(getattr(page, "context", None), "request", None)
            if request is None:
                return None
            response = await request.get(
                absolute, timeout=15000, headers={"Referer": base or absolute}
            )
            if not response.ok:
                return None
            return await response.body()
        except Exception:  # noqa: BLE001
            return None

    def _background_gap_ratio(self, raw: bytes) -> tuple[float, str] | None:
        """Locate the black puzzle hole in Baidu's background image.

        The hole is a solid near-black puzzle piece. Natural shadows can also
        be dark, so only components with puzzle-like proportions are accepted.
        """
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(raw)).convert("L")
        except Exception:  # noqa: BLE001
            return None
        if image.width > 900:
            target_height = max(1, round(image.height * 900 / image.width))
            image = image.resize((900, target_height))
        width, height = image.size
        if width < 80 or height < 40:
            return None
        pixels = image.load()
        threshold = 18
        mask = [[pixels[x, y] < threshold for x in range(width)] for y in range(height)]
        seen = [bytearray(width) for _ in range(height)]
        best: tuple[int, float, str] | None = None
        min_area = max(80, int(width * height * 0.0015))
        for y in range(height):
            row = mask[y]
            for x in range(width):
                if not row[x] or seen[y][x]:
                    continue
                stack = [(x, y)]
                seen[y][x] = 1
                area = 0
                min_x = max_x = x
                min_y = max_y = y
                while stack:
                    cx, cy = stack.pop()
                    area += 1
                    if cx < min_x:
                        min_x = cx
                    elif cx > max_x:
                        max_x = cx
                    if cy < min_y:
                        min_y = cy
                    elif cy > max_y:
                        max_y = cy
                    if cx > 0 and mask[cy][cx - 1] and not seen[cy][cx - 1]:
                        seen[cy][cx - 1] = 1
                        stack.append((cx - 1, cy))
                    if cx + 1 < width and mask[cy][cx + 1] and not seen[cy][cx + 1]:
                        seen[cy][cx + 1] = 1
                        stack.append((cx + 1, cy))
                    if cy > 0 and mask[cy - 1][cx] and not seen[cy - 1][cx]:
                        seen[cy - 1][cx] = 1
                        stack.append((cx, cy - 1))
                    if cy + 1 < height and mask[cy + 1][cx] and not seen[cy + 1][cx]:
                        seen[cy + 1][cx] = 1
                        stack.append((cx, cy + 1))
                if area < min_area:
                    continue
                box_w = max_x - min_x + 1
                box_h = max_y - min_y + 1
                if not (0.035 * width <= box_w <= 0.32 * width):
                    continue
                if not (0.07 * height <= box_h <= 0.85 * height):
                    continue
                if not (0.35 <= box_w / box_h <= 1.9):
                    continue
                if area / (box_w * box_h) < 0.30:
                    continue
                # The real hole has a light 1-3px outline. Dark clouds and
                # shadows do not, and used to win the "largest black area".
                ring_total = 0
                ring_bright = 0
                for rx in range(max(0, min_x - 3), min(width, max_x + 4)):
                    for ry in (max(0, min_y - 3), max(0, min_y - 2), max(0, min_y - 1),
                               min(height - 1, max_y + 1), min(height - 1, max_y + 2),
                               min(height - 1, max_y + 3)):
                        ring_total += 1
                        if pixels[rx, ry] > 170:
                            ring_bright += 1
                for ry in range(max(0, min_y - 3), min(height, max_y + 4)):
                    for rx in (max(0, min_x - 3), max(0, min_x - 2), max(0, min_x - 1),
                               min(width - 1, max_x + 1), min(width - 1, max_x + 2),
                               min(width - 1, max_x + 3)):
                        ring_total += 1
                        if pixels[rx, ry] > 170:
                            ring_bright += 1
                if ring_total < 24 or ring_bright / ring_total < 0.08:
                    continue
                ratio = ((min_x + max_x) / 2) / width
                if not (0.05 <= ratio <= 0.95):
                    continue
                if best is None or area > best[0]:
                    best = (area, ratio,
                            f"x={ratio:.3f} box={box_w}x{box_h} area={area}")
        if best is None:
            return None
        return best[1], best[2]

    async def _slider_track(self, page: Any, probe: CaptchaProbe) -> dict[str, float] | None:
        frames = [probe.frame] if probe.frame is not None else list(page.frames)
        handle_width = float((probe.box or {}).get("width", 0))
        for frame in frames:
            for selector in SLIDER_TRACK_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count():
                        continue
                    box = await locator.bounding_box()
                    if box and box.get("width", 0) > max(handle_width * 2, 80):
                        return await self._abs_box(frame, box)
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _drag(self, page: Any, box: dict[str, float], distance: float) -> None:
        start_x = float(box.get("x", 0)) + float(box.get("width", 20)) / 2
        start_y = float(box.get("y", 0)) + float(box.get("height", 20)) / 2
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(60, 140))
        steps = random.randint(26, 42)
        moved = 0.0
        for index in range(1, steps + 1):
            eased = 1 - (1 - index / steps) ** 2.3
            target = distance * eased
            moved = target
            await page.mouse.move(
                start_x + moved,
                start_y + random.uniform(-1.3, 1.3),
                steps=1,
            )
            await page.wait_for_timeout(random.randint(7, 24))
        if random.random() < 0.55:
            await page.mouse.move(start_x + moved - random.uniform(1.5, 3.5), start_y)
            await page.wait_for_timeout(random.randint(90, 180))
            await page.mouse.move(start_x + moved, start_y)
        await page.wait_for_timeout(random.randint(160, 420))
        await page.mouse.up()
        await page.wait_for_timeout(900)

    # --------------------------------------------------------------- 点选型
    async def _solve_click(self, page: Any, probe: CaptchaProbe) -> tuple[bool, str]:
        shot = await self._grab(page, probe, pad=8)
        if not shot:
            return False, "截不到点选图片"
        data = await self.llm.complete_json(CLICK_PROMPT, images=[shot], timeout=60) or {}
        points = data.get("points") or []
        box = probe.box or {}
        clicked = 0
        for point in points[:5]:
            try:
                x = float(box.get("x", 0)) + float(point.get("x", 0)) * float(box.get("width", 0))
                y = float(box.get("y", 0)) + float(point.get("y", 0)) * float(box.get("height", 0))
            except (TypeError, ValueError):
                continue
            await page.mouse.click(x, y)
            await page.wait_for_timeout(random.randint(250, 600))
            clicked += 1
        if not clicked:
            return False, "视觉模型没给出点击位置"
        return True, f"已点击 {clicked} 个位置"

    # --------------------------------------------------------------- 整页型
    async def _solve_page(self, page: Any, probe: CaptchaProbe) -> tuple[bool, str]:
        """整页验证页：先看是不是滑块/图片，实在不行只留证据。"""
        inner = await self.probe_local(page)
        if inner.found and inner.kind in {"image", "slider"}:
            if inner.kind == "image":
                return await self._solve_image(page, inner)
            return await self._solve_slider(page, inner)
        return False, "整页安全验证，页面里没有可识别的验证码控件"

    async def probe_local(self, page: Any) -> CaptchaProbe:
        """只看局部控件，不把当前整页判定算进去。"""
        frames = list(page.frames)
        slider = await self._find(frames, SLIDER_SELECTORS, min_w=20, min_h=16)
        if slider:
            return CaptchaProbe(found=True, kind="slider", label="滑块验证",
                                box=slider[0], frame=slider[1])
        image = await self._find(frames, IMAGE_SELECTORS, min_w=48, min_h=20)
        if image:
            return CaptchaProbe(found=True, kind="image", label="图片验证码",
                                box=image[0], frame=image[1])
        return CaptchaProbe()

    # --------------------------------------------------------------- 工具
    async def _has_success_marker(self, page: Any) -> bool:
        for frame in list(page.frames):
            for selector in CAPTCHA_SUCCESS_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() and await locator.is_visible(timeout=500):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def _slider_message(self, page: Any) -> str:
        """Baidu writes the drag verdict here; an empty string means pending."""
        for frame in list(page.frames):
            for selector in (".passMod_puzzle-msg", ".passMod_verify-fail"):
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count() or not await locator.is_visible(timeout=300):
                        continue
                    text = (await locator.inner_text(timeout=800) or "").strip()
                    if text:
                        return text[:60]
                except Exception:  # noqa: BLE001
                    continue
        return ""

    async def _refresh(self, page: Any, probe: CaptchaProbe) -> None:
        for frame in self._ordered_frames(page, probe):
            for selector in REFRESH_SELECTORS:
                try:
                    button = frame.locator(selector).first
                    if not await button.count() or not await button.is_visible(timeout=400):
                        continue
                    await button.click(timeout=2500)
                    await page.wait_for_timeout(900)
                    return
                except Exception:  # noqa: BLE001
                    continue
        await page.wait_for_timeout(700)

    async def _grab(self, page: Any, probe: CaptchaProbe, *, pad: int = 8) -> Path | None:
        """截验证码区域并压到视觉模型友好的尺寸。"""
        if not probe.box:
            return await self._save_page_shot(page, "page")
        return await self._grab_box(page, probe.box, pad=pad)

    async def _clip(self, page: Any, box: dict[str, float], *, pad: int = 8) -> dict[str, float] | None:
        """把元素框收进视口，得到真正会截下来的区域。"""
        try:
            viewport = page.viewport_size or {"width": 1280, "height": 800}
        except Exception:  # noqa: BLE001
            viewport = {"width": 1280, "height": 800}
        x = max(0.0, float(box.get("x", 0)) - pad)
        y = max(0.0, float(box.get("y", 0)) - pad)
        width = min(float(box.get("width", 0)) + pad * 2, viewport["width"] - x)
        height = min(float(box.get("height", 0)) + pad * 2, viewport["height"] - y)
        if width < 8 or height < 8:
            return None
        return {"x": x, "y": y, "width": width, "height": height}

    async def _grab_box(self, page: Any, box: dict[str, float], *, pad: int = 8) -> Path | None:
        clip = await self._clip(page, box, pad=pad)
        if not clip:
            return await self._save_page_shot(page, "fallback")
        return await self._grab_clip(page, clip, "captcha")

    async def _grab_clip(self, page: Any, clip: dict[str, float], tag: str) -> Path | None:
        try:
            raw = await page.screenshot(clip=clip, type="png")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[cyber-life] 验证码截图失败：{exc}")
            return None
        return self._write(raw, tag)

    async def _save_page_shot(self, page: Any, tag: str) -> Path | None:
        try:
            raw = await page.screenshot(type="png")
        except Exception:  # noqa: BLE001
            return None
        return self._write(raw, tag or "page")

    def _write(self, raw: bytes, tag: str) -> Path | None:
        target = self.shots_dir / f"{tag}-{int(time.time() * 1000)}-{random.randint(100, 999)}.png"
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(raw))
            if image.width > 1280 or image.height > 1280:
                image.thumbnail((1280, 1280))
            image.convert("RGB").save(target, "PNG")
            return target
        except Exception as exc:  # noqa: BLE001
            # 没有 Pillow 时至少把原始 PNG 留下来
            try:
                target.write_bytes(raw)
                return target
            except OSError:
                logger.warning(f"[cyber-life] 保存验证码截图失败：{exc}")
                return None


def kind_label(kind: str) -> str:
    return {
        "image": "图片验证码",
        "slider": "滑块验证",
        "click": "点选验证",
        "page": "整页安全验证",
        "unknown": "验证码",
    }.get(kind, "验证码")
