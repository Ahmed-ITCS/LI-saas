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
# Ensure project root is importable when running as "python bot/runner.py".
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# This runner is an async script that intentionally performs Django ORM calls.
# Allow ORM access from this async context for this subprocess only.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "libot.settings")
django.setup()

from accounts.models import LinkedInProfile, CommentLog
from accounts.crypto import decrypt

import linkedin_feed

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE   = os.environ.get("BOT_LOG_FILE", "bot.log")
PROFILE_ID = int(os.environ.get("BOT_PROFILE_ID", "0"))
STATE_FILE = os.environ.get("BOT_STATE_FILE", "state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger(__name__)


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


LINKEDIN_HEADLESS = _truthy_env("LINKEDIN_HEADLESS", "true")
LINKEDIN_WAIT_NETWORK_IDLE = _truthy_env("LINKEDIN_WAIT_NETWORK_IDLE", "true")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

VIEWPORT_W, VIEWPORT_H = 1365, 900
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_CHROME_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
)
_ANTIDETECT_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

# ── Gemini key rotation ───────────────────────────────────────────────────────
_gemini_key_idx = 0
_gemini_keys: list[str] = []

def current_gemini_key() -> str | None:
    if not _gemini_keys:
        return None
    # After the last key hits rate limits, rotate_gemini_key() leaves the index
    # at len(_gemini_keys); indexing would raise IndexError.
    if _gemini_key_idx < 0 or _gemini_key_idx >= len(_gemini_keys):
        return None
    return _gemini_keys[_gemini_key_idx]

def rotate_gemini_key() -> str | None:
    global _gemini_key_idx
    _gemini_key_idx += 1
    if _gemini_key_idx >= len(_gemini_keys):
        log.error("🔴 All Gemini API keys exhausted")
        _gemini_key_idx = len(_gemini_keys)  # stable sentinel: current_gemini_key() returns None
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
    normalized = (text or "").strip()
    if not normalized:
        return False

    h = _content_hash(text)
    if h in _commented_hashes:
        return True

    # Use stricter duplicate detection to avoid false positives on common post
    # prefixes like "I’m excited to share...".
    exists = CommentLog.objects.filter(
        profile_id=PROFILE_ID,
        post_text=normalized[:300],
    ).exists()
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
    if provider != "gemini":
        log.error(f"Unsupported LLM provider {provider!r} — only gemini is supported")
        return ""

    try:
        from google import genai
    except ImportError:
        log.error("google-genai not installed — cannot generate comments")
        return ""

    try:
        from google.api_core.exceptions import ResourceExhausted
    except ImportError:
        # google-genai works without google-api-core in some envs;
        # keep Gemini enabled and use message-based 429 detection below.
        ResourceExhausted = None

    prompt = (
        f"{persona} "
        "Write a short (exactly 1-2 sentences), professional, human-sounding LinkedIn comment. "
        "Add genuine value or insight. No emojis. No hashtags. Sound natural, not corporate."
    )
    full_prompt = f"{prompt}\n\nPost:\n{post_text[:700]}"

    if not _gemini_keys:
        log.error("No Gemini API keys configured — cannot generate comments")
        return ""

    global _gemini_key_idx
    # After the last key is rate-limited, rotate_gemini_key() leaves the index at
    # len(keys). Without resetting, every later post skips the API entirely
    # (current_gemini_key() is None) and only logs the generic failure below.
    if _gemini_key_idx >= len(_gemini_keys):
        log.info(
            "↩️ Gemini key index was past end (keys were exhausted earlier); "
            "retrying from first key (quotas may have recovered)"
        )
        _gemini_key_idx = 0

    attempts = 0
    while attempts < len(_gemini_keys):
        key = current_gemini_key()
        if not key:
            log.error(
                "No Gemini API key available at current index — "
                "check gemini_keys on the profile"
            )
            break
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=full_prompt,
            )
            raw = (response.text or "").strip()
            if not raw:
                dbg: list[str] = []
                try:
                    pf = getattr(response, "prompt_feedback", None)
                    if pf is not None:
                        dbg.append(f"prompt_feedback={pf}")
                    cands = getattr(response, "candidates", None) or []
                    if cands:
                        fr = getattr(cands[0], "finish_reason", None)
                        if fr is not None:
                            dbg.append(f"finish_reason={fr}")
                        fm = getattr(cands[0], "finish_message", None)
                        if fm:
                            dbg.append(f"finish_message={fm!r}")
                except Exception:
                    pass
                suffix = f" — {'; '.join(dbg)}" if dbg else ""
                log.error(f"Gemini returned an empty comment{suffix}")
                break
            log.info(f"🤖 Gemini generated comment ({len(raw)} chars)")
            return raw
        except Exception as e:
            msg = str(e)
            upper_msg = msg.upper()
            is_rate_limited = (
                (ResourceExhausted is not None and isinstance(e, ResourceExhausted))
                or "RESOURCE_EXHAUSTED" in upper_msg
                or "RATE LIMIT" in upper_msg
                or "429" in msg
            )
            if is_rate_limited:
                log.warning(f"⚠️  Gemini key #{_gemini_key_idx + 1} rate-limited: {e}")
                if rotate_gemini_key() is None:
                    break
                attempts += 1
                continue
            log.error(f"❌ Gemini error: {e}")
            break

    log.error("Could not generate a comment (keys exhausted, errors, or empty response)")
    return ""

