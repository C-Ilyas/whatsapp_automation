"""
Personalized message generator backed by OpenAI via LangChain.

Given a row of CSV context (arbitrary columns) and a list of services the
sender offers, produce an attractive, personalized WhatsApp proposal on behalf
of The Crew Technologies.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

load_dotenv()

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

COMPANY_BLURB = (
    "The Crew Technologies is a technology studio specializing in artificial "
    "intelligence, AI agents, and end-to-end digital solutions (web apps, "
    "mobile apps, automation, integrations). We help businesses save time, "
    "win customers, and unlock new revenue with practical AI."
)

SYSTEM_PROMPT = """You are an expert B2B copywriter at **The Crew Technologies**,
a studio specializing in artificial intelligence, AI agents, and digital
solutions. You write short, warm, personalized outreach messages for WhatsApp.

About us (use this as background, do NOT recite it verbatim):
{company_blurb}

How to reason (do this silently — do NOT write the reasoning, only the final message):
1. Read the recipient's context carefully. Infer their business type, scale,
   and likely day-to-day pain points (e.g. a clinic juggles appointments and
   no-shows; a restaurant juggles reservations and reviews; a plumber juggles
   inbound calls and quoting; a boutique juggles inventory and customer
   re-engagement).
2. Look at the listed services as a TOOLBOX, not a script. You are encouraged
   to be creative and propose a specific, concrete APPLICATION of those
   services that would clearly matter to THIS recipient. For example:
     - clinic + "AI automation" → an AI agent that handles appointment
       booking, reminders, and rescheduling over WhatsApp/email/SMS, cutting
       no-shows and freeing reception time.
     - restaurant + "marketing automation" → automated review-request
       follow-ups and a WhatsApp reservation/cancellation flow.
     - plumber + "AI chatbot" → a WhatsApp agent that qualifies emergencies,
       quotes simple jobs, and books slots into your calendar.
   Pick ONE or TWO ideas — the most relevant, the most tangible.
3. If none of the listed services obviously fit, lead with the closest
   adjacent service and frame it as the bridge.

Constraints:
- Output ONLY the final message body. No preamble, no markdown, no quotes,
  no subject line, no bullet-point lists, no headings.
- Address the recipient by name if provided; otherwise open warmly.
- Reference one or two concrete details from their context (business name,
  city/address, industry, category) so the message feels hand-written.
- Briefly introduce The Crew Technologies in one short line, then propose a
  SPECIFIC application of the services tailored to their business (per the
  reasoning above) — not a generic services pitch.
- End with a soft, low-friction call to action: {cta_style}
- Sign off as the sender ("{sender}") on behalf of The Crew Technologies.
- Tone: {tone}.
- Length: {length_hint}.
- Emojis: {emoji_policy}
- Write the ENTIRE message in: {language}. Do not mix languages, even if the
  context is in a different language.
"""

USER_PROMPT = """Recipient context (CSV row):
{context_block}

Services we want to propose to this recipient:
{services_block}

