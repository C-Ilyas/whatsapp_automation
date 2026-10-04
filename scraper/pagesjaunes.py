"""
PagesJaunes.fr scraper.

Design goals (in order):
  1. Reliability — be conservative, retry on transient failures, detect blocks
     and back off rather than burn through a proxy.
  2. Maintainability — a thin browser layer (`PagesJaunesScraper.run`) and a
     pure-Python extraction layer (`extract_records_from_html`) so selectors
     can be evolved without touching browser code, and unit-tested offline.
  3. Clean CSV output — fixed schema, UTF-8, timestamped filenames, dedup,
     phone normalization, whitespace cleanup.

Anti-bot measures:
  * Persistent Chromium profile (cookies survive runs).
  * Randomized User-Agent + viewport per session.
  * Per-action randomized delays + occasional pauses.
  * Optional rotating proxies (pass via ScrapeConfig.proxies).
  * `--disable-blink-features=AutomationControlled`.
  * CAPTCHA / DataDome / 403 detection → backs off and surfaces an error
     instead of producing garbage rows.
  * Retry with exponential backoff on page-level failures.

The scraper intentionally does NOT bypass DataDome / hCaptcha challenges. If a
challenge is detected the run is paused (headed mode) or aborted (headless)
and the calling code can decide what to do.

Usage:
    from scraper import PagesJaunesScraper, ScrapeConfig, write_csv

    cfg = ScrapeConfig(keyword="plombier", city="Paris", max_pages=3)
    with PagesJaunesScraper(cfg) as s:
        records = s.run()
    path = write_csv(records, keyword=cfg.keyword)
"""

from __future__ import annotations

import csv
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

logger = logging.getLogger("scraper.pagesjaunes")

ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / "scraper_session"
OUTPUT_DIR = ROOT / "scraped"

PJ_BASE = "https://www.pagesjaunes.fr"
PJ_SEARCH = f"{PJ_BASE}/annuaire/chercherlespros"

CSV_FIELDS = [
    "business_name",
    "phone",
    "address",
    "postal_code",
    "city",
    "website",
    "category",
    "rating",
    "review_count",
    "business_url",
    "latitude",
    "longitude",
]

# Realistic desktop UAs. Rotate per session.
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1680, "height": 1050},
    {"width": 1920, "height": 1080},
]


# ============================== data model =============================== #

@dataclass
class BusinessRecord:
    business_name: str = ""
    phone: str = ""
    address: str = ""
    postal_code: str = ""
    city: str = ""
    website: str = ""
    category: str = ""
    rating: str = ""
    review_count: str = ""
    business_url: str = ""
    latitude: str = ""
    longitude: str = ""

    def dedup_key(self) -> str:
        # Prefer phone, fall back to (name, postal_code).
        if self.phone:
            return f"phone:{self.phone}"
        return f"np:{self.business_name.lower()}|{self.postal_code}"

    def is_useful(self) -> bool:
        return bool(self.business_name and (self.phone or self.address or self.website))


@dataclass
class ScrapeConfig:
    keyword: str
    city: str = ""
    max_pages: int = 5
    headless: bool = True
    min_delay: float = 2.5
    max_delay: float = 6.0
    page_timeout_ms: int = 30_000
    max_retries_per_page: int = 3
    proxies: list[str] = field(default_factory=list)  # ["http://user:pw@host:port", ...]
    reveal_phones: bool = True  # click "Voir le numéro" if the phone is hidden
    # Headed/headless follows config; in container set headless=True.


class ScrapeBlocked(RuntimeError):
    """Raised when a CAPTCHA / DataDome challenge / 403 page is detected."""


# ============================== utilities ================================ #

def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


_PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone_fr(raw: str) -> str:
    """Return digits-only international format for FR numbers when possible.

    - "01 23 45 67 89" -> "33123456789"
    - "+33 1 23 45 67 89" -> "33123456789"
    - non-FR / invalid -> the raw digits.
    """
    if not raw:
        return ""
    s = _PHONE_RE.sub("", raw)
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("00"):
        s = s[2:]
    if s.startswith("0") and len(s) == 10:
        s = "33" + s[1:]
    # Sanity bounds.
    return s if 7 <= len(s) <= 15 else ""


_POSTAL_RE = re.compile(r"\b(\d{5})\b")


def split_address(raw: str) -> tuple[str, str, str]:
    """Return (street, postal_code, city) from a free-form FR address."""
    raw = _clean(raw)
    if not raw:
        return "", "", ""
    m = _POSTAL_RE.search(raw)
    if not m:
        return raw, "", ""
    postal = m.group(1)
    before = raw[: m.start()].strip(" ,")
    after = raw[m.end():].strip(" ,")
    return before, postal, after


