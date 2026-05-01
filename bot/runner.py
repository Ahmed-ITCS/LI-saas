"""
bot/runner.py — launched as a subprocess per LinkedIn profile.

Environment variables injected by dashboard/views.py:
  BOT_PROFILE_ID          — LinkedInProfile.pk
  BOT_LOG_FILE            — path to log file for this profile
  BOT_STATE_FILE          — path to Playwright storage_state JSON
  DJANGO_SETTINGS_MODULE  — always "libot.settings"
"""

import asyncio
import hashlib
import os
import random
import sys
import logging
import django

# ── Bootstrap Django ──────────────────────────────────────────────────────────
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "libot.settings")
django.setup()

from accounts.models import LinkedInProfile, CommentLog
from accounts.crypto import decrypt

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE   = os.environ.get("BOT_LOG_FILE", "bot.log")
PROFILE_ID = int(os.environ.get("BOT_PROFILE_ID", "0"))
STATE_FILE = os.environ.get("BOT_STATE_FILE", "state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Gemini key rotation ───────────────────────────────────────────────────────
_gemini_key_idx = 0
_gemini_keys: list[str] = []

def current_gemini_key() -> str | None:
    return _gemini_keys[_gemini_key_idx] if _gemini_keys else None

def rotate_gemini_key() -> str | None:
    global _gemini_key_idx
    _gemini_key_idx += 1
    if _gemini_key_idx >= len(_gemini_keys):
        log.error("🔴 All Gemini API keys exhausted")
        return None
    log.warning(f"🔁 Rotated to Gemini key #{_gemini_key_idx + 1}")
    return _gemini_keys[_gemini_key_idx]

# ── DB helpers ────────────────────────────────────────────────────────────────
def _content_hash(text: str) -> str:
    return hashlib.md5(text.strip()[:500].encode()).hexdigest()

# We track commented URNs and content hashes in memory (process-local) PLUS
# query the DB so we never double-comment across restarts.
_commented_urns:    set[str] = set()
_commented_hashes:  set[str] = set()

def is_already_commented(urn: str) -> bool:
    if urn in _commented_urns:
        return True
    exists = CommentLog.objects.filter(profile_id=PROFILE_ID, urn=urn).exists()
    if exists:
        _commented_urns.add(urn)
    return exists

def is_content_already_commented(text: str) -> bool:
    h = _content_hash(text)
    if h in _commented_hashes:
        return True
    exists = CommentLog.objects.filter(profile_id=PROFILE_ID, post_text__startswith=text[:50]).exists()
    if exists:
        _commented_hashes.add(h)
    return exists

def mark_as_commented(urn: str, post_text: str, comment: str, age_minutes: int | None):
    _commented_urns.add(urn)
    _commented_hashes.add(_content_hash(post_text))
    CommentLog.objects.create(
        profile_id=PROFILE_ID,
        urn=urn,
        post_text=post_text[:300],
        comment=comment,
        post_age_minutes=age_minutes,
    )

# ── Post age parsing ──────────────────────────────────────────────────────────
def parse_post_age_minutes(time_str: str) -> int | None:
    """
    Parse LinkedIn relative timestamp strings like:
    '45m', '1h', '2h', '3d', 'Just now', '1w' etc.
    Returns age in minutes, or None if unparseable.
    """
    import re
    if not time_str:
        return None
    s = time_str.strip().lower()
    if s in ("just now", "now", "1s", "moments ago"):
        return 0
    m = re.match(r"(\d+)\s*(s|m|h|d|w)", s)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    return {"s": 0, "m": val, "h": val * 60, "d": val * 1440, "w": val * 10080}[unit]

async def get_post_age_minutes(post) -> int | None:
    """Extract and parse the timestamp from a LinkedIn post element."""
    selectors = [
        "span.update-components-actor__sub-description span[aria-hidden='true']",
        ".feed-shared-actor__sub-description span[aria-hidden='true']",
        "span.visually-hidden",
        "time",
        ".update-components-actor__sub-description",
    ]
    for sel in selectors:
        try:
            el = post.locator(sel).first
            if await el.count():
                txt = (await el.inner_text(timeout=2000)).strip()
                if txt:
                    age = parse_post_age_minutes(txt)
                    if age is not None:
                        return age
        except Exception:
            continue
    return None

# ── LLM ───────────────────────────────────────────────────────────────────────
async def generate_comment(post_text: str, persona: str, provider: str) -> str:
    prompt = (
        f"{persona} "
        "Write a short (exactly 1-2 sentences), professional, human-sounding LinkedIn comment. "
        "Add genuine value or insight. No emojis. No hashtags. Sound natural, not corporate."
    )
    full_prompt = f"{prompt}\n\nPost:\n{post_text[:700]}"

    if provider == "gemini":
        try:
            from google import genai
            from google.api_core.exceptions import ResourceExhausted
        except ImportError:
            log.error("google-genai not installed — falling back to mock")
            provider = "mock"

    if provider == "gemini":
        global _gemini_key_idx
        attempts = 0
        while attempts < len(_gemini_keys):
            key = current_gemini_key()
            if not key:
                break
            try:
                client = genai.Client(api_key=key)
                response = client.models.generate_content(
                    model="gemini-2.0-flash-lite",
                    contents=full_prompt,
                )
                comment = response.text.strip()
                log.info(f"🤖 Gemini generated comment ({len(comment)} chars)")
                return comment
            except ResourceExhausted as e:
                log.warning(f"⚠️  Gemini key #{_gemini_key_idx + 1} rate-limited: {e}")
                if rotate_gemini_key() is None:
                    break
                attempts += 1
            except Exception as e:
                log.error(f"❌ Gemini error: {e}")
                break

    log.warning("⚠️  Using mock comment")
    mocks = [
        "Great insights — the point about scalability resonates with my experience building distributed systems.",
        "This is a nuanced take that's easy to overlook. Thanks for putting it so clearly.",
        "Solid perspective. The trade-off you described is exactly what teams underestimate in early architecture decisions.",
        "Appreciate the breakdown. This aligns with what I've seen when transitioning monoliths to microservices.",
        "Well articulated. The devil really is in the operational complexity, not just the initial implementation.",
        "Interesting angle. I've found that communication overhead is often the hidden cost teams miss early on.",
        "This resonates. Getting the boundaries right from the start saves so much refactoring down the line.",
        "Good point. The observability piece is often the last thing teams plan for and the first thing they wish they had.",
    ]
    return mocks[len(post_text.strip()) % len(mocks)]

# ── Playwright helpers ────────────────────────────────────────────────────────
async def get_post_text(post) -> str:
    selectors = [
        ".feed-shared-update-v2__description .break-words",
        ".feed-shared-text .break-words",
        ".feed-shared-update-v2__description span[dir='ltr']",
        ".update-components-text span[dir='ltr']",
        ".feed-shared-inline-show-more-text span[dir='ltr']",
    ]
    for sel in selectors:
        try:
            el = post.locator(sel).first
            if await el.count():
                txt = await el.inner_text(timeout=3000)
                if txt and len(txt.strip()) >= 40:
                    return txt.strip()
        except Exception:
            continue
    return ""

async def submit_comment(page, comment_box, post) -> bool:
    btn = post.locator("button.comments-comment-box__submit-button--cr").first
    if not await btn.count():
        btn = post.locator("button[class*='submit-button']").first
    if not await btn.count():
        btn = page.locator("button.comments-comment-box__submit-button--cr").first
    try:
        await btn.wait_for(state="visible", timeout=5000)
        if await btn.get_attribute("disabled") is not None:
            await comment_box.dispatch_event("input")
            await page.wait_for_timeout(1000)
            if await btn.get_attribute("disabled") is not None:
                log.warning("⚠️  Submit button still disabled — aborting")
                return False
        await btn.click()
        log.info("✅ Submit button clicked")
        await page.wait_for_timeout(4000)
        return True
    except Exception as e:
        log.error(f"❌ Submit error: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
# ── Main ──────────────────────────────────────────────────────────────────────

TARGET_URL   = os.environ.get("BOT_TARGET_URL", "").strip()
TARGET_LABEL = os.environ.get("BOT_TARGET_LABEL", "").strip()


async def _process_post(page, post, urn, min_age, max_age, persona, provider, max_cpr, commented_this_round):
    """
    Shared post-processing logic for both feed mode and targeted mode.
    Returns True if a comment was posted, False otherwise.
    """
    short = urn[:50]
    log.info(f"👀 Processing {short}...")

    # ── Age filter ────────────────────────────────────────────────────────────
    age = await get_post_age_minutes(post)
    if age is None:
        log.info(f"⏭️  {short} — could not parse age, skipping")
        return False
    if age < min_age:
        log.info(f"⏭️  {short} — too fresh ({age}m < {min_age}m), skipping")
        return False
    if age > max_age:
        log.info(f"⏭️  {short} — too old ({age}m > {max_age}m), skipping")
        mark_as_commented(urn, "", "", age)
        return False

    log.info(f"🕐 Post age: {age}m ✓")

    # ── Text ─────────────────────────────────────────────────────────────────
    post_text = await get_post_text(post)
    if not post_text:
        log.info(f"⏭️  {short} — no text, skipping")
        mark_as_commented(urn, "", "", age)
        return False

    if is_content_already_commented(post_text):
        log.info(f"⏭️  {short} — duplicate content, skipping")
        mark_as_commented(urn, post_text, "", age)
        return False

    # ── Generate ──────────────────────────────────────────────────────────────
    log.info(f"✍️  Generating comment for {short}")
    comment = await generate_comment(post_text, persona, provider)
    log.info(f'💬 Comment: "{comment}"')

    # ── Interact ──────────────────────────────────────────────────────────────
    await post.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)

    await post.locator('button[aria-label="Comment"]').first.click(timeout=8000)
    await page.wait_for_timeout(1500)

    comment_box = post.locator('div[role="textbox"][contenteditable="true"]').first
    await comment_box.click()
    await page.wait_for_timeout(500)
    await comment_box.fill("")
    await comment_box.type(comment, delay=random.randint(15, 35))
    await comment_box.dispatch_event("input")
    await comment_box.dispatch_event("change")
    await page.wait_for_timeout(2000)

    submitted = await submit_comment(page, comment_box, post)
    if submitted:
        mark_as_commented(urn, post_text, comment, age)
        log.info(f"✅ Commented on {short} ({commented_this_round + 1}/{max_cpr})")
        return True
    else:
        log.warning(f"⚠️  Could not submit for {short}")
        return False


async def _run_targeted(page, profile, persona, provider, min_age, max_age, max_cpr):
    """Visit a specific person's activity page and comment on their posts (one-shot)."""
    log.info("=" * 60)
    log.info(f"🎯 TARGETED MODE — {TARGET_LABEL}")
    log.info(f"   URL: {TARGET_URL}")
    log.info("=" * 60)

    await page.goto(TARGET_URL)
    try:
        await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
        log.info("✅ Target activity page loaded")
    except Exception:
        log.error("❌ Could not load target activity page — wrong URL or not logged in")
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=f"debug_targeted_{PROFILE_ID}_{ts}.png")
        return

    # Scroll a couple of times to load more posts
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.5)

    post_containers = await page.locator('div[data-urn^="urn:li:activity:"]').all()
    candidates = []
    seen_urns: set[str] = set()
    for post in post_containers:
        urn = await post.get_attribute("data-urn")
        if urn and not is_already_commented(urn) and urn not in seen_urns:
            seen_urns.add(urn)
            candidates.append((urn, post))

    log.info(f"📊 {len(post_containers)} posts found, {len(candidates)} unseen on target page")

    commented = 0
    for urn, post in candidates:
        if commented >= max_cpr:
            log.info(f"🛑 Max comments reached ({max_cpr})")
            break
        if is_already_commented(urn):
            continue
        try:
            posted = await _process_post(page, post, urn, min_age, max_age, persona, provider, max_cpr, commented)
            if posted:
                commented += 1
                if commented < max_cpr:
                    wait_secs = random.uniform(18, 38)
                    log.info(f"⏳ Waiting {wait_secs:.1f}s...")
                    await asyncio.sleep(wait_secs)
        except Exception as e:
            log.error(f"❌ Error: {e}", exc_info=True)
            continue

    log.info(f"🎯 Targeted run complete — {commented} comment(s) posted on '{TARGET_LABEL}'")


