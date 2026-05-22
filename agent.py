"""
Personalized message generator backed by OpenAI via LangChain.

Given a row of CSV context (arbitrary columns) and a list of services the
sender offers, produce an attractive, personalized WhatsApp proposal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

load_dotenv()

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """You are an expert B2B copywriter writing short, warm,
personalized outreach messages for WhatsApp.

Constraints:
- Output ONLY the final message body. No preamble, no markdown, no quotes.
- Keep it under 120 words. Sound human, not corporate.
- Address the recipient by name if provided. Otherwise open warmly.
- Reference one or two concrete details from their context to show this is not
  a mass blast (e.g. their business, address/city, industry).
- Clearly propose how the listed services would help THEIR specific situation.
- End with a soft, low-friction call to action (a short question or offer to
  send more info).
- Sign off as the sender if a sender name is provided.
- Use plain text only; no emojis unless context strongly invites it.
- Match the language of the recipient's context when obvious (e.g. French
  business name -> write in French). Otherwise default to English.
"""

USER_PROMPT = """Recipient context (CSV row):
{context_block}

Services we want to propose:
{services_block}

Sender name: {sender}

Write the WhatsApp message now."""


@dataclass
class AgentConfig:
    model: str = DEFAULT_MODEL
    temperature: float = 0.7
    sender_name: str = ""
    api_key: str | None = None


class MessageAgent:
    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self.llm = ChatOpenAI(
            model=self.config.model,
            temperature=self.config.temperature,
            api_key=self.config.api_key or os.getenv("OPENAI_API_KEY"),
        )
        self.prompt = ChatPromptTemplate.from_messages(
            [("system", SYSTEM_PROMPT), ("user", USER_PROMPT)]
        )
        self.chain = self.prompt | self.llm

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

    def generate(self, row: Mapping[str, object], services: Iterable[str]) -> str:
        result = self.chain.invoke(
            {
                "context_block": self._format_context(row),
                "services_block": self._format_services(services),
                "sender": self.config.sender_name or "the team",
            }
        )
        text = result.content if hasattr(result, "content") else str(result)
        return text.strip()