Write the WhatsApp message now."""


# ---------------------------- option vocabulary --------------------------- #

LANGUAGES = [
    "Auto (match recipient's context)",
    "English",
    "French (Français)",
    "Arabic (العربية)",
    "Spanish (Español)",
    "German (Deutsch)",
    "Italian (Italiano)",
    "Portuguese (Português)",
    "Dutch (Nederlands)",
]

TONES = ["Friendly", "Professional", "Casual", "Formal", "Enthusiastic", "Concise"]

LENGTHS = {
    "Short (≈40 words)": "around 40 words, very punchy",
    "Medium (≈80 words)": "around 80 words",
    "Long (≈120 words)": "around 120 words, but never longer",
}

CTA_STYLES = {
    "Ask a question": "ask one short open-ended question to invite a reply",
    "Offer a quick call": "offer a short 15-minute discovery call",
    "Offer to send more info": "offer to send a short deck or examples",
    "Ask for the right person": "ask who at their company is the right person to discuss this with",
}

EMOJI_POLICIES = {
    "None": "Do not use emojis at all.",
    "Minimal (1 max)": "At most one tasteful emoji, only if it clearly fits.",
    "Light": "Up to two tasteful emojis, never decorative.",
}


# ------------------------------- config ---------------------------------- #

@dataclass
class AgentConfig:
    model: str = DEFAULT_MODEL
    temperature: float = 0.7
    sender_name: str = ""
    api_key: str | None = None
    # New knobs:
    language: str = "Auto (match recipient's context)"
    tone: str = "Friendly"
    length: str = "Medium (≈80 words)"
    cta_style: str = "Ask a question"
    emoji_policy: str = "None"
    company_blurb: str = COMPANY_BLURB


# ------------------------------- agent ----------------------------------- #

CACHE_PATH = Path(os.getenv(
    "MESSAGE_CACHE_PATH",
    str(Path(__file__).resolve().parent / "cache" / "messages.json"),
))
_CACHE_LOCK = threading.Lock()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _normalize_row(row: Mapping[str, object]) -> dict:
    """Canonicalize a row so cache keys are stable across column ordering /
    whitespace / NaN noise."""
    out = {}
    for k, v in row.items():
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() == "nan":
            continue
        out[str(k).strip().lower()] = s
    return dict(sorted(out.items()))


def _normalize_services(services: Iterable[str]) -> list[str]:
    return sorted({s.strip() for s in services if s and s.strip()})


def cache_key(row: Mapping[str, object], services: Iterable[str],
              cfg: "AgentConfig") -> str:
    payload = {
        "row": _normalize_row(row),
        "services": _normalize_services(services),
        "model": cfg.model,
        "language": cfg.language,
        "tone": cfg.tone,
        "length": cfg.length,
        "cta_style": cfg.cta_style,
        "emoji_policy": cfg.emoji_policy,
        "sender": cfg.sender_name,
        "blurb": cfg.company_blurb,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class MessageAgent:
    def __init__(self, config: AgentConfig | None = None,
                 use_cache: bool = True) -> None:
        self.config = config or AgentConfig()
        self.use_cache = use_cache
        self.llm = ChatOpenAI(
            model=self.config.model,
            temperature=self.config.temperature,
            api_key=self.config.api_key or os.getenv("OPENAI_API_KEY"),
        )
        self.prompt = ChatPromptTemplate.from_messages(
            [("system", SYSTEM_PROMPT), ("user", USER_PROMPT)]
        )
        self.chain = self.prompt | self.llm
        # Per-call stats so the UI can show "12 reused, 3 fresh".
        self.last_cache_hits = 0
        self.last_cache_misses = 0

    @staticmethod
    def _format_context(row: Mapping[str, object]) -> str:
        lines = []
        for k, v in row.items():
            if v is None:
                continue
            s = str(v).strip()
            if not s or s.lower() == "nan":
                continue
            lines.append(f"- {k}: {s}")
        return "\n".join(lines) if lines else "- (no context provided)"

    @staticmethod
    def _format_services(services: Iterable[str]) -> str:
        items = [s.strip() for s in services if s and s.strip()]
        return "\n".join(f"- {s}" for s in items) if items else "- (general services)"

    def _resolve_language(self) -> str:
        lang = self.config.language
        if lang.startswith("Auto"):
            return ("the same language as the recipient's context. If the "
                    "context is mixed or ambiguous, default to English.")
        return lang

    def generate(self, row: Mapping[str, object], services: Iterable[str],
                 force_refresh: bool = False) -> str:
        key = cache_key(row, services, self.config) if self.use_cache else None
        if key is not None and not force_refresh:
            with _CACHE_LOCK:
                cache = _load_cache()
                hit = cache.get(key)
            if hit:
                self.last_cache_hits += 1
                return hit
        self.last_cache_misses += 1
        result = self.chain.invoke(
            {
                "context_block": self._format_context(row),
                "services_block": self._format_services(services),
                "sender": self.config.sender_name or "The Crew Technologies team",
                "company_blurb": self.config.company_blurb,
                "language": self._resolve_language(),
                "tone": self.config.tone,
                "length_hint": LENGTHS.get(self.config.length, self.config.length),
                "cta_style": CTA_STYLES.get(self.config.cta_style, self.config.cta_style),
                "emoji_policy": EMOJI_POLICIES.get(self.config.emoji_policy, self.config.emoji_policy),
            }
        )
        text = (result.content if hasattr(result, "content") else str(result)).strip()
        if key is not None and text:
            with _CACHE_LOCK:
                cache = _load_cache()
                cache[key] = text
                _save_cache(cache)
        return text