async def _run_feed(page, profile, persona, provider, min_age, max_age, max_cpr):
    """Standard mode: continually scan the home feed."""
    log.info("📡 Navigating to LinkedIn feed")
    await page.goto("https://www.linkedin.com/feed/")

    try:
        await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
        log.info("✅ Feed loaded")
    except Exception:
        log.error("❌ Feed not found — possibly not logged in or DOM changed")
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=f"debug_{PROFILE_ID}_{ts}.png")
        return

    round_number = 0
    while True:
        round_number += 1
        log.info("─" * 50)
        log.info(f"🔄 Round #{round_number} — profile '{profile.label}'")

        commented_this_round = 0
        seen_urns: set[str] = set()

        post_containers = await page.locator('div[data-urn^="urn:li:activity:"]').all()
        candidates = []
        for post in post_containers:
            urn = await post.get_attribute("data-urn")
            if urn and not is_already_commented(urn) and urn not in seen_urns:
                seen_urns.add(urn)
                candidates.append((urn, post))

        log.info(f"📊 {len(post_containers)} posts visible, {len(candidates)} unseen")

        for urn, post in candidates:
            if commented_this_round >= max_cpr:
                log.info(f"🛑 Max comments/round reached ({max_cpr})")
                break
            if is_already_commented(urn):
                continue
            try:
                posted = await _process_post(page, post, urn, min_age, max_age, persona, provider, max_cpr, commented_this_round)
                if posted:
                    commented_this_round += 1
                    wait_secs = random.uniform(18, 38)
                    log.info(f"⏳ Waiting {wait_secs:.1f}s...")
                    await asyncio.sleep(wait_secs)
            except Exception as e:
                log.error(f"❌ Error on {urn[:50]}: {e}", exc_info=True)
                continue

        log.info(f"✅ Round #{round_number} done — {commented_this_round} comments")
        await page.evaluate("window.scrollBy(0, 700)")
        await asyncio.sleep(random.randint(25, 45))
        log.info("😴 Sleeping 30 minutes before next round...")
        await asyncio.sleep(30 * 60)


