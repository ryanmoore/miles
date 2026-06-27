"""Take a screenshot of the Miles UI for visual verification."""
import asyncio
from playwright.async_api import async_playwright

CHROMIUM = "/usr/bin/chromium-browser"
URL = "http://localhost:8000"
OUT = "screenshot.png"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=CHROMIUM)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        await page.goto(URL, wait_until="networkidle")
        await page.screenshot(path=OUT, full_page=False)
        await browser.close()
    print(f"Saved {OUT}")


asyncio.run(main())
