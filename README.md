# WhatsApp Web Automation

A lead-outreach platform: **scrape** businesses from PagesJaunes or Google
Maps, **filter** the list, **generate** a personalized message per recipient
with an LLM, review the drafts, and **send** them over WhatsApp Web via
Playwright and a persistent Chromium profile (scan the QR once, reuse
forever).

There are two ways in: the Streamlit UI (the full pipeline) and a plain CLI
(send a fixed message to a list of numbers).

## Setup

```bash
python -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env   # then fill in OPENAI_API_KEY
```

---

# The platform (Streamlit UI)

```bash
streamlit run app.py
```

The page walks through five steps.

### 1. Get recipients

Three tabs, each producing the same table:

- **Upload CSV** — any columns are accepted (name, phone, business, address,
  industry, …). The phone column is auto-detected; override it if the guess is
  wrong. See [sample_recipients.csv](sample_recipients.csv) for the shape.
- **Scrape PagesJaunes** — keyword + optional city + page count.
- **Scrape Google Maps** — keyword + optional location + max results.

Both scrapers show live progress, write a CSV under `scraped/`, and list
recent scrapes so you can reload one without re-scraping. Each has an
**Advanced** section for rotating proxies and a headless toggle; Google Maps
adds an interface-language picker.

Two filters apply to whichever list you loaded:

- **Skip landlines (country-aware)** — `classify_number()` in
  [send_whatsapp.py](send_whatsapp.py) matches the country code against
  per-country mobile-prefix tables (France `6`/`7`, UK `7`, Germany
  `15`/`16`/`17`, UAE `5`, …). Numbers it cannot confidently classify — an
  unknown country, or NANP where prefixes carry no mobile/landline
  distinction — are **kept**, so the filter never silently drops good leads.
- **Skip already-sent** — reads `logs/results.csv` and excludes every number
  you have already messaged.

### 2. Services to propose

Pick from the catalogue, or type a custom service.

### 3. Generate personalized messages

The agent ([agent.py](agent.py)) feeds each row in as `{column: value}`
context plus the chosen services, and writes one proposal per recipient. The
prompt asks the model to infer the recipient's business type and pain points
and to propose a *specific application* of the services, rather than reciting
a service list.

Message style is controlled from the sidebar and applies to every draft:

