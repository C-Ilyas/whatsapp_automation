"""
WhatsApp Web automation: send a predefined message to a list of numbers.

Usage:
    python send_whatsapp.py [--numbers numbers.txt] [--message message.txt] [--headless]

Behavior:
- Persists the WhatsApp Web session in ./wa_session so the QR code is scanned once.
- Reads phone numbers (international, digits only) from numbers.txt.
- Reads message body from message.txt.
- Opens each chat via the wa.me deep link, types the message with human-like
  delays, sends it, and waits for delivery confirmation (the message tick).
- Logs every attempt to logs/run_<timestamp>.log and to logs/results.csv.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / "wa_session"
LOG_DIR = ROOT / "logs"
RESULTS_CSV = LOG_DIR / "results.csv"

WA_BASE = "https://web.whatsapp.com"



def human_sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def human_type(page: Page, selector_handle, text: str) -> None:
    """Type with per-character jitter and occasional micro-pauses.

    Newlines are inserted via Shift+Enter so they become line breaks inside
    the composer rather than sending the message prematurely.
    """
    for ch in text:
        if ch == "\n":
            selector_handle.press("Shift+Enter")
            time.sleep(random.uniform(0.05, 0.15))
            continue
        if ch == "\r":
            continue
        selector_handle.type(ch, delay=random.uniform(35, 110))
        if random.random() < 0.04:
            time.sleep(random.uniform(0.15, 0.45))



def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"run_{ts}.log"

    logger = logging.getLogger("wa")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def append_result(number: str, status: str, detail: str) -> None:
    is_new = not RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "number", "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), number, status, detail])



@dataclass
class Recipient:
    name: str
    number: str


@dataclass
class Job:
    recipients: list[Recipient]
    template: str


def load_job(numbers_path: Path, message_path: Path) -> Job:
    raw = numbers_path.read_text(encoding="utf-8").splitlines()
    recipients: list[Recipient] = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            name_part, num_part = line.split(",", 1)
            name = name_part.strip() or "there"
        else:
            name, num_part = "there", line
        digits = re.sub(r"\D", "", num_part)
        if 7 <= len(digits) <= 15:
            recipients.append(Recipient(name=name, number=digits))
    if not recipients:
        raise SystemExit(f"No valid recipients found in {numbers_path}")

    template = message_path.read_text(encoding="utf-8").strip()
    if not template:
        raise SystemExit(f"Empty message body in {message_path}")
    return Job(recipients=recipients, template=template)


def render_message(template: str, name: str) -> str:
    try:
        return template.format(name=name)
    except (KeyError, IndexError):
        return template



def wait_for_login(page: Page, logger: logging.Logger, timeout_s: int = 180) -> None:
    """Wait until the chat list is visible (i.e. user is logged in)."""
    page.goto(WA_BASE, wait_until="domcontentloaded")
    logger.info("Waiting for WhatsApp Web to be ready (scan QR if prompted)...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # Side panel / chat list is a reliable post-login signal.
        if page.locator('div[id="side"]').count() > 0:
            logger.info("WhatsApp Web is ready.")
            return
        # Or the "Chats" header
        if page.get_by_role("button", name=re.compile("New chat", re.I)).count() > 0:
            logger.info("WhatsApp Web is ready.")
            return
        time.sleep(1.5)
    raise RuntimeError("Timed out waiting for WhatsApp Web login.")


def open_chat(page: Page, number: str) -> None:
    page.goto(f"{WA_BASE}/send?phone={number}", wait_until="domcontentloaded")


def _invalid_number_dialog(page: Page) -> bool:
    # WhatsApp shows a modal: "Phone number shared via url is invalid."
    try:
        return page.get_by_text(re.compile("phone number.*invalid", re.I)).count() > 0
    except Exception:
        return False


def _find_message_box(page: Page, timeout_ms: int = 45_000):
    # The composer is a contenteditable footer; pick the last one (message box,
    # not the search box).
    page.wait_for_selector('footer div[contenteditable="true"]', timeout=timeout_ms)
    boxes = page.locator('footer div[contenteditable="true"]')
    return boxes.last


def send_message(page: Page, number: str, message: str, logger: logging.Logger) -> tuple[bool, str]:
    open_chat(page, number)

    # Allow brief load; then check for invalid number dialog.
    for _ in range(20):
        if _invalid_number_dialog(page):
            # dismiss
            try:
                page.get_by_role("button", name=re.compile("ok", re.I)).first.click()
            except Exception:
                pass
            return False, "invalid_number"
        if page.locator('footer div[contenteditable="true"]').count() > 0:
            break
        time.sleep(0.5)

    try:
        box = _find_message_box(page)
    except PWTimeout:
        return False, "chat_did_not_load"

    human_sleep(0.6, 1.6)
    box.click()
    human_sleep(0.2, 0.6)
    human_type(page, box, message)
    human_sleep(0.4, 1.1)
    box.press("Enter")

    # Confirm actually sent: wait for the single/double check icon.
    # locally and the message hasn't left the device yet.
    try:
        page.wait_for_selector(
            'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"], '
            'span[data-icon="msg-dblcheck-ack"]',
            timeout=30_000,
        )
    except PWTimeout:
        if page.locator('span[data-icon="msg-time"]').count() > 0:
            return False, "queued_not_sent"
        return False, "no_send_confirmation"

    return True, "sent"



class WhatsAppSender:
    """Reusable sender that owns a persistent browser context.

    Usage:
        with WhatsAppSender(headless=False) as s:
            s.wait_for_login()
            ok, detail = s.send("213558880464", "Hello there")
    """

    def __init__(self, headless: bool = False, channel: str | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.headless = headless
        env_channel = os.getenv("WA_BROWSER_CHANNEL", "chrome").strip().lower()
        self.channel: str | None = channel if channel is not None else (
            None if env_channel in ("", "chromium") else env_channel
        )
        self.logger = logger or logging.getLogger("wa")
        self._pw = None
        self._context = None
        self.page: Page | None = None

    def __enter__(self) -> "WhatsAppSender":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        SESSION_DIR.mkdir(exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs = dict(
            user_data_dir=str(SESSION_DIR),
            headless=self.headless,
        )
        if self.channel:
            launch_kwargs["channel"] = self.channel
        self._context = self._pw.chromium.launch_persistent_context(
            **launch_kwargs,
            viewport={"width": 1280, "height": 820},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        try:
            if self._context is not None:
                time.sleep(3)
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._context = None
            self._pw = None
            self.page = None

    def wait_for_login(self, timeout_s: int = 180) -> None:
        assert self.page is not None
        wait_for_login(self.page, self.logger, timeout_s=timeout_s)

    def send(self, number: str, message: str) -> tuple[bool, str]:
        assert self.page is not None
        return send_message(self.page, number, message, self.logger)


def normalize_number(raw: object) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if 7 <= len(digits) <= 15 else None



def main() -> int:
    ap = argparse.ArgumentParser(description="WhatsApp Web bulk sender")
    ap.add_argument("--numbers", type=Path, default=ROOT / "numbers.txt")
    ap.add_argument("--message", type=Path, default=ROOT / "message.txt")
    ap.add_argument("--headless", action="store_true", help="Run browser headless (only after first login).")
    ap.add_argument("--min-delay", type=float, default=6.0, help="Min seconds between messages.")
    ap.add_argument("--max-delay", type=float, default=14.0, help="Max seconds between messages.")
    ap.add_argument("--login", action="store_true",
                    help="Just open WhatsApp Web and wait for QR login, then exit. "
                         "Useful for priming wa_session/ before running in Docker.")
    args = ap.parse_args()

    logger = setup_logging()

    if args.login:
        SESSION_DIR.mkdir(exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(SESSION_DIR),
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 820},
                args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                wait_for_login(page, logger, timeout_s=300)
                logger.info("Login OK. Session stored in %s", SESSION_DIR)
                time.sleep(5)  # let cookies flush
            finally:
                context.close()
        return 0

    job = load_job(args.numbers, args.message)
    logger.info("Loaded %d recipients; template length=%d chars.", len(job.recipients), len(job.template))

    SESSION_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            channel="chrome",
            headless=args.headless,
            viewport={"width": 1280, "height": 820},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            wait_for_login(page, logger)
        except Exception as e:
            logger.error("Login failed: %s", e)
            context.close()
            return 2

        ok = fail = 0
        total = len(job.recipients)
        for idx, recipient in enumerate(job.recipients, 1):
            message = render_message(job.template, recipient.name)
            logger.info("[%d/%d] -> %s (%s)", idx, total, recipient.number, recipient.name)
            try:
                success, detail = send_message(page, recipient.number, message, logger)
            except Exception as e:  # never let one number kill the run
                success, detail = False, f"exception:{e.__class__.__name__}:{e}"

            status = "OK" if success else "FAIL"
            if success:
                ok += 1
            else:
                fail += 1
            logger.info("    %s (%s)", status, detail)
            append_result(recipient.number, status, detail)

            if idx < total:
                delay = random.uniform(args.min_delay, args.max_delay)
                logger.info("    sleeping %.1fs", delay)
                time.sleep(delay)

        logger.info("Done. ok=%d fail=%d total=%d", ok, fail, total)
        time.sleep(5)
        context.close()
        return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