async def run():
    try:
        profile = LinkedInProfile.objects.get(pk=PROFILE_ID)
    except LinkedInProfile.DoesNotExist:
        log.error(f"❌ Profile {PROFILE_ID} not found in DB — exiting")
        return

    global _gemini_keys
    _gemini_keys = profile.get_gemini_keys()

    email    = decrypt(profile.li_email)
    password = decrypt(profile.li_password)
    provider = profile.llm_provider
    persona  = profile.persona_prompt
    min_age  = profile.min_post_age_minutes
    max_age  = profile.max_post_age_minutes
    max_cpr  = profile.max_comments_per_round

    mode = f"TARGETED → {TARGET_LABEL}" if TARGET_URL else "FEED"
    log.info("=" * 60)
    log.info(f"🚀 Bot starting — profile: {profile.label} (id={PROFILE_ID})")
    log.info(f"   Mode          : {mode}")
    log.info(f"   LLM provider  : {provider}")
    log.info(f"   Post age range: {min_age}–{max_age} min")
    log.info(f"   Max/round     : {max_cpr}")
    log.info(f"   Gemini keys   : {len(_gemini_keys)}")
    log.info("=" * 60)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1280, "height": 800},
                user_agent=UA,
            )
            log.info("✅ Loaded saved LinkedIn session")
        else:
            log.warning("⚠️  No session file found — attempting login (may fail on headless)")
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=UA,
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")
            await page.fill('input[name="session_key"]', email)
            await page.fill('input[name="session_password"]', password)
            await page.click('button[type="submit"]')
            log.info("⏳ Waiting 15s after login...")
            await page.wait_for_timeout(15000)
            await context.storage_state(path=STATE_FILE)
            log.info(f"✅ Session saved → {STATE_FILE}")

        page = await context.new_page()

        if TARGET_URL:
            await _run_targeted(page, profile, persona, provider, min_age, max_age, max_cpr)
        else:
            await _run_feed(page, profile, persona, provider, min_age, max_age, max_cpr)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
