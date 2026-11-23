from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import astrbot.api
except ImportError:
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("browser-test")
    sys.modules["astrbot.api"] = api

from core.browser import BrowserError, BrowserManager


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(
            types.SimpleNamespace(
                bool=lambda key: True,
                str=lambda key: "chromium" if key == "browser_channel" else "",
            ),
            self.root / "astrbot_plugin_cyber_life",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, root, revision="1234"):
        path = root / f"chromium-{revision}" / "chrome-linux64/chrome"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing-browser")
        path.chmod(0o755)
        return str(path)

    def test_existing_plugin_cache_survives_playwright_version_change(self):
        cache = self.root / "astrbot_plugin_qzone_tools/browsers"
        expected = self.binary(cache)
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}), \
             patch("core.browser.Path.home", return_value=self.root / "home"), \
             patch("core.browser.shutil.which", return_value=None):
            paths = self.manager.local_executables()
        self.assertEqual(paths, [expected])

    def test_standard_cache_and_media_cache_are_discovered(self):
        home = self.root / "home"
        first = self.binary(home / ".cache/ms-playwright")
        second = self.binary(self.root / "astrbot_plugin_media_parser/browsers")
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"}), \
             patch("core.browser.Path.home", return_value=home), \
             patch("core.browser.shutil.which", return_value=None):
            self.assertEqual(self.manager.local_executables(), [first, second])

    async def test_missing_current_revision_falls_back_to_existing_binary(self):
        browser = types.SimpleNamespace()
        launch = AsyncMock(side_effect=[RuntimeError("current revision missing"),
                                       RuntimeError("headless revision missing"), browser])
        runtime = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch), stop=AsyncMock())
        api = types.ModuleType("playwright.async_api")
        api.async_playwright = lambda: types.SimpleNamespace(start=AsyncMock(return_value=runtime))
        with patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"),
                                      "playwright.async_api": api}), \
             patch.object(self.manager, "local_executables", return_value=["/cache/old/chrome"]):
            with patch("core.browser.os.path.exists", return_value=False):
                self.assertIs(await self.manager._ensure(), browser)
        self.assertEqual(launch.await_args_list[-1].kwargs["executable_path"], "/cache/old/chrome")
        runtime.stop.assert_not_awaited()

    async def test_all_launch_failures_stop_playwright(self):
        runtime = types.SimpleNamespace(
            chromium=types.SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("missing"))),
            stop=AsyncMock(),
        )
        api = types.ModuleType("playwright.async_api")
        api.async_playwright = lambda: types.SimpleNamespace(start=AsyncMock(return_value=runtime))
        with patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"),
                                      "playwright.async_api": api}), \
             patch.object(self.manager, "local_executables", return_value=[]):
            with self.assertRaises(BrowserError):
                await self.manager._ensure()
        runtime.stop.assert_awaited_once()
        self.assertIsNone(self.manager._playwright)

    async def test_installer_uses_persistent_cache_and_fixed_command(self):
        process = types.SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"installed", None)))
        with patch("core.browser.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process) as spawn, \
             patch.object(self.manager, "local_executables", return_value=["/persistent/chrome"]):
            self.assertEqual(await self.manager.install_chromium(), ["/persistent/chrome"])
        self.assertEqual(spawn.await_args.args[1:], ("-m", "playwright", "install", "--no-shell", "chromium"))
        self.assertEqual(spawn.await_args.kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"], str(self.manager.data_dir / "browsers"))

    async def test_installer_cancellation_kills_child(self):
        import asyncio
        from unittest.mock import Mock
        process = types.SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError),
                                        kill=Mock(), wait=AsyncMock())
        with patch("core.browser.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager.install_chromium()
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    def prepare_old_runtime(self):
        cache = self.manager.data_dir / "browsers"
        selected = self.binary(cache)
        old = cache / "chromium_headless_shell-1234"
        old.mkdir()
        (old / "old-shell").write_bytes(b"old")
        self.manager.settings.str = lambda key: selected if key == "browser_executable" else ""
        return Path(selected), old

    def test_cleanup_preserves_full_browser_and_unrelated_files(self):
        selected, old = self.prepare_old_runtime()
        other = old.parent / "other-cache"
        other.mkdir()
        result = self.manager.remove_old_headless_shell()
        self.assertTrue(result["removed"])
        self.assertEqual(result["bytes_removed"], 3)
        self.assertFalse(old.exists())
        self.assertTrue(selected.is_file())
        self.assertTrue(other.is_dir())
        self.assertFalse(self.manager.remove_old_headless_shell()["removed"])

    def test_cleanup_rejects_active_shell_and_external_browser(self):
        selected, old = self.prepare_old_runtime()
        for invalid in (str(old / "old-shell"), self.binary(self.root / "external")):
            self.manager.settings.str = lambda key: invalid if key == "browser_executable" else ""
            with self.assertRaises(BrowserError):
                self.manager.remove_old_headless_shell()
            self.assertTrue(old.exists())

    def test_cleanup_rejects_symlink_directory(self):
        selected, old = self.prepare_old_runtime()
        with patch("core.browser.Path.is_symlink", return_value=True):
            with self.assertRaises(BrowserError):
                self.manager.remove_old_headless_shell()
        self.assertTrue(old.is_dir())
        self.assertTrue(selected.is_file())


if __name__ == "__main__":
    unittest.main()
