# WhatsApp Web Automation

Sends a predefined message to a list of phone numbers via WhatsApp Web using
Playwright + a persistent Chromium profile (scan the QR once, reuse forever).

## Setup

```bash
python -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Configure

- `numbers.txt` — one phone number per line, international format, digits only
  (e.g. `14155552671`). Lines starting with `#` are ignored.
- `message.txt` — the message body to send.

## Run

First run (headed, so you can scan the QR):

```bash
python send_whatsapp.py
```

Subsequent runs (session is cached in `./wa_session`):

```bash
python send_whatsapp.py --headless
```

Options:

- `--numbers PATH` / `--message PATH` — override defaults
- `--min-delay` / `--max-delay` — randomized delay (seconds) between numbers
  (default 6–14s) to mimic a human cadence

## Output

- `logs/run_<timestamp>.log` — per-run log
- `logs/results.csv` — append-only `timestamp,number,status,detail`

## How it behaves like a human

- Persistent real Chromium profile (no incognito, no fresh fingerprint each run).
- Realistic UA, viewport, and `--disable-blink-features=AutomationControlled`.
- Per-character typing delays with occasional micro-pauses.
- Randomized inter-message delays.
- Waits for actual send confirmation (message tick icon) before moving on.

## Failure handling

Each number is processed in a try/except. Failures are recorded with a reason
(`invalid_number`, `chat_did_not_load`, `no_send_confirmation`, `exception:...`)
and the run continues with the next number.

## Streamlit UI + AI message generation

A web UI lets you upload a CSV of leads, pick services to offer, generate
personalized messages with an OpenAI/LangChain agent, review/edit them, and
send via WhatsApp Web.

```bash
cp .env.example .env   # then fill in OPENAI_API_KEY
streamlit run app.py
```

How it works:

1. **Upload CSV** — any columns are accepted (name, phone, business, address,
   industry, …). The phone column is auto-detected (override if needed).
2. **Select services** from a catalogue (or add custom ones).
3. **Generate** — the agent ([agent.py](agent.py)) injects each row as
   `{column: value}` context plus the services list into the prompt, and
   produces a personalized proposal per recipient.
4. **Edit drafts** inline in a data editor.
5. **Send** — `WhatsAppSender` reuses the same persistent Chrome profile as
   the CLI, so login is shared.

See [sample_recipients.csv](sample_recipients.csv) for the expected shape.

## Docker

```bash
cp .env.example .env   # fill in OPENAI_API_KEY
docker compose up --build
```

Open http://localhost:8501.

Notes:
- The container uses the Playwright image's bundled **Chromium** (not Google
  Chrome) — controlled by `WA_BROWSER_CHANNEL=chromium` in
  `docker-compose.yml`.
- `wa_session/` and `logs/` are bind-mounted, so the QR login and result CSV
  survive container restarts.
- **First-time QR scan in Docker**: the container runs Chromium headless. To
  log in, either:
  1. Run `streamlit run app.py` once on the host (Chrome opens, you scan the
     QR), then `docker compose up` — the `wa_session/` volume already has the
     session.
  2. Or temporarily uncheck "Run Chrome headless" in the sidebar **and** add
     an X server bridge (e.g. `-e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix`).
     Approach (1) is much simpler.

## Notes

- WhatsApp's Terms of Service prohibit bulk/automated messaging. Use this only
  for legitimate use cases on numbers that have consented to receive messages.
- If WhatsApp updates its DOM, the message-box / confirmation selectors in
  `send_whatsapp.py` may need adjustment.
