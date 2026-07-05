"""Take full-page screenshots of every static page for visual verification.

Launches the system chromium as a subprocess with remote debugging enabled
and attaches Playwright over CDP.
"""
import asyncio
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

CHROMIUM = "/usr/bin/chromium-browser"
BASE_URL = "http://localhost:8000"
CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
USER_DATA_ROOT = Path.home() / "snap" / "chromium" / "common"
STARTUP_TIMEOUT_S = 30

PAGES: list[tuple[str, str, str | None]] = [
    # (path, output filename, optional selector to click before screenshotting)
    ("/races.html", "races_overview.png", None),
    ("/races.html", "races_marathon.png", 'button.dist-tab[data-tab="marathon"]'),
    ("/builds.html", "builds.png", None),
    ("/compare.html", "compare.png", None),
    ("/training.html", "training.png", None),
    ("/years.html", "years.png", None),
]


async def _wait_for_page_ready(page: Page, timeout_ms: float = 8000) -> None:
    """Every page shows a literal 'Loading…' placeholder until its fetch(es)
    resolve. Wait for those to clear instead of guessing a fixed delay —
    networkidle alone fires before in-flight fetches finish rendering."""
    try:
        await page.wait_for_function(
            "() => !document.body.innerText.includes('Loading…')",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass


def _wait_for_cdp(timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1)
            return
        except (urllib.error.URLError, ConnectionError) as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"chromium never opened CDP port {CDP_PORT}") from last_err


async def main() -> None:
    user_data_dir = tempfile.mkdtemp(dir=USER_DATA_ROOT)
    browser_proc = subprocess.Popen([
        CHROMIUM,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data_dir}",
    ])

    try:
        _wait_for_cdp(STARTUP_TIMEOUT_S)

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.set_viewport_size({"width": 1600, "height": 900})

            for path, fname, click_selector in PAGES:
                await page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
                await _wait_for_page_ready(page)
                if click_selector:
                    await page.click(click_selector)
                    await _wait_for_page_ready(page)
                await page.screenshot(path=fname, full_page=True)
                print(f"Saved {fname}")

            await browser.close()
    finally:
        browser_proc.terminate()
        try:
            browser_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


asyncio.run(main())
