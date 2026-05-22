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

from agent import AgentConfig, MessageAgent
from send_whatsapp import (
    WhatsAppSender,
    append_result,
    normalize_number,
    setup_logging,
)

load_dotenv()

st.set_page_config(page_title="WhatsApp AI Outreach", layout="wide")
st.title("WhatsApp AI Outreach")

# Predefined catalogue. Add freely; users can also type their own.
SERVICE_CATALOGUE = [
    "Web application development",
    "Mobile application development",
    "E-commerce store setup",
    "Marketing automation",
    "Email & WhatsApp campaign automation",
    "SEO and content strategy",
    "Social media management",
    "Branding and graphic design",
    "AI chatbot integration",
    "CRM setup and automation",
    "Data analytics and dashboards",
    "Cloud hosting and DevOps",
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
    sender_name = st.text_input("Sender / sign-off name", value="Ilyes")
    temperature = st.slider("Creativity (temperature)", 0.0, 1.2, 0.7, 0.1)

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



st.subheader("1. Upload recipients CSV")
file = st.file_uploader("CSV file", type=["csv"])

if file is not None:
    df = pd.read_csv(file).fillna("")
    st.session_state.df = df

df: pd.DataFrame | None = st.session_state.get("df")

if df is not None:
    st.dataframe(df, use_container_width=True, height=240)

    guess = next(
        (c for c in df.columns if any(k in c.lower() for k in ("phone", "number", "tel", "mobile", "whatsapp"))),
        df.columns[0],
    )
    phone_col = st.selectbox("Which column holds the phone number?", list(df.columns),
                             index=list(df.columns).index(guess))

    st.subheader("2. Services to propose")
    selected = st.multiselect("Pick from the catalogue", SERVICE_CATALOGUE)
    extra = st.text_input("Add a custom service (optional)")
    services = list(selected) + ([extra] if extra.strip() else [])

    st.subheader("3. Generate personalized messages")
    if st.button("Generate messages", type="primary", disabled=not services or not api_key):
        agent = MessageAgent(AgentConfig(
            model=model,
            temperature=temperature,
            sender_name=sender_name,
            api_key=api_key,
        ))
        progress = st.progress(0.0, text="Drafting messages...")
        drafts = []
        for i, row in enumerate(df.to_dict(orient="records")):
            try:
                msg = agent.generate(row, services)
            except Exception as e:
                msg = f"[ERROR generating: {e}]"
            drafts.append(msg)
            progress.progress((i + 1) / len(df), text=f"Drafted {i + 1}/{len(df)}")
        progress.empty()
        out = df.copy()
        out["_message"] = drafts
        st.session_state.drafts_df = out
        st.success(f"Generated {len(drafts)} messages.")

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
