"""
Streamlit UI:
  1. Upload a CSV of recipients (any columns; one column must hold a phone
     number — auto-detected, or selected manually).
  2. Pick which services to propose.
  3. Click "Generate messages" to have the OpenAI agent draft a personalized
     message per row from the row's columns + the selected services.
  4. Review / edit the drafts in a table.
  5. Click "Send via WhatsApp Web" to launch the browser and send them.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent import (
    AgentConfig,
    CTA_STYLES,
    EMOJI_POLICIES,
    LANGUAGES,
    LENGTHS,
    MessageAgent,
    TONES,
)
from send_whatsapp import (
    WhatsAppSender,
    append_result,
    classify_number,
    normalize_number,
    setup_logging,
)
from scraper import (
    GMapsConfig,
    GoogleMapsScraper,
    PagesJaunesScraper,
    ScrapeConfig,
    write_csv,
)
from dataclasses import asdict as _asdict

load_dotenv()

st.set_page_config(page_title="WhatsApp AI Outreach", layout="wide")
st.title("WhatsApp AI Outreach")

# Predefined catalogue. Add freely; users can also type their own.
SERVICE_CATALOGUE = [
    # --- AI agents & assistants ---
    "Custom AI agents (autonomous task execution)",
    "WhatsApp AI assistant (24/7 customer chat & lead qualification)",
    "Appointment-booking AI agent (WhatsApp / email / SMS, with reminders)",
    "AI voice agent (inbound/outbound calls, FAQs, scheduling)",
    "AI receptionist & front-desk automation",
    "Sales-outreach AI agent (lead research + personalized follow-ups)",
    "Internal knowledge-base AI assistant (Q&A on company docs)",
    # --- Automation & integrations ---
    "End-to-end workflow automation (n8n / Zapier / Make)",
    "CRM setup, integration & automation (HubSpot, Pipedrive, custom)",
    "Marketing & re-engagement automation (email, WhatsApp, SMS)",
    "Review-collection & reputation automation",
    "Document processing & OCR pipelines (invoices, IDs, contracts)",
    "Data scraping & enrichment pipelines",
    # --- Digital solutions ---
    "Custom web application development",
    "Mobile application development (iOS / Android / cross-platform)",
    "E-commerce store setup & optimization",
    "Internal tools & admin dashboards",
    "Data analytics dashboards (BI, KPIs, reporting)",
    # --- Foundations ---
    "Cloud hosting, deployment & DevOps",
    "Technical audit & AI strategy consulting",
]



with st.sidebar:
    st.header("Settings")
    api_key = st.text_input(
        "OpenAI API key",
        value=os.getenv("OPENAI_API_KEY", ""),
        type="password",
        help="Stored only in memory for this session.",
    )
    model = st.text_input("OpenAI model", value=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    sender_name = st.text_input("Your name (sign-off)", value="Ilyes",
                                help="Signed on behalf of The Crew Technologies.")
    temperature = st.slider("Creativity (temperature)", 0.0, 1.2, 0.7, 0.1)

    st.divider()
    st.subheader("Message style")
    language = st.selectbox("Language", LANGUAGES, index=0)
    tone = st.selectbox("Tone", TONES, index=0)
    length = st.selectbox("Length", list(LENGTHS.keys()), index=1)
    cta_style = st.selectbox("Call to action", list(CTA_STYLES.keys()), index=0)
    emoji_policy = st.selectbox("Emojis", list(EMOJI_POLICIES.keys()), index=0)

    st.divider()
    st.subheader("Send behaviour")
    force_headless = os.getenv("WA_FORCE_HEADLESS", "").lower() in ("1", "true", "yes")
    if force_headless:
        st.caption("Running headless (forced by WA_FORCE_HEADLESS env — e.g. inside Docker).")
        headless = True
    else:
        headless = st.checkbox("Run Chrome headless (only after first login)", value=False)
    min_delay = st.number_input("Min seconds between messages", 2.0, 120.0, 6.0)
    max_delay = st.number_input("Max seconds between messages", 2.0, 300.0, 14.0)



st.subheader("1. Get recipients")

source_tab, scrape_tab, gmaps_tab = st.tabs(
    ["📁 Upload CSV", "🟡 Scrape PagesJaunes", "🗺️ Scrape Google Maps"]
)

with source_tab:
    file = st.file_uploader("CSV file", type=["csv"])
    if file is not None:
        df = pd.read_csv(file).fillna("").astype(str)
        st.session_state.df = df

with scrape_tab:
    st.caption("Scrape French businesses from PagesJaunes.fr. Results become "
               "your recipient list below — no separate upload needed.")

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        pj_keyword = st.text_input(
            "Keyword",
            placeholder="plombier, restaurant, coiffeur…",
            key="pj_kw",
        )
    with c2:
        pj_city = st.text_input(
            "City / location",
            placeholder="Paris (optional — leave empty for all of France)",
            key="pj_city",
        )
    with c3:
        pj_pages = st.number_input("Pages", 1, 30, 3, key="pj_pages",
                                   help="~15–20 businesses per page.")

    with st.expander("Advanced (proxies, headless)", expanded=False):
        pj_proxies_raw = st.text_area(
            "Rotating proxies (one URL per line)",
            placeholder="http://user:pw@host:port",
            key="pj_proxies",
            height=80,
        )
        in_docker = os.getenv("WA_FORCE_HEADLESS", "").lower() in ("1", "true", "yes")
        pj_headless = st.checkbox(
            "Headless browser",
            value=in_docker,
            disabled=in_docker,
            key="pj_headless",
            help="Forced on inside Docker (no X server)." if in_docker else
                 "Off shows the browser — useful for debugging captchas.",
        )

    run_clicked = st.button(
        "🔎 Scrape PagesJaunes",
        type="primary",
        disabled=not pj_keyword,
        use_container_width=True,
    )

    if run_clicked:
        proxies = [p.strip() for p in pj_proxies_raw.splitlines() if p.strip()]
        cfg = ScrapeConfig(
            keyword=pj_keyword,
            city=pj_city,
            max_pages=int(pj_pages),
            headless=pj_headless,
            proxies=proxies,
        )
        progress = st.progress(0.0, text="Launching browser…")
        status = st.empty()
        metrics = st.empty()

        def _on_progress(p: dict) -> None:
            frac = p["page"] / max(p["total_pages"], 1)
            label = f"Page {p['page']}/{p['total_pages']} — "
            if p["status"] == "ok":
                label += f"+{p['new']} new (total {p['total']})"
            elif p["status"] == "empty":
                label += "no more results"
            elif p["status"] == "blocked":
                label += f"BLOCKED ({p['detail']})"
            else:
                label += f"error: {p['detail']}"
            progress.progress(min(frac, 1.0), text=label)
            with metrics.container():
                mc1, mc2, mc3 = st.columns(3)
                mc1.metric("Pages done", p["page"])
                mc2.metric("New on page", p["new"])
                mc3.metric("Total so far", p["total"])

        try:
            status.info(f"Scraping **{pj_keyword}**"
                        + (f" in **{pj_city}**" if pj_city else " across France")
                        + f" — up to {pj_pages} pages.")
            with PagesJaunesScraper(cfg) as scraper:
                records = scraper.run(on_progress=_on_progress)
            progress.progress(1.0, text="Done.")
            status.empty()

            if not records:
                st.warning("No records extracted. PagesJaunes may have served a "
                           "challenge, or the query is too narrow. Try a broader "
                           "keyword, switch off headless to inspect, or add proxies.")
            else:
                csv_path = write_csv(records, keyword=pj_keyword)
                df_scraped = pd.DataFrame([_asdict(r) for r in records]).fillna("").astype(str)
                st.session_state.df = df_scraped
                st.session_state.last_scrape_path = str(csv_path)
                st.success(f"✅ Scraped **{len(records)}** businesses → `{csv_path.name}`")
                # Preview right in the scrape tab
                st.dataframe(df_scraped.head(50), use_container_width=True, height=280)
                with open(csv_path, "rb") as f:
                    st.download_button(
                        "⬇️ Download CSV",
                        f,
                        file_name=csv_path.name,
                        mime="text/csv",
                        use_container_width=True,
                    )
        except Exception as e:
            st.error(f"Scraper failed: {e}")
            progress.empty()
            status.empty()

    # ---------------- recent scrapes ---------------- #
    scraped_dir = Path(__file__).resolve().parent / "scraped"
    recent = sorted(scraped_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)[:10] \
        if scraped_dir.exists() else []
    if recent:
        with st.expander(f"📂 Recent scrapes ({len(recent)})", expanded=False):
            for p in recent:
                cols = st.columns([4, 1, 1])
                cols[0].write(f"`{p.name}` · {p.stat().st_size // 1024} KB")
                if cols[1].button("Use", key=f"use_{p.name}"):
                    df_loaded = pd.read_csv(p).fillna("").astype(str)
                    st.session_state.df = df_loaded
                    st.success(f"Loaded {len(df_loaded)} rows from {p.name}")
                with open(p, "rb") as f:
                    cols[2].download_button("⬇", f, file_name=p.name,
                                            mime="text/csv", key=f"dl_{p.name}")


with gmaps_tab:
    st.caption("Scrape businesses from **Google Maps**. Same schema as "
               "PagesJaunes, so the result drops straight into the pipeline below.")

    g1, g2, g3 = st.columns([2, 2, 1])
    with g1:
        gm_keyword = st.text_input(
            "What to search",
            placeholder="plombier, coffee shop, dentist…",
            key="gm_kw",
        )
    with g2:
        gm_location = st.text_input(
            "Location (city / neighbourhood)",
            placeholder="Paris (optional but recommended)",
            key="gm_loc",
        )
    with g3:
        gm_max = st.number_input("Max results", 5, 200, 30, key="gm_max")

    with st.expander("Advanced (language, proxies, headless)", expanded=False):
        gm_lang = st.selectbox("Interface language", ["fr", "en", "es", "de", "it"],
                               index=0, key="gm_lang")
        gm_proxies_raw = st.text_area(
            "Rotating proxies (one URL per line)",
            placeholder="http://user:pw@host:port",
            key="gm_proxies",
            height=80,
        )
        in_docker = os.getenv("WA_FORCE_HEADLESS", "").lower() in ("1", "true", "yes")
        gm_headless = st.checkbox(
            "Headless browser",
            value=in_docker,
            disabled=in_docker,
            key="gm_headless",
            help="Forced on inside Docker." if in_docker else
                 "Off shows the browser — useful for debugging consent screens.",
        )

    if st.button("🗺️ Scrape Google Maps", type="primary",
                 disabled=not gm_keyword, use_container_width=True):
        proxies = [p.strip() for p in gm_proxies_raw.splitlines() if p.strip()]
        cfg = GMapsConfig(
            keyword=gm_keyword,
            location=gm_location,
            max_results=int(gm_max),
            headless=gm_headless,
            language=gm_lang,
            proxies=proxies,
        )
        progress = st.progress(0.0, text="Launching browser…")
        status = st.empty()
        metrics = st.empty()

        def _gm_progress(p: dict) -> None:
            frac = p["index"] / max(p["total"], 1)
            progress.progress(min(frac, 1.0),
                              text=f"{p['index']}/{p['total']} — {p['detail'][:50]}")
            with metrics.container():
                mc1, mc2 = st.columns(2)
                mc1.metric("Visited", p["index"])
                mc2.metric("Captured", p["total_records"])

        try:
            status.info(f"Scraping **{gm_keyword}**"
                        + (f" in **{gm_location}**" if gm_location else "")
                        + f" — up to {gm_max} results.")
            with GoogleMapsScraper(cfg) as scraper:
                records = scraper.run(on_progress=_gm_progress)
            progress.progress(1.0, text="Done.")
            status.empty()

            if not records:
                st.warning("No records extracted. Try a more specific query, "
                           "switch off headless to inspect, or check for a "
                           "consent screen.")
            else:
                # Save with gmap_ prefix to distinguish from PagesJaunes CSVs.
                csv_path = write_csv(records, keyword=f"gmap_{gm_keyword}")
                df_scraped = pd.DataFrame([_asdict(r) for r in records]).fillna("").astype(str)
                st.session_state.df = df_scraped
                st.success(f"✅ Scraped **{len(records)}** businesses → `{csv_path.name}`")
                st.dataframe(df_scraped.head(50), use_container_width=True, height=280)
                with open(csv_path, "rb") as f:
                    st.download_button("⬇️ Download CSV", f, file_name=csv_path.name,
                                       mime="text/csv", use_container_width=True)
        except Exception as e:
            st.error(f"Scraper failed: {e}")
            progress.empty()
            status.empty()


df: pd.DataFrame | None = st.session_state.get("df")

if df is not None:
    guess = next(
        (c for c in df.columns if any(k in c.lower() for k in ("phone", "number", "tel", "mobile", "whatsapp"))),
        df.columns[0],
    )
    phone_col = st.selectbox("Which column holds the phone number?", list(df.columns),
                             index=list(df.columns).index(guess))

    fcol1, fcol2 = st.columns(2)
    with fcol1:
        skip_landlines = st.checkbox(
            "📵 Skip landlines (country-aware)",
            value=True,
            help="Detects the country from the international prefix and "
                 "classifies the number as mobile / landline using each "
                 "country's mobile-prefix rules. Supported: FR, ES, IT, DE, "
                 "UK, BE, NL, PT, CH, LU, DZ, MA, TN, EG, TR, "
                 "AE, SA, KW, QA, BH, OM, YE, JO, IQ, LB. "
                 "Unknown / ambiguous countries (e.g. US/CA, BR) are kept.",
        )
    with fcol2:
        skip_already_sent = st.checkbox(
            "🚫 Skip numbers already messaged (any prior run)",
            value=True,
            help="Reads logs/results.csv and excludes any number previously "
                 "logged as OK. Useful when iterating on the same lead list.",
        )

    def _previously_sent_numbers() -> set[str]:
        results_csv = Path(__file__).resolve().parent / "logs" / "results.csv"
        if not results_csv.exists():
            return set()
        try:
            past = pd.read_csv(results_csv, dtype=str).fillna("")
        except Exception:
            return set()
        if "status" in past.columns and "number" in past.columns:
            ok = past[past["status"].str.upper() == "OK"]
            return set(ok["number"].astype(str))
        return set()

    def _is_landline(raw) -> bool:
        n = normalize_number(raw) or ""
        _, kind = classify_number(n)
        return kind == "landline"

    notes: list[str] = []
    if skip_landlines:
        mask = ~df[phone_col].apply(_is_landline)
        removed = len(df) - int(mask.sum())
        if removed:
            notes.append(f"📵 {removed} landline(s)")
        df = df[mask].reset_index(drop=True)

    if skip_already_sent:
        sent = _previously_sent_numbers()
        if sent:
            mask = ~df[phone_col].apply(lambda x: (normalize_number(x) or "") in sent)
            removed = len(df) - int(mask.sum())
            if removed:
                notes.append(f"🚫 {removed} already-messaged")
            df = df[mask].reset_index(drop=True)

    if notes:
        st.info(f"Filtered out: {', '.join(notes)} — **{len(df)} recipients remain.**")

    st.dataframe(df, use_container_width=True, height=240)

    st.subheader("2. Services to propose")
    selected = st.multiselect("Pick from the catalogue", SERVICE_CATALOGUE)
    extra = st.text_input("Add a custom service (optional)")
    services = list(selected) + ([extra] if extra.strip() else [])

    st.subheader("3. Generate personalized messages")
    gen_cols = st.columns([3, 2])
    with gen_cols[0]:
        generate_clicked = st.button(
            "Generate messages",
            type="primary",
            disabled=not services or not api_key,
            use_container_width=True,
        )
    with gen_cols[1]:
        force_refresh = st.checkbox(
            "Force regenerate (ignore cache)",
            value=False,
            help="By default, identical (row + services + style) combos reuse "
                 "previously generated messages to save tokens.",
        )

    if generate_clicked:
        agent = MessageAgent(AgentConfig(
            model=model,
            temperature=temperature,
            sender_name=sender_name,
            api_key=api_key,
            language=language,
            tone=tone,
            length=length,
            cta_style=cta_style,
            emoji_policy=emoji_policy,
        ))
        progress = st.progress(0.0, text="Drafting messages...")
        drafts = []
        for i, row in enumerate(df.to_dict(orient="records")):
            try:
                msg = agent.generate(row, services, force_refresh=force_refresh)
            except Exception as e:
                msg = f"[ERROR generating: {e}]"
            drafts.append(msg)
            progress.progress(
                (i + 1) / len(df),
                text=f"Drafted {i + 1}/{len(df)} "
                     f"(cached {agent.last_cache_hits} · new {agent.last_cache_misses})",
            )
        progress.empty()
        out = df.copy()
        out["_message"] = drafts
        st.session_state.drafts_df = out
        st.success(
            f"Generated {len(drafts)} messages — "
            f"{agent.last_cache_hits} reused from cache, "
            f"{agent.last_cache_misses} new API calls."
        )

    drafts_df: pd.DataFrame | None = st.session_state.get("drafts_df")

    if drafts_df is not None:
        st.subheader("4. Review & edit drafts")
        edited = st.data_editor(
            drafts_df,
            use_container_width=True,
            height=360,
            num_rows="fixed",
            key="editor",
        )
        st.session_state.drafts_df = edited

        st.subheader("5. Send via WhatsApp Web")
        st.caption("First run will open Chrome; scan the QR if not logged in yet. "
                   "Subsequent runs reuse the session.")

        if st.button("Send all", type="primary"):
            logger = setup_logging()
            log_area = st.empty()
            log_lines: list[str] = []

            def log(msg: str) -> None:
                log_lines.append(msg)
                log_area.code("\n".join(log_lines[-25:]))
                logger.info(msg)

            valid_rows = []
            for _, row in edited.iterrows():
                number = normalize_number(row.get(phone_col))
                msg = str(row.get("_message", "")).strip()
                if number and msg:
                    valid_rows.append((number, msg))

            if not valid_rows:
                st.error("No valid recipients with messages to send.")
            else:
                progress = st.progress(0.0, text="Connecting to WhatsApp Web...")
                ok = fail = 0
                try:
                    with WhatsAppSender(headless=headless, logger=logger) as sender:
                        sender.wait_for_login()
                        log("WhatsApp Web ready.")
                        total = len(valid_rows)
                        for i, (number, msg) in enumerate(valid_rows, 1):
                            log(f"[{i}/{total}] -> {number}")
                            try:
                                success, detail = sender.send(number, msg)
                            except Exception as e:
                                success, detail = False, f"exception:{e.__class__.__name__}:{e}"
                            append_result(number, "OK" if success else "FAIL", detail)
                            if success:
                                ok += 1
                            else:
                                fail += 1
                            log(f"    {'OK' if success else 'FAIL'} ({detail})")
                            progress.progress(i / total, text=f"Sent {i}/{total}")
                            if i < total:
                                delay = random.uniform(min_delay, max_delay)
                                log(f"    sleeping {delay:.1f}s")
                                time.sleep(delay)
                except Exception as e:
                    st.error(f"Sender failed: {e}")
                progress.empty()
                st.success(f"Done. ok={ok} fail={fail} total={ok + fail}")

else:
    st.info("Upload a CSV to begin. Any columns are accepted — the agent will use "
            "them as context. One column must contain phone numbers.")
