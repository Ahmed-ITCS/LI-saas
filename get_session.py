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
        print("Once your feed is visible, come back here.")
        print("Waiting 120 seconds for you to log in...")
        await asyncio.sleep(120)
        await context.storage_state(path="state.json")
        print("Saved state.json!")
        await browser.close()

asyncio.run(main())