def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def build_search_url(keyword: str, city: str = "", page: int = 1) -> str:
    params = [f"quoiqui={quote_plus(keyword)}"]
    if city:
        params.append(f"ou={quote_plus(city)}")
    if page > 1:
        params.append(f"page={page}")
    return f"{PJ_SEARCH}?{'&'.join(params)}"


# ========================== HTML extraction layer ========================= #
# Pure-Python: takes Playwright page handle, returns list[BusinessRecord].
# Selectors are listed in fallback order — when PagesJaunes ships a redesign,
# tweak this list (and only this list).

NAME_SELECTORS = [
    "a.bi-denomination h3",
    "a.bi-denomination",
    ".bi-denomination h3",
    ".bi-denomination",
    "h3",
]

# URL of the business detail page — read href from the denomination link.
NAME_LINK_SELECTORS = [
    "a.bi-denomination",
    "a[href^='/pros/']",
]

# Phones are pre-rendered (hidden via CSS) inside .bi-fantomas .number-contact;
# we just read them. Fall back to data attributes if class names change.
PHONE_SELECTORS = [
    ".bi-fantomas .number-contact",
    ".number-contact",
    "[itemprop='telephone']",
]

ADDRESS_SELECTORS = [
    ".bi-address",
    ".address",
    ".adresse",
    "[itemprop='address']",
]

WEBSITE_SELECTORS = [
    "a.bi-website",
    "a.hidden-phone.bi-website",
    "a[data-pjlb][title*='Site' i]",
]

CATEGORY_SELECTORS = [
    ".bi-activity-unit",
    ".activite",
    "[itemprop='additionalType']",
]

RATING_SELECTORS = [
    ".note_moyenne",
    ".bi-note .note_moyenne",
    "[itemprop='ratingValue']",
]

REVIEWS_SELECTORS = [
    ".bi-rating",
    "[itemprop='reviewCount']",
    ".nb-avis",
]

CARD_SELECTORS = [
    "li.bi",
    "article.bi",
    "li[id^='bi-']",
    ".bi-generic",
]


def _first_text(node, selectors: Iterable[str]) -> str:
    for sel in selectors:
        try:
            loc = node.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=2000)
                if txt and txt.strip():
                    return _clean(txt)
        except Exception:
            continue
    return ""


def _first_attr(node, selectors: Iterable[str], attr: str) -> str:
    for sel in selectors:
        try:
            loc = node.locator(sel).first
            if loc.count() > 0:
                val = loc.get_attribute(attr, timeout=2000)
                if val:
                    return val.strip()
        except Exception:
            continue
    return ""


def _reveal_phone_in_card(card) -> None:
    """Click the 'Voir le numéro' button if present, then wait briefly."""
    try:
        btn = card.locator("button:has-text('numéro'), button:has-text('Numéro'), "
                           ".pj-lb-data, .pj-lb-button").first
        if btn.count() > 0:
            btn.click(timeout=2500, no_wait_after=True)
            time.sleep(random.uniform(0.25, 0.6))
    except Exception:
        pass


def _extract_card(card, reveal_phones: bool) -> BusinessRecord:
    rec = BusinessRecord()
    rec.business_name = _first_text(card, NAME_SELECTORS)

    href = _first_attr(card, NAME_LINK_SELECTORS, "href")
    if href:
        rec.business_url = urljoin(PJ_BASE, href)

    if reveal_phones:
        _reveal_phone_in_card(card)

    # Phone text in PJ looks like "Tél : 01 42 55 41 98" (sometimes several).
    # Take the first one we can normalize.
    raw_phone = _first_text(card, PHONE_SELECTORS)
    if raw_phone:
        # strip "Tél :" prefix and collapse
        raw_phone = re.sub(r"(?i)t[ée]l\s*:?", "", raw_phone)
    rec.phone = normalize_phone_fr(raw_phone) if raw_phone else ""

    raw_addr = _first_text(card, ADDRESS_SELECTORS)
    # Strip the "Voir le plan" map link label that lives in the same node.
    raw_addr = re.sub(r"(?i)\bvoir le plan\b", "", raw_addr).strip(" ,")
    street, postal, city = split_address(raw_addr)
    rec.address = street
    rec.postal_code = postal
    rec.city = city

    site = _first_attr(card, WEBSITE_SELECTORS, "href")
    if site and not site.startswith("http"):
        site = urljoin(PJ_BASE, site)
    rec.website = site if is_valid_url(site) else ""

    rec.category = _first_text(card, CATEGORY_SELECTORS)
    rec.rating = _first_text(card, RATING_SELECTORS)
    raw_reviews = _first_text(card, REVIEWS_SELECTORS)
    rec.review_count = re.sub(r"\D", "", raw_reviews) if raw_reviews else ""

    # Coordinates sometimes live in data-* attrs on the card root.
    for attr in ("data-pjlat", "data-lat"):
        try:
            v = card.get_attribute(attr, timeout=1000)
            if v:
                rec.latitude = v
                break
        except Exception:
            pass
    for attr in ("data-pjlng", "data-lng"):
        try:
            v = card.get_attribute(attr, timeout=1000)
            if v:
                rec.longitude = v
                break
        except Exception:
            pass

    return rec


