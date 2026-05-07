"""
Resilient LinkedIn home-feed URN harvesting and card scoping (Playwright).

Extracted so bot/runner.py stays Django-focused. DOM churn on LinkedIn means we
combine multiple strategies: anchors, encoded URNs in HTML, article scans, cards.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote, unquote

log = logging.getLogger(__name__)

FEED_HOME = "https://www.linkedin.com/feed/"

FEED_READY_SELECTOR = (
    '[data-urn^="urn:li:activity:"], '
    '[data-activity-urn^="urn:li:activity:"], '
    '[data-urn^="urn:li:ugcPost:"], '
    '[data-activity-urn^="urn:li:ugcPost:"], '
    "div.feed-shared-update-v2, article.feed-shared-update-v2, "
    '[class*="feed-shared-update"], [class*="feed-shared-update-v2"], '
    'a[href*="/feed/update/"], a[href*="feed/update"]'
)

URN_TAGS = (
    '[data-urn^="urn:li:activity:"], [data-activity-urn^="urn:li:activity:"], '
    '[data-urn^="urn:li:ugcPost:"], [data-activity-urn^="urn:li:ugcPost:"]'
)

CARD_SELECTOR = (
    'div.feed-shared-update-v2, article.feed-shared-update-v2, '
    '[class*="feed-shared-update-v2"], [class*="feed-shared-update"]'
)

ACTIVITY_PREFIX = ("urn:li:activity:", "urn:li:ugcPost:")

URN_LI_CHUNK = re.compile(r"urn:li:(activity|ugcPost):(\d+)", re.I)
_FEED_NUM_ID = re.compile(r"/feed/update/(\d{12,24})(?:[/\?]|$)", re.I)
_POSTS_TAIL_ID = re.compile(r"/posts/[^\s\"'<>]+-(\d{12,})(?:\?|[/#\"]|$)", re.I)

_LINK_HARVEST_JS = r"""
() => {
  const sel =
    'a[href*="feed/update"], ' +
    'a[href*="/posts/"], ' +
    'a[href*="urn%3Ali%3Aactivity"], ' +
    'a[href*="li%3Aactivity"], ' +
    'a[href*="ugcPost"], ' +
    'a[href*="activity%3A"]';
  const join = [...document.querySelectorAll(sel)].slice(0, 1800);
  const out = [];
  for (const a of join) {
    const href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('#')) continue;
    if (a.closest('nav[aria-label*="Primary"], header.global-nav, .global-nav')) continue;
    if (a.closest('footer')) continue;
    out.push(href);
  }
  return out;
}
"""

def _activity_urns_from_dom_js() -> str:
    return """
