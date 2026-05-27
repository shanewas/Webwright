"""Stealth browser environment using Agentic Stealth Browser (ASB) CDP proxy.

Launches ASB's AgentBrowser with debug_cdp=True, then connects Playwright
via connect_over_cdp() so Webwright agents get stealth/fingerprinting/anti-block.

Interface matches LocalBrowserEnvironment for drop-in config switching.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import textwrap
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_STEALTH_CDP_HOST = "127.0.0.1"
_STEALTH_CDP_PORT = 0  # random port via chrome


class StealthBrowserEnvironmentConfig(BaseModel):
    start_url: str | None = None
    headless: bool = True
    slow_mo_ms: int = 50
    browser_width: int = 1280
    browser_height: int = 1440
    browser_timeout_ms: int = 10000
    browser_navigation_timeout_ms: int = 30000
    step_execution_timeout_ms: int = 20000
    observation_timeout_ms: int = 5000
    output_dir: Path = Path("outputs/default")
    launch_args: list[str] = Field(default_factory=list)
    asb_preset: str = ""
    asb_region: str = "japan"
    asb_startup_timeout_seconds: float = 30


class StealthBrowserEnvironment:
    """Webwright environment backed by Agentic Stealth Browser.

    Launches ASB (AgentBrowser) as a subprocess with CDP debugging on,
    then connects Webwright's Playwright via ``connect_over_cdp()``.
    All stealth features (fingerprinting, human behavior, anti-block)
    are inherited from ASB automatically.
    """

    def __init__(self, *, config_class: type = StealthBrowserEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)
        self.config.output_dir = self.config.output_dir.expanduser()

        self._asb_browser = None  # AgentBrowser instance
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._step_index = 0
        self._prepared_task: dict[str, Any] = {}
        self._console_history: list[str] = []
        self._step_console: list[str] = []

    # ── lifecycle ──────────────────────────────────────────────────────

    def _screenshots_dir(self) -> Path:
        return self.config.output_dir / "screenshots"

    def _steps_dir(self) -> Path:
        return self.config.output_dir / "steps"

    def prepare(self, **kwargs) -> None:
        self._prepared_task = dict(kwargs)
        self._step_index = 0
        self._console_history = []
        self._step_console = []

        start_url = kwargs.get("start_url") or self.config.start_url
        if start_url:
            self.config.start_url = str(start_url)

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._steps_dir().mkdir(parents=True, exist_ok=True)
        self._screenshots_dir().mkdir(parents=True, exist_ok=True)
        (self.config.output_dir / "task.json").write_text(
            json.dumps(kwargs, indent=2), encoding="utf-8",
        )
        self._run(self._prepare_async())

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coro):
        return self._ensure_loop().run_until_complete(coro)

    async def _prepare_async(self) -> None:
        from core.agent_browser import AgentBrowser
        from playwright.async_api import async_playwright

        if self._page is not None:
            return

        # ── 1. Launch ASB with CDP enabled ────────────────────────────
        asb = AgentBrowser(session_name="webwright-stealth", anonymous=True)
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "debug_cdp": True,  # enables --remote-debugging-port=0 on 127.0.0.1
        }
        if self.config.asb_preset:
            launch_kwargs["preset"] = self.config.asb_preset
        if self.config.asb_region:
            launch_kwargs["region"] = self.config.asb_region
        if self.config.launch_args:
            launch_kwargs["launch_options"] = {"args": self.config.launch_args}

        await asb.launch(**launch_kwargs)

        # ── 2. Get the CDP WebSocket endpoint ─────────────────────────
        cdp_info = await asb.get_cdp_endpoint()
        if isinstance(cdp_info, dict) and cdp_info.get("status") == "enabled":
            cdp_endpoint = cdp_info["ws_endpoint"]
        else:
            # fallback: scrape /json/version from the debug interface
            cdp_endpoint = self._discover_cdp_endpoint(asb)

        self._asb_browser = asb

        # ── 3. Connect Playwright via CDP ─────────────────────────────
        pw = await async_playwright().start()
        self._playwright = pw
        self._browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        self._connected_over_cdp = True

        self._context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context(no_viewport=True)
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        # Align ASB's internal page reference to match Webwright's page
        # so that asb.safe_goto(), asb.safe_click() etc. operate on the same tab
        if hasattr(asb, 'page') and asb.page is not None:
            try:
                await asb.page.close()
            except Exception:
                pass
        asb.page = self._page

        self._context.set_default_timeout(self.config.browser_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.browser_navigation_timeout_ms)
        self._attach_page_listeners(self._page)

        if self.config.start_url:
            await self._page.goto(self.config.start_url, wait_until="domcontentloaded")

    def _discover_cdp_endpoint(self, asb) -> str:
        """Fallback CDP endpoint discovery via /json/version."""
        import urllib.request
        import json as j

        # ASB binds CDP to 127.0.0.1 on a random port; try to extract it
        # from the browser process args or default heuristic
        port = getattr(asb, "_cdp_port", None)
        if port:
            base = f"http://127.0.0.1:{port}"
            try:
                with urllib.request.urlopen(f"{base}/json/version", timeout=3) as resp:
                    info = j.loads(resp.read())
                    return info.get("webSocketDebuggerUrl", base)
            except Exception:
                pass
        raise RuntimeError(
            "Could not discover ASB CDP endpoint. "
            "Ensure AgentBrowser was launched with debug_cdp=True."
        )

    # ── page listeners ────────────────────────────────────────────────

    def _attach_page_listeners(self, page: Any) -> None:
        page.on("console", self._on_console_message)
        page.on("pageerror", self._on_page_error)

    def _on_console_message(self, message: Any) -> None:
        text = getattr(message, "text", "")
        if callable(text):
            text = text()
        self._console_history.append(str(text))
        self._step_console.append(str(text))

    def _on_page_error(self, error: Any) -> None:
        line = f"Page error: {error}"
        self._console_history.append(line)
        self._step_console.append(line)

    # ── execution ─────────────────────────────────────────────────────

    def execute(self, action: dict[str, Any], cwd: str = "") -> dict[str, Any]:
        del cwd
        return self._run(self._execute_async(action))

    async def _execute_async(self, action: dict[str, Any]) -> dict[str, Any]:
        self._step_index += 1
        self._step_console = []
        python_code = str(action.get("python_code", "") or "")
        self._persist_step_code(python_code)

        success = True
        exception_text = ""
        output = ""
        try:
            if python_code.strip():
                if self._page is None or self._context is None or self._playwright is None:
                    raise RuntimeError("Stealth browser environment was not prepared.")

                buffer = io.StringIO()
                globals_dict: dict[str, Any] = {
                    "asyncio": asyncio,
                    "asb": self._asb_browser,
                }
                locals_dict: dict[str, Any] = {}

                wrapped = "async def __agent_step__(page, context, browser, playwright, task):\n"
                wrapped += textwrap.indent(python_code, "    ")

                with redirect_stdout(buffer), redirect_stderr(buffer):
                    exec(wrapped, globals_dict, locals_dict)
                    await asyncio.wait_for(
                        locals_dict["__agent_step__"](
                            self._page,
                            self._context,
                            self._browser,
                            self._playwright,
                            self._prepared_task,
                        ),
                        timeout=self.config.step_execution_timeout_ms / 1000,
                    )
                output = buffer.getvalue()
            await self._wait_for_observation_ready()
        except Exception:
            success = False
            exception_text = traceback.format_exc()

        observation = await self._capture_observation(success=success, exception_text=exception_text)
        return {
            "output": output,
            "returncode": 0 if success else 1,
            "exception_info": exception_text,
            "observation": observation,
        }

    def _persist_step_code(self, python_code: str) -> None:
        step_path = self._steps_dir() / f"step_{self._step_index:04d}.py"
        step_path.write_text(python_code, encoding="utf-8")
        script_path = self.config.output_dir / "script.py"
        with script_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n# Step {self._step_index}\n")
            fh.write(python_code)
            fh.write("\n")

    async def _wait_for_observation_ready(self) -> None:
        if self._page is None:
            return
        try:
            await self._page.wait_for_load_state(
                "domcontentloaded",
                timeout=self.config.observation_timeout_ms,
            )
        except Exception:
            pass

    async def _capture_observation(self, *, success: bool, exception_text: str) -> dict[str, Any]:
        page = self._page
        url = ""
        title = ""
        aria_snapshot = ""

        if page is not None:
            try:
                url = page.url
            except Exception:
                url = self.config.start_url or ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            try:
                aria_snapshot = await page.locator("body").aria_snapshot(
                    timeout=self.config.observation_timeout_ms,
                )
            except Exception:
                aria_snapshot = ""

        return {
            "success": success,
            "exception": exception_text,
            "url": url or self.config.start_url or "",
            "title": title,
            "aria_snapshot": aria_snapshot,
            "python_code": "",
            "python_output": "",
            "console_output": "\n".join(self._step_console[-20:]),
            "recent_console": "\n".join(self._console_history[-50:]),
        }

    # ── serialisation ─────────────────────────────────────────────────

    def serialize(self) -> dict[str, Any]:
        return {
            "environment": {
                "config": self.config.model_dump(mode="json"),
                "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
            }
        }

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {
            "start_url": self.config.start_url or "",
            "output_dir": str(self.config.output_dir.resolve()),
            "headless": self.config.headless,
            **kwargs,
        }

    # ── cleanup ───────────────────────────────────────────────────────

    def close(self) -> None:
        self._run(self._close_async())

    async def _close_async(self) -> None:
        page = self._page
        browser = self._browser
        context = self._context
        pw = self._playwright
        asb = self._asb_browser

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._asb_browser = None

        try:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            elif browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
        finally:
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
            if asb is not None:
                try:
                    await asb.close()
                except Exception:
                    pass
            if self._loop is not None:
                try:
                    if not self._loop.is_closed():
                        self._loop.close()
                except RuntimeError:
                    pass  # loop may be running — let GC handle it
                self._loop = None