# ── Playwright helpers ────────────────────────────────────────────────────────
async def is_post_hydrated(post) -> bool:
    """
    True only if the resolved element is a real feed post card with comment UI.
    LinkedIn sometimes serves a stripped feed where only permalink anchors exist
    (no <article>/feed-shared-update wrappers); in that case the URN resolver
    falls back to a bare <a> element and we should NOT try to comment on it.
    """
    try:
        tag = (await post.evaluate("el => el.tagName")).lower()
    except Exception:
        return False
    if tag == "a":
        return False
    try:
        cbtn = post.locator(
            'button[aria-label="Comment"], '
            'button[aria-label*="Comment"][aria-expanded], '
            "button.comments-comment-box__open-button"
        ).first
        if await cbtn.count():
            return True
    except Exception:
        pass
    try:
        wrap = post.locator(
            'xpath=self::*[contains(@class,"feed-shared-update") '
            'or contains(@class,"update-components")]'
        ).first
        if await wrap.count():
            return True
    except Exception:
        pass
    return False


async def get_post_text(post) -> str:
    selectors = [
        ".feed-shared-update-v2__description .break-words",
        ".feed-shared-text .break-words",
        ".feed-shared-update-v2__description span[dir='ltr']",
        '[class*="feed-shared-update-v2__description"] .break-words',
        '[class*="feed-shared-update-v2__description"] span[dir="ltr"]',
        '[class*="update-components-text"] span[dir="ltr"]',
        ".update-components-text span[dir='ltr']",
        ".feed-shared-inline-show-more-text span[dir='ltr']",
        '[class*="feed-shared-inline-show-more-text"] span[dir="ltr"]',
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

    # ── Hydration check ───────────────────────────────────────────────────────
    # Skip without persisting if the URN resolved to a bare anchor / unhydrated
    # placeholder. Marking it would block retries after the feed re-renders.
    if not await is_post_hydrated(post):
        log.info(f"⏭️  {short} — post not hydrated in feed (no card/comment UI), skipping")
        return False

    # ── Age filter ────────────────────────────────────────────────────────────
    # min_age / max_age may be None — meaning "no bound on this side".
    age = await get_post_age_minutes(post)
    if age is None:
        # LinkedIn frequently renders relative time in dynamic/locale-specific
        # structures; don't hard-skip a valid candidate just because age parser
        # couldn't extract it.
        log.info(f"ℹ️  {short} — could not parse age, continuing without age filter")
    else:
        if min_age is not None and age < min_age:
            log.info(f"⏭️  {short} — too fresh ({age}m < {min_age}m), skipping")
            return False
        if max_age is not None and age > max_age:
            log.info(f"⏭️  {short} — too old ({age}m > {max_age}m), skipping")
            mark_as_commented(urn, "", "", age)
            return False
        log.info(f"🕐 Post age: {age}m ✓")

    # ── Text ─────────────────────────────────────────────────────────────────
    post_text = await get_post_text(post)
    if not post_text:
        # Don't persist — text may simply not be hydrated yet. Retry next round.
        log.info(f"⏭️  {short} — no text extracted, skipping (will retry later)")
        return False

    if is_content_already_commented(post_text):
        log.info(f"⏭️  {short} — duplicate content, skipping")
        mark_as_commented(urn, post_text, "", age)
        return False

    # ── Generate ──────────────────────────────────────────────────────────────
    log.info(f"✍️  Generating comment for {short}")
    comment = await generate_comment(post_text, persona, provider)
    if not (comment or "").strip():
        log.warning(f"⏭️  {short} — no comment generated, skipping")
        return False
    log.info(f'💬 Comment: "{comment}"')

    # ── Interact ──────────────────────────────────────────────────────────────
    await post.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)

    cbtn = post.locator(
        'button[aria-label="Comment"], '
        'button[aria-label*="Comment"][aria-expanded], '
        "button.comments-comment-box__open-button"
    ).first
    await cbtn.click(timeout=8000)
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