() => {
  function canon(g1, num) {
    return g1.toLowerCase().startsWith("act") ? `urn:li:activity:${num}` : `urn:li:ugcPost:${num}`;
  }
  function pullSlice(slice) {
    const out = [];
    const r1 = /urn:li:(activity|ugcPost):(\\d+)/gi;
    const r2 = /urn%3Ali%3A(activity|ugcPost)%3A(\\d+)/gi;
    let m;
    while ((m = r1.exec(slice)) !== null) out.push(canon(m[1], m[2]));
    while ((m = r2.exec(slice)) !== null) out.push(canon(m[1], m[2]));
    return out;
  }
  const h = document.documentElement.outerHTML;
  const win = 650000;
  const overlap = 90000;
  const seen = new Set();
  const merged = [];
  for (let i = 0; i < h.length && merged.length < 400; i += win) {
    const slice = h.slice(Math.max(0, i - overlap), Math.min(h.length, i + win + overlap));
    for (const u of pullSlice(slice)) {
      if (!seen.has(u)) {
        seen.add(u);
        merged.push(u);
        if (merged.length >= 400) break;
      }
    }
  }
  return merged;
}
"""


ACTIVITY_URNS_FROM_DOM_JS = _activity_urns_from_dom_js()


def _looks_like_activity_urn(val: str | None) -> bool:
    return bool(val) and val.startswith(ACTIVITY_PREFIX)


def _canon_li_urn(m: re.Match[str]) -> str:
    kind, num = m.group(1), m.group(2)
    if kind.lower().startswith("activity"):
        return f"urn:li:activity:{num}"
    return f"urn:li:ugcPost:{num}"


def _iter_urns_in_string(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        raw = unquote(raw)
    except Exception:
        pass
    urns = [_canon_li_urn(m) for m in URN_LI_CHUNK.finditer(raw)]
    for m in _FEED_NUM_ID.finditer(raw):
        urns.append(f"urn:li:activity:{m.group(1)}")
    for m in _POSTS_TAIL_ID.finditer(raw):
        urns.append(f"urn:li:activity:{m.group(1)}")
    return urns


async def dismiss_sticky_alerts(page) -> None:
    for sel in (
        'button[aria-label*="Dismiss"]',
        '[data-test-global-alert-dismiss]',
        "button.artdeco-global-alert__dismiss",
    ):
        btn = page.locator(sel).first
        if not await btn.count():
            continue
        try:
            await btn.click(timeout=2000)
            await page.wait_for_timeout(500)
        except Exception:
            continue


async def activity_urns_from_markup(page) -> list[str]:
    try:
        raw = await page.evaluate(ACTIVITY_URNS_FROM_DOM_JS)
    except Exception:
        raw = []
    out: list[str] = []
    if not isinstance(raw, list):
        return []
    for u in raw:
        if isinstance(u, str) and _looks_like_activity_urn(u):
            out.append(u)
    return out


async def activity_urns_from_article_roots(page) -> list[str]:
    chunks = await page.evaluate(
        r"""
      () =>
        [...document.querySelectorAll('main article, [role="main"] article, article')]
          .filter((a) => !a.closest('nav[aria-label*="Primary"], footer.global-footer'))
          .slice(0, 56)
          .map((el) => el.outerHTML.slice(0, 220000))
    """
    )
    seen: dict[str, None] = {}
    ordered: list[str] = []
    if not isinstance(chunks, list):
        return []
    for blob in chunks:
        if not isinstance(blob, str):
            continue
        for u in _iter_urns_in_string(blob):
            if _looks_like_activity_urn(u) and u not in seen:
                seen[u] = None
                ordered.append(u)
    return ordered


async def hydrate_feed_timeline(page, *, passes: int = 12) -> None:
    await page.keyboard.press("Home")
    await page.wait_for_timeout(400)
    for i in range(passes):
        await page.mouse.wheel(0, 850 + (i % 4) * 120)
        await page.wait_for_timeout(420 + (i % 3) * 80)
    await page.keyboard.press("Home")
    await page.wait_for_timeout(520)


async def activity_urns_from_page_links(page) -> list[str]:
    urls = await page.evaluate(_LINK_HARVEST_JS)

    seen_flag: dict[str, None] = {}
    ordered: list[str] = []
    for h in urls or []:
        if not isinstance(h, str):
            continue
        for u in _iter_urns_in_string(h):
            if _looks_like_activity_urn(u) and u not in seen_flag:
                seen_flag[u] = None
                ordered.append(u)
    return ordered


async def dom_has_encoded_activity_updates(page) -> bool:
    if await activity_urns_from_page_links(page):
        return True
    return await page.evaluate(
        """
      () => {
        const h = document.documentElement.outerHTML;
        const head = h.slice(0, Math.min(h.length, 1800000));
        const tail = h.length > 2000000 ? h.slice(-980000) : "";
        const blob = head + tail;
        return (
          /urn:li:(activity|ugcPost):\\d+/i.test(blob) ||
          /urn%3Ali%3A(activity|ugcPost)%3A\\d+/i.test(blob)
        );
      }
    """
    )


async def _urn_on_element(el) -> str | None:
    for attr in ("data-urn", "data-activity-urn"):
        v = await el.get_attribute(attr)
        if _looks_like_activity_urn(v):
            return v
    inner = el.locator(URN_TAGS).first
    if await inner.count():
        return await _urn_on_element(inner)
    return None


async def scoped_post_for_urn(page, urn: str):
    empty = page.locator("#linkedin-commenter-scope-missing-xxxx").first

    attr_hit = page.locator(f'[data-urn="{urn}"], [data-activity-urn="{urn}"]').first
    if await attr_hit.count():
        card = attr_hit.locator(
            'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update-v2") or '
            'contains(@class,"feed-shared-update")][1]'
        ).first
        if await card.count():
            return card
        art = attr_hit.locator("xpath=ancestor-or-self::article[1]").first
        if await art.count():
            return art
        return attr_hit

    nid = urn.rsplit(":", 1)[-1]
    if not nid.isdigit():
        return empty

    picked = empty
    for scope_sl in ("main", '[role="main"]'):
        root = page.locator(scope_sl).first
        if not await root.count():
            continue
        lk = root.locator(f"a[href*='{nid}']").first
        if await lk.count():
            picked = lk
            break

    if not await picked.count():
        for pat in (
            f'a[href*="/feed/update/"][href*="{nid}"]',
            f'a[href*="feed/update"][href*="{nid}"]',
            f'a[href*="{nid}"]',
        ):
            alt = page.locator(pat).first
            if await alt.count():
                try:
                    if await alt.evaluate(
                        """(el) => !!el.closest('nav[aria-label*="Primary"],footer,header.global-nav,.global-nav')"""
                    ):
                        continue
                except Exception:
                    pass
                picked = alt
                break

    if not await picked.count():
        return empty

    art_up = picked.locator("xpath=ancestor-or-self::article[1]").first
    if await art_up.count():
        return art_up
    wrap = picked.locator(
        'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update") '
        'or contains(@class,"update-components")][1]'
    ).first
    if await wrap.count():
        return wrap
    return picked


def activity_detail_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{quote(urn, safe='')}/"


async def scoped_post_on_detail_view(page, urn: str):
    empty = page.locator("#linkedin-commenter-scope-missing-xxxx").first
    nid = urn.rsplit(":", 1)[-1]

    attr_hit = page.locator(
        f'[data-urn="{urn}"], [data-activity-urn="{urn}"], '
        f'[data-urn*="activity:{nid}"], [data-activity-urn*="activity:{nid}"], '
        f'[data-urn*="ugcPost:{nid}"], [data-activity-urn*="ugcPost:{nid}"]'
    ).first
    if await attr_hit.count():
        art = attr_hit.locator("xpath=ancestor-or-self::article[1]").first
        if await art.count():
            return art
        wrap = attr_hit.locator(
            'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update")][1]'
        ).first
        if await wrap.count():
            return wrap
        return attr_hit

    with_comment = (
        page.locator("article")
        .filter(
            has=page.locator(
                'button[aria-label="Comment"], '
                'button[aria-label*="Comment"][aria-expanded], '
                "button.comments-comment-box__open-button"
            )
        )
        .first
    )
    if await with_comment.count():
        return with_comment

    fb_wrap = page.locator('[class*="feed-shared-update"]').first
    if await fb_wrap.count():
        return fb_wrap

    for scope in ("main article", '[role="main"] article', "article.relative"):
        a = page.locator(scope).first
        if await a.count():
            return a

    return empty


async def return_to_feed_home(page, wait_for_feed_ready_fn) -> None:
    try:
        await page.goto(FEED_HOME, wait_until="domcontentloaded", timeout=75000)
        await page.wait_for_timeout(2000)
        try:
            await wait_for_feed_ready_fn(page, timeout_ms=40000)
        except Exception as e:
            log.warning("⚠️  /feed/ nav ok but readiness soft-fail: %s", e)
    except Exception as e:
        log.warning("⚠️  RETURN_TO_FEED_NAV %s", e)


async def collect_feed_activity_urns(
    page,
    *,
    link_wait_network_idle: bool,
    is_already_commented: Callable[[str], bool],
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def append(u: str | None):
        if u and u not in seen and not is_already_commented(u):
            seen.add(u)
            out.append(u)

    await dismiss_sticky_alerts(page)

    if link_wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=38000)
        except Exception:
            pass

    await hydrate_feed_timeline(page)

    anchors = await activity_urns_from_page_links(page)
    for u in anchors:
        append(u)

    embedded = await activity_urns_from_markup(page)
    for u in embedded:
        append(u)

    from_articles = await activity_urns_from_article_roots(page)
    for u in from_articles:
        append(u)

    cards = await page.locator(CARD_SELECTOR).all()
    iterable = cards or await page.locator(URN_TAGS).all()
    for node in iterable:
        append(await _urn_on_element(node))

    ncards = await page.locator(CARD_SELECTOR).count()
    nun = await page.locator(URN_TAGS).count()
    n_article = await page.locator("article").count()
    if not out:
        log.warning(
            "⚠️  Feed harvest empty · <article>≈%s · cards(class)≈%s · data-attrs≈%s · "
            "anchors≈%s · dom-regex≈%s · articles-scan≈%s — "
            "re-export session state or try LINKEDIN_CHROME_CHANNEL=chrome",
            n_article,
            ncards,
            nun,
            len(anchors),
            len(embedded),
            len(from_articles),
        )
    else:
        log.info(
            "🔗 Resolved %s unseen URNs (anchors:%s markup:%s articles-scan:%s · "
            "<article>≈%s · cards(class)≈%s)",
            len(out),
            len(anchors),
            len(embedded),
            len(from_articles),
            n_article,
            ncards,
        )

    return out


async def wait_for_feed_ready(page, timeout_ms: float = 55000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_exc: BaseException | None = None
    scroll_n = 0
    while time.monotonic() < deadline:
        slot_ms = (deadline - time.monotonic()) * 1000.0
        if slot_ms < 300:
            break
        slice_ms = min(4500.0, slot_ms)
        try:
            await page.wait_for_selector(FEED_READY_SELECTOR, timeout=slice_ms)
            return
        except Exception as e:
            last_exc = e

        if await dom_has_encoded_activity_updates(page):
            log.info("✅ Feed detected via permalinks (activity URN in anchor href)")
            return

        await page.mouse.wheel(0, 900)
        scroll_n += 1
        await page.wait_for_timeout(450 + (scroll_n % 4) * 150)
        if scroll_n % 6 == 0:
            await page.keyboard.press("End")
            await page.wait_for_timeout(400)

        if await dom_has_encoded_activity_updates(page):
            log.info("✅ Feed detected via permalinks after scroll")
            return

    if last_exc:
        raise last_exc
    raise TimeoutError("Timed out waiting for feed content")


async def initial_feed_navigation(
    page, *, wait_network_idle: bool, screenshot_path_prefix: str
) -> None:
    try:
        await page.goto(FEED_HOME, wait_until="domcontentloaded", timeout=90000)
    except Exception as e:
        # LinkedIn sometimes loops redirects for stale/challenged sessions.
        # Fall back to home so we can continue diagnostics instead of crashing.
        if "ERR_TOO_MANY_REDIRECTS" in str(e):
            log.warning("⚠️  Feed redirect loop detected — falling back to linkedin.com home")
            try:
                await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=90000)
            except Exception as e2:
                log.error("❌ Fallback navigation failed: %s", e2)
                return
        else:
            raise

    await dismiss_sticky_alerts(page)
    await page.wait_for_timeout(3500)
    if wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass

    try:
        await wait_for_feed_ready(page)
        log.info("✅ Feed loaded")
    except Exception:
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(1800)
        try:
            await wait_for_feed_ready(page, timeout_ms=15000)
            log.info("✅ Feed loaded after scroll")
        except Exception:
            try:
                n_raw = await page.evaluate(
                    """() => document.querySelectorAll('a[href*="feed/update"]').length"""
                )
                n_harvest = len(await activity_urns_from_page_links(page))
            except Exception:
                n_raw = -1
                n_harvest = -1
            log.error(
                "❌ Feed markers not found — refresh browser session state "
                "(log in locally then replace storage JSON) or clear security checkpoints."
            )
            log.error("   diagnostics: generic feed/update anchors=%s · parsed URNs=%s", n_raw, n_harvest)
            path = f"{screenshot_path_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=path)
            log.error("📸 Screenshot: %s", path)
