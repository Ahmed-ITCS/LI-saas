"""
management command: python manage.py setup_session <profile_id>

Opens a HEADED Chromium browser so the user can log in to LinkedIn manually.
Once logged in, saves the Playwright storage_state JSON so the bot can reuse it.
"""

import asyncio
import os
import django
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Open a headed browser to manually log in to LinkedIn and save the session."

    def add_arguments(self, parser):
        parser.add_argument("profile_id", type=int, help="LinkedInProfile pk")

    def handle(self, *args, **options):
        pk = options["profile_id"]

        from accounts.models import LinkedInProfile
        from accounts.crypto import decrypt

        try:
            profile = LinkedInProfile.objects.get(pk=pk)
        except LinkedInProfile.DoesNotExist:
            raise CommandError(f"Profile {pk} not found.")

        state_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "..", "states")
        state_dir = os.path.abspath(state_dir)
        os.makedirs(state_dir, exist_ok=True)
        state_file = os.path.join(state_dir, f"profile_{pk}.json")

        email    = decrypt(profile.li_email)
        password = decrypt(profile.li_password)

        self.stdout.write(self.style.WARNING(f"\n{'='*60}"))
        self.stdout.write(self.style.WARNING(f"  Session Setup — {profile.label}"))
        self.stdout.write(self.style.WARNING(f"{'='*60}"))
        self.stdout.write(f"  Profile ID : {pk}")
        self.stdout.write(f"  Email      : {email}")
        self.stdout.write(f"  State file : {state_file}\n")
        self.stdout.write(self.style.NOTICE(
            "A browser window will open. Log in to LinkedIn normally.\n"
            "Complete any 2FA/CAPTCHA if prompted.\n"
            "Once you see your feed, come back here and press ENTER.\n"
        ))

        asyncio.run(_setup(email, password, state_file, self.stdout))

        # Update the profile's state_file path in DB
        profile.state_file = state_file
        profile.save(update_fields=["state_file"])

        self.stdout.write(self.style.SUCCESS(f"\n✅ Session saved to {state_file}"))
        self.stdout.write(self.style.SUCCESS(f"   Profile '{profile.label}' is ready to run.\n"))


async def _setup(email: str, password: str, state_file: str, stdout):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        stdout.write("  → Navigating to LinkedIn login page...")
        await page.goto("https://www.linkedin.com/login")

        # Pre-fill credentials to save the user time
        try:
            await page.fill('input[name="session_key"]',      email,    timeout=5000)
            await page.fill('input[name="session_password"]', password, timeout=5000)
            stdout.write("  → Credentials pre-filled. You may click Sign In.")
        except Exception:
            stdout.write("  → Could not pre-fill — please type credentials manually.")

        stdout.write("\n" + "─" * 60)
        stdout.write("  Complete login in the browser, then press ENTER here...")
        stdout.write("─" * 60 + "\n")

        # Wait for user to press Enter (non-blocking via run_in_executor)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input)

        # Save session
        await context.storage_state(path=state_file)
        stdout.write(f"  → Session captured.")
        await browser.close()
