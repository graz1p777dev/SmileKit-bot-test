"""Вызовы модели: диспетчер (понимание реплики) и роли, пишущие ответ.

Модель возвращает только структуру или текст ответа. Никакое действие
(QR, заказ, скидка, передача) из её текста не выводится — это решает
engine.py по памяти диалога.
"""

import json
import re
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
KNOWLEDGE_BASE = Path(__file__).resolve().parents[2] / "knowledge_base.md"

Intent = Literal[
    "order_now", "order_later", "question", "price_objection", "not_ready",
    "stop_contact", "complaint", "adverse_reaction", "cooperation", "wants_human",
    "order_status", "cancel_order", "order_data", "greeting", "smalltalk", "other",
]


class Tolerant(BaseModel):
    """Ответ модели: пустую строку или null в любом поле считаем «не указано»."""

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data):
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None and value != ""}
        return data


class GeoSeen(Tolerant):
    mentioned: bool = False
    place: str = ""
    place_to: str = ""
    zone: str = "unknown"
    clarify: Literal["", "country", "region"] = ""


class OrderData(Tolerant):
    quantity: int | None = None
    address: str = ""
    phone: str = ""
    recipient_name: str = ""
    payment: Literal["", "cod", "prepay", "full"] = ""

    @field_validator("quantity", mode="before")
    @classmethod
    def _number(cls, value):
        # Модель иногда пишет «1 коробка» вместо числа.
        if isinstance(value, str):
            found = re.search(r"\d+", value)
            return int(found.group()) if found else None
        return value


class AskedQuestion(Tolerant):
    text: str
    topic: str = "other"


class Understanding(Tolerant):
    language: str = "ru"
    intent: Intent = "other"
    # Ясное желание купить сейчас — отдельно от intent: рядом могут быть вопросы.
    buy_now: bool = False
    # Отношение к ПОСЛЕДНЕМУ предложению бота (оно передаётся в контексте).
    offer_reply: Literal["none", "accept", "decline_temporary", "decline_final", "conditional"] = "none"
    geo: GeoSeen = Field(default_factory=GeoSeen)
    questions: list[AskedQuestion] = Field(default_factory=list)
    order_data: OrderData = Field(default_factory=OrderData)
    wants: Literal["", "composition", "instruction"] = ""
    callback_at: str = ""   # «YYYY-MM-DD HH:MM» по Бишкеку, если просил написать позже


class Written(Tolerant):
    messages: list[str] = Field(default_factory=list)
    answered: list[int] = Field(default_factory=list)   # номера вопросов из контекста
    unknown: list[int] = Field(default_factory=list)
    offer: Literal["", "composition", "instruction"] = ""


class ImageCheck(Tolerant):
    # receipt — похоже на чек; other — другая картинка; unreadable — проверить не удалось.
    kind: Literal["receipt", "other", "unreadable"] = "unreadable"
    amount: float | None = None
    recipient_ok: bool | None = None
    date_ok: bool | None = None


class Brain(Protocol):
    def understand(self, context: dict) -> Understanding: ...
    def write(self, role: str, context: dict) -> Written: ...
    def check_image(self, image: object) -> ImageCheck: ...


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _loads(content: str) -> dict:
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1]) if start != -1 and end > start else {}


class OpenAIBrain:
    """Рабочая реализация. Текст идёт в DeepSeek, как у рабочего бота; OpenAI —
    только если ключа DeepSeek нет. Проверку картинок задаёт подкласс."""

    def __init__(
        self, api_key: str = "", model: str = "", *, deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-flash", deepseek_base_url: str = "https://api.deepseek.com",
    ) -> None:
        from openai import OpenAI

        self.deepseek = bool(deepseek_api_key)
        if self.deepseek:
            self.client, self.model = OpenAI(api_key=deepseek_api_key, base_url=deepseek_base_url), deepseek_model
        else:
            self.client, self.model = OpenAI(api_key=api_key), model
        self.guard = load_prompt("guard")
        self.knowledge = KNOWLEDGE_BASE.read_text(encoding="utf-8")

    @classmethod
    def from_env(cls, env) -> "OpenAIBrain | None":
        deepseek = env.get("DEEPSEEK_API_KEY", "")
        openai_key = env.get("OPENAI_API_KEY", "")
        if not deepseek and openai_key in ("", "replace_me"):
            return None
        return cls(
            openai_key, env.get("OPENAI_MODEL", "gpt-5.6-luna"), deepseek_api_key=deepseek,
            deepseek_model=env.get("DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    def _json(self, instructions: str, payload: dict) -> dict:
        user = json.dumps(payload, ensure_ascii=False)
        if self.deepseek:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": instructions}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=1200,
                # У V4 thinking включён по умолчанию и съедает max_tokens до ответа.
                extra_body={"thinking": {"type": "disabled"}},
            )
            return _loads(response.choices[0].message.content or "{}")
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            instructions=instructions,
            input=[{"role": "user", "content": user}],
            store=False,
        )
        return _loads(response.output_text or "{}")

    def understand(self, context: dict) -> Understanding:
        return Understanding.model_validate(self._json(load_prompt("dispatcher"), context))

    def write(self, role: str, context: dict) -> Written:
        # Роль получает только свой промпт, общие запреты и базу знаний.
        instructions = f"{load_prompt(role)}\n\n{self.guard}\n\n# База знаний\n{self.knowledge}"
        return Written.model_validate(self._json(instructions, context))

    def translate(self, texts: list[str], language: str) -> list[str]:
        data = self._json(load_prompt("translate"), {"language": language, "texts": texts})
        result = data.get("translations")
        return [str(item) for item in result] if isinstance(result, list) else []

    def check_image(self, image: object) -> ImageCheck:
        return ImageCheck(kind="unreadable")
