"""
Google Maps scraper.

Same architecture as the PagesJaunes scraper:
  * Browser layer (`GoogleMapsScraper`) — persistent Chromium, retry, block
    detection, anti-bot hardening.
  * Extraction layer (`_extract_from_detail_panel`) — pure selectors with
    fallback chains. One place to edit when Google ships a redesign.
  * Output layer — reuses `pagesjaunes.BusinessRecord` + `write_csv` so both
    sources produce identical CSV schemas and can be concatenated freely.

Google Maps loads results lazily into a virtualised scrollable feed, then the
real detail (phone, address, website) lives in a side panel that only appears
after clicking a result card. So the scraper:
  1. Opens https://www.google.com/maps/search/{keyword} (+ optional location).
  2. Scrolls the feed until it sees "Vous êtes arrivé(e) à la fin de la liste"
     / "You've reached the end of the list", or hits `max_results`.
  3. Clicks each result card, waits for the detail panel, extracts fields.
  4. Dedup by phone (fallback to name+address).

Usage:
    from scraper.google_maps import GoogleMapsScraper, GMapsConfig
    from scraper.pagesjaunes import write_csv

    cfg = GMapsConfig(keyword="plombier Paris", max_results=40)
    with GoogleMapsScraper(cfg) as s:
        records = s.run()
    write_csv(records, keyword=cfg.keyword)
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from .pagesjaunes import (
    BusinessRecord,
    USER_AGENTS,
    VIEWPORTS,
    normalize_phone_fr,
    split_address,
)

logger = logging.getLogger("scraper.google_maps")

ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / "gmaps_session"

GMAPS_BASE = "https://www.google.com/maps"


@dataclass
class GMapsConfig:
    keyword: str  # e.g. "plombier Paris" or "coffee shop Brooklyn"
    location: str = ""  # optional — appended to keyword if set
    max_results: int = 40
    headless: bool = True
    min_action_delay: float = 0.6
    max_action_delay: float = 1.6
    page_timeout_ms: int = 30_000
    max_scroll_rounds: int = 25
    language: str = "fr"  # hl= param
    proxies: list[str] = field(default_factory=list)


class GMapsBlocked(RuntimeError):
    pass


# ----------------------------- helpers ------------------------------------ #

def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def _clean(t: str | None) -> str:
    return re.sub(r"\s+", " ", t).strip() if t else ""


def build_url(cfg: GMapsConfig) -> str:
    q = cfg.keyword.strip()
    if cfg.location:
        q = f"{q} {cfg.location.strip()}"
    return f"{GMAPS_BASE}/search/{quote_plus(q)}/?hl={cfg.language}"


def _coords_from_url(url: str) -> tuple[str, str]:
    # URL contains "!3d{lat}!4d{lng}" once a place is selected.
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# --------------------------- extraction layer ----------------------------- #

FEED_SELECTOR = '[role="feed"]'
CARD_SELECTOR = 'a.hfpxzc'  # one per result; aria-label holds the name

NAME_SELECTORS = ["h1.DUwDvf", "h1"]
ADDRESS_SELECTORS = ['button[data-item-id="address"]',
                     'button[aria-label^="Address" i]',
                     'button[aria-label^="Adresse" i]']
PHONE_SELECTORS = ['button[data-item-id^="phone:tel:"]',
                   'button[aria-label^="Phone" i]',
                   'button[aria-label^="Téléphone" i]']
WEBSITE_SELECTORS = ['a[data-item-id="authority"]',
                     'a[aria-label^="Website" i]',
                     'a[aria-label^="Site Web" i]',
                     'a[aria-label^="Site web" i]']
CATEGORY_SELECTORS = ['button[jsaction*="category"]', '.DkEaL']
RATING_SELECTORS = ['div.F7nice span[aria-hidden="true"]',
                    '.F7nice',
                    'span.ceNzKf[aria-label]']
REVIEWS_SELECTORS = ['div.F7nice span[aria-label*="review" i]',
                     'div.F7nice span[aria-label*="avis" i]']


def _first_text(node, selectors):
    for sel in selectors:
        try:
            loc = node.locator(sel).first
            if loc.count() > 0:
                t = loc.inner_text(timeout=2000)
                if t and t.strip():
                    return _clean(t)
        except Exception:
            continue
    return ""


def _first_attr(node, selectors, attr):
    for sel in selectors:
        try:
            loc = node.locator(sel).first
            if loc.count() > 0:
                v = loc.get_attribute(attr, timeout=2000)
                if v:
                    return v.strip()
        except Exception:
            continue
    return ""


def _extract_from_detail_panel(page: Page) -> BusinessRecord:
    rec = BusinessRecord()
    rec.business_name = _first_text(page, NAME_SELECTORS)

    raw_addr = _first_text(page, ADDRESS_SELECTORS)
    street, postal, city = split_address(raw_addr)
    rec.address = street
    rec.postal_code = postal
    rec.city = city

    raw_phone = _first_text(page, PHONE_SELECTORS)
    if not raw_phone:
        raw_phone = _first_attr(page, PHONE_SELECTORS, "aria-label")
        # aria-label is like "Phone: +33 1 23 45 67 89"
        raw_phone = re.sub(r"(?i)^(phone|téléphone)\s*:?\s*", "", raw_phone) if raw_phone else ""
    rec.phone = normalize_phone_fr(raw_phone) if raw_phone else ""

    site = _first_attr(page, WEBSITE_SELECTORS, "href")
    rec.website = site if _is_valid_url(site or "") else ""

    rec.category = _first_text(page, CATEGORY_SELECTORS)

    rating = _first_text(page, RATING_SELECTORS)
    # Sometimes returns "4,3" — keep as-is, just normalize comma → dot
    rec.rating = rating.replace(",", ".") if rating else ""

    raw_reviews = _first_text(page, REVIEWS_SELECTORS) or \
                  _first_attr(page, REVIEWS_SELECTORS, "aria-label")
    rec.review_count = re.sub(r"\D", "", raw_reviews) if raw_reviews else ""

    rec.business_url = page.url
    lat, lng = _coords_from_url(page.url)
    rec.latitude = lat
    rec.longitude = lng

    return rec


# ----------------------------- block detection ---------------------------- #

BLOCK_PATTERNS = [
    re.compile(r"unusual traffic", re.I),
    re.compile(r"trafic inhabituel", re.I),
    re.compile(r"recaptcha", re.I),
    re.compile(r"about this page", re.I),
]


def _detect_block(page: Page) -> str | None:
    try:
        if "sorry" in page.url.lower() or "consent.google" in page.url.lower():
            return f"blocked URL: {page.url}"
        body = page.inner_text("body", timeout=4000)[:5000]
    except Exception:
        return None
    for rx in BLOCK_PATTERNS:
        if rx.search(body):
            return f"matched {rx.pattern}"
    return None


# ----------------------------- browser layer ------------------------------ #

class GoogleMapsScraper:
    def __init__(self, config: GMapsConfig) -> None:
        self.cfg = config
        self._pw = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._proxy_idx = 0

    def __enter__(self) -> "GoogleMapsScraper":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _next_proxy(self) -> dict | None:
        if not self.cfg.proxies:
            return None
        url = self.cfg.proxies[self._proxy_idx % len(self.cfg.proxies)]
        self._proxy_idx += 1
        p = urlparse(url)
        proxy = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            proxy["username"] = p.username
        if p.password:
            proxy["password"] = p.password
        return proxy

    def start(self) -> None:
        SESSION_DIR.mkdir(exist_ok=True)
        self._pw = sync_playwright().start()
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)
        proxy = self._next_proxy()
        logger.info("Launching browser (headless=%s, ua=%s, vp=%s, proxy=%s)",
                    self.cfg.headless, ua, vp, bool(proxy))
        kwargs = dict(
            user_data_dir=str(SESSION_DIR),
            headless=self.cfg.headless,
            viewport=vp,
            user_agent=ua,
            locale=f"{self.cfg.language}-FR" if self.cfg.language == "fr" else self.cfg.language,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        if proxy:
            kwargs["proxy"] = proxy
        self._context = self._pw.chromium.launch_persistent_context(**kwargs)
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.page.set_default_timeout(self.cfg.page_timeout_ms)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._context = None
            self.page = None
            self._pw = None

    # -------- consent handling -------- #

    def _dismiss_consent(self) -> None:
        assert self.page is not None
        # Google consent.google.com shows "Tout accepter" / "Accept all".
        for sel in [
            'button:has-text("Tout accepter")',
            'button:has-text("Accept all")',
            'button:has-text("J\'accepte")',
            'button[aria-label*="Accept" i]',
            'form[action*="consent"] button',
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    _sleep(0.6, 1.4)
                    return
            except Exception:
                continue

    # -------- scrolling the feed -------- #

    def _scroll_feed_until_done(self, target_count: int) -> None:
        """Scroll the result feed until we've loaded `target_count` cards or
        reach the end-of-list marker."""
        assert self.page is not None
        feed = self.page.locator(FEED_SELECTOR).first
        if feed.count() == 0:
            return

        prev_count = -1
        rounds_without_progress = 0
        for round_idx in range(self.cfg.max_scroll_rounds):
            count = self.page.locator(CARD_SELECTOR).count()
            logger.debug("Scroll round %d: %d cards", round_idx, count)
            if count >= target_count:
                return
            # End-of-list marker
            try:
                if self.page.locator(
                    'span:has-text("Vous êtes arrivé"), '
                    'span:has-text("You\'ve reached the end")'
                ).count() > 0:
                    return
            except Exception:
                pass

            # Scroll the feed itself (not the window).
            try:
                feed.evaluate("el => el.scrollBy(0, el.clientHeight)")
            except Exception:
                pass
            _sleep(self.cfg.min_action_delay, self.cfg.max_action_delay)

            if count == prev_count:
                rounds_without_progress += 1
                if rounds_without_progress >= 3:
                    return
            else:
                rounds_without_progress = 0
            prev_count = count

    # -------- public entry point -------- #

    def run(self, on_progress: Callable[[dict], None] | None = None) -> list[BusinessRecord]:
        assert self.page is not None
        url = build_url(self.cfg)
        logger.info("GET %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout_ms)
        _sleep(1.0, 2.0)
        self._dismiss_consent()

        # Some queries land directly on a single place page (no feed).
        # Detect that, scrape, return.
        try:
            self.page.wait_for_selector(FEED_SELECTOR + ", " + ",".join(NAME_SELECTORS),
                                        timeout=self.cfg.page_timeout_ms)
        except PWTimeout:
            pass

        if self.page.locator(FEED_SELECTOR).count() == 0 and \
                self.page.locator(NAME_SELECTORS[0]).count() > 0:
            rec = _extract_from_detail_panel(self.page)
            return [rec] if rec.business_name else []

        block = _detect_block(self.page)
        if block:
            raise GMapsBlocked(block)

        self._scroll_feed_until_done(self.cfg.max_results)

        # Collect card URLs (the href on a.hfpxzc points to a /place/... page,
        # but clicking is more reliable for triggering the detail panel).
        cards = self.page.locator(CARD_SELECTOR)
        total = min(cards.count(), self.cfg.max_results)
        logger.info("Found %d cards to visit (max_results=%d).", total, self.cfg.max_results)

        records: list[BusinessRecord] = []
        seen: set[str] = set()

        def emit(**kw):
            if on_progress:
                try:
                    on_progress(kw)
                except Exception:
                    pass

        for i in range(total):
            try:
                card = self.page.locator(CARD_SELECTOR).nth(i)
                card.scroll_into_view_if_needed(timeout=4000)
                card.click(timeout=6000)
            except Exception as e:
                logger.debug("Card %d click failed: %s", i, e)
                continue

            # Wait for detail panel to populate.
            try:
                self.page.wait_for_selector(NAME_SELECTORS[0], timeout=8000)
            except PWTimeout:
                logger.debug("Card %d: detail panel didn't appear", i)
                continue

            _sleep(0.4, 0.9)
            rec = _extract_from_detail_panel(self.page)
            if not rec.business_name:
                continue
            key = rec.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
            emit(index=i + 1, total=total, new=1, total_records=len(records),
                 status="ok", detail=rec.business_name)
            _sleep(self.cfg.min_action_delay, self.cfg.max_action_delay)

        return records


# ----------------------------- CLI --------------------------------------- #

def _cli() -> int:
    import argparse
    from .pagesjaunes import write_csv

    ap = argparse.ArgumentParser(description="Google Maps scraper")
    ap.add_argument("keyword", help='e.g. "plombier Paris"')
    ap.add_argument("--location", default="")
    ap.add_argument("--max-results", type=int, default=40)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--lang", default="fr")
    ap.add_argument("--proxy", action="append", default=[])
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = GMapsConfig(
        keyword=args.keyword,
        location=args.location,
        max_results=args.max_results,
        headless=not args.headed,
        language=args.lang,
        proxies=args.proxy,
    )
    with GoogleMapsScraper(cfg) as s:
        records = s.run()
    path = write_csv(records, keyword=cfg.keyword)
    print(f"Saved {len(records)} records -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
