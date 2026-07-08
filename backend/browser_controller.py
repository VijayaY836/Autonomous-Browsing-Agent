"""
browser_controller.py

Thin wrapper around Playwright that exposes exactly what a browser agent
needs: a way to *see* the page (a numbered list of interactive elements +
a screenshot) and a small set of *actions* (click, type, navigate, scroll,
select, press key). Keeping this layer dumb and deterministic makes the
LLM's job (deciding WHAT to do) easier and more reliable.
"""

import base64
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

_JS_PATH = Path(__file__).parent / "dom_extractor.js"
_DOM_EXTRACTOR_JS = _JS_PATH.read_text()


class BrowserController:
    def __init__(self, headless: bool = True, viewport=(1280, 800)):
        self.headless = headless
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context(
            viewport=self.viewport,
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Basic stealth: hide the most common automation fingerprint that
        # search engines and bot-detection scripts check for.
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self.context.set_default_timeout(15000)
        self.page = await self.context.new_page()

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    # ---------- perception ----------

    async def observe(self) -> dict[str, Any]:
        """Return current URL/title/scroll info + a numbered list of interactive elements."""
        try:
            data = await self.page.evaluate(_DOM_EXTRACTOR_JS)
        except Exception:
            # page may be mid-navigation; wait briefly and retry once
            await self.page.wait_for_timeout(400)
            data = await self.page.evaluate(_DOM_EXTRACTOR_JS)
        return data

    async def screenshot_b64(self) -> str:
        img_bytes = await self.page.screenshot(type="jpeg", quality=70)
        return base64.b64encode(img_bytes).decode("utf-8")

    async def get_visible_text(self, max_chars: int = 4000) -> str:
        text = await self.page.evaluate("document.body ? document.body.innerText : ''")
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        # Keep both ends: newly-added content (e.g. a confirmation modal that just
        # appeared) is typically appended at the END of the DOM, so naive head-only
        # truncation was silently hiding exactly the thing the agent needed to see.
        half = max_chars // 2
        return text[:half] + " …[middle truncated]… " + text[-half:]

    # ---------- actions ----------

    async def navigate(self, url: str):
        url = url.strip()
        if re.match(r"^https?://", url):
            target = url
        elif url.startswith(("/", "./", "../")):
            # relative path -> resolve against the current page
            target = urljoin(self.page.url, url)
        elif "." in url.split("/")[0]:
            # looks like a bare domain, e.g. "example.com/path"
            target = "https://" + url
        else:
            # relative path with no leading slash, e.g. "wiki/Capital_of_France"
            target = urljoin(self.page.url, url)
        await self.page.goto(target, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(500)

    async def click(self, agent_id: int):
        locator = self.page.locator(f'[data-agent-id="{agent_id}"]')
        await locator.scroll_into_view_if_needed()
        if await locator.is_disabled():
            raise RuntimeError(f"Element #{agent_id} is disabled — cannot click it yet.")
        try:
            await locator.click(timeout=8000)
        except Exception:
            raise RuntimeError(
                f"Could not click element #{agent_id} (it may be covered by something, e.g. a "
                f"confirmation dialog that already appeared after a previous successful action). "
                f"Before retrying, check the current visible page text for a success/confirmation "
                f"message — the action may have already worked."
            )
        await self.page.wait_for_timeout(400)

    async def type_text(self, agent_id: int, text: str, submit: bool = False):
        locator = self.page.locator(f'[data-agent-id="{agent_id}"]')
        await locator.scroll_into_view_if_needed()
        if await locator.is_disabled():
            raise RuntimeError(f"Element #{agent_id} is disabled — cannot type into it yet.")
        try:
            await locator.click(timeout=8000)
            await locator.fill("")
            await locator.type(text, delay=15)
        except Exception:
            raise RuntimeError(
                f"Could not type into element #{agent_id} — it likely isn't a plain text field "
                f"(e.g. it may be a calendar/date-picker widget that opens a popup instead). Try "
                f"clicking it first to see what opens, then interact with whatever appears (e.g. "
                f"click a specific day in a calendar) rather than typing."
            )
        if submit:
            await locator.press("Enter")
        await self.page.wait_for_timeout(400)

    async def select_option(self, agent_id: int, value: str):
        locator = self.page.locator(f'[data-agent-id="{agent_id}"]')
        await locator.scroll_into_view_if_needed()
        try:
            await locator.select_option(label=value)
        except Exception:
            await locator.select_option(value=value)
        await self.page.wait_for_timeout(300)

    async def scroll(self, direction: str = "down", amount: int = 600):
        delta = amount if direction == "down" else -amount
        await self.page.evaluate(f"window.scrollBy(0, {delta})")
        await self.page.wait_for_timeout(300)

    async def press_key(self, key: str):
        await self.page.keyboard.press(key)
        await self.page.wait_for_timeout(300)

    async def go_back(self):
        await self.page.go_back()
        await self.page.wait_for_timeout(400)