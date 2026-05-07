import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
        )
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login")
        print("Log in to LinkedIn in the opened browser.")
        print("Script will wait up to 6 minutes and only save when li_at cookie exists.")

        deadline_seconds = 6 * 60
        interval_seconds = 3
        elapsed = 0
        authenticated = False

        while elapsed < deadline_seconds:
            cookies = await context.cookies("https://www.linkedin.com")
            has_li_at = any(c.get("name") == "li_at" and c.get("value") for c in cookies)
            if has_li_at:
                authenticated = True
                break
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds

        if not authenticated:
            await page.screenshot(path="state_capture_failed.png", full_page=True)
            print("Did not detect authenticated LinkedIn session (li_at missing).")
            print("Saved screenshot: state_capture_failed.png")
            await browser.close()
            return

        await context.storage_state(path="state.json")
        print("Saved state.json (authenticated)!")
        await browser.close()

asyncio.run(main())