async def _open_detail_view(page, urn: str) -> bool:
    """
    Navigate to /feed/update/<urn>/ as a fallback when the URN didn't hydrate
    on the feed. Returns True iff the detail page actually loaded a post node.

    Hardened against LinkedIn's redirect-loop / domcontentloaded edge cases.
    """
    from playwright.async_api import Error as PlaywrightError

    detail_url = linkedin_feed.activity_detail_url(urn)
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightError as e:
        msg = str(e)
        if "ERR_TOO_MANY_REDIRECTS" in msg or "ERR_ABORTED" in msg:
            log.warning(f"⚠️  detail nav redirect/abort for {urn[:50]}: {msg.splitlines()[0]}")
        else:
            log.warning(f"⚠️  detail nav playwright error for {urn[:50]}: {msg.splitlines()[0]}")
        return False
    except Exception as e:
        log.warning(f"⚠️  detail nav unexpected error for {urn[:50]}: {e}")
        return False

    await linkedin_feed.dismiss_sticky_alerts(page)
    try:
        await page.wait_for_selector(
            'article, [class*="feed-shared-update"], '
            'button[aria-label="Comment"], button.comments-comment-box__open-button',
            timeout=15000,
        )
    except Exception:
        log.info(f"⏭️  detail page didn't render a post for {urn[:50]}")
        return False
    await page.wait_for_timeout(1500)
    return True


async def _run_feed_attempt_urn(
    page,
    urn: str,
    min_age: int,
    max_age: int,
    persona: str,
    provider: str,
    max_cpr: int,
    commented_this_round: int,
) -> bool:
    """Locate post on feed or /feed/update/ permalink, run comment flow, restore feed if needed."""
    opened_detail = False
    try:
        post = await linkedin_feed.scoped_post_for_urn(page, urn)
        feed_hydrated = (await post.count()) > 0 and await is_post_hydrated(post)

        if not feed_hydrated:
            # Detail-page fallback: try opening /feed/update/<urn>/ directly.
            log.info(f"🔗 {urn[:50]}… not hydrated on feed — trying detail page")
            if not await _open_detail_view(page, urn):
                return False
            opened_detail = True
            post = await linkedin_feed.scoped_post_on_detail_view(page, urn)
            if not await post.count():
                log.info(f"⏭️  {urn[:50]}… post node not found on detail page — skipping")
                return False

        return await _process_post(
            page,
            post,
            urn,
            min_age,
            max_age,
            persona,
            provider,
            max_cpr,
            commented_this_round,
        )
    finally:
        if opened_detail:
            try:
                await linkedin_feed.return_to_feed_home(page, linkedin_feed.wait_for_feed_ready)
            except Exception as e:
                log.warning(f"⚠️  return-to-feed failed: {e}")