| Control | Options |
|---|---|
| Language | Auto (match the recipient's context), English, French, Arabic, Spanish, German, Italian, Portuguese, Dutch |
| Tone | Friendly, Professional, Casual, Formal, Enthusiastic, Concise |
| Length | Short ≈40 words, Medium ≈80, Long ≈120 |
| Call to action | Ask a question, Offer a quick call, Offer to send info, Ask for the right person |
| Emojis | None, Minimal (1 max), Light |

Alongside those: your sign-off name, the OpenAI model, and a creativity
(temperature) slider.

**Caching.** Every generated message is cached on disk, keyed by a SHA-256 of
the recipient row *plus* the services *plus* every style setting above. Re-run
a list and unchanged rows cost nothing; change the tone, the services, or the
row itself and only the affected rows regenerate. The UI reports how many were
reused versus freshly generated. Tick **Force refresh** to bypass the cache and
regenerate everything.

- Cache file: `cache/messages.json`, relocatable via `MESSAGE_CACHE_PATH`.
- Writes are atomic (temp file + replace) and lock-guarded, so a crash
  mid-write cannot corrupt it. Delete the file to reset.

### 4. Review & edit drafts

Every message is editable inline before anything is sent.

### 5. Send via WhatsApp Web

`WhatsAppSender` reuses the same persistent Chrome profile as the CLI, so the
login is shared between them. The first run opens Chrome for the QR scan.

---

# The CLI

### Send

```bash
python send_whatsapp.py              # first run: headed, so you can scan the QR
python send_whatsapp.py --headless   # afterwards (session cached in ./wa_session)
```

- `numbers.txt` — one number per line, international format, digits only
  (e.g. `14155552671`). Lines starting with `#` are ignored.
- `message.txt` — the message body.
- `--numbers PATH` / `--message PATH` — override those defaults.
- `--min-delay` / `--max-delay` — randomized gap between numbers (default
  6–14s), to mimic a human cadence.

### Scrape

```bash
python -m scraper.pagesjaunes "plombier" --city "Paris" --max-pages 5
python -m scraper.google_maps  "plombier" --location "Paris" --max-results 40 --lang fr

# rotating proxies (repeat the flag), and --headed to watch it work:
python -m scraper.pagesjaunes "plombier" --city Lyon \
  --proxy http://user:pw@p1:8080 --proxy http://user:pw@p2:8080
```

Both write `scraped/pj_<keyword>_<timestamp>.csv` with one fixed schema:
`business_name, phone, address, postal_code, city, website, category, rating,
review_count, business_url, latitude, longitude`. (The `pj_` prefix is used by
both scrapers — they share `write_csv`.) Rows are deduplicated by phone,
falling back to name + postal code.

---

## Output

- `logs/run_<timestamp>.log` — per-run log.
- `logs/results.csv` — append-only `timestamp,number,status,detail`; also what
  the "skip already-sent" filter reads.
- `logs/*.png` — screenshot of the page whenever a chat fails to load, so you
  can see what WhatsApp actually showed.
- `scraped/*.csv` — scraper output.
- `cache/messages.json` — generated-message cache.

## How it behaves like a human

- Persistent real Chromium profile (no incognito, no fresh fingerprint each
  run) — `wa_session/` for sending, `scraper_session/` and `gmaps_session/`
  for the scrapers.
- Realistic UA and viewport, randomized per session, plus
  `--disable-blink-features=AutomationControlled` and `navigator.webdriver`
  masking.
- Per-character typing delays with occasional micro-pauses.
- Randomized inter-message and inter-action delays, exponential backoff on
  retry.
- Waits for real send confirmation (the message tick icon) before moving on.
- Scrapers detect CAPTCHA / DataDome / 403 and stop cleanly rather than
  writing garbage rows.

## Failure handling

Each number is processed in its own try/except: failures are recorded with a
reason (`invalid_number`, `chat_did_not_load`, `no_send_confirmation`,
`exception:…`) and the run moves on to the next number.

On login the sender waits for the chat list *and* for the "Syncing messages…"
overlay to clear, which can take 30–60s on a fresh link. Invalid-number
detection covers the English and French variants of both "phone number is
invalid" and "isn't on WhatsApp".

## Docker

```bash
cp .env.example .env   # fill in OPENAI_API_KEY
docker compose up --build
```

Open http://localhost:8501.

- The container uses the Playwright image's bundled **Chromium** (not Google
  Chrome), set by `WA_BROWSER_CHANNEL=chromium` in `docker-compose.yml`.
- Bind-mounted so they survive restarts: `wa_session/`, `scraper_session/`,
  `gmaps_session/`, `scraped/`, `logs/`, `cache/`.
- **First-time QR scan**: the container runs headless, so either
  1. run `streamlit run app.py` once on the host, scan the QR, then
     `docker compose up` — the `wa_session/` volume already holds the session;
     or
  2. uncheck "Run Chrome headless" in the sidebar **and** bridge an X server
     (`-e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix`).

  Option (1) is much simpler.

## Architecture

- **Browser layer** (`PagesJaunesScraper`, `GoogleMapsScraper`) — owns the
  Playwright context, retries, block detection.
- **Extraction layer** (`extract_records_from_page`) — pure and
  selector-driven, with a fallback chain per field. When a site changes its
  DOM, edit the `*_SELECTORS` lists and nothing else.
- **Output layer** (`write_csv`, shared by both scrapers) — fixed schema,
  UTF-8, timestamped filenames, dedup, phone normalization, URL validation.

## Notes

- The default model is inconsistent right now: `.env.example` and the UI
  default to `gpt-4o-mini`, while `DEFAULT_MODEL` in [agent.py](agent.py)
  falls back to `gpt-4o`. Set `OPENAI_MODEL` in `.env` explicitly so you know
  which one you are paying for.
- WhatsApp's Terms of Service prohibit bulk/automated messaging. Use this only
  for legitimate outreach, to numbers that have consented to hear from you.
  Scraped numbers have not consented by virtue of being listed.
- If WhatsApp updates its DOM, the message-box and confirmation selectors in
  [send_whatsapp.py](send_whatsapp.py) may need adjusting.