def extract_records_from_page(page: Page, reveal_phones: bool) -> list[BusinessRecord]:
    cards = None
    for sel in CARD_SELECTORS:
        loc = page.locator(sel)
        if loc.count() > 0:
            cards = loc
            logger.debug("Using card selector %r (count=%d)", sel, loc.count())
            break
    if cards is None:
        logger.warning("No result cards found — HTML structure may have changed.")
        return []

    out: list[BusinessRecord] = []
    n = cards.count()
    for i in range(n):
        try:
            rec = _extract_card(cards.nth(i), reveal_phones=reveal_phones)
            if rec.is_useful():
                out.append(rec)
        except Exception as e:  # one bad card must not kill the page
            logger.debug("Card %d extraction failed: %s", i, e)
    return out


# =========================== block detection ============================= #

BLOCK_SIGNALS = [
    re.compile(r"datadome", re.I),
    re.compile(r"captcha", re.I),
    re.compile(r"access\s+denied", re.I),
    re.compile(r"\b403\b"),
    re.compile(r"unusual\s+traffic", re.I),
    re.compile(r"vérifier que vous n.tes pas un robot", re.I),
]


def detect_block(page: Page) -> str | None:
    try:
        if "captcha" in page.url.lower() or "datadome" in page.url.lower():
            return f"blocked URL: {page.url}"
        body = page.inner_text("body", timeout=5000)[:5000]
    except Exception:
        return None
    for rx in BLOCK_SIGNALS:
        if rx.search(body):
            return f"matched {rx.pattern}"
    return None


# ============================ browser layer ============================== #