async def _run_targeted(page, profile, persona, provider, min_age, max_age, max_cpr):
    """Visit a specific person's activity page and comment on their posts (one-shot)."""
    log.info("=" * 60)
    log.info(f"🎯 TARGETED MODE — {TARGET_LABEL}")
    log.info(f"   URL: {TARGET_URL}")
    log.info("=" * 60)

    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=75000)
    await linkedin_feed.dismiss_sticky_alerts(page)
    try:
        await page.wait_for_selector(
            linkedin_feed.URN_TAGS,
            timeout=20000,
        )
        log.info("✅ Target activity page loaded")
    except Exception:
        log.error("❌ Could not load target activity page — wrong URL or not logged in")
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=f"debug_targeted_{PROFILE_ID}_{ts}.png")
        return

    try:
        if LINKEDIN_WAIT_NETWORK_IDLE:
            await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass

    # Scroll to virtualize additional posts
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.5)

    post_containers = await page.locator(linkedin_feed.URN_TAGS).all()
    candidates = []
    seen_urns: set[str] = set()
    for post in post_containers:
        urn = await post.get_attribute("data-urn") or await post.get_attribute("data-activity-urn")
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
    """Standard mode: resilient home-feed URN harvest + optional /feed/update/ fallback."""
    log.info("📡 Navigating to LinkedIn feed")
    await linkedin_feed.initial_feed_navigation(
        page,
        wait_network_idle=LINKEDIN_WAIT_NETWORK_IDLE,
        screenshot_path_prefix=f"debug_{PROFILE_ID}",
    )

    round_number = 0
    while True:
        round_number += 1
        log.info("─" * 50)
        log.info(f"🔄 Round #{round_number} — profile '{profile.label}'")

        commented_this_round = 0

        all_urns = await linkedin_feed.collect_feed_activity_urns(
            page,
            link_wait_network_idle=LINKEDIN_WAIT_NETWORK_IDLE,
            is_already_commented=is_already_commented,
        )

        for urn in all_urns:
            if commented_this_round >= max_cpr:
                log.info(f"🛑 Max comments/round reached ({max_cpr})")
                break
            if is_already_commented(urn):
                continue
            try:
                posted = await _run_feed_attempt_urn(
                    page,
                    urn,
                    min_age,
                    max_age,
                    persona,
                    provider,
                    max_cpr,
                    commented_this_round,
                )
                if posted:
                    commented_this_round += 1
                    wait_secs = random.uniform(18, 38)
                    log.info(f"⏳ Waiting {wait_secs:.1f}s...")
                    await asyncio.sleep(wait_secs)
            except Exception as e:
                log.error(f"❌ Error on {urn[:50]}: {e}", exc_info=True)
                continue

        log.info(f"✅ Round #{round_number} done — {commented_this_round} comments")
        try:
            await page.evaluate("window.scrollBy(0, 700)")
        except Exception as e:
            # Page may still be transitioning after a failed detail navigation.
            log.warning("⚠️  Feed scroll skipped due to navigation state: %s", e)
            try:
                await page.goto(linkedin_feed.FEED_HOME, wait_until="domcontentloaded", timeout=90000)
                await linkedin_feed.wait_for_feed_ready(page)
            except Exception as nav_e:
                log.warning("⚠️  Feed recovery navigation failed: %s", nav_e)
        await asyncio.sleep(random.randint(25, 45))
        log.info("😴 Sleeping 30 minutes before next round...")
        await asyncio.sleep(30 * 60)

        log.info("🔄 Refreshing feed page...")
        await page.goto(linkedin_feed.FEED_HOME, wait_until="domcontentloaded", timeout=90000)
        try:
            await linkedin_feed.wait_for_feed_ready(page)
            log.info("✅ Feed refreshed successfully")
        except Exception:
            log.error("❌ Feed reload failed — will retry next round")


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
    age_lo = min_age if min_age is not None else "any"
    age_hi = max_age if max_age is not None else "any"
    log.info(f"   Post age range: {age_lo}–{age_hi} min")
    log.info(f"   Max/round     : {max_cpr}")
    log.info(f"   Gemini keys   : {len(_gemini_keys)}")
    if provider == "gemini":
        log.info(f"   Gemini model  : {GEMINI_MODEL}")
    log.info("   Chromium      : %s (trimmed automation fingerprints)", "headless" if LINKEDIN_HEADLESS else "headed")
    if LINKEDIN_WAIT_NETWORK_IDLE:
        log.info("   Feed settle   : waits for networkidle after /feed/")
    log.info("=" * 60)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch_kw: dict = {
            "headless": LINKEDIN_HEADLESS,
            "args": list(_CHROME_ARGS),
            "ignore_default_args": ["--enable-automation"],
        }
        chrome_ch = os.environ.get("LINKEDIN_CHROME_CHANNEL", "").strip()
        if chrome_ch:
            launch_kw["channel"] = chrome_ch
        browser = await p.chromium.launch(**launch_kw)

        _ctx_kw = dict(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            user_agent=CHROME_USER_AGENT,
            locale="en-US",
            timezone_id=os.environ.get("LINKEDIN_TZ", "UTC"),
            permissions=["notifications"],
            java_script_enabled=True,
        )

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                **_ctx_kw,
            )
            await context.add_init_script(_ANTIDETECT_INIT)
            log.info("✅ Loaded saved LinkedIn session")
        else:
            log.warning("⚠️  No session file found — attempting login (may fail on headless)")
            context = await browser.new_context(
                **_ctx_kw,
            )
            await context.add_init_script(_ANTIDETECT_INIT)
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