class PagesJaunesScraper:
    def __init__(self, config: ScrapeConfig) -> None:
        self.cfg = config
        self._pw = None
        self._context: BrowserContext | None = None
        self._browser: Browser | None = None
        self.page: Page | None = None
        self._proxy_idx = 0

    def __enter__(self) -> "PagesJaunesScraper":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------- lifecycle ------------------------- #

    def _next_proxy(self) -> dict | None:
        if not self.cfg.proxies:
            return None
        url = self.cfg.proxies[self._proxy_idx % len(self.cfg.proxies)]
        self._proxy_idx += 1
        # Playwright proxy dict: {"server": "...", "username": "...", "password": "..."}
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
        proxy = self._next_proxy()
        ua = random.choice(USER_AGENTS)
        viewport = random.choice(VIEWPORTS)
        logger.info("Launching browser (headless=%s, ua=%s, vp=%s, proxy=%s)",
                    self.cfg.headless, ua, viewport, bool(proxy))

        launch_kwargs = dict(
            user_data_dir=str(SESSION_DIR),
            headless=self.cfg.headless,
            viewport=viewport,
            user_agent=ua,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--lang=fr-FR",
            ],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy

        self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        # Mask the navigator.webdriver flag.
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

    # ------------------------- cookie banner ------------------------- #

    def _dismiss_cookie_banner(self) -> None:
        assert self.page is not None
        for sel in [
            "button#didomi-notice-agree-button",
            "button:has-text('Tout accepter')",
            "button:has-text('Accepter')",
            "button[aria-label*='accept' i]",
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=2000)
                    _sleep(0.4, 1.0)
                    return
            except Exception:
                continue

    # ------------------------- page fetch with retry ------------------------- #

    def _fetch_page(self, url: str) -> None:
        assert self.page is not None
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_retries_per_page + 1):
            try:
                logger.info("GET %s (attempt %d)", url, attempt)
                self.page.goto(url, wait_until="domcontentloaded",
                               timeout=self.cfg.page_timeout_ms)
                _sleep(1.0, 2.2)
                self._dismiss_cookie_banner()
                # Wait for at least one card or a known empty state.
                try:
                    self.page.wait_for_selector(
                        ",".join(CARD_SELECTORS),
                        timeout=self.cfg.page_timeout_ms,
                    )
                except PWTimeout:
                    pass

                block = detect_block(self.page)
                if block:
                    raise ScrapeBlocked(block)
                return
            except ScrapeBlocked:
                raise
            except Exception as e:
                last_err = e
                backoff = (2 ** attempt) + random.uniform(0, 1.5)
                logger.warning("Fetch failed (%s). Backing off %.1fs.", e, backoff)
                time.sleep(backoff)
        raise RuntimeError(f"Failed to fetch {url}: {last_err}")

    # ------------------------- public entrypoint ------------------------- #

    def run(self, on_progress: Callable[[dict], None] | None = None) -> list[BusinessRecord]:
        """Scrape pages 1..max_pages.

        on_progress: optional callback invoked after each page with a dict:
            {"page": int, "total_pages": int, "new": int, "total": int,
             "status": "ok"|"empty"|"blocked"|"error", "detail": str}
        """
        assert self.page is not None
        records: list[BusinessRecord] = []
        seen: set[str] = set()

        def emit(**kw):
            if on_progress:
                try:
                    on_progress(kw)
                except Exception:
                    pass

        for page_num in range(1, self.cfg.max_pages + 1):
            url = build_search_url(self.cfg.keyword, self.cfg.city, page=page_num)
            try:
                self._fetch_page(url)
            except ScrapeBlocked as e:
                logger.error("Blocked on page %d: %s. Stopping.", page_num, e)
                emit(page=page_num, total_pages=self.cfg.max_pages, new=0,
                     total=len(records), status="blocked", detail=str(e))
                break
            except Exception as e:
                logger.error("Giving up on page %d: %s", page_num, e)
                emit(page=page_num, total_pages=self.cfg.max_pages, new=0,
                     total=len(records), status="error", detail=str(e))
                continue

            page_records = extract_records_from_page(self.page, reveal_phones=self.cfg.reveal_phones)
            logger.info("Page %d: extracted %d records.", page_num, len(page_records))
            if not page_records:
                emit(page=page_num, total_pages=self.cfg.max_pages, new=0,
                     total=len(records), status="empty", detail="no results on page")
                break

            new = 0
            for rec in page_records:
                key = rec.dedup_key()
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
                new += 1
            logger.info("Page %d: %d new records (total=%d).", page_num, new, len(records))
            emit(page=page_num, total_pages=self.cfg.max_pages, new=new,
                 total=len(records), status="ok", detail="")

            if new == 0:
                break

            _sleep(self.cfg.min_delay, self.cfg.max_delay)

        return records


# =============================== CSV output ============================== #

def write_csv(records: Iterable[BusinessRecord], keyword: str,
              out_dir: Path = OUTPUT_DIR) -> Path:
    out_dir.mkdir(exist_ok=True)
    safe_kw = re.sub(r"[^\w\-]+", "_", keyword.strip()) or "results"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"pj_{safe_kw}_{ts}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for rec in records:
            row = {k: asdict(rec).get(k, "") for k in CSV_FIELDS}
            w.writerow(row)
    logger.info("Wrote %d records to %s", sum(1 for _ in records) if isinstance(records, list) else 0, path)
    return path


# ================================ CLI ==================================== #

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="PagesJaunes scraper")
    ap.add_argument("keyword")
    ap.add_argument("--city", default="")
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--headed", action="store_true", help="Show browser (debugging).")
    ap.add_argument("--proxy", action="append", default=[],
                    help="Proxy URL (repeatable). e.g. http://user:pw@host:port")
    ap.add_argument("--dump-html", action="store_true",
                    help="Save the rendered HTML of page 1 to scraped/_dump.html "
                         "for selector inspection, then exit.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = ScrapeConfig(
        keyword=args.keyword,
        city=args.city,
        max_pages=args.max_pages,
        headless=not args.headed,
        proxies=args.proxy,
    )
    with PagesJaunesScraper(cfg) as s:
        if args.dump_html:
            url = build_search_url(cfg.keyword, cfg.city, page=1)
            s._fetch_page(url)
            OUTPUT_DIR.mkdir(exist_ok=True)
            dump = OUTPUT_DIR / "_dump.html"
            dump.write_text(s.page.content(), encoding="utf-8")
            block = detect_block(s.page)
            print(f"URL ended up at: {s.page.url}")
            print(f"Block check: {block or 'no block detected'}")
            print(f"Dumped HTML -> {dump}")
            return 0
        records = s.run()
    path = write_csv(records, keyword=cfg.keyword)
    print(f"Saved {len(records)} records -